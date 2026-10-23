"""引擎适配层：优先使用 `qingfeng.core`，不可用时无缝退化为内置演示数据。

界面只依赖本文件暴露的稳定接口（load_rules / scan / clean / drive_usage ...），
因此即使 core 尚未就绪（ImportError）、或运行中规则文件损坏，GUI 也始终能启动。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import traceback

from . import mock_engine

CATEGORY_ORDER = ["系统", "浏览器", "应用", "隐私痕迹", "大文件", "系统维护"]
RISK_ORDER = {"safe": 0, "caution": 1, "advanced": 2}
RISK_LABEL = {"safe": "安全", "caution": "谨慎", "advanced": "高级"}

# 清理前需要提醒用户"先退出"的常见程序（exe 小写 -> 中文名）
WATCHED_PROCESSES: tuple[tuple[str, str], ...] = (
    ("msedge.exe", "Microsoft Edge"),
    ("chrome.exe", "Google Chrome"),
    ("firefox.exe", "Mozilla Firefox"),
    ("winword.exe", "Microsoft Word"),
    ("excel.exe", "Microsoft Excel"),
)


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


__all__ = [
    "Engine",
    "CATEGORY_ORDER",
    "RISK_ORDER",
    "RISK_LABEL",
    "WATCHED_PROCESSES",
    "human_size",
    "category_index",
]


def human_size(n: float) -> str:
    """兜底的人类可读大小（%APPDATA% 外的引擎实现存在时优先用引擎的）。"""
    return mock_engine._human_size(n)


def category_index(category: str) -> int:
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)


class Engine:
    """统一引擎门面。属性 `mode` 为 "native" 或 "mock"。"""

    def __init__(self) -> None:
        self.mode = "mock"
        self.reason = ""
        self._rules_mod = None
        self._scanner_mod = None
        self._cleaner_mod = None
        self._winapi_mod = None
        self._detect()

    # -- 检测 ----------------------------------------------------------
    def _detect(self) -> None:
        try:
            from qingfeng.core import cleaner as cleaner_mod
            from qingfeng.core import rules as rules_mod
            from qingfeng.core import scanner as scanner_mod
            from qingfeng.core import winapi as winapi_mod
        except Exception as exc:  # ImportError / 半成品模块的语法或依赖错误
            self.reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.mode = "mock"
            return
        missing = [
            name
            for name, mod in (
                ("load_rules", rules_mod),
                ("scan", scanner_mod),
                ("clean", cleaner_mod),
                ("drive_usage", winapi_mod),
            )
            if not callable(getattr(mod, name, None))
        ]
        if missing:
            self.reason = "引擎接口不完整：" + ", ".join(missing)
            self.mode = "mock"
            return
        self._rules_mod = rules_mod
        self._scanner_mod = scanner_mod
        self._cleaner_mod = cleaner_mod
        self._winapi_mod = winapi_mod
        self.mode = "native"
        self.reason = ""

    # -- 状态 ----------------------------------------------------------
    @property
    def is_native(self) -> bool:
        return self.mode == "native"

    @property
    def status_text(self) -> str:
        if self.is_native:
            return "引擎：系统规则库"
        return "引擎：内置演示数据"

    @property
    def status_detail(self) -> str:
        if self.is_native:
            return "已连接 qingfeng.core 引擎"
        return f"qingfeng.core 未就绪，使用演示数据（{self.reason or '未检测到引擎'}）"

    def _degrade(self, exc: Exception) -> None:
        self.mode = "mock"
        self.reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        traceback.print_exc()

    # -- 规则 ----------------------------------------------------------
    def load_rules(self) -> list:
        """加载规则；native 失败时自动退化为演示规则。"""
        if self.is_native and self._rules_mod is not None:
            try:
                rules = self._rules_mod.load_rules()
                rules = list(rules or [])
                if not rules:
                    raise RuntimeError("规则库为空")
                return rules
            except Exception as exc:
                self._degrade(exc)
        return mock_engine.load_rules()

    # -- 扫描 / 清理 ---------------------------------------------------
    def scan(self, rules, *, progress=None, cancel=None):
        if self.is_native and self._scanner_mod is not None:
            return self._scanner_mod.scan(rules, progress=progress, cancel=cancel)
        return mock_engine.scan(rules, progress=progress, cancel=cancel)

    def clean(self, results, *, mode: str = "recycle", progress=None, cancel=None):
        if self.is_native and self._cleaner_mod is not None:
            return self._cleaner_mod.clean(
                results, mode=mode, progress=progress, cancel=cancel
            )
        return mock_engine.clean(results, mode=mode, progress=progress, cancel=cancel)

    # -- 系统信息 ------------------------------------------------------
    def is_admin(self) -> bool:
        if self.is_native and self._winapi_mod is not None:
            try:
                return bool(self._winapi_mod.is_admin())
            except Exception:
                pass
        return mock_engine._is_admin()

    def drive_usage(self, drive: str = "C:") -> tuple[int, int]:
        if self.is_native and self._winapi_mod is not None:
            try:
                free, total = self._winapi_mod.drive_usage(drive)
                return int(free), int(total)
            except Exception:
                pass
        return mock_engine._drive_usage(drive)

    def human_size(self, n: float) -> str:
        if self.is_native and self._winapi_mod is not None:
            try:
                return str(self._winapi_mod.human_size(int(n)))
            except Exception:
                pass
        return human_size(n)

    def restart_as_admin(self, argv: list[str] | None = None) -> bool:
        if self.is_native and self._winapi_mod is not None:
            try:
                return bool(self._winapi_mod.restart_as_admin(argv))
            except Exception:
                pass
        try:
            exe = sys.executable
            params = subprocess.list2cmdline(argv if argv is not None else sys.argv)
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            return int(result) > 32
        except Exception:
            return False

    def open_recycle_bin(self) -> bool:
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "open", "explorer.exe", "shell:RecycleBinFolder", None, 1
            )
            return int(result) > 32
        except Exception:
            return False

    def open_in_explorer(self, path: str) -> None:
        if self.is_native and self._winapi_mod is not None:
            try:
                self._winapi_mod.open_in_explorer(path)
                return
            except Exception:
                pass
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            pass

    def empty_recycle_bin(self) -> bool:
        """清空回收站（不可恢复）。演示模式下仅模拟成功，不触碰真实回收站。"""
        if self.is_native and self._winapi_mod is not None:
            try:
                return bool(self._winapi_mod.empty_recycle_bin())
            except Exception:
                pass
        if self.mode == "mock":
            return True
        try:
            # SHEmptyRecycleBinW：NOCONFIRMATION | NOPROGRESSUI | NOSOUND
            result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
            return int(result) == 0
        except Exception:
            return False

    # -- 回收站占用（只读，SHQueryRecycleBinW）--------------------------
    def recycle_bin_size(self) -> tuple[int, int]:
        """返回 (回收站占用字节数, 项目数)。只读查询，任何模式都返回真实值。"""
        try:
            shell32 = ctypes.windll.shell32
            shell32.SHQueryRecycleBinW.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO),
            ]
            shell32.SHQueryRecycleBinW.restype = ctypes.c_long
            info = _SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
            hr = shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
            if hr == 0:
                return max(0, int(info.i64Size)), max(0, int(info.i64NumItems))
        except Exception:
            pass
        return 0, 0

    # -- 运行中的相关程序（清理前置检查）------------------------------
    def running_processes(self) -> list[str]:
        """检测浏览器 / Office 是否正在运行，返回中文程序名列表。"""
        if sys.platform != "win32":
            return []
        found: set[str] = set()
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            invalid_handle = ctypes.c_void_p(-1).value
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
            if not snapshot or snapshot == invalid_handle:
                return []
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            try:
                ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                while ok:
                    found.add(str(entry.szExeFile).lower())
                    ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            traceback.print_exc()
            return []
        return [label for exe, label in WATCHED_PROCESSES if exe in found]

    # -- 重启资源管理器（重建图标/缩略图缓存）--------------------------
    def restart_explorer(self) -> bool:
        """非阻塞重启 explorer.exe；不等待、不阻塞 UI。"""
        if sys.platform != "win32":
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                ["taskkill", "/f", "/im", "explorer.exe"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            subprocess.Popen(
                ["cmd", "/c", "timeout /t 2 /nobreak >nul & start explorer.exe"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        except Exception:
            traceback.print_exc()
            return False
