"""PyInstaller 打包入口：修复 --noconsole（窗口化）exe 的控制台输出，再转交 main.main()。

为什么需要这个文件
------------------
PyInstaller 的窗口化引导器（--noconsole / runw.exe）没有控制台，会把
``sys.stdout`` / ``sys.stderr`` 置为 ``None``。于是 ``main.py --scan-only`` /
``--version`` 里的 ``print()`` 会抛 ``AttributeError: 'NoneType' object has no
attribute 'write'``；窗口化模式下 PyInstaller 还会弹出阻塞的 "Error" 对话框，
导致命令行完全不可用（源码运行 ``python main.py`` 没有这个问题）。

修复放在打包入口而不是产品代码 ``main.py`` 里，保持产品入口单一：

1. 若 PyInstaller 保留了可用的标准句柄（例如从 cmd 重定向到文件/管道启动），
   直接把 sys.stdout/stderr 重新接上去；
2. 否则尝试 ``AttachConsole(-1)`` 附着到父进程控制台（从 cmd / PowerShell 启动 exe 时）；
3. 都不行（双击启动、无控制台）则退化为空设备 —— --scan-only 至少不会崩溃或弹错误框。

GUI 模式（无参数）不依赖 stdio，上述处理是透明的。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# 冻结（onefile）时 __file__ 位于 _MEIPASS 中；源码/开发态时位于项目根的 packaging/ 下。
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
ATTACH_PARENT_PROCESS = -1
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
NULL_HANDLE = 0


def _wrap_std_handle(handle_id: int):
    """把已有的标准句柄重新包装成文本流；句柄无效时返回 None。"""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(handle_id)
    except Exception:
        return None
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    try:
        import msvcrt

        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1)
    except (OSError, OverflowError, ValueError):
        return None


def _attach_parent_console() -> bool:
    try:
        return bool(ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS))
    except Exception:
        return False


def repair_stdio() -> None:
    """确保 sys.stdout / sys.stderr 非 None（窗口化 exe 下）。"""
    if sys.stdout is not None and sys.stderr is not None:
        return

    out = sys.stdout if sys.stdout is not None else _wrap_std_handle(STD_OUTPUT_HANDLE)
    err = sys.stderr if sys.stderr is not None else _wrap_std_handle(STD_ERROR_HANDLE)

    if out is None or err is None:
        if _attach_parent_console():
            out = out if out is not None else _wrap_std_handle(STD_OUTPUT_HANDLE)
            err = err if err is not None else _wrap_std_handle(STD_ERROR_HANDLE)

    if out is None:
        out = open(os.devnull, "w", encoding="utf-8")
    if err is None:
        err = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = out
    sys.stderr = err


def main() -> int:
    repair_stdio()
    import main as app

    return int(app.main())


if __name__ == "__main__":
    sys.exit(main())
