"""清风清理 — 系统托盘（纯 ctypes：Shell_NotifyIconW + 隐藏窗口消息循环）。

设计：
- 在后台线程创建隐藏窗口（该线程负责 GetMessage 循环，满足 Shell_NotifyIcon 的线程要求）；
- 菜单使用 CreatePopupMenu + TrackPopupMenu(TPM_RETURNCMD)；
- 菜单/托盘点击的回调在**托盘线程**执行，回调实现必须线程安全（主窗口用 queue 回送）；
- 支持 TaskbarCreated 消息（资源管理器重启后自动恢复图标）；
- stop() 会销毁窗口、删除图标并退出消息循环，确保进程可以真正结束。

注意：Shell_NotifyIconW 需要向任务栏窗口发送窗口消息，受 Windows UIPI 限制，
低完整性（Low integrity）进程调用会返回 ERROR_ACCESS_DENIED(WinError 5)。
主窗口对托盘启动失败做了降级：界面照常可用，关闭窗口时直接退出。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
from pathlib import Path
from typing import Callable

from qingfeng import APP_NAME, ICON_PATH

log = logging.getLogger(__name__)

# -- 常量 -------------------------------------------------------------------
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_NULL = 0x0000
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NOTIFYICON_VERSION_4 = 4

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

ID_SHOW = 1001
ID_SCAN = 1002
ID_QUIT = 1003

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


# -- 结构体 -----------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_apis_ready = False
_user32 = None
_shell32 = None
_kernel32 = None


def _ensure_apis() -> None:
    global _apis_ready, _user32, _shell32, _kernel32
    if _apis_ready:
        return
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("系统托盘仅支持 Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR
    ]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
    ]
    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)
    ]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _user32, _shell32, _kernel32 = user32, shell32, kernel32
    _apis_ready = True


# 图标路径解析：允许显式传入；默认使用项目 assets/app.ico
def _resolve_icon_path(icon_path: Path | None) -> Path | None:
    candidates = []
    if icon_path:
        candidates.append(Path(icon_path))
    candidates.append(Path(ICON_PATH))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


class TrayIcon:
    """系统托盘图标。

    参数中的三个回调由托盘线程调用，实现方必须线程安全（例如写入 queue.Queue）。
    """

    def __init__(
        self,
        on_show: Callable[[], None],
        on_scan: Callable[[], None],
        on_quit: Callable[[], None],
        icon_path: Path | None = None,
    ) -> None:
        self.on_show = on_show
        self.on_scan = on_scan
        self.on_quit = on_quit
        self.icon_path = _resolve_icon_path(icon_path)
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hwnd: int | None = None
        self._hinst = None
        self._hicon: int | None = None
        self._nid = NOTIFYICONDATAW()
        self._nid_ready = False
        self._icon_added = False
        self._ready = threading.Event()
        self._error: str | None = None
        self._class_name = f"QingFengCleanerTray_{id(self)}"
        self._wndproc = WNDPROC(self._wnd_proc)
        self._taskbar_created = 0

    # -- 公开 API ------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(
            self._icon_added
            and self._thread is not None
            and self._thread.is_alive()
        )

    @property
    def error(self) -> str | None:
        return self._error

    def start(self) -> None:
        """启动托盘线程并等待图标创建完成（失败抛 RuntimeError）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        _ensure_apis()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run, name="qingfeng-tray", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("托盘初始化超时")
        if self._error:
            raise RuntimeError(self._error)

    def stop(self) -> None:
        """销毁图标并结束消息循环（可重复调用）。"""
        thread = self._thread
        if thread is None:
            return
        if self._hwnd:
            try:
                _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        thread.join(timeout=3.0)
        if thread.is_alive():
            log.warning("托盘消息循环未在 3 秒内退出，发送 WM_QUIT")
            try:
                if self._thread_id:
                    _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
            thread.join(timeout=1.5)
        self._thread = None
        self._hwnd = None
        log.info("托盘已停止")

    # -- 线程内部实现 --------------------------------------------------
    def _run(self) -> None:
        try:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            self._create_window()
            self._add_icon()
            log.info("托盘图标已创建 hwnd=0x%X icon=%s", self._hwnd, self.icon_path)
            self._ready.set()
            self._message_loop()
        except Exception as exc:  # 托盘失败不应拖垮主程序
            self._error = f"{type(exc).__name__}: {exc}"
            log.exception("托盘初始化失败")
            self._ready.set()
        finally:
            self._cleanup()

    def _create_window(self) -> None:
        self._hinst = _kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinst
        wc.lpszClassName = self._class_name
        atom = _user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())
        self._taskbar_created = int(_user32.RegisterWindowMessageW("TaskbarCreated"))
        hwnd = _user32.CreateWindowExW(
            0, self._class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, self._hinst, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = int(hwnd)

    def _load_icon(self) -> int:
        hicon = None
        if self.icon_path is not None:
            hicon = _user32.LoadImageW(
                None, str(self.icon_path), IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
        if not hicon:
            hicon = _user32.LoadIconW(
                None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
            )
        if not hicon:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hicon = int(hicon)
        return self._hicon

    def _add_icon(self) -> None:
        if self._hwnd is None:
            raise RuntimeError("窗口尚未创建")
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._load_icon()
        nid.szTip = f"{APP_NAME} — 右键打开菜单"
        ok = _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = nid
        self._nid_ready = True
        self._icon_added = True

        version_nid = NOTIFYICONDATAW()
        version_nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        version_nid.hWnd = self._hwnd
        version_nid.uID = 1
        version_nid.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        _shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(version_nid))

    def _remove_icon(self) -> None:
        if not self._icon_added or not self._nid_ready:
            return
        try:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            log.info("托盘图标已移除")
        except Exception:
            log.exception("移除托盘图标失败")
        self._icon_added = False

    def _message_loop(self) -> None:
        msg = wintypes.MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    # -- 窗口消息 ------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001 - ctypes 回调
        try:
            if msg == WM_TRAYICON:
                event = int(lparam) & 0xFFFF
                if event in (WM_CONTEXTMENU, WM_RBUTTONUP):
                    self._show_menu()
                elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire(self.on_show)
            elif msg == WM_COMMAND:
                self._handle_command(int(wparam) & 0xFFFF)
            elif msg and msg == self._taskbar_created:
                log.info("检测到 TaskbarCreated，恢复托盘图标")
                try:
                    self._icon_added = False
                    self._nid_ready = False
                    self._add_icon()
                except Exception:
                    log.exception("恢复托盘图标失败")
            elif msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                self._remove_icon()
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except Exception:
            log.exception("托盘窗口过程异常")
            return 0

    def _show_menu(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        command = 0
        try:
            _user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示主窗口")
            _user32.AppendMenuW(menu, MF_STRING, ID_SCAN, "快速扫描")
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, MF_STRING, ID_QUIT, f"退出 {APP_NAME}")
            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            # 经典技巧：先 SetForegroundWindow，菜单外点击才会自动消失
            _user32.SetForegroundWindow(hwnd)
            command = _user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x, point.y, 0, hwnd, None,
            )
            _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(menu)
        if command:
            self._handle_command(int(command))

    def _handle_command(self, command: int) -> None:
        if command == ID_SHOW:
            self._fire(self.on_show)
        elif command == ID_SCAN:
            self._fire(self.on_scan)
        elif command == ID_QUIT:
            self._fire(self.on_quit)

    def _fire(self, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            log.exception("托盘回调执行失败")

    def _cleanup(self) -> None:
        try:
            self._remove_icon()
        except Exception:
            pass
        try:
            if self._hwnd:
                _user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
        try:
            if self._hicon:
                _user32.DestroyIcon(self._hicon)
        except Exception:
            pass
        try:
            if self._hinst:
                _user32.UnregisterClassW(self._class_name, self._hinst)
        except Exception:
            pass
        self._hwnd = None
        self._icon_added = False
        log.info("托盘线程结束")
