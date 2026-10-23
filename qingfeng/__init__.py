"""清风清理 QingFeng Cleaner — 包根。

模块划分见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

from pathlib import Path

APP_NAME = "清风清理"
APP_NAME_EN = "QingFeng Cleaner"
APP_ID = "QingFengCleaner"
__version__ = "0.1.0"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
ASSETS_DIR = PROJECT_ROOT / "assets"
RULES_DIR = PACKAGE_DIR / "data" / "rules"
ICON_PATH = ASSETS_DIR / "app.ico"

__all__ = [
    "APP_NAME",
    "APP_NAME_EN",
    "APP_ID",
    "__version__",
    "PACKAGE_DIR",
    "PROJECT_ROOT",
    "ASSETS_DIR",
    "RULES_DIR",
    "ICON_PATH",
]
