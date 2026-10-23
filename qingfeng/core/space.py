"""清风清理 v2 —— 磁盘空间索引（只读）。

职责（对应 ``docs/ARCHITECTURE-V2.md`` §2）：

- :func:`build_index` 多线程遍历整盘，构建可按目录下钻的 ``SpaceNode`` 树；
- :func:`probe_system_space` 探测休眠/页面/交换文件、WinSxS、Windows.old、
  回收站、还原点、Installer 缓存等「看不见的大头」；
- 索引缓存读写（``%LOCALAPPDATA%\\QingFengCleaner\\space_index.json``，24h 有效）。

硬性约束：

- **只读**：只使用 ``os.scandir`` / ``os.stat``，绝不修改被扫目录；
- 跳过 reparse point（junction/symlink），避免目录环与跨卷越界；
- 无权限目录记录 ``error`` / ``protected_hint`` 后继续，绝不抛异常；
- 节点数超过 ``max_nodes`` 截断（``truncated=True``），小项聚合进 ``other_size``；
- ``unaccounted_bytes`` 显式给出「已用 - 可访问 - 已知系统文件」的差额（受保护/无权限/系统占用），
  界面必须展示而不是假装不存在。
"""

from __future__ import annotations

import ctypes
import json
import ntpath
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import safety, winapi

#: FILE_ATTRIBUTE_REPARSE_POINT
_REPARSE = 0x400
#: FILE_ATTRIBUTE_DIRECTORY
_ATTR_DIRECTORY = 0x10
#: 默认索引线程数（契约要求 8）
THREADS = 8
#: 文件节点保留门槛：小于该值的文件不单独建节点，聚合进 other_size
_FILE_NODE_MIN = 32 * 1024 * 1024
#: 缓存目录/文件
_CACHE_DIR_NAME = "QingFengCleaner"
_CACHE_FILE_NAME = "space_index.json"
_CACHE_VERSION = 1
#: 缓存体积上限（字节），超过则不写盘（契约要求 < 15 MB）
_CACHE_MAX_BYTES = 15 * 1024 * 1024
#: FILETIME(1601) 与 Unix 纪元之间的 100ns 数
_FILETIME_EPOCH_DELTA = 116444736000000000
#: NtQueryDirectoryFile 的 FileIdBothDirectoryInformation
_FILE_ID_BOTH_DIR_INFORMATION = 37
_STATUS_NO_MORE_FILES = 0x80000006
_STATUS_BUFFER_OVERFLOW = 0x80000005
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: 已知受保护/不可访问目录的中文提示
_KNOWN_PROTECTED_HINTS: dict[str, str] = {
    "$Recycle.Bin": "回收站数据，建议用「清空回收站」功能处理",
    "System Volume Information": "系统还原点/卷影副本，需管理员，请勿手删",
    "Recovery": "系统恢复数据，请勿手删",
    "Config.Msi": "MSI 安装事务日志，请勿手删",
    "MSOCache": "Office 安装缓存，建议用 Office 安装程序维护",
    "Windows.old": "旧版 Windows 备份，建议用「磁盘清理」删除",
}


#: 是否按 ``(st_dev, st_ino)`` 去重硬链接字节数（契约要求必须开启；
#: 仅用于测试/基准对比时可临时设为 False）。
DEDUPE_HARDLINKS = True


class _Cancelled(Exception):
    """内部取消信号。"""


@dataclass
class SpaceNode:
    """空间树节点（目录或文件）。"""

    path: str
    name: str
    is_dir: bool
    size: int
    file_count: int
    mtime: float
    children: list["SpaceNode"] = field(default_factory=list)
    truncated: bool = False
    other_size: int = 0
    error: str | None = None


