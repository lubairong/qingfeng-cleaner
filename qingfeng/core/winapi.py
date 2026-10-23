"""清风清理 —— Win32 API 封装（纯 ctypes，零第三方依赖）。

提供回收站删除、磁盘空间查询、管理员判断、清空回收站、以管理员重启、
大小格式化、资源管理器定位等能力，供 scanner / cleaner / GUI 使用。

接口定义见 ``docs/ARCHITECTURE.md`` §4。所有函数在非 Windows 或调用失败时
都返回安全兜底值，绝不把异常抛到 UI 线程。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

#: 当前解释器是否运行在 Windows 上（单元测试/文档构建环境可能不是）
IS_WINDOWS: bool = os.name == "nt"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

FO_DELETE = 3

FOF_ALLOWUNDO = 0x0040  # 进回收站（可撤销）
FOF_NOCONFIRMATION = 0x0010  # 不弹确认框
FOF_SILENT = 0x0004  # 不显示进度
FOF_NOERRORUI = 0x0400  # 不显示错误弹窗

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

SW_SHOWNORMAL = 1

#: SHFileOperationW 常见错误码
_SHERR_MESSAGES: dict[int, str] = {
    0x71: "路径不存在",
    0x75: "操作被取消",
    0x78: "拒绝访问（文件可能被占用或权限不足）",
    0x79: "路径过长或文件被占用",
    0x7C: "无效的文件名/路径",
    0x7E: "目标路径非法",
    0x20: "系统内部错误",
    0x100: "内存不足",
}


# ---------------------------------------------------------------------------
# ctypes 结构体与函数原型
# ---------------------------------------------------------------------------


class SHFILEOPSTRUCTW(ctypes.Structure):
    """``SHFILEOPSTRUCTW`` 结构体（字段顺序/类型必须与 shellapi.h 一致）。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class SHQUERYRBINFO(ctypes.Structure):
    """``SHQUERYRBINFO``：回收站文件数与字节数（shellapi.h）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


_shell32: ctypes.WinDLL | None = None
_kernel32: ctypes.WinDLL | None = None
_ole32: ctypes.WinDLL | None = None
_api_ready = False
_admin_cache: bool | None = None


def _configure() -> None:
    """加载 DLL 并声明函数原型（只执行一次）。"""
    global _shell32, _kernel32, _ole32, _api_ready
    if _api_ready or not IS_WINDOWS:
        return
    try:
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _ole32 = ctypes.WinDLL("ole32", use_last_error=True)

        _shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        _shell32.SHFileOperationW.restype = ctypes.c_int

        _shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
        _shell32.SHEmptyRecycleBinW.restype = ctypes.c_long

        _shell32.SHQueryRecycleBinW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(SHQUERYRBINFO),
        ]
        _shell32.SHQueryRecycleBinW.restype = ctypes.c_long

        _shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        _shell32.ShellExecuteW.restype = ctypes.c_void_p  # HINSTANCE，>32 表示成功

        _shell32.IsUserAnAdmin.argtypes = []
        _shell32.IsUserAnAdmin.restype = wintypes.BOOL

        _kernel32.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        _kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

        _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        _kernel32.GetDriveTypeW.restype = wintypes.UINT

        _ole32.CoInitialize.argtypes = [ctypes.c_void_p]
        _ole32.CoInitialize.restype = ctypes.c_long
        _ole32.CoUninitialize.argtypes = []
        _ole32.CoUninitialize.restype = None

        _api_ready = True
    except (AttributeError, OSError):
        _api_ready = False


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    """当前进程是否拥有管理员权限。"""
    global _admin_cache
    if _admin_cache is not None:
        return _admin_cache
    _configure()
    if not _api_ready or _shell32 is None:
        _admin_cache = False
        return False
    try:
        _admin_cache = bool(_shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        _admin_cache = False
    return _admin_cache


def drive_usage(drive: str = "C:") -> tuple[int, int]:
    """返回 ``(可用字节, 总字节)``；失败时返回 ``(0, 0)``。"""
    _configure()
    if not _api_ready or _kernel32 is None:
        return (0, 0)
    root = (drive or "C:").strip()
    if not root.endswith("\\"):
        root += "\\"
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    free_total = ctypes.c_ulonglong(0)
    try:
        ok = _kernel32.GetDiskFreeSpaceExW(
            root, ctypes.byref(free), ctypes.byref(total), ctypes.byref(free_total)
        )
    except (OSError, AttributeError):
        return (0, 0)
    if not ok:
        return (0, 0)
    return (int(free.value), int(total.value))


#: GetDriveTypeW 返回值
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6


def drive_type(drive: str = "C:") -> int:
    """返回 ``GetDriveTypeW`` 的盘符类型（3 = 固定磁盘）；失败返回 0。"""
    _configure()
    if not _api_ready or _kernel32 is None:
        return 0
    root = str(drive or "").strip()
    if not root:
        return 0
    if not root.endswith("\\"):
        root += "\\"
    try:
        return int(_kernel32.GetDriveTypeW(root))
    except (OSError, AttributeError):
        return 0


def _shfileop_delete(paths: list[str]) -> list[tuple[str, str]]:
    """执行一次 ``SHFileOperationW``（FO_DELETE + FOF_ALLOWUNDO），返回失败列表。"""
    failures: list[tuple[str, str]] = []
    if not paths or _shell32 is None:
        return [(p, "当前系统不支持回收站删除") for p in paths]

    buffer = "\0".join(paths) + "\0\0"  # 多路径用 \0 分隔，整体以双 \0 结尾
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buffer
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    try:
        code = int(_shell32.SHFileOperationW(ctypes.byref(op)))
    except (OSError, AttributeError) as exc:  # pragma: no cover - 极端环境
        return [(p, f"调用 SHFileOperationW 失败: {exc}") for p in paths]

    if code == 0:
        if op.fAnyOperationsAborted:
            return [(p, "删除操作被取消") for p in paths]
        return []

    # 批量操作返回了错误码：逐项重试以定位失败项（已消失的视为成功）
    reason = _SHERR_MESSAGES.get(code, f"SHFileOperationW 错误码 0x{code & 0xFFFFFFFF:X}")
    for path in paths:
        if not os.path.exists(path):
            continue
        single = SHFILEOPSTRUCTW()
        single.hwnd = None
        single.wFunc = FO_DELETE
        single.pFrom = path + "\0\0"
        single.pTo = None
        single.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        single.fAnyOperationsAborted = False
        single.hNameMappings = None
        single.lpszProgressTitle = None
        try:
            single_code = int(_shell32.SHFileOperationW(ctypes.byref(single)))
        except (OSError, AttributeError) as exc:  # pragma: no cover
            failures.append((path, f"调用 SHFileOperationW 失败: {exc}"))
            continue
        if single_code != 0 or single.fAnyOperationsAborted:
            failures.append((path, reason if single_code == 0 else _SHERR_MESSAGES.get(single_code, reason)))
    return failures


def recycle_paths(paths: list[str]) -> list[tuple[str, str]]:
    """把路径移入回收站（可撤销删除），返回 ``[(path, 错误信息)]`` 失败列表。

    - 目录会连同其内容一起移入回收站；
    - 使用 ``FOF_ALLOWUNDO``，在支持回收站的卷上可通过回收站还原；
    - 需要 COM 环境：本函数自行 ``CoInitialize`` / ``CoUninitialize``，
      可在任意工作线程调用。
    """
    if not paths:
        return []
    cleaned: list[str] = []
    for path in paths:
        if not path or not str(path).strip():
            continue
        cleaned.append(os.path.abspath(os.fspath(path)))
    if not cleaned:
        return []

    _configure()
    if not _api_ready or _shell32 is None:
        return [(p, "当前系统不支持回收站删除") for p in cleaned]

    inited = False
    if _ole32 is not None:
        try:
            inited = int(_ole32.CoInitialize(None)) >= 0
        except (OSError, AttributeError):  # pragma: no cover
            inited = False
    try:
        return _shfileop_delete(cleaned)
    finally:
        if inited and _ole32 is not None:
            try:
                _ole32.CoUninitialize()
            except (OSError, AttributeError):  # pragma: no cover
                pass


def empty_recycle_bin() -> bool:
    """清空回收站（不弹确认框、不显示进度、不播放声音）。成功返回 ``True``。"""
    _configure()
    if not _api_ready or _shell32 is None:
        return False
    flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    try:
        return int(_shell32.SHEmptyRecycleBinW(None, None, flags)) == 0
    except (OSError, AttributeError):  # pragma: no cover
        return False


def recycle_bin_size(drive: str = "C:") -> tuple[int, int]:
    """查询指定盘的回收站占用，返回 ``(文件数, 字节数)``。

    使用 ``SHQueryRecycleBinW`` + ``SHQUERYRBINFO``；任何失败（盘不存在、
    无回收站、API 不可用）都返回 ``(0, 0)``，绝不抛异常。

    语义约定：**空字符串 / ``None`` 一律返回 ``(0, 0)``**（不做"当前驱动器"
    猜测，调用方应传入具体盘符，如 ``"C:"``）。
    """
    _configure()
    if not _api_ready or _shell32 is None:
        return (0, 0)
    root = str(drive or "").strip()
    if not root:
        return (0, 0)
    if not root.endswith("\\"):
        root += "\\"
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    try:
        hr = int(_shell32.SHQueryRecycleBinW(root, ctypes.byref(info)))
    except (OSError, AttributeError, TypeError):  # pragma: no cover - 极端环境
        return (0, 0)
    if hr != 0:  # 非 S_OK
        return (0, 0)
    num_items = max(0, int(info.i64NumItems))
    size = max(0, int(info.i64Size))
    return (num_items, size)


def _is_frozen() -> bool:
    """当前进程是否为打包（PyInstaller 等 frozen）模式。"""
    return bool(getattr(sys, "frozen", False))


def relaunch_command(extra_args: list[str] | None = None) -> tuple[str, list[str]]:
    """返回提权重启用的 ``(可执行文件, 参数列表)``，供 ShellExecuteW(runas) 使用。

    - frozen（打包版）：``file=sys.executable``，参数为 ``extra_args`` 或现有
      ``sys.argv[1:]``——**绝不再把 exe 自身路径塞进参数**；
    - 源码模式：``file=sys.executable``，参数为 ``[sys.argv[0]] + (extra_args 或
      sys.argv[1:])``。

    :param extra_args: 覆盖默认尾部参数，如 GUI 传 ``["--page", "space"]``。
    """
    exe = sys.executable or ""
    argv = list(sys.argv) if sys.argv else []
    tail = list(extra_args) if extra_args is not None else argv[1:]
    if _is_frozen():
        return exe, tail
    script = argv[0] if argv else ""
    return exe, ([script] if script else []) + tail


def restart_as_admin(extra_args: list[str] | None = None) -> bool:
    """以管理员身份重新启动当前程序（``ShellExecuteW`` + ``runas``）。

    成功发起 UAC 提权返回 ``True``；用户取消/失败返回 ``False``。
    命令行由 :func:`relaunch_command` 生成，打包版不会重复传 exe 路径。
    """
    _configure()
    if not _api_ready or _shell32 is None:
        return False

    file, args = relaunch_command(extra_args)
    params = subprocess.list2cmdline(args) if args else ""
    try:
        result = _shell32.ShellExecuteW(None, "runas", file, params, None, SW_SHOWNORMAL)
    except (OSError, AttributeError):  # pragma: no cover
        return False
    try:
        return int(result or 0) > 32  # ShellExecuteW 返回值 > 32 表示成功
    except (TypeError, ValueError):  # pragma: no cover
        return False


def human_size(n: int) -> str:
    """字节数转人类可读字符串，例如 ``1536`` → ``"1.5 KB"``。"""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return str(n)
    if value != value or value < 0:  # NaN / 负数
        return f"{n} B"
    if value < 1024:
        return f"{int(value)} B"
    units = ("KB", "MB", "GB", "TB", "PB")
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{n} B"  # pragma: no cover - 逻辑上不可达


def open_in_explorer(path: str) -> None:
    """在资源管理器中打开目录，或定位到指定文件（选中）。失败时静默忽略。"""
    _configure()
    if not _api_ready or _shell32 is None:
        return
    try:
        target = os.path.abspath(os.fspath(path))
        if os.path.isdir(target):
            params = f'"{target}"'
        else:
            params = f'/select,"{target}"'
        _shell32.ShellExecuteW(None, "open", "explorer.exe", params, None, SW_SHOWNORMAL)
    except (OSError, AttributeError, ValueError):
        return


__all__ = [
    "DRIVE_CDROM",
    "DRIVE_FIXED",
    "DRIVE_NO_ROOT_DIR",
    "DRIVE_RAMDISK",
    "DRIVE_REMOTE",
    "DRIVE_REMOVABLE",
    "DRIVE_UNKNOWN",
    "IS_WINDOWS",
    "SHFILEOPSTRUCTW",
    "SHQUERYRBINFO",
    "drive_type",
    "drive_usage",
    "empty_recycle_bin",
    "human_size",
    "is_admin",
    "open_in_explorer",
    "recycle_bin_size",
    "recycle_paths",
    "relaunch_command",
    "restart_as_admin",
]
