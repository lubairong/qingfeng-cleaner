"""真实界面截图工具（开发 / 文档用，不参与产品运行）。

流程
----
1. 用**真实引擎**启动主窗口（不是 mock / 演示数据），强制触发一次真实扫描；
2. 等扫描真正结束（`report` 就绪且不在扫描中），打印真实规则数与可清理体积；
3. 依次截取：
     docs/images/main.png     主窗口 · 浅色
     docs/images/confirm.png  清理确认弹窗（截完按 Esc 取消，**绝不执行清理**）
     docs/images/dark.png     主窗口 · 深色
4. 退出。

依赖：Pillow（仅用于抓屏和存 PNG，开发/文档用途；产品本体零第三方依赖）。

用法：
    python tools/capture_screenshots.py
    python tools/capture_screenshots.py --out-dir docs/images --timeout 240
    python tools/capture_screenshots.py --skip-confirm
"""

from __future__ import annotations

import argparse
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GA_ROOT = 2


def enable_dpi_awareness() -> None:
    """必须在创建 Tk 窗口之前调用，否则 GetWindowRect / 抓屏坐标会被系统按 DPI 虚拟化，
    导致截出来的窗口偏移、被裁切（并且会把桌面上的其它窗口拍进去）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _root_hwnd(widget) -> int:
    try:
        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), GA_ROOT)
    except Exception:
        hwnd = 0
    return hwnd or widget.winfo_id()


def _window_rect(widget) -> tuple[int, int, int, int]:
    """返回窗口可见边框（DWM 扩展边框）的屏幕坐标。

    直接 GetWindowRect 会包含 Win10/11 用于拖拽缩放的不可见边框（约 8px），
    抓屏时那圈区域会拍到桌面背景；DWMWA_EXTENDED_FRAME_BOUNDS 才是真正可见的边框。
    """
    hwnd = _root_hwnd(widget)
    rect = RECT()
    got = False
    try:
        import ctypes.wintypes as wt

        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        got = hr == 0 and rect.right - rect.left > 0
    except Exception:
        got = False
    if not got:
        if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            raise OSError("GetWindowRect 失败")
    # 夹到虚拟屏幕范围内，避免抓屏越界
    vx = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = ctypes.windll.user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    vw = ctypes.windll.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = ctypes.windll.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    left = max(rect.left, vx)
    top = max(rect.top, vy)
    right = min(rect.right, vx + vw)
    bottom = min(rect.bottom, vy + vh)
    if right - left < 400 or bottom - top < 300:
        raise OSError(f"窗口超出屏幕范围：{(rect.left, rect.top, rect.right, rect.bottom)}")
    return left, top, right, bottom


def pump(root, seconds: float) -> None:
    """在不进入 mainloop 的情况下驱动 Tk 事件（更新界面 + 处理 after 回调）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            root.update()
        except Exception:
            return
        time.sleep(0.02)


def grab(widget, out_path: Path, *, settle: float = 0.7) -> tuple[int, int]:
    """把窗口（含系统标题栏）抓成 PNG；返回图片尺寸。"""
    from PIL import ImageGrab

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        widget.attributes("-topmost", True)
        widget.lift()
    except Exception:
        pass
    widget.update_idletasks()
    widget.update()
    pump(widget, settle)
    box = _window_rect(widget)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(out_path, "PNG")
    try:
        widget.attributes("-topmost", False)
    except Exception:
        pass
    gray = img.convert("L")
    lo, hi = gray.getextrema()
    if img.size[0] < 400 or img.size[1] < 300:
        raise RuntimeError(f"截图尺寸异常：{img.size}（{out_path.name}）")
    if hi - lo < 10:
        raise RuntimeError(f"截图疑似空白：extrema={gray.getextrema()}（{out_path.name}）")
    return img.size


