"""清风清理 — 图形界面包。

对外入口（见 docs/ARCHITECTURE.md §6）：

    from qingfeng.ui.main_window import MainWindow, launch
    exit_code = launch(with_tray=True, start_hidden=False)
"""

from __future__ import annotations

from .main_window import MainWindow, launch

__all__ = ["MainWindow", "launch"]
