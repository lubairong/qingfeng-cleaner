"""清风清理 v2 — 空间透视的 UI 动作封装。

原则：只做用户显式点击的动作；系统级命令只复制/打开管理员 PowerShell，绝不代执行。
所有函数同步、快速，不碰 tkinter；线程/事件由调用方负责。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# 打开位置 / 管理员 PowerShell / 剪贴板
# ---------------------------------------------------------------------------


def open_location(path: str) -> bool:
    """在资源管理器中定位文件，或在资源管理器中打开目录。"""
    try:
        if not path:
            return False
        p = Path(path)
        if p.is_dir():
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True
        if p.parent.is_dir():
            subprocess.Popen(
                ["explorer.exe", "/select,", str(p)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
            return True
        # 路径不存在时至少打开父目录
        parent = p.parent
        if parent.is_dir():
            os.startfile(str(parent))  # type: ignore[attr-defined]
            return True
    except Exception:
        log.exception("打开位置失败：%s", path)
    return False


def open_admin_powershell() -> bool:
    """以管理员身份打开一个空的 PowerShell 窗口（不执行任何清理命令）。"""
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe", "-NoExit -NoLogo", None, 1
        )
        return int(result) > 32
    except Exception:
        log.exception("打开管理员 PowerShell 失败")
        return False


def copy_text(root, text: str) -> bool:
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update_idletasks()
        return True
    except Exception:
        log.exception("复制到剪贴板失败")
        return False


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------


def recycle_items(paths: list[str], *, allow_real: bool = True) -> list[tuple[str, str]]:
    """把文件/目录移入回收站，返回失败列表 [(path, error)]。

    allow_real=False（演示数据）时只做校验、不实际删除。
    """
    failures: list[tuple[str, str]] = []
    if not paths:
        return failures
    if not allow_real:
        return failures

    # 优先用 core 的受保护路径守卫 + SHFileOperation 封装
    try:
        from qingfeng.core import safety as safety_mod
        from qingfeng.core import winapi as winapi_mod

        safe_paths: list[str] = []
        for path in paths:
            try:
                safety_mod.assert_deletable(path)
                safe_paths.append(path)
            except Exception as exc:
                failures.append((path, f"已拒绝（受保护路径）：{exc}"))
        if safe_paths:
            failures.extend(winapi_mod.recycle_paths(safe_paths))
        return failures
    except Exception:
        pass

    # 兜底：SHFileOperationW + FOF_ALLOWUNDO
    if sys.platform != "win32":
        return [(p, "当前系统不支持回收站") for p in paths]
    try:
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 0x0003
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        for path in paths:
            if not os.path.exists(path):
                failures.append((path, "路径不存在"))
                continue
            op = SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = FO_DELETE
            op.pFrom = path + "\0\0"
            op.pTo = None
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
            try:
                rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
                if rc != 0:
                    failures.append((path, f"回收站操作失败（Shell 错误 {rc}）"))
            except Exception as exc:
                failures.append((path, str(exc)))
    except Exception:
        log.exception("回收站兜底实现失败")
        failures.extend((p, "回收站调用异常") for p in paths)
    return failures


# ---------------------------------------------------------------------------
# 其他小工具
# ---------------------------------------------------------------------------


def target_free_bytes(path: str) -> int:
    """目标目录所在磁盘的可用字节；失败返回 0。"""
    try:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        return int(shutil.disk_usage(str(probe)).free)
    except Exception:
        return 0


def can_write_dir(path: str) -> bool:
    """尽力判断目录可写（不存在时看最近的已存在父目录）。"""
    try:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if not probe.is_dir():
            return False
        return os.access(str(probe), os.W_OK)
    except Exception:
        return False


def estimate_minutes(total_bytes: int, *, speed_mb_s: float = 300.0) -> float:
    """按 ~300MB/s 估算跨盘复制+校验耗时（分钟，至少 0.1）。"""
    seconds = max(1.0, total_bytes / (speed_mb_s * 1024 * 1024))
    return max(0.1, seconds / 60.0)