@dataclass
class SpaceIndex:
    """一次完整磁盘索引的结果。"""

    root: str
    total_bytes: int
    accessible_bytes: int
    unaccounted_bytes: int
    system_files: dict[str, int]
    protected_hint: dict[str, str]
    duration: float
    built_at: float
    root_node: SpaceNode
    cancelled: bool = False
    truncated: bool = False
    #: 关键子树的「原始」（不去重硬链接）字节数，供 probe 展示两种口径。
    #: 键为规范化绝对路径（如 ``C:\\Windows``），值含重复计算的字节数。
    raw_tree_bytes: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    _path_map: dict[str, SpaceNode] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._rebuild_path_map()

    def _rebuild_path_map(self) -> None:
        mapping: dict[str, SpaceNode] = {}

        def visit(node: SpaceNode) -> None:
            mapping[ntpath.normcase(node.path)] = node
            for child in node.children:
                visit(child)

        if self.root_node is not None:
            visit(self.root_node)
        self._path_map = mapping

    def find(self, path: str) -> SpaceNode | None:
        """按路径查找已物化的节点（被折叠/未扫描的路径返回 ``None``）。"""
        if not path:
            return None
        target = ntpath.normcase(safety.expand(path) or path)
        node = self._path_map.get(target)
        if node is None:
            # 根节点 key 带结尾分隔符，查询侧通常不带
            node = self._path_map.get(target + "\\")
        return node

    def to_json(self) -> dict:
        """转成可 json.dumps 的 dict（缓存用）。"""

        def node_to_dict(node: SpaceNode) -> dict:
            return {
                "path": node.path,
                "name": node.name,
                "is_dir": bool(node.is_dir),
                "size": int(node.size),
                "file_count": int(node.file_count),
                "mtime": float(node.mtime),
                "children": [node_to_dict(child) for child in node.children],
                "truncated": bool(node.truncated),
                "other_size": int(node.other_size),
                "error": node.error,
            }

        return {
            "version": _CACHE_VERSION,
            "root": self.root,
            "total_bytes": int(self.total_bytes),
            "accessible_bytes": int(self.accessible_bytes),
            "unaccounted_bytes": int(self.unaccounted_bytes),
            "system_files": dict(self.system_files),
            "protected_hint": dict(self.protected_hint),
            "duration": float(self.duration),
            "built_at": float(self.built_at),
            "cancelled": bool(self.cancelled),
            "truncated": bool(self.truncated),
            "raw_tree_bytes": {str(k): int(v) for k, v in self.raw_tree_bytes.items()},
            "root_node": node_to_dict(self.root_node),
        }

    @classmethod
    def from_json(cls, data: dict) -> "SpaceIndex":
        """从 :meth:`to_json` 的结果还原（缓存读回用）。"""
        if not isinstance(data, dict):
            raise ValueError("索引缓存格式非法")
        version = data.get("version", 1)
        if version != _CACHE_VERSION:
            raise ValueError(f"索引缓存版本不支持: {version!r}")

        def dict_to_node(raw: dict) -> SpaceNode:
            children = [dict_to_node(child) for child in raw.get("children", [])]
            return SpaceNode(
                path=str(raw["path"]),
                name=str(raw.get("name") or ""),
                is_dir=bool(raw.get("is_dir", True)),
                size=int(raw.get("size", 0)),
                file_count=int(raw.get("file_count", 0)),
                mtime=float(raw.get("mtime", 0.0)),
                children=children,
                truncated=bool(raw.get("truncated", False)),
                other_size=int(raw.get("other_size", 0)),
                error=raw.get("error"),
            )

        return cls(
            root=str(data["root"]),
            total_bytes=int(data.get("total_bytes", 0)),
            accessible_bytes=int(data.get("accessible_bytes", 0)),
            unaccounted_bytes=int(data.get("unaccounted_bytes", 0)),
            system_files={str(k): int(v) for k, v in (data.get("system_files") or {}).items()},
            protected_hint={str(k): str(v) for k, v in (data.get("protected_hint") or {}).items()},
            duration=float(data.get("duration", 0.0)),
            built_at=float(data.get("built_at", 0.0)),
            root_node=dict_to_node(data["root_node"]),
            cancelled=bool(data.get("cancelled", False)),
            truncated=bool(data.get("truncated", False)),
            raw_tree_bytes={
                str(k): int(v) for k, v in (data.get("raw_tree_bytes") or {}).items()
            },
        )


# ---------------------------------------------------------------------------
# 内部扫描统计
# ---------------------------------------------------------------------------


class _DirStat:
    """单个目录的扫描统计（尽量省内存）。"""

    __slots__ = ("size", "raw_size", "files", "mtime", "error")

    def __init__(self) -> None:
        self.size = 0
        self.raw_size = 0
        self.files = 0
        self.mtime = 0.0
        self.error: str | None = None


def _norm_root(root: str) -> str:
    text = safety.expand(root) if root else ""
    if not text:
        text = (os.environ.get("SystemDrive") or "C:") + "\\"
    if not ntpath.isabs(text):
        text = ntpath.abspath(text)
    text = ntpath.normpath(text)
    if not text.endswith("\\"):
        text += "\\"
    return text


def _drive_of(path: str) -> str:
    drive, _ = ntpath.splitdrive(path)
    return drive.upper()


def _safe_stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _inode_key(path: str) -> tuple[int, int] | None:
    """取文件的 ``(st_dev, st_ino)``；不可用返回 ``None``。

    注意：Windows 上 ``DirEntry.stat()`` 返回的 ``st_ino/st_dev`` 恒为 0，
    必须用 ``os.stat()`` 才能拿到真实 inode（硬链接去重依赖它）。
    调用方需保证 ``path`` 已排除 reparse point，因此这里允许跟随（普通文件等价）。
    """
    if not DEDUPE_HARDLINKS:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    ino = int(getattr(stat, "st_ino", 0) or 0)
    if ino <= 0:
        return None
    return (int(getattr(stat, "st_dev", 0) or 0), ino)


