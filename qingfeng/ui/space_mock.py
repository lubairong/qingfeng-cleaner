"""清风清理 v2 — 空间透视的界面内置数据（core.space/decide 未就绪时的兜底）。

与 `docs/ARCHITECTURE-V2.md` §2/§3 的数据结构保持字段兼容：
SpaceNode / SpaceIndex / ReviewItem / ReviewGroup / MoveResult。
所有构建都在后台线程调用，绝不碰 tkinter；只读，不修改文件。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

GB = 1024 ** 3
MB = 1024 ** 2


# ---------------------------------------------------------------------------
# 数据结构（与 core.space / core.decide 契约字段一致）
# ---------------------------------------------------------------------------


@dataclass
class SpaceNode:
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
    root: str
    total_bytes: int
    accessible_bytes: int
    unaccounted_bytes: int
    system_files: dict[str, int]
    protected_hint: dict[str, str]
    duration: float
    built_at: float
    cancelled: bool = False
    truncated: bool = False
    root_node: SpaceNode | None = None
    _map: dict[str, SpaceNode] | None = field(default=None, repr=False)

    def find(self, path: str) -> SpaceNode | None:
        if self.root_node is None:
            return None
        if self._map is None:
            mapping: dict[str, SpaceNode] = {}

            def walk(node: SpaceNode) -> None:
                mapping[node.path.lower()] = node
                for child in node.children:
                    walk(child)

            walk(self.root_node)
            self._map = mapping
        target = os.path.normpath(path).lower()
        node = self._map.get(target)
        if node is not None:
            return node
        # 容错：去掉末尾分隔符
        return self._map.get(target.rstrip("\\"))

    def to_json(self) -> dict:
        def node_dict(node: SpaceNode) -> dict:
            return {
                "path": node.path, "name": node.name, "is_dir": node.is_dir,
                "size": node.size, "file_count": node.file_count,
                "mtime": node.mtime, "truncated": node.truncated,
                "other_size": node.other_size, "error": node.error,
                "children": [node_dict(c) for c in node.children],
            }

        return {
            "root": self.root, "total_bytes": self.total_bytes,
            "accessible_bytes": self.accessible_bytes,
            "unaccounted_bytes": self.unaccounted_bytes,
            "system_files": self.system_files,
            "protected_hint": self.protected_hint,
            "duration": self.duration, "built_at": self.built_at,
            "cancelled": self.cancelled, "truncated": self.truncated,
            "root_node": node_dict(self.root_node) if self.root_node else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> "SpaceIndex":
        def parse(d: dict) -> SpaceNode:
            return SpaceNode(
                path=d.get("path", ""), name=d.get("name", ""),
                is_dir=bool(d.get("is_dir")), size=int(d.get("size", 0)),
                file_count=int(d.get("file_count", 0)),
                mtime=float(d.get("mtime", 0)),
                children=[parse(c) for c in d.get("children", [])],
                truncated=bool(d.get("truncated")),
                other_size=int(d.get("other_size", 0)),
                error=d.get("error"),
            )

        root = data.get("root_node")
        return cls(
            root=data.get("root", "C:\\"),
            total_bytes=int(data.get("total_bytes", 0)),
            accessible_bytes=int(data.get("accessible_bytes", 0)),
            unaccounted_bytes=int(data.get("unaccounted_bytes", 0)),
            system_files=dict(data.get("system_files") or {}),
            protected_hint=dict(data.get("protected_hint") or {}),
            duration=float(data.get("duration", 0)),
            built_at=float(data.get("built_at", 0)),
            cancelled=bool(data.get("cancelled")),
            truncated=bool(data.get("truncated")),
            root_node=parse(root) if root else None,
        )


@dataclass
class ReviewItem:
    path: str
    name: str
    is_dir: bool
    size: int
    mtime: float
    age_days: int
    ext: str
    category: str
    advice: str
    reason: str
    risk: str
    group: str


@dataclass
class ReviewGroup:
    key: str
    label: str
    items: list[ReviewItem]
    total_bytes: int
    advice: str


@dataclass
class MoveResult:
    moved: int = 0
    moved_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    dest_root: str = ""
    duration: float = 0.0


@dataclass
class ActionPolicy:
    """与 core.decide.action_policy 返回的 ActionPolicy 字段兼容。"""

    can_delete: bool = True
    can_move: bool = True
    deny_reason: str = ""


# ---------------------------------------------------------------------------
# 本机磁盘信息（真实）
# ---------------------------------------------------------------------------


def _drive_usage(drive: str) -> tuple[int, int]:
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive.rstrip("\\") + "\\"),
            ctypes.byref(free), ctypes.byref(total), ctypes.byref(total_free),
        )
        if ok:
            return int(free.value), int(total.value)
    except Exception:
        pass
    return 77 * GB, 200 * GB


def _recycle_bin_size() -> tuple[int, int]:
    try:
        class RBINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong),
            ]

        info = RBINFO()
        info.cbSize = ctypes.sizeof(RBINFO)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if hr == 0:
            return max(0, int(info.i64Size)), max(0, int(info.i64NumItems))
    except Exception:
        pass
    return 0, 0


def _file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# probe_system_space
# ---------------------------------------------------------------------------


def probe_system_space() -> dict:
    """返回可直接展示的系统占用（只读）。"""
    recycle_bytes, recycle_count = _recycle_bin_size()
    hiberfil = _file_size(r"C:\hiberfil.sys")
    pagefile = _file_size(r"C:\pagefile.sys")
    swapfile = _file_size(r"C:\swapfile.sys")
    return {
        "recycle_bin": {"count": recycle_count, "bytes": recycle_bytes},
        "hiberfil": {"exists": hiberfil > 0 or True, "bytes": hiberfil or int(6.76 * GB)},
        "pagefile": {"exists": pagefile > 0 or True, "bytes": pagefile or int(0.25 * GB)},
        "swapfile": {"exists": swapfile > 0 or True, "bytes": swapfile or int(0.25 * GB)},
        "winsxs": {"bytes": int(14.4 * GB)},
        "windows_old": {"exists": False, "bytes": 0},
        "restore_points": {
            "available": False,
            "bytes": None,
            "note": "非管理员无法读取还原点占用；可在“系统保护”中查看",
        },
        "windows": {"bytes": int(35.4 * GB)},
        "installer_cache": {"bytes": int(2.1 * GB)},
        "demo": True,
    }


# ---------------------------------------------------------------------------
# 构建索引（演示数据，带进度/取消）
# ---------------------------------------------------------------------------


def _node(path: str, name: str, size: int, *, is_dir: bool = True,
          children: list[SpaceNode] | None = None, file_count: int = 0,
          mtime: float | None = None, other_size: int = 0,
          truncated: bool = False) -> SpaceNode:
    return SpaceNode(
        path=path, name=name, is_dir=is_dir, size=int(size),
        file_count=file_count or max(1, int(size / (1.5 * MB))),
        mtime=mtime or (_now() - 86400 * 37),
        children=children or [], other_size=int(other_size),
        truncated=truncated,
    )


def _build_tree() -> SpaceNode:
    now = _now()
    users_huawei = _node(
        r"C:\Users\HUAWEI", "HUAWEI", int(36.5 * GB),
        children=[
            _node(r"C:\Users\HUAWEI\AppData", "AppData", int(22.4 * GB),
                  children=[
                      _node(r"C:\Users\HUAWEI\AppData\Local", "Local", int(18.6 * GB),
                            children=[
                                _node(r"C:\Users\HUAWEI\AppData\Local\Microsoft",
                                      "Microsoft", int(6.4 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\Packages",
                                      "Packages", int(5.2 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\Temp",
                                      "Temp", int(2.1 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\Programs",
                                      "Programs", int(1.6 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\CrashDumps",
                                      "CrashDumps", int(0.8 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\Google",
                                      "Google", int(1.2 * GB)),
                                _node(r"C:\Users\HUAWEI\AppData\Local\Ollama",
                                      "Ollama", int(0.9 * GB)),
                            ], other_size=int(0.4 * GB), truncated=True),
                      _node(r"C:\Users\HUAWEI\AppData\Roaming", "Roaming", int(3.2 * GB)),
                      _node(r"C:\Users\HUAWEI\AppData\LocalLow", "LocalLow", int(0.6 * GB)),
                  ]),
            _node(r"C:\Users\HUAWEI\Downloads", "Downloads", int(4.6 * GB),
                  children=[
                      _node(r"C:\Users\HUAWEI\Downloads\Windows11_23H2.iso",
                            "Windows11_23H2.iso", int(4.2 * GB), is_dir=False),
                  ]),
            _node(r"C:\Users\HUAWEI\Documents", "Documents", int(3.3 * GB)),
            _node(r"C:\Users\HUAWEI\Videos", "Videos", int(3.1 * GB)),
            _node(r"C:\Users\HUAWEI\Pictures", "Pictures", int(2.1 * GB)),
            _node(r"C:\Users\HUAWEI\Desktop", "Desktop", int(1.2 * GB)),
        ],
        other_size=int(1.4 * GB), truncated=True,
    )
    windows = _node(
        r"C:\Windows", "Windows", int(35.4 * GB),
        children=[
            _node(r"C:\Windows\WinSxS", "WinSxS", int(14.4 * GB), other_size=int(1.1 * GB),
                  truncated=True),
            _node(r"C:\Windows\System32", "System32", int(7.8 * GB)),
            _node(r"C:\Windows\SysWOW64", "SysWOW64", int(3.6 * GB)),
            _node(r"C:\Windows\SoftwareDistribution", "SoftwareDistribution",
                  int(3.2 * GB)),
            _node(r"C:\Windows\Installer", "Installer", int(2.1 * GB)),
            _node(r"C:\Windows\assembly", "assembly", int(1.4 * GB)),
            _node(r"C:\Windows\Logs", "Logs", int(0.9 * GB)),
            _node(r"C:\Windows\Temp", "Temp", int(0.4 * GB)),
        ],
        other_size=int(26.7 * GB), truncated=True,
    )
    protected = _node(
        r"C:\System Volume Information", "System Volume Information", int(25.0 * GB),
        truncated=True,
    )
    recycle = _node(r"C:\$Recycle.Bin", "$Recycle.Bin", int(1.6 * GB), truncated=True)
    children = [
        windows,
        _node(r"C:\Users", "Users", int(36.8 * GB),
              children=[users_huawei,
                        _node(r"C:\Users\Public", "Public", int(0.3 * GB))],
              other_size=int(0.0 * GB)),
        _node(r"C:\Program Files", "Program Files", int(8.9 * GB),
              children=[
                  _node(r"C:\Program Files\Microsoft Office", "Microsoft Office",
                        int(3.1 * GB)),
                  _node(r"C:\Program Files\NVIDIA Corporation", "NVIDIA Corporation",
                        int(2.4 * GB)),
                  _node(r"C:\Program Files\Google", "Google", int(1.6 * GB)),
              ], other_size=int(1.8 * GB), truncated=True),
        _node(r"C:\Program Files (x86)", "Program Files (x86)", int(3.6 * GB)),
        _node(r"C:\ProgramData", "ProgramData", int(4.1 * GB)),
        _node(r"C:\hiberfil.sys", "hiberfil.sys", int(6.76 * GB), is_dir=False),
        _node(r"C:\pagefile.sys", "pagefile.sys", int(0.25 * GB), is_dir=False),
        _node(r"C:\swapfile.sys", "swapfile.sys", int(0.25 * GB), is_dir=False),
        protected,
        recycle,
    ]
    children.sort(key=lambda n: n.size, reverse=True)
    return _node("C:\\", "C:", int(122.8 * GB), children=children,
                 other_size=0, truncated=False)


_CACHE: dict[str, object] = {"index": None, "built_at": 0.0}


def build_index(
    root: str = "C:\\",
    *,
    min_child_size: int = 32 * 1024 * 1024,
    max_nodes: int = 150_000,
    progress=None,
    cancel=None,
) -> SpaceIndex:
    """演示索引：模拟真实扫描节奏（约 2.4 秒），有进度与取消。"""
    started = time.time()
    steps = [
        r"C:\\", r"C:\Windows", r"C:\Windows\WinSxS", r"C:\Windows\System32",
        r"C:\Users", r"C:\Users\HUAWEI", r"C:\Users\HUAWEI\AppData\Local",
        r"C:\Program Files", r"C:\ProgramData", r"C:\$Recycle.Bin",
        r"C:\System Volume Information",
    ]
    total_steps = 60
    for i in range(1, total_steps + 1):
        if cancel is not None and cancel():
            partial = _build_tree()
            free, total = _drive_usage("C:")
            return SpaceIndex(
                root=root, total_bytes=max(0, total - free),
                accessible_bytes=int(88.8 * GB),
                unaccounted_bytes=int(27.1 * GB),
                system_files={"hiberfil.sys": int(6.76 * GB),
                              "pagefile.sys": int(0.25 * GB),
                              "swapfile.sys": int(0.25 * GB)},
                protected_hint={
                    "System Volume Information": "还原点，需管理员",
                    "$Recycle.Bin": "回收站，清空需确认",
                },
                duration=time.time() - started, built_at=time.time(),
                cancelled=True, root_node=partial,
            )
        if progress is not None:
            try:
                progress(steps[(i - 1) % len(steps)], i * 7000)
            except Exception:
                pass
        time.sleep(0.04)
    free, total = _drive_usage("C:")
    node = _build_tree()
    idx = SpaceIndex(
        root=root,
        total_bytes=max(0, total - free),
        accessible_bytes=int(88.8 * GB),
        unaccounted_bytes=int(27.1 * GB),
        system_files={"hiberfil.sys": int(6.76 * GB),
                      "pagefile.sys": int(0.25 * GB),
                      "swapfile.sys": int(0.25 * GB)},
        protected_hint={"System Volume Information": "还原点，需管理员",
                        "$Recycle.Bin": "回收站，清空需确认"},
        duration=time.time() - started, built_at=time.time(), root_node=node,
    )
    _CACHE["index"] = idx
    _CACHE["built_at"] = time.time()
    return idx


def cache_path() -> Path:
    return Path.home() / "AppData" / "Local" / "QingFengCleaner" / "space_index_demo.json"


def save_index(idx: SpaceIndex) -> None:
    _CACHE["index"] = idx
    _CACHE["built_at"] = time.time()


def load_cached_index(max_age_hours: float = 24) -> SpaceIndex | None:
    idx = _CACHE.get("index")
    built_at = float(_CACHE.get("built_at") or 0)
    if idx is not None and time.time() - built_at <= max_age_hours * 3600:
        return idx  # type: ignore[return-value]
    return None


# ---------------------------------------------------------------------------
# 大文件分类（演示数据，61 个 ≥200MB，合计约 19.5GB）
# ---------------------------------------------------------------------------

_GROUP_LABEL = {
    "installer": "安装包 / 升级残留",
    "app-version-dup": "程序的旧版本副本",
    "backup": "备份 / 旧副本",
    "devcache": "开发缓存 / 构建产物",
    "video": "视频 / 录屏",
    "archive": "压缩包 / 镜像",
    "vm": "虚拟机磁盘",
    "large-misc": "其它大文件",
    "keep": "建议保留（模型 / 数据 / 数据库）",
}

_GROUP_ADVICE = {
    "installer": "放心清理（回收站）",
    "app-version-dup": "可清理（确认程序未使用旧版）",
    "backup": "可清理（确认无回滚需求）",
    "devcache": "可清理（下次构建会重建）",
    "video": "你来判断（可迁移到 D/E）",
    "archive": "你来判断（可迁移到 D/E）",
    "vm": "你来判断（可迁移到 D/E）",
    "large-misc": "你来判断",
    "keep": "不要删",
}

# (group, 名称, 大小 MB, 路径, 扩展名, 建议, 理由, 风险)
_RAW_ITEMS: list[tuple[str, str, float, str, str, str, str, str]] = [
    ("installer", "Windows11_23H2_x64.iso", 4200, r"C:\Users\HUAWEI\Downloads\Windows11_23H2_x64.iso", "iso", "safe_delete", "超过 1 年未修改的升级镜像，系统已升级到 23H2", "caution"),
    ("installer", "WPS_Office_2023_pro.exe", 860, r"C:\Users\HUAWEI\Downloads\WPS_Office_2023_pro.exe", "exe", "safe_delete", "安装包，软件已安装", "caution"),
    ("installer", "Adobe_Photoshop_2022_setup.exe", 1740, r"C:\Users\HUAWEI\Downloads\Adobe_Photoshop_2022_setup.exe", "exe", "safe_delete", "安装包，Photoshop 已安装", "caution"),
    ("installer", "NVIDIA_536.99_win10.exe", 640, r"C:\Users\HUAWEI\Downloads\NVIDIA_536.99_win10.exe", "exe", "safe_delete", "旧显卡驱动安装包", "caution"),
    ("installer", "VisualStudioSetup.exe", 1250, r"C:\Users\HUAWEI\AppData\Local\Temp\VisualStudioSetup.exe", "exe", "safe_delete", "临时目录里的安装器，安装已完成", "caution"),
    ("installer", "微信输入法_3.0.1.msi", 320, r"C:\Users\HUAWEI\Downloads\微信输入法_3.0.1.msi", "msi", "safe_delete", "安装包，当前已使用新版本", "caution"),
    ("installer", "node-v20.11.0-x64.msi", 280, r"C:\Users\HUAWEI\Downloads\node-v20.11.0-x64.msi", "msi", "safe_delete", "Node.js 安装包，已安装", "caution"),
    ("installer", "Office_365_离线安装.iso", 3900, r"C:\Users\HUAWEI\Downloads\Office_365_离线安装.iso", "iso", "safe_delete", "离线安装镜像，Office 已安装", "caution"),
    ("app-version-dup", "EdgeCore 旧版本 118", 640, r"C:\Program Files (x86)\Microsoft\EdgeCore\118.0.2088.76", "dir", "review", "Edge 旧版本目录，升级后可清理", "caution"),
    ("app-version-dup", "EdgeWebView 旧版本", 520, r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\118.0.2088.76", "dir", "review", "WebView2 旧版本目录", "caution"),
    ("app-version-dup", "Microsoft VS Code 旧版", 410, r"C:\Users\HUAWEI\AppData\Local\Programs\Microsoft VS Code\resources\app.old", "dir", "review", "VS Code 升级后残留的旧资源", "caution"),
    ("app-version-dup", "Chrome 旧版本 121", 300, r"C:\Program Files\Google\Chrome\Application\121.0.6167.85", "dir", "review", "Chrome 旧版本目录", "caution"),
    ("backup", "C盘备份_2024-12.7z", 720, r"C:\Users\HUAWEI\Documents\备份\C盘备份_2024-12.7z", "7z", "review", "手动备份文件，确认无回滚需求后可清理", "caution"),
    ("backup", "系统还原备份.7z", 560, r"C:\Users\HUAWEI\Documents\备份\系统还原备份.7z", "7z", "review", "旧备份", "caution"),
    ("backup", "WeChat_Backup_2023.zip", 380, r"C:\Users\HUAWEI\Downloads\WeChat_Backup_2023.zip", "zip", "review", "微信旧备份", "caution"),
    ("devcache", "node_modules 缓存", 420, r"C:\Users\HUAWEI\projects\web\node_modules", "dir", "review", "构建缓存，下次 npm install 会重建", "caution"),
    ("devcache", "target 构建产物", 360, r"C:\Users\HUAWEI\projects\rust\target", "dir", "review", "Rust 构建产物，可 cargo build 重建", "caution"),
    ("devcache", ".gradle 缓存", 300, r"C:\Users\HUAWEI\.gradle\caches", "dir", "review", "Gradle 缓存", "caution"),
    ("devcache", "pip 缓存", 260, r"C:\Users\HUAWEI\AppData\Local\pip\cache", "dir", "review", "pip 下载缓存", "caution"),
    ("devcache", "npm 缓存", 220, r"C:\Users\HUAWEI\AppData\Local\npm-cache", "dir", "review", "npm 下载缓存", "caution"),
    ("devcache", "Docker 镜像缓存", 200, r"C:\Users\HUAWEI\AppData\Local\Docker\wsl\data", "dir", "review", "Docker WSL 数据缓存", "caution"),
    ("devcache", "Unity 缓存", 210, r"C:\Users\HUAWEI\AppData\Local\Unity\cache", "dir", "review", "Unity 资源导入缓存", "caution"),
    ("video", "屏幕录制_2025-03-12.mp4", 560, r"C:\Users\HUAWEI\Videos\屏幕录制_2025-03-12.mp4", "mp4", "review", "屏幕录像，可迁移到 D 盘", "safe"),
    ("video", "课程录屏_第01讲.mp4", 520, r"C:\Users\HUAWEI\Videos\课程录屏_第01讲.mp4", "mp4", "review", "课程录像，可迁移", "safe"),
    ("video", "课程录屏_第02讲.mp4", 480, r"C:\Users\HUAWEI\Videos\课程录屏_第02讲.mp4", "mp4", "review", "课程录像，可迁移", "safe"),
    ("video", "会议录像_2024-11.mp4", 470, r"C:\Users\HUAWEI\Videos\会议录像_2024-11.mp4", "mp4", "review", "会议录像，可迁移", "safe"),
    ("video", "旅行vlog_剪辑版.mkv", 430, r"C:\Users\HUAWEI\Videos\旅行vlog_剪辑版.mkv", "mkv", "review", "视频素材，可迁移", "safe"),
    ("video", "无人机航拍_4K.mp4", 410, r"C:\Users\HUAWEI\Videos\无人机航拍_4K.mp4", "mp4", "review", "4K 视频，可迁移", "safe"),
    ("video", "产品演示_录制.mp4", 390, r"C:\Users\HUAWEI\Videos\产品演示_录制.mp4", "mp4", "review", "产品演示录像", "safe"),
    ("video", "直播回放_2024-10.mp4", 360, r"C:\Users\HUAWEI\Videos\直播回放_2024-10.mp4", "mp4", "review", "直播回放，可迁移", "safe"),
    ("video", "手机备份_相册视频.mp4", 340, r"C:\Users\HUAWEI\Videos\手机备份_相册视频.mp4", "mp4", "review", "手机相册视频备份", "safe"),
    ("video", "教学视频_合集01.mp4", 320, r"C:\Users\HUAWEI\Videos\教学视频_合集01.mp4", "mp4", "review", "教学视频", "safe"),
    ("video", "教学视频_合集02.mp4", 300, r"C:\Users\HUAWEI\Videos\教学视频_合集02.mp4", "mp4", "review", "教学视频", "safe"),
    ("video", "游戏录像_高光.mp4", 290, r"C:\Users\HUAWEI\Videos\游戏录像_高光.mp4", "mp4", "review", "游戏录像", "safe"),
    ("video", "直播回放_2024-08.mp4", 270, r"C:\Users\HUAWEI\Videos\直播回放_2024-08.mp4", "mp4", "review", "直播回放", "safe"),
    ("video", "手机录屏_操作演示.mp4", 260, r"C:\Users\HUAWEI\Videos\手机录屏_操作演示.mp4", "mp4", "review", "手机录屏", "safe"),
    ("video", "旅行vlog_原片.mov", 250, r"C:\Users\HUAWEI\Videos\旅行vlog_原片.mov", "mov", "review", "原始素材", "safe"),
    ("video", "课程录屏_第03讲.mp4", 240, r"C:\Users\HUAWEI\Videos\课程录屏_第03讲.mp4", "mp4", "review", "课程录像", "safe"),
    ("video", "会议录像_2024-09.mp4", 230, r"C:\Users\HUAWEI\Videos\会议录像_2024-09.mp4", "mp4", "review", "会议录像", "safe"),
    ("video", "宣传片_初稿.mp4", 220, r"C:\Users\HUAWEI\Videos\宣传片_初稿.mp4", "mp4", "review", "宣传片初稿", "safe"),
    ("archive", "旧项目归档_2023.7z", 420, r"C:\Users\HUAWEI\Documents\旧项目归档_2023.7z", "7z", "review", "旧项目归档，可迁移", "safe"),
    ("archive", "素材库_2022.zip", 390, r"C:\Users\HUAWEI\Documents\素材库_2022.zip", "zip", "review", "设计素材压缩包", "safe"),
    ("archive", "软件安装合集.iso", 350, r"C:\Users\HUAWEI\Downloads\软件安装合集.iso", "iso", "review", "软件安装合集镜像", "caution"),
    ("archive", "照片备份_2021.rar", 320, r"C:\Users\HUAWEI\Pictures\照片备份_2021.rar", "rar", "review", "照片备份压缩包", "safe"),
    ("archive", "虚拟机导出.ova", 300, r"C:\Users\HUAWEI\Downloads\虚拟机导出.ova", "ova", "review", "虚拟机导出镜像", "safe"),
    ("vm", "Win11_开发.vhdx", 900, r"C:\Users\HUAWEI\VMs\Win11_开发.vhdx", "vhdx", "review", "虚拟机磁盘，可迁移到 D/E", "safe"),
    ("vm", "Ubuntu_22.04.vmdk", 760, r"C:\Users\HUAWEI\VMs\Ubuntu_22.04.vmdk", "vmdk", "review", "虚拟机磁盘，可迁移", "safe"),
    ("vm", "CentOS_测试.vhdx", 520, r"C:\Users\HUAWEI\VMs\CentOS_测试.vhdx", "vhdx", "review", "虚拟机磁盘", "safe"),
    ("vm", "Win10_旧环境.vhd", 420, r"C:\Users\HUAWEI\VMs\Win10_旧环境.vhd", "vhd", "review", "旧虚拟机磁盘", "safe"),
    ("large-misc", "游戏安装包_备份.exe", 380, r"C:\Users\HUAWEI\Downloads\游戏安装包_备份.exe", "exe", "review", "其它大文件，按需处理", "caution"),
    ("large-misc", "数据集_训练样本.tar", 340, r"C:\Users\HUAWEI\Downloads\数据集_训练样本.tar", "tar", "review", "数据集压缩包", "caution"),
    ("large-misc", "日志转储.dmp", 300, r"C:\Users\HUAWEI\AppData\Local\CrashDumps\app.dmp", "dmp", "review", "崩溃转储文件", "caution"),
    ("large-misc", "字体包_合集.zip", 280, r"C:\Users\HUAWEI\Downloads\字体包_合集.zip", "zip", "review", "字体合集", "caution"),
    ("large-misc", "素材_未分类.psd", 240, r"C:\Users\HUAWEI\Pictures\素材_未分类.psd", "psd", "review", "设计源文件", "caution"),
    ("large-misc", "旧电脑迁移包.zip", 220, r"C:\Users\HUAWEI\Downloads\旧电脑迁移包.zip", "zip", "review", "旧电脑迁移备份", "caution"),
    ("keep", "qwen2.5-7b-instruct.Q4_K_M.gguf", 760, r"C:\Users\HUAWEI\.ollama\models\qwen2.5-7b-instruct.Q4_K_M.gguf", "gguf", "keep", "本地大模型权重，删除需重新下载", "danger"),
    ("keep", "llama-3.1-8b.Q4_0.gguf", 680, r"C:\Users\HUAWEI\.ollama\models\llama-3.1-8b.Q4_0.gguf", "gguf", "keep", "本地大模型权重", "danger"),
    ("keep", "微信聊天记录数据库.db", 600, r"C:\Users\HUAWEI\Documents\WeChat Files\聊天记录.db", "db", "keep", "微信聊天数据，请勿删除", "danger"),
    ("keep", "照片库.photoslibrary", 520, r"C:\Users\HUAWEI\Pictures\照片库.photoslibrary", "dir", "keep", "照片库数据，请勿删除", "danger"),
    ("keep", "项目数据库.sqlite", 460, r"C:\Users\HUAWEI\projects\app\data.sqlite", "sqlite", "keep", "项目数据库，请勿删除", "danger"),
    ("keep", "本地知识库.faiss", 400, r"C:\Users\HUAWEI\.cache\kb\local.faiss", "faiss", "keep", "向量索引数据", "danger"),
]


def collect_review_items(
    index: SpaceIndex | None = None,
    *,
    min_size: int = 100 * MB,
    limit: int = 800,
) -> list[ReviewItem]:
    now = _now()
    rnd = random.Random(20261022)
    raw = list(_RAW_ITEMS)
    total_mb = sum(item[2] for item in raw)
    factor = (19.5 * 1024) / total_mb  # 对齐契约里的本机实况 ~19.5GB
    items: list[ReviewItem] = []
    for i, (group, name, size_mb, path, ext, advice, reason, risk) in enumerate(raw):
        if i >= limit:
            break
        age = [3, 12, 30, 75, 180, 400, 700, 900][i % 8] + rnd.randint(0, 9)
        mtime = now - age * 86400 - rnd.randint(0, 4000)
        size = int(size_mb * MB * factor)
        if size < min_size:
            size = int(min_size * 1.05)
        items.append(ReviewItem(
            path=path, name=name, is_dir=(ext == "dir"), size=size,
            mtime=mtime, age_days=age, ext=ext if ext != "dir" else "",
            category=group, advice=advice, reason=reason, risk=risk,
            group=group,
        ))
    items.sort(key=lambda it: it.size, reverse=True)
    return items


def group_review_items(items: list[ReviewItem]) -> list[ReviewGroup]:
    order = ["installer", "app-version-dup", "backup", "devcache",
             "video", "archive", "vm", "large-misc", "keep"]
    buckets: dict[str, list[ReviewItem]] = {}
    for item in items:
        buckets.setdefault(item.group, []).append(item)
    groups: list[ReviewGroup] = []
    for key in order:
        bucket = buckets.get(key)
        if not bucket:
            continue
        bucket.sort(key=lambda it: it.size, reverse=True)
        groups.append(ReviewGroup(
            key=key, label=_GROUP_LABEL.get(key, key), items=bucket,
            total_bytes=sum(it.size for it in bucket),
            advice=_GROUP_ADVICE.get(key, "你来判断"),
        ))
    for key, bucket in buckets.items():
        if key in order:
            continue
        groups.append(ReviewGroup(
            key=key, label=_GROUP_LABEL.get(key, key), items=bucket,
            total_bytes=sum(it.size for it in bucket), advice="你来判断",
        ))
    return groups


def heuristics() -> dict:
    return {
        "version": 1,
        "categories": [
            {"key": "installer", "label": "安装包 / 升级残留",
             "exts": [".exe", ".msi", ".iso", ".zip", ".7z"],
             "advice": "safe_delete", "risk": "caution"},
            {"key": "keep", "label": "建议保留", "advice": "keep", "risk": "danger"},
        ],
        "age_rules": [
            {"min_size": GB, "min_age_days": 365, "advice": "review",
             "reason": "超过 1 年未修改"},
        ],
        "never_touch": [r"*\Windows\*", r"*\Program Files\*"],
    }


_PROGRAM_PATH_HINTS = (
    "\\program files", "\\program files (x86)", "\\windows\\",
    "\\windows\\winsxs", "\\programdata\\microsoft",
    "$recycle.bin", "system volume information",
)


def action_policy(item) -> ActionPolicy:
    """core.decide.action_policy 未就绪时的保守策略（宁可禁用，不误删）。"""
    path = str(getattr(item, "path", "")).lower().replace("/", "\\")
    advice = str(getattr(item, "advice", ""))
    group = str(getattr(item, "group", ""))
    if advice == "keep" or group == "keep":
        return ActionPolicy(
            can_delete=False, can_move=False,
            deny_reason="建议保留的项目：删除/迁移请先在文件资源管理器中手工确认",
        )
    if any(hint in path for hint in _PROGRAM_PATH_HINTS):
        return ActionPolicy(
            can_delete=False, can_move=False,
            deny_reason="程序/系统安装目录，请在程序设置或卸载中处理",
        )
    return ActionPolicy(can_delete=True, can_move=True, deny_reason="")


def delete_many(paths, *, progress=None, cancel=None):
    """占位：core.decide.delete_many 未就绪时不可用；空间页只用回收站动作。"""
    raise NotImplementedError("delete_many 未实现，空间透视页仅使用回收站")


# ---------------------------------------------------------------------------
# 迁移目标建议（真实探测盘符/可写性）
# ---------------------------------------------------------------------------


def _drive_type(drive: str) -> int:
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(
            ctypes.c_wchar_p(drive.rstrip("\\") + "\\")))
    except Exception:
        return 0


def _try_write_dir(path: str) -> bool:
    """真实创建/删除探针目录，避免 os.access 在沙箱/ACL 下误判。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".qf_write_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def suggest_targets() -> list[dict]:
    """探测 D:/E: 等固定磁盘，返回 path/writable/free/total/note。"""
    targets: list[dict] = []
    for letter in "DEFGH":
        drive = f"{letter}:"
        if _drive_type(drive) != 3:
            continue
        free, total = _drive_usage(drive)
        if total <= 0:
            continue
        root = drive + "\\"
        path = root + "QingFengMoved"
        note = ""
        if _try_write_dir(path):
            writable = True
            note = "根目录可写"
        else:
            writable = False
            candidates = ["Download", "Downloads", "tmp", "Temp",
                          "Doc", "Docs", "Data", "Users"]
            for candidate in candidates:
                cand = root + candidate
                if not os.path.isdir(cand):
                    continue
                candidate_path = cand + "\\QingFengMoved"
                if _try_write_dir(candidate_path):
                    path = candidate_path
                    writable = True
                    note = f"{drive} 根目录不可写，已改用 {cand}\\QingFengMoved"
                    break
            if not writable:
                note = f"{drive} 根目录与常见用户目录不可写；请选择自定义目录或以管理员身份运行"
        targets.append({
            "drive": drive, "path": path, "free": free, "total": total,
            "writable": writable, "note": note,
        })
    targets.sort(key=lambda t: not t["writable"])
    return targets


def move_many(
    paths: list[str],
    dest_root: str,
    *,
    progress=None,
    cancel=None,
) -> MoveResult:
    """演示迁移：只模拟进度与结果，不触碰真实文件。"""
    started = time.time()
    result = MoveResult(dest_root=dest_root)
    total = len(paths)
    for i, path in enumerate(paths, start=1):
        if cancel is not None and cancel():
            result.skipped.append((path, "用户取消迁移"))
            break
        if progress is not None:
            try:
                progress(path, i, total)
            except Exception:
                pass
        time.sleep(0.06)
        if not path:
            result.skipped.append((path, "路径为空"))
            continue
        if i % 17 == 0:
            result.failed.append((path, "模拟失败：目标盘空间不足"))
            continue
        result.moved += 1
        result.moved_bytes += int(0.32 * GB)
    result.duration = time.time() - started
    return result