def wait_scan(root, win, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except Exception:
            return False
        if win.report is not None and not win.scanning:
            return True
        time.sleep(0.05)
    return False


def capture_confirm(root, win, out_path: Path) -> str:
    """打开真实「清理所选」确认弹窗并截图；截完取消（绝不清理）。"""
    state = {"captured": "", "error": ""}

    def snap() -> None:
        try:
            dialogs = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
            if not dialogs:
                state["error"] = "未找到确认弹窗"
                return
            dlg = dialogs[-1]
            pump(root, 0.8)
            size = grab(dlg, out_path)
            state["captured"] = f"{out_path.name} {size[0]}x{size[1]}"
            # 只取消，不确认：等价于点「取消」按钮
            try:
                dlg.cancel()
            except Exception:
                try:
                    dlg.destroy()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 - 截图工具，出错要能继续
            state["error"] = f"{type(exc).__name__}: {exc}"
            try:
                dialogs = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
                for dlg in dialogs:
                    dlg.destroy()
            except Exception:
                pass

    root.after(1500, snap)
    # clean_selected 内部走模态弹窗（wait_window），会阻塞到弹窗被取消
    win.clean_selected()
    pump(root, 0.4)
    if state["error"]:
        raise RuntimeError(state["error"])
    return state["captured"]


def scroll_tree_top(win) -> None:
    """把规则列表滚到最顶，保证截图能看到「系统」分类分组头与它下面的规则行。"""
    try:
        tree = win.tree.tree  # 自定义 Treeview 包装组件内部的 ttk.Treeview
        tree.yview_moveto(0.0)
        tree.update_idletasks()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清风清理真实界面截图工具")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--timeout", type=float, default=240.0, help="等待真实扫描完成的秒数")
    parser.add_argument("--skip-confirm", action="store_true", help="不截确认弹窗")
    parser.add_argument("--no-watermark", action="store_true", help="保留参数兼容（默认不加水印）")
    args = parser.parse_args(argv)

    try:
        import tkinter as tk

        from qingfeng.ui.main_window import MainWindow
    except Exception as exc:  # noqa: BLE001
        print(f"[capture] 无法导入界面模块：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    enable_dpi_awareness()
    root = tk.Tk()
    try:
        win = MainWindow(root, with_tray=False)
        print(f"[capture] 规则数：{len(win.rules)}；引擎管理员：{win.is_admin}")
        if win._rules_load_error:
            print(f"[capture] 警告：规则加载有错误：{win._rules_load_error}", file=sys.stderr)

        if not win.scanning:
            win.start_scan(auto=True)
        print("[capture] 等待真实扫描完成…")
        if not wait_scan(root, win, args.timeout):
            print("[capture] 扫描超时，未截到真实数据", file=sys.stderr)
            return 3
        report = win.report
        cleanable = int(getattr(report, "cleanable_bytes", 0))
        print(
            f"[capture] 扫描完成：{len(win.rules)} 条规则，"
            f"可清理 {cleanable} 字节，用时 {getattr(report, 'duration', 0):.2f}s"
        )

        # 1) 浅色主窗口（列表滚到顶部：要能看到「系统」分类头）
        win.settings["theme"] = "light"
        win._rebuild_ui()
        pump(root, 1.2)
        scroll_tree_top(win)
        pump(root, 0.4)
        size = grab(root, out_dir / "main.png")
        print(f"[capture] main.png  {size[0]}x{size[1]}")

        # 2) 深色主窗口（同样滚到顶部，保持两张一致）
        win.settings["theme"] = "dark"
        win._rebuild_ui()
        pump(root, 1.2)
        scroll_tree_top(win)
        pump(root, 0.4)
        size = grab(root, out_dir / "dark.png")
        print(f"[capture] dark.png   {size[0]}x{size[1]}")

        # 3) 清理确认弹窗（真实数据，截完取消，绝不清理）
        #    放在最后：取消后状态栏会变成"已取消清理"，不影响前两张。
        if not args.skip_confirm:
            win.settings["theme"] = "light"
            win._rebuild_ui()
            pump(root, 0.8)
            try:
                info = capture_confirm(root, win, out_dir / "confirm.png")
                print(f"[capture] confirm.png {info}")
            except Exception as exc:  # noqa: BLE001
                print(f"[capture] 确认弹窗截图失败（继续）：{exc}", file=sys.stderr)
        return 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