def _is_reparse(st: os.stat_result) -> bool:
    return bool((getattr(st, "st_file_attributes", 0) or 0) & _REPARSE)


def _should_cancel(cancel: Callable[[], bool] | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001 - 回调异常不能影响索引
        return False


def _stat_system_files(drive: str) -> dict[str, int]:
    """系统盘根目录的休眠/页面/交换文件大小（os.stat 只读，不做提权）。"""
    result: dict[str, int] = {}
    root = drive.rstrip("\\") + "\\" if drive else "C:\\"
    for name in ("hiberfil.sys", "pagefile.sys", "swapfile.sys", "DumpStack.log.tmp"):
        st = _safe_stat(ntpath.join(root, name))
        if st is not None and st.st_size > 0:
            result[name] = int(st.st_size)
    return result


# ---------------------------------------------------------------------------
# 快速目录枚举（NtQueryDirectoryFile + FileIdBothDirectoryInfo）
#
# Windows 上 ``os.scandir`` 的 DirEntry 不带 inode，逐文件 ``os.stat`` 拿 inode
# 又太慢（全盘要 +45s）。这里直接用 NtQueryDirectoryFile 一次拿到
# 「文件名 / 大小 / 属性 / 时间 / 64 位 FileId」，既满足硬链接去重又不牺牲性能；
# 任何失败都退回 ``os.scandir`` 路径（正确性不受影响，只是慢一些）。
# ---------------------------------------------------------------------------


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_longlong),
        ("Information", ctypes.c_size_t),
    ]


_ntdll: ctypes.WinDLL | None = None
_kernel32_nt: ctypes.WinDLL | None = None
_nt_ready = False


def _configure_nt() -> None:
    global _ntdll, _kernel32_nt, _nt_ready
    if _nt_ready or os.name != "nt":
        return
    try:
        _ntdll = ctypes.WinDLL("ntdll")
        _kernel32_nt = ctypes.WinDLL("kernel32", use_last_error=True)
        _ntdll.NtQueryDirectoryFile.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.BOOL,
            ctypes.c_void_p,
            wintypes.BOOL,
        ]
        _ntdll.NtQueryDirectoryFile.restype = ctypes.c_long
        _kernel32_nt.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        _kernel32_nt.CreateFileW.restype = wintypes.HANDLE
        _kernel32_nt.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32_nt.CloseHandle.restype = wintypes.BOOL
        _nt_ready = True
    except (AttributeError, OSError):
        _nt_ready = False


def _long_path(path: str) -> str:
    """给 Win32 API 用的长路径形式（``\\\\?\\`` 前缀）。"""
    absolute = ntpath.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _filetime_to_unix(filetime: int) -> float:
    if not filetime:
        return 0.0
    return (int(filetime) - _FILETIME_EPOCH_DELTA) / 10_000_000.0


def _enum_dir_fast(directory: str) -> list[tuple[str, int, int, int, int, float]] | None:
    """快速枚举一个目录，返回 ``[(name, size, file_id, attributes, is_dir, mtime)]``。

    失败返回 ``None``（调用方自行退回 ``os.scandir``）。不跟随 reparse point；
    目录本身通过 ``FILE_FLAG_BACKUP_SEMANTICS`` 打开。
    """
    _configure_nt()
    if not _nt_ready or _ntdll is None or _kernel32_nt is None:
        return None
    handle = _kernel32_nt.CreateFileW(
        _long_path(directory),
        0x0001 | 0x0020,  # FILE_LIST_DIRECTORY | FILE_TRAVERSE
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    buffer = ctypes.create_string_buffer(64 * 1024)
    entries: list[tuple[str, int, int, int, int, float]] = []
    restart = True
    try:
        while True:
            iosb = _IO_STATUS_BLOCK()
            status = int(
                _ntdll.NtQueryDirectoryFile(
                    handle,
                    None,
                    None,
                    None,
                    ctypes.byref(iosb),
                    buffer,
                    len(buffer),
                    _FILE_ID_BOTH_DIR_INFORMATION,
                    False,
                    None,
                    restart,
                )
            ) & 0xFFFFFFFF
            restart = False
            if status == _STATUS_NO_MORE_FILES:
                break
            if status != 0:
                # 缓冲区不足等异常：有数据就返回已解析部分，否则整体回退
                if not entries:
                    return None
                break
            raw = buffer.raw  # 必须在每次 API 调用后重新读取缓冲区内容
            offset = 0
            while True:
                # FILE_ID_BOTH_DIR_INFORMATION 字段偏移（x64，无 packing）：
                # NextEntryOffset 0 / LastWriteTime 24 / EndOfFile 40 /
                # FileAttributes 56 / FileNameLength 60 / FileId 96 / FileName 104
                next_offset = struct.unpack_from("<I", raw, offset)[0]
                attributes = struct.unpack_from("<I", raw, offset + 56)[0]
                name_length = struct.unpack_from("<I", raw, offset + 60)[0]
                size = struct.unpack_from("<q", raw, offset + 40)[0]
                mtime = _filetime_to_unix(struct.unpack_from("<q", raw, offset + 24)[0])
                file_id = struct.unpack_from("<Q", raw, offset + 96)[0]
                name = raw[offset + 104 : offset + 104 + name_length].decode("utf-16-le")
                if name and name not in (".", ".."):
                    entries.append(
                        (
                            name,
                            int(size),
                            int(file_id),
                            int(attributes),
                            bool(attributes & _ATTR_DIRECTORY),
                            mtime,
                        )
                    )
                if next_offset == 0:
                    break
                offset += next_offset
            if int(iosb.Information) == 0:
                break
    except (OSError, struct.error, UnicodeDecodeError):  # pragma: no cover - 极端环境
        if not entries:
            return None
    finally:
        try:
            _kernel32_nt.CloseHandle(handle)
        except OSError:  # pragma: no cover
            pass
    return entries


def _iter_entries_fallback(directory: str) -> list[tuple[str, int, int, int, int, float]]:
    """``os.scandir`` 回退路径（inode 通过 os.stat 获取，慢但正确）。"""
    entries: list[tuple[str, int, int, int, int, float]] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                attributes = int(getattr(entry_stat, "st_file_attributes", 0) or 0)
                if attributes & _REPARSE:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                file_id = 0
                if not is_dir:
                    inode_key = _inode_key(entry.path)
                    if inode_key is not None:
                        file_id = inode_key[1]
                entries.append(
                    (
                        entry.name,
                        int(entry_stat.st_size),
                        file_id,
                        attributes,
                        is_dir,
                        float(entry_stat.st_mtime or 0.0),
                    )
                )
    except OSError:
        return []
    return entries


def _iter_entries(directory: str) -> list[tuple[str, int, int, int, int, float]]:
    """枚举目录：优先快速路径，失败退回 os.scandir。"""
    fast = _enum_dir_fast(directory)
    if fast is not None:
        return fast
    return _iter_entries_fallback(directory)


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


def build_index(
    root: str = "C:\\",
    *,
    min_child_size: int = 32 * 1024 * 1024,
    max_nodes: int = 150_000,
    progress: Callable[[str, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> SpaceIndex:
    """构建磁盘空间索引（只读，多线程）。

    :param root: 索引根（默认 ``"C:\\\\"``，也接受任意目录用于测试/局部索引）
    :param min_child_size: 小于该值的子项不单独建节点，聚合进父节点 ``other_size``
    :param max_nodes: 节点数上限，超过则截断并置 ``truncated=True``
    :param progress: ``progress(current_path, processed_count)``，可能从工作线程调用
    :param cancel: 返回 ``True`` 时尽快结束，``index.cancelled=True``
    """
    started = time.time()
    root_norm = _norm_root(root)
    min_child_size = max(0, int(min_child_size))
    max_nodes = max(1, int(max_nodes))
    file_keep = min(min_child_size, _FILE_NODE_MIN)

    stats: dict[str, _DirStat] = {}
    big_files: dict[str, tuple[int, float]] = {}  # path -> (size, mtime)
    counter = [0]
    last_report = [0.0]
    report_lock = threading.Lock()
    cancelled_flag = [False]

    def report(path: str) -> None:
        counter[0] += 1
        if progress is None:
            return
        now = time.time()
        with report_lock:
            if now - last_report[0] < 0.25:
                return
            last_report[0] = now
            count = counter[0]
        try:
            progress(path, count)
        except Exception:  # noqa: BLE001 - UI 回调异常不能中断索引
            pass

    seen_inodes: set[int] = set()
    inode_lock = threading.Lock()

    def record_file(stat_obj: _DirStat, path: str, size: int, file_id: int, mtime: float) -> None:
        """累计一个文件；字节数按 inode 去重（硬链接只算一次），同时记录原始字节数。"""
        stat_obj.files += 1
        stat_obj.raw_size += int(size)
        first_seen = True
        if DEDUPE_HARDLINKS and file_id:
            with inode_lock:
                first_seen = file_id not in seen_inodes
                if first_seen:
                    seen_inodes.add(file_id)
        if first_seen:
            stat_obj.size += int(size)
            if size >= file_keep:
                big_files[path] = (int(size), mtime)

    def describe_dir_error(directory: str) -> str | None:
        """目录枚举为空时，用 os.scandir 确认是「真空目录」还是「无权限」。"""
        try:
            with os.scandir(directory) as it:
                list(it)
        except PermissionError:
            return "无权限访问（需要管理员）"
        except OSError as exc:
            return f"读取失败: {exc.__class__.__name__}"
        return None

    def walk(directory: str) -> None:
        if _should_cancel(cancel):
            raise _Cancelled
        stat = _DirStat()
        entries = _iter_entries(directory)
        if not entries:
            error = describe_dir_error(directory)
            if error:
                stat.error = error
            stats[directory] = stat
            report(directory)
            return

        base = directory if directory.endswith("\\") else directory + "\\"
        max_mtime = 0.0
        checks = 0
        for name, size, file_id, attributes, is_dir, mtime in entries:
            checks += 1
            if checks & 0x1FF == 0 and _should_cancel(cancel):
                raise _Cancelled
            path = base + name
            report(path)
            if attributes & _REPARSE:  # junction / symlink：绝不跟随
                continue
            if is_dir:
                walk(path)
                child = stats.get(path)
                if child is not None:
                    stat.size += child.size
                    stat.raw_size += child.raw_size
                    stat.files += child.files
                    if child.mtime > max_mtime:
                        max_mtime = child.mtime
                continue
            record_file(stat, path, size, file_id, mtime)
            if mtime > max_mtime:
                max_mtime = mtime
        stat.mtime = max_mtime
        stats[directory] = stat

    # --- 根目录条目：文件直接统计，目录作为并行任务 ---
    root_stat = _DirStat()
    top_dirs: list[str] = []
    root_errors: list[str] = []
    for name, size, file_id, attributes, is_dir, mtime in _iter_entries(root_norm):
        if attributes & _REPARSE:
            continue
        path = root_norm + name
        if is_dir:
            top_dirs.append(path)
            report(path)
            continue
        record_file(root_stat, path, size, file_id, mtime)
        if mtime > root_stat.mtime:
            root_stat.mtime = mtime
    if not top_dirs and not root_stat.files:
        error = describe_dir_error(root_norm)
        if error:
            root_stat.error = f"无法读取根目录: {error}"
            root_errors.append(root_norm)

    cancelled = False
    if top_dirs and not _should_cancel(cancel):
        with ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="qf-space") as pool:
            futures = [pool.submit(walk, directory) for directory in top_dirs]
            for future in futures:
                try:
                    future.result()
                except _Cancelled:
                    cancelled = True
                except Exception as exc:  # noqa: BLE001 - 单棵子树异常不能中断整体
                    root_errors.append(f"{exc.__class__.__name__}: {exc}")
    elif _should_cancel(cancel):
        cancelled = True

    if cancelled or _should_cancel(cancel):
        cancelled = True

    # 合并顶层目录统计到根
    for directory in top_dirs:
        child = stats.get(directory)
        if child is None:
            continue
        root_stat.size += child.size
        root_stat.raw_size += child.raw_size
        root_stat.files += child.files
        if child.mtime > root_stat.mtime:
            root_stat.mtime = child.mtime
    stats[root_norm] = root_stat

    # --- 构建树（折叠小项 / 截断） ---
    def _parent_key(path: str) -> str | None:
        """求父目录在 stats 中的 key。

        根目录 key 带结尾 ``\\``（如 ``E:\\tmp\\``），而 ``dirname`` 返回
        ``E:\\tmp``，两者需要互相兼容；盘符根 ``dirname`` 等于自身需排除。
        """
        parent = ntpath.dirname(path)
        if not parent or parent == path:
            return None
        for candidate in (parent, parent if parent.endswith("\\") else parent + "\\"):
            if candidate == path:
                continue
            if candidate in stats:
                return candidate
        return None

    parents: dict[str, list[str]] = {}
    for path in stats:
        parent = _parent_key(path)
        if parent is not None:
            parents.setdefault(parent, []).append(path)

    files_by_parent: dict[str, list[tuple[str, int, float]]] = {}
    for path, (size, mtime) in big_files.items():
        parent = _parent_key(path)
        if parent is not None:
            files_by_parent.setdefault(parent, []).append((path, size, mtime))

    budget = [max_nodes]
    truncated_flag = [False]

    def make_dir_node(directory: str) -> SpaceNode:
        stat = stats.get(directory) or _DirStat()
        node = SpaceNode(
            path=directory,
            name=ntpath.basename(directory.rstrip("\\")) or _drive_of(directory) or directory,
            is_dir=True,
            size=stat.size,
            file_count=stat.files,
            mtime=stat.mtime,
        )
        if stat.error:
            node.error = stat.error

        candidates: list[tuple[int, bool, str]] = []
        for child_path in parents.get(directory, []):
            child_stat = stats.get(child_path)
            if child_stat is None or child_stat.size < min_child_size:
                continue
            candidates.append((child_stat.size, True, child_path))
        for file_path, size, _mtime in files_by_parent.get(directory, []):
            if size < min_child_size:
                continue
            candidates.append((size, False, file_path))
        candidates.sort(key=lambda item: item[0], reverse=True)

        for size, is_dir_child, child_path in candidates:
            if budget[0] <= 0:
                truncated_flag[0] = True
                break
            budget[0] -= 1
            if is_dir_child:
                node.children.append(make_dir_node(child_path))
            else:
                file_size, file_mtime = big_files.get(child_path, (size, 0.0))
                node.children.append(
                    SpaceNode(
                        path=child_path,
                        name=ntpath.basename(child_path),
                        is_dir=False,
                        size=file_size,
                        file_count=1,
                        mtime=file_mtime,
                    )
                )
        consumed = sum(child.size for child in node.children)
        node.other_size = max(0, node.size - consumed)
        node.truncated = bool(node.other_size > 0 or truncated_flag[0])
        node.children.sort(key=lambda child: child.size, reverse=True)
        return node

    root_node = make_dir_node(root_norm)
    accessible = root_node.size
    drive = _drive_of(root_norm) or "C:"
    system_files = _stat_system_files(drive)
    free, total = winapi.drive_usage(drive)
    if total <= 0:
        total = accessible
        free = 0
    used = max(0, int(total) - int(free))
    unaccounted = max(0, used - accessible - sum(system_files.values()))

    # 受保护/无权限提示（已知目录 + 顶层读取失败）
    protected_hint: dict[str, str] = {}
    if ntpath.normcase(root_norm) == ntpath.normcase(drive + "\\"):
        for name, hint in _KNOWN_PROTECTED_HINTS.items():
            if os.path.exists(ntpath.join(root_norm, name)):
                protected_hint[name] = hint
    for directory in top_dirs:
        child = stats.get(directory)
        if child is not None and child.error:
            protected_hint[ntpath.basename(directory.rstrip("\\"))] = child.error

    # 关键子树的「原始字节」（不去重硬链接），供 probe_system_space 展示两种口径
    raw_tree_bytes: dict[str, int] = {}
    for relative in ("Windows", r"Windows\WinSxS", r"Windows\Installer"):
        candidate = ntpath.join(root_norm, relative)
        candidate_stat = stats.get(candidate)
        if candidate_stat is None:
            candidate_stat = stats.get(candidate + "\\")
        if candidate_stat is not None:
            raw_tree_bytes[candidate] = int(candidate_stat.raw_size)

    return SpaceIndex(
        root=root_norm,
        total_bytes=used,
        accessible_bytes=accessible,
        unaccounted_bytes=unaccounted,
        system_files=system_files,
        protected_hint=protected_hint,
        duration=time.time() - started,
        built_at=time.time(),
        root_node=root_node,
        cancelled=cancelled,
        truncated=truncated_flag[0],
        raw_tree_bytes=raw_tree_bytes,
    )


# ---------------------------------------------------------------------------
# 系统占用探测（只读）
# ---------------------------------------------------------------------------


def _scan_tree(
    path: str, special: str | None = None, *, workers: int = THREADS
) -> tuple[int, int, int, int]:
    """并行统计目录树大小（只读），按 inode 去重硬链接。

    ``special`` 为子树时额外统计该子树。
    返回 ``(去重总字节, 去重 special 字节, 原始总字节, 原始 special 字节)``；
    读取失败静默跳过，绝不抛异常。``特殊`` 子树在自身范围内去重（结果稳定可复现）。
    """
    try:
        if not os.path.isdir(path):
            return 0, 0, 0, 0
    except OSError:
        return 0, 0, 0, 0

    special_key = ntpath.normcase(special) if special else None
    totals = [0, 0, 0, 0]  # [去重总, 去重 special, 原始总, 原始 special]
    seen_global: set[int] = set()
    seen_special: set[int] = set()
    lock = threading.Lock()

    def in_special(candidate: str) -> bool:
        if not special_key:
            return False
        key = ntpath.normcase(candidate)
        return key == special_key or key.startswith(special_key + "\\")

    def record(file_path: str, size: int, file_id: int) -> None:
        size = int(size)
        is_special = in_special(file_path)
        dedupe = bool(DEDUPE_HARDLINKS and file_id)
        count_global = True
        if dedupe:
            with lock:
                if file_id in seen_global:
                    count_global = False
                else:
                    seen_global.add(file_id)
        with lock:
            totals[2] += size  # 原始口径（不去重）
            if is_special:
                totals[3] += size
            if count_global:
                totals[0] += size
            if is_special:
                if dedupe:
                    if file_id not in seen_special:
                        seen_special.add(file_id)
                        totals[1] += size
                else:
                    totals[1] += size

    def walk(directory: str) -> None:
        base = directory if directory.endswith("\\") else directory + "\\"
        for name, size, file_id, attributes, is_dir, _mtime in _iter_entries(directory):
            if attributes & _REPARSE:
                continue
            path_inside = base + name
            if is_dir:
                walk(path_inside)
            else:
                record(path_inside, size, file_id)

    # 顶层子目录并行（8 线程按子树分发）
    jobs: list[str] = []
    base = path if path.endswith("\\") else path + "\\"
    for name, size, file_id, attributes, is_dir, _mtime in _iter_entries(path):
        if attributes & _REPARSE:
            continue
        child = base + name
        if is_dir:
            jobs.append(child)
        else:
            record(child, size, file_id)

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            list(pool.map(walk, jobs))
    return int(totals[0]), int(totals[1]), int(totals[2]), int(totals[3])


def _failure(message: str) -> dict:
    return {"available": False, "error": message}


def probe_system_space(index: "SpaceIndex | None" = None) -> dict:
    """探测系统占用（只读），返回可直接展示的 dict。

    任何异常都降级为 ``{"available": False, ...}``，绝不抛出。
    返回键：recycle_bin / hiberfil / pagefile / swapfile / winsxs /
    windows_old / restore_points / windows / installer_cache。
    ``windows`` / ``winsxs`` / ``installer_cache`` 同时带两种口径：

    - ``bytes``：按 inode 去重后的真实占用（界面主口径）；
    - ``bytes_raw``：资源管理器式的不去重口径（硬链接重复计算），仅作对照说明。

    :param index: 传入空间索引时直接从索引树读取上述三项，不再重复扫描
        （通常 < 0.2s）；不传则现场扫描 Windows/WinSxS/Installer（约 15~25s）。
    """
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\")
    root = system_drive + "\\"
    windows_dir = ntpath.join(root, "Windows")
    winsxs_dir = ntpath.join(windows_dir, "WinSxS")
    installer_dir = ntpath.join(windows_dir, "Installer")
    raw_lookup: dict[str, int] = {}
    if index is not None:
        raw_lookup = {
            ntpath.normcase(str(key)): int(value)
            for key, value in (index.raw_tree_bytes or {}).items()
        }
    result: dict = {}

    # 回收站（优先 Shell API，失败降级）
    try:
        count, size = winapi.recycle_bin_size(system_drive)
        result["recycle_bin"] = {"count": int(count), "bytes": int(size)}
    except Exception as exc:  # noqa: BLE001
        result["recycle_bin"] = _failure(f"回收站查询失败: {exc.__class__.__name__}")

    # 根目录系统文件
    for key, name in (
        ("hiberfil", "hiberfil.sys"),
        ("pagefile", "pagefile.sys"),
        ("swapfile", "swapfile.sys"),
    ):
        try:
            path = ntpath.join(root, name)
            exists = os.path.exists(path)
            size = int(os.stat(path).st_size) if exists else 0
            result[key] = {"exists": bool(exists), "bytes": size}
        except OSError as exc:
            result[key] = {"exists": False, "bytes": 0, "available": False, "error": exc.__class__.__name__}

    def index_size(path: str) -> int | None:
        if index is None:
            return None
        node = index.find(path)
        return int(node.size) if node is not None else None

    # Windows 目录 + WinSxS + Installer 缓存
    if index is not None:
        windows_bytes = index_size(windows_dir)
        winsxs_bytes = index_size(winsxs_dir)
        installer_bytes = index_size(installer_dir)
        if windows_bytes is None or winsxs_bytes is None:
            result["windows"] = {
                "available": False,
                "bytes": 0,
                "bytes_raw": 0,
                "error": "索引中未找到系统目录（可能被截断），请重新扫描",
            }
            result["winsxs"] = {
                "available": False,
                "bytes": 0,
                "bytes_raw": 0,
                "error": "索引中未找到 WinSxS（可能被截断），请重新扫描",
            }
        else:
            result["windows"] = {
                "bytes": windows_bytes,
                "bytes_raw": raw_lookup.get(ntpath.normcase(windows_dir), windows_bytes),
                "available": True,
                "source": "index",
            }
            result["winsxs"] = {
                "bytes": winsxs_bytes,
                "bytes_raw": raw_lookup.get(ntpath.normcase(winsxs_dir), winsxs_bytes),
                "available": True,
                "source": "index",
            }
        if installer_bytes is None:
            result["installer_cache"] = {
                "available": False,
                "bytes": 0,
                "bytes_raw": 0,
                "error": "索引中未找到 Installer 缓存（可能为空或被截断）",
            }
        else:
            result["installer_cache"] = {
                "bytes": installer_bytes,
                "bytes_raw": raw_lookup.get(ntpath.normcase(installer_dir), installer_bytes),
                "available": True,
                "source": "index",
            }
    else:
        try:
            windows_bytes, winsxs_bytes, windows_raw, winsxs_raw = _scan_tree(
                windows_dir, winsxs_dir
            )
            result["windows"] = {
                "bytes": int(windows_bytes),
                "bytes_raw": int(windows_raw),
                "available": True,
            }
            result["winsxs"] = {
                "bytes": int(winsxs_bytes),
                "bytes_raw": int(winsxs_raw),
                "available": True,
            }
        except Exception as exc:  # noqa: BLE001
            result["windows"] = _failure(f"Windows 目录统计失败: {exc.__class__.__name__}")
            result["winsxs"] = _failure(f"WinSxS 统计失败: {exc.__class__.__name__}")
        try:
            installer_bytes, _ignored, installer_raw, _ignored2 = _scan_tree(installer_dir, workers=4)
            result["installer_cache"] = {
                "bytes": int(installer_bytes),
                "bytes_raw": int(installer_raw),
                "available": True,
            }
        except Exception as exc:  # noqa: BLE001
            result["installer_cache"] = _failure(f"Installer 缓存统计失败: {exc.__class__.__name__}")

    # Windows.old
    try:
        windows_old = ntpath.join(root, "Windows.old")
        exists = os.path.isdir(windows_old)
        old_bytes = 0
        if exists:
            old_index = index_size(windows_old)
            if old_index is not None:
                old_bytes = old_index
            elif index is None:
                old_bytes = _scan_tree(windows_old, workers=4)[0]
        result["windows_old"] = {"exists": bool(exists), "bytes": int(old_bytes)}
    except Exception as exc:  # noqa: BLE001
        result["windows_old"] = _failure(f"Windows.old 统计失败: {exc.__class__.__name__}")

    # 还原点（非管理员不尝试提权）
    try:
        if not winapi.is_admin():
            result["restore_points"] = {
                "available": False,
                "bytes": None,
                "note": "需要管理员权限；可用 vssadmin list shadowstorage 查看占用",
            }
        else:
            svp = ntpath.join(root, "System Volume Information")
            svp_bytes: int | None
            if index is not None:
                svp_bytes = index_size(svp)
            else:
                try:
                    svp_bytes = _scan_tree(svp, workers=4)[0]
                except OSError:
                    svp_bytes = None
            if svp_bytes is None:
                result["restore_points"] = {
                    "available": False,
                    "bytes": None,
                    "note": "无法读取 System Volume Information；可用 vssadmin list shadowstorage 查看",
                }
            else:
                result["restore_points"] = {
                    "available": True,
                    "bytes": int(svp_bytes),
                    "note": "含还原点/卷影副本；建议用系统保护 UI 或 vssadmin 调整",
                }
    except Exception as exc:  # noqa: BLE001
        result["restore_points"] = {
            "available": False,
            "bytes": None,
            "note": f"还原点探测失败（{exc.__class__.__name__}）；可用 vssadmin 查看",
        }

    return result


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def cache_path() -> Path:
    """索引缓存路径：``%LOCALAPPDATA%\\QingFengCleaner\\space_index.json``。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / _CACHE_DIR_NAME / _CACHE_FILE_NAME


def save_index(idx: SpaceIndex) -> None:
    """写出索引缓存；失败静默（写缓存失败不能影响主流程）。"""
    try:
        target = cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(idx.to_json(), ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > _CACHE_MAX_BYTES:
            return  # 超过 15 MB 不落盘
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
    except (OSError, TypeError, ValueError):
        return


def load_cached_index(max_age_hours: float = 24) -> SpaceIndex | None:
    """读取索引缓存；不存在/损坏/过期返回 ``None``。"""
    try:
        target = cache_path()
        if not target.is_file():
            return None
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        index = SpaceIndex.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None
    if max_age_hours is not None and max_age_hours >= 0:
        if time.time() - index.built_at > max_age_hours * 3600:
            return None
    return index


__all__ = [
    "SpaceIndex",
    "SpaceNode",
    "build_index",
    "cache_path",
    "load_cached_index",
    "probe_system_space",
    "save_index",
]
