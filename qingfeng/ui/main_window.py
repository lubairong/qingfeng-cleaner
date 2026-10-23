"""清风清理 — 主窗口（Windows 11 风格）。

线程约定：
- 扫描 / 清理都在后台线程执行；
- 子线程只向 `queue.Queue` 写事件，绝不触碰 tkinter；
- 主线程用 root.after(80ms) 轮询队列，更新界面；
- 托盘回调同样只写队列。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import tkinter as tk
from tkinter import ttk

from qingfeng import APP_NAME, APP_NAME_EN, ICON_PATH, __version__
from . import dialogs, theme, widgets
from .engine import Engine, RISK_LABEL, RISK_ORDER, category_index

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

CHECK_ON = "☑"
CHECK_OFF = "☐"
CHECK_PARTIAL = "－"
CHECK_NA = "—"

DEFAULT_SETTINGS: dict[str, Any] = {
    # 浅色为默认；设置里可切换「跟随系统 / 深色」
    "theme": "light",
    "delete_mode": "recycle",
    "minimize_to_tray": True,
    "scan_on_start": True,
}


# ---------------------------------------------------------------------------
# 设置持久化
# ---------------------------------------------------------------------------


def _settings_path() -> Path | None:
    try:
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "QingFengCleaner" / "settings.json"
    except Exception:
        return None


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    path = _settings_path()
    if path is None:
        return settings
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_SETTINGS:
                    if key in raw:
                        settings[key] = raw[key]
    except Exception:
        log.warning("读取设置失败，使用默认值", exc_info=True)
    return settings


def save_settings(settings: dict[str, Any]) -> bool:
    path = _settings_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({k: settings.get(k) for k in DEFAULT_SETTINGS},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        # 沙箱 / 只读安装目录下静默失败：设置仅在本次运行生效
        log.info("设置无法写入磁盘（%s），仅本次运行生效", path)
        return False


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        engine: Engine | None = None,
        *,
        with_tray: bool = True,
        start_hidden: bool = False,
    ) -> None:
        self.root = root
        self.engine = engine or Engine()
        self.with_tray = with_tray
        self.start_hidden = start_hidden
        self.settings = load_settings()
        self.exit_code = 0

        # 数据状态
        self.rules: list = []
        self._rule_map: dict[str, Any] = {}
        self.report = None
        self.results_by_rule: dict[str, Any] = {}
        self.checked: dict[str, bool] = {}
        self._elevation_rule_ids: set[str] = set()
        self._elevation_pending: int = 0
        self._elevation_is_fallback: bool = False
        self.sort_key: str | None = None
        self.sort_rev = False
        self._last_scan_at = ""
        self._last_status = ""
        self._last_status_detail = ""
        self._rules_load_error = ""
        self.selected_rule_id: str | None = None
        self.selected_category: str | None = None
        self._detail_copy_text = ""
        self.recycle_bin_bytes = 0
        self.recycle_bin_items = 0

        # 并发状态
        self.scanning = False
        self.cleaning = False
        self._cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self._poll_after_id: str | None = None
        self._quitting = False
        self._dialog_depth = 0
        self._tray_ok = False
        self.tray = None

        # 外观：先校正 DPI（打包/不同启动方式下 DPI 探测可能失真），
        # 再决定紧凑布局与样式
        theme.refine_dpi(root)
        self.compact = self._compute_compact()
        self._init_metrics()
        theme.apply_tk_scaling(root)
        self._resolve_palette()
        self.fonts = theme.init_fonts(root)
        theme.configure_defaults(root, self.palette, self.fonts)
        self.style = theme.apply_ttk_style(
            root, self.palette, self.fonts, compact=self.compact
        )

        self.is_admin = self.engine.is_admin()
        # 先加载规则（可能从 native 降级为演示数据），再构建界面，
        # 这样标题栏的引擎状态徽标与真实数据源一致。
        self._load_rules_data()
        self._refresh_recycle_bin_usage()
        self._setup_window()
        self._build_ui()
        self._render_tree()
        self._update_disk_card()
        self._update_selection_summary()
        self._start_poller()

        if self._rules_load_error:
            self._set_status("加载规则失败，已切换到内置演示数据")
            self.root.after(400, lambda: self._open_error(
                "规则加载失败",
                "无法读取清理规则库，界面已使用内置演示数据。",
                self._rules_load_error,
            ))

        if with_tray:
            self._start_tray()
            self._tray_ok = bool(
                self.tray is not None and getattr(self.tray, "available", False)
            )

        if start_hidden and self._tray_ok:
            self.root.withdraw()
        else:
            self.start_hidden = False
            self.root.deiconify()
            self.root.lift()

        # 启动时无论最终落在哪一页，都在后台跑一次 v1 规则扫描，
        # 保证顶部磁盘卡不会停留在「尚未扫描」（空间页也复用这张卡）。
        if self.settings.get("scan_on_start", True):
            self.root.after(350, self._auto_scan_once)

        # 标题栏颜色需要在窗口真正映射后设置，Windows 偶发需要重试
        self.root.after(120, self._apply_titlebar_theme)
        self.root.after(700, self._apply_titlebar_theme)
        self.root.after(1800, self._apply_titlebar_theme)

    def _auto_scan_once(self) -> None:
        """启动自动扫描（只跑一次；已在扫描/已有报告则跳过）。"""
        if self._quitting or self.scanning or self.cleaning:
            return
        if self.report is not None:
            return
        self.start_scan(auto=True)

    def _apply_titlebar_theme(self) -> None:
        try:
            theme.set_titlebar_dark(self.root, self.palette.mode == "dark")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 窗口基础
    # ------------------------------------------------------------------
    def _compute_compact(self) -> bool:
        """工作区较小时启用紧凑布局（压缩卡片/边距，优先保证操作区可见）。"""
        try:
            left, top, right, bottom = theme.work_area()
            work_w, work_h = right - left, bottom - top
        except Exception:
            work_w, work_h = theme.screen_size()
        return work_h < theme.sp(820) or work_w < theme.sp(1000)

    def _init_metrics(self) -> None:
        compact = self.compact
        self._pad_x = theme.sp(12 if compact else 22)
        self._pad_top = theme.sp(8 if compact else 14)
        self._pad_bottom = theme.sp(10 if compact else 16)
        self._gap = theme.sp(8 if compact else 12)
        self._disk_h = theme.sp(112 if compact else 138)
        self._action_h = theme.sp(116 if compact else 126)
        self._detail_note_lines = 3 if compact else 6
        self._detail_desc_lines = 2 if compact else 3
        self._detail_placeholder_lines = 1 if compact else 2

    def _resolve_palette(self) -> None:
        self.palette = theme.get_palette(str(self.settings.get("theme", "auto")))

    def _setup_window(self) -> None:
        pal = self.palette
        root = self.root
        root.title(f"{APP_NAME} {APP_NAME_EN}")
        root.configure(bg=pal.window)
        try:
            if ICON_PATH.exists():
                root.iconbitmap(default=str(ICON_PATH))
        except Exception:
            pass

        # 基于工作区（已排除任务栏）计算窗口尺寸：约 92%，并夹在合理区间。
        # 尺寸单位与 Tk 几何单位一致，fit_window 会保证外框完整落在工作区内。
        left, top, right, bottom = theme.work_area()
        avail_w = max(400, right - left)
        avail_h = max(320, bottom - top)
        scale = theme.SCALE
        lo_w = min(int(720 * scale), int(avail_w * 0.90))
        hi_w = max(lo_w, int(1280 * scale))
        lo_h = min(int(560 * scale), int(avail_h * 0.90))
        hi_h = max(lo_h, int(880 * scale))
        target_w = min(max(int(avail_w * 0.92), lo_w), hi_w)
        target_h = min(max(int(avail_h * 0.92), lo_h), hi_h)
        target_w = min(target_w, max(400, avail_w - int(24 * scale)))
        target_h = min(target_h, max(320, avail_h - int(24 * scale)))
        self._window_info = theme.fit_window(root, target_w, target_h)
        root.minsize(
            min(int(640 * scale), target_w),
            min(int(480 * scale), target_h),
        )

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.report_callback_exception = self._report_exception

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        pal = self.palette
        self.outer = tk.Frame(self.root, bg=pal.window)
        self.outer.pack(
            fill="both", expand=True,
            padx=self._pad_x, pady=(self._pad_top, self._pad_bottom),
        )
        self._build_header(self.outer)

        body = tk.Frame(self.outer, bg=pal.window)
        body.pack(fill="both", expand=True, pady=(self._gap, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self._body = body

        self.disk_card = widgets.Card(
            body, pal, height=self._disk_h / theme.SCALE,
            radius=12,
            padding=((12, 9) if self.compact else (20, 15)),
        )
        self.disk_card.grid(row=0, column=0, sticky="ew")
        self._build_disk_card()

        # 两页（清理 / 空间透视）共享顶部磁盘卡片
        self.pages_frame = tk.Frame(body, bg=pal.window)
        self.pages_frame.grid(row=1, column=0, sticky="nsew",
                              pady=(self._gap, 0))
        self.pages_frame.columnconfigure(0, weight=1)
        self.pages_frame.rowconfigure(0, weight=1)
        self.space_page = None

        self.clean_page = tk.Frame(self.pages_frame, bg=pal.window)
        self.clean_page.grid(row=0, column=0, sticky="nsew")
        self.clean_page.columnconfigure(0, weight=1)
        self.clean_page.rowconfigure(0, weight=1)

        self.list_card = widgets.Card(
            self.clean_page, pal, radius=12, padding=(0, 0), height=200,
        )
        self.list_card.grid(row=0, column=0, sticky="nsew")
        self._build_list_card()

        self.action_card = widgets.Card(
            self.clean_page, pal, height=self._action_h / theme.SCALE,
            radius=12,
            padding=((12, 9) if self.compact else (20, 13)),
        )
        self.action_card.grid(row=1, column=0, sticky="ew", pady=(self._gap, 0))
        self._build_action_card()

        self._active_page = "clean"
        self.clean_page.tkraise()

        # 键盘快捷键
        self.root.bind("<Control-f>", lambda _e: self.start_scan())
        self.root.bind("<F5>", lambda _e: self.start_scan())

    def _build_header(self, parent: tk.Misc) -> None:
        pal, fonts = self.palette, self.fonts
        header = tk.Frame(parent, bg=pal.window)
        header.pack(fill="x")

        left = tk.Frame(header, bg=pal.window)
        left.pack(side="left", anchor="w")
        title_row = tk.Frame(left, bg=pal.window)
        title_row.pack(anchor="w")
        tk.Label(
            title_row, text=APP_NAME, bg=pal.window, fg=pal.text,
            font=fonts.title, anchor="w",
        ).pack(side="left")
        widgets.Pill(
            title_row, f"v{__version__}", palette=pal, fonts=fonts,
            kind="neutral", bg=pal.window, height=20, padx=8,
        ).pack(side="left", padx=(theme.sp(10), 0), pady=(theme.sp(2 if self.compact else 4), 0))
        # 顶部主导航：清理 / 空间透视
        self.nav_control = widgets.SegmentedControl(
            title_row, [("clean", "清理"), ("space", "空间透视")],
            getattr(self, "_active_page", "clean"),
            palette=pal, fonts=fonts, bg=pal.window,
            height=30, padx=14, command=self._switch_page,
        )
        self.nav_control.pack(side="left", padx=(theme.sp(18), 0),
                              pady=(theme.sp(1), 0))
        if not self.compact:
            tk.Label(
                left, text="安全清理 C 盘垃圾 · 删除前二次确认 · 默认移入回收站",
                bg=pal.window, fg=pal.text_secondary, font=fonts.small, anchor="w",
            ).pack(anchor="w", pady=(theme.sp(3), 0))

        right = tk.Frame(header, bg=pal.window)
        right.pack(side="right", anchor="e")
        self.theme_btn = widgets.RoundedButton(
            right,
            "切换深色" if pal.mode == "light" else "切换浅色",
            self._toggle_theme,
            palette=pal, fonts=fonts, kind="secondary", bg=pal.window,
            height=32, padx=15, font=fonts.small,
        )
        self.theme_btn.pack(side="right")
        if self.engine.is_native:
            widgets.Pill(right, "系统规则库", palette=pal, fonts=fonts,
                         kind="green", bg=pal.window, height=22).pack(
                side="right", padx=(0, theme.sp(10)))
        else:
            self._engine_pill = widgets.Pill(
                right, "演示数据", palette=pal, fonts=fonts,
                kind="caution", bg=pal.window, height=22,
            )
            self._engine_pill.pack(side="right", padx=(0, theme.sp(10)))

        # 权限状态 chip：让用户一眼看到系统项为什么受限
        self.privilege_pill = widgets.Pill(
            right,
            "管理员模式 · 系统项可用" if self.is_admin
            else "标准权限 · 系统项受限",
            palette=pal, fonts=fonts,
            kind="green" if self.is_admin else "caution",
            bg=pal.window, height=22,
        )
        self.privilege_pill.pack(side="right", padx=(0, theme.sp(10)))

    # ------------------------------------------------------------------
    # 页面切换
    # ------------------------------------------------------------------
    def _switch_page(self, key: str) -> None:
        if key not in ("clean", "space"):
            return
        if key == getattr(self, "_active_page", "clean"):
            return
        try:
            if key == "space":
                self._ensure_space_page()
                self.space_page.on_show()
                self.space_page.tkraise()
            else:
                self.clean_page.tkraise()
                # 恢复清理页原有行高（空间页曾临时压缩 Treeview 行高）
                self.style = theme.apply_ttk_style(
                    self.root, self.palette, self.fonts, compact=self.compact
                )
        except Exception:
            log.exception("切换页面失败：%s", key)
            return
        if (key == "space" and self.report is None
                and self.settings.get("scan_on_start", True)):
            # 直接以 --page space 启动时，确保 v1 扫描随后补上磁盘卡数据
            self.root.after(80, self._auto_scan_once)
        self._active_page = key
        nav = getattr(self, "nav_control", None)
        if nav is not None and getattr(nav, "value", None) != key:
            try:
                nav.select(key)
            except Exception:
                pass

    def _ensure_space_page(self):
        if self.space_page is not None:
            return self.space_page
        from .space_page import SpacePage

        self.space_page = SpacePage(
            self.pages_frame,
            palette=self.palette,
            fonts=self.fonts,
            engine=self.engine,
            root=self.root,
            compact=self.compact,
            on_restart_as_admin=self._restart_as_admin,
            cleanable_provider=lambda: int(
                getattr(self.report, "cleanable_bytes", 0)
            ) if self.report is not None else 0,
        )
        self.space_page.grid(row=0, column=0, sticky="nsew")
        return self.space_page

    def _build_disk_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        b = self.disk_card.body
        b.columnconfigure(1, weight=1)
        b.rowconfigure(0, weight=1)

        self.ring = widgets.RingGauge(
            b, pal, fonts,
            size=82 if self.compact else 100,
            thickness=8 if self.compact else 9,
        )
        self.ring.grid(row=0, column=0, sticky="w",
                       padx=(0, theme.sp(12 if self.compact else 20)))

        info = tk.Frame(b, bg=pal.surface)
        info.grid(row=0, column=1, sticky="w")
        tk.Label(info, text="系统盘 (C:) · 可用空间", bg=pal.surface,
                 fg=pal.text_secondary, font=fonts.small_bold,
                 anchor="w").pack(anchor="w")
        self.free_label = tk.Label(info, text="—", bg=pal.surface, fg=pal.text,
                                   font=fonts.num_lg, anchor="w")
        self.free_label.pack(anchor="w", pady=(theme.sp(1), 0))
        self.used_label = tk.Label(info, text="正在读取磁盘信息…", bg=pal.surface,
                                   fg=pal.text_tertiary, font=fonts.small, anchor="w")
        self.used_label.pack(anchor="w", pady=(theme.sp(1), 0))

        stats = tk.Frame(b, bg=pal.surface)
        stats.grid(row=0, column=2, sticky="e",
                   padx=(theme.sp(10 if self.compact else 14), 0))
        tk.Label(stats, text="可清理空间", bg=pal.surface, fg=pal.text_secondary,
                 font=fonts.small, anchor="e").pack(anchor="e")
        self.cleanable_label = tk.Label(stats, text="—", bg=pal.surface, fg=pal.accent,
                                        font=fonts.num_md, anchor="e")
        self.cleanable_label.pack(anchor="e", pady=(theme.sp(1), 0))
        self.last_scan_label = tk.Label(stats, text="尚未扫描", bg=pal.surface,
                                        fg=pal.text_tertiary, font=fonts.tiny, anchor="e")
        self.last_scan_label.pack(anchor="e", pady=(theme.sp(2 if self.compact else 3), 0))

        if not self.is_admin:
            self.admin_btn = widgets.RoundedButton(
                b, "以管理员身份重启（解锁系统清理）", self._restart_as_admin,
                palette=pal, fonts=fonts, kind="secondary",
                height=30 if self.compact else 32, padx=12 if self.compact else 15,
                font=fonts.small,
            )
            self.admin_btn.grid(
                row=0, column=3, sticky="e",
                padx=(theme.sp(10 if self.compact else 20), 0),
            )

    def _build_list_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        b = self.list_card.body
        b.columnconfigure(0, weight=1)
        b.rowconfigure(3, weight=1)

        toolbar = tk.Frame(b, bg=pal.surface)
        toolbar.grid(row=0, column=0, sticky="ew",
                     padx=theme.sp(16),
                     pady=(theme.sp(8 if self.compact else 12),
                           theme.sp(4 if self.compact else 6)))
        tk.Label(toolbar, text="清理项目", bg=pal.surface, fg=pal.text,
                 font=fonts.h2, anchor="w").pack(side="left")
        self.rules_pill = widgets.Pill(
            toolbar, "正在加载规则…", palette=pal, fonts=fonts,
            kind="neutral", bg=pal.surface, height=20,
        )
        self.rules_pill.pack(side="left", padx=(theme.sp(10), 0))

        tools = tk.Frame(toolbar, bg=pal.surface)
        tools.pack(side="right")
        for text, command in (
            ("全部勾选", self._check_all),
            ("全部取消", self._uncheck_all),
            ("只勾安全项", self._check_safe_only),
        ):
            widgets.RoundedButton(
                tools, text, command, palette=pal, fonts=fonts, kind="link",
                bg=pal.surface, height=28, padx=10, font=fonts.small,
            ).pack(side="left", padx=(theme.sp(2), 0))

        tk.Frame(b, bg=pal.border, height=max(1, theme.sp(1))).grid(
            row=2, column=0, sticky="ew")

        # 系统规则提权横幅（扫描后存在 requires_elevation 规则时显示）
        banner = tk.Frame(b, bg=pal.caution_soft, cursor="hand2")
        banner.grid(row=1, column=0, sticky="ew", padx=theme.sp(16),
                    pady=(theme.sp(4), 0))
        banner.grid_remove()
        banner_icon = tk.Label(
            banner, text="!", bg=pal.caution_soft, fg=pal.caution_text,
            font=fonts.small_bold, width=2,
        )
        banner_icon.pack(side="left", padx=(theme.sp(8), 0), pady=theme.sp(5))
        self._elevation_banner_text = tk.Label(
            banner, text="", bg=pal.caution_soft, fg=pal.caution_text,
            font=fonts.small, anchor="w", justify="left",
        )
        self._elevation_banner_text.pack(
            side="left", fill="x", expand=True, pady=theme.sp(5)
        )
        banner_action = tk.Label(
            banner, text="点此以管理员身份重启 →", bg=pal.caution_soft,
            fg=pal.accent, font=fonts.small_bold, anchor="e", cursor="hand2",
        )
        banner_action.pack(side="right", padx=(theme.sp(8), theme.sp(10)))
        for widget in (banner, banner_icon, self._elevation_banner_text,
                       banner_action):
            widget.bind("<Button-1>", lambda _e: self._restart_as_admin())
        self._elevation_banner = banner
        self._elevation_banner_action = banner_action

        self.tree = widgets.RuleTree(
            b, pal, fonts,
            on_toggle_rule=self._on_toggle_rule,
            on_toggle_category=self._on_toggle_category,
            on_sort=self._on_sort,
            on_select_rule=self._on_tree_select,
            on_columns_changed=self._on_tree_columns_changed,
        )
        self.tree.grid(row=3, column=0, sticky="nsew",
                       padx=(theme.sp(4), theme.sp(4)), pady=(theme.sp(2), 0))

        # 规则详情（完整说明 + report_note 多行命令，可复制）
        detail_frame = tk.Frame(b, bg=pal.surface)
        detail_frame.grid(row=4, column=0, sticky="ew",
                          padx=theme.sp(16),
                          pady=(theme.sp(2 if self.compact else 4), 0))
        detail_head = tk.Frame(detail_frame, bg=pal.surface)
        detail_head.pack(fill="x")
        tk.Label(
            detail_head, text="规则详情", bg=pal.surface, fg=pal.text_secondary,
            font=fonts.small_bold, anchor="w",
        ).pack(side="left")
        self.detail_title = tk.Label(
            detail_head, text="", bg=pal.surface, fg=pal.text,
            font=fonts.small, anchor="w",
        )
        self.detail_title.pack(side="left", padx=(theme.sp(8), 0))
        self.copy_btn = widgets.RoundedButton(
            detail_head, "复制命令", self._copy_detail,
            palette=pal, fonts=fonts, kind="link", bg=pal.surface,
            height=26, padx=10, font=fonts.small,
        )
        self.copy_btn.pack(side="right")
        self.copy_btn.set_enabled(False)

        text_bg = pal.surface_alt
        text_row = tk.Frame(detail_frame, bg=pal.surface)
        text_row.pack(fill="x", pady=(theme.sp(4), 0))
        self.detail_text = tk.Text(
            text_row,
            height=self._detail_placeholder_lines,
            wrap="word",
            bg=text_bg,
            fg=pal.text,
            font=fonts.small,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=pal.border,
            highlightcolor=pal.border_strong,
            padx=theme.sp(10),
            pady=theme.sp(6),
            cursor="arrow",
        )
        self.detail_text.pack(side="left", fill="both", expand=True)
        self.detail_scroll = ttk.Scrollbar(
            text_row, orient="vertical", command=self.detail_text.yview
        )
        self.detail_scroll.pack(side="right", fill="y", padx=(theme.sp(4), 0))
        self.detail_text.configure(yscrollcommand=self.detail_scroll.set)
        self.detail_text.tag_configure("desc", foreground=pal.text, font=fonts.small)
        self.detail_text.tag_configure(
            "note", foreground=pal.text_secondary, font=fonts.mono,
            lmargin1=theme.sp(2), lmargin2=theme.sp(2), spacing1=theme.sp(2),
        )
        self.detail_text.tag_configure(
            "meta", foreground=pal.text_tertiary, font=fonts.tiny
        )
        self.detail_text.tag_configure(
            "hl", foreground=pal.accent, font=fonts.small_bold
        )
        self.detail_text.configure(state="disabled")
        self.detail_text.bind("<MouseWheel>", self._on_detail_wheel)
        self._set_detail_placeholder()

        if not self.compact:
            tk.Label(
                b,
                text="提示：点击首列或双击行可勾选；点击行查看完整说明与建议命令；"
                     "标记「仅报告」的项目不会删除。",
                bg=pal.surface, fg=pal.text_tertiary, font=fonts.tiny, anchor="w",
            ).grid(row=5, column=0, sticky="ew",
                   padx=theme.sp(16), pady=(theme.sp(4), theme.sp(8)))

    # ------------------------------------------------------------------
    # 规则详情面板
    # ------------------------------------------------------------------
    def _on_detail_wheel(self, event: tk.Event) -> str:
        try:
            self.detail_text.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass
        return "break"

    def _set_detail_text(self, segments: list[tuple[str, str]],
                         copy_text: str = "") -> None:
        text = self.detail_text
        try:
            text.configure(state="normal")
            text.delete("1.0", "end")
            for tag, chunk in segments:
                text.insert("end", chunk, tag)
            text.configure(state="disabled")
            text.yview_moveto(0)
            # 有建议命令时给多行命令留出空间（内部可滚动、可全选复制）
            if copy_text:
                height = self._detail_note_lines
            elif segments and segments[0][0] == "desc":
                height = self._detail_desc_lines
            else:
                height = self._detail_placeholder_lines
            if int(text.cget("height")) != height:
                text.configure(height=height)
        except Exception:
            pass
        self._detail_copy_text = copy_text
        if hasattr(self, "copy_btn"):
            self.copy_btn.set_enabled(bool(copy_text))

    def _set_detail_placeholder(self) -> None:
        if hasattr(self, "detail_title"):
            self.detail_title.configure(text="")
        self._set_detail_text([(
            "meta",
            "点击列表中的规则或分类，查看完整说明与建议命令；report 规则只提供命令，"
            "本工具不会执行。",
        )])

    def _on_tree_select(self, iid: str) -> None:
        kind, _, key = iid.partition("::")
        if kind == "cat":
            self.selected_category = key
            self.selected_rule_id = None
            self._show_category_detail(key)
            return
        self.selected_rule_id = key
        self.selected_category = None
        rule = self._rule_map.get(key)
        if rule is not None:
            self._show_rule_detail(rule)
        # 详情面板高度变化可能把选中行挤出可视区，布局稳定后再滚动到它
        self.root.after(40, lambda: self._ensure_row_visible(iid))

    def _ensure_row_visible(self, iid: str) -> None:
        try:
            self.root.update_idletasks()
            if iid in self.tree.tree.get_children(""):
                self.tree.tree.see(iid)
        except Exception:
            pass

    def _show_category_detail(self, category: str) -> None:
        rules = [r for r in self.rules if getattr(r, "category", "") == category]
        cleanable = [r for r in rules if getattr(r, "action", "delete") == "delete"]
        report = [r for r in rules if getattr(r, "action", "delete") == "report"]
        operations = [
            r for r in rules if getattr(r, "action", "delete") == "empty_recycle_bin"
        ]
        total_bytes = 0
        for rule in cleanable:
            res = self.results_by_rule.get(getattr(rule, "id", ""))
            total_bytes += int(getattr(res, "total_bytes", 0))
        if hasattr(self, "detail_title"):
            self.detail_title.configure(text=f"{category} · 分类")
        segments: list[tuple[str, str]] = [(
            "desc",
            f"共 {len(rules)} 条规则：可清理 {len(cleanable)} 条"
            + (f"、不可恢复操作 {len(operations)} 条" if operations else "")
            + (f"、仅报告 {len(report)} 条" if report else "")
            + "。",
        )]
        if self.report is not None:
            segments.append((
                "meta",
                f"\n当前扫描可清理合计：{self.engine.human_size(total_bytes)}",
            ))
        segments.append((
            "meta",
            "\n勾选分类左侧的复选框可整组选择；分类内的规则可在右侧切换。",
        ))
        self._set_detail_text(segments)

    def _show_rule_detail(self, rule) -> None:
        rule_id = getattr(rule, "id", "")
        action = getattr(rule, "action", "delete")
        risk = getattr(rule, "risk", "safe")
        res = self.results_by_rule.get(rule_id)
        risk_text = (
            RISK_LABEL.get(risk, risk) if action == "delete"
            else "不可恢复操作" if action == "empty_recycle_bin"
            else "仅报告"
        )
        if hasattr(self, "detail_title"):
            self.detail_title.configure(
                text=f"{getattr(rule, 'name', rule_id)} · "
                     f"{getattr(rule, 'category', '')} · {risk_text}"
            )
        desc = str(getattr(rule, "description", "") or "")
        note = str(getattr(rule, "report_note", "") or "")
        segments: list[tuple[str, str]] = [("desc", desc)]
        if action == "report":
            segments.append((
                "meta",
                "\n仅报告：本工具不会扫描或删除，只给出建议命令。",
            ))
        elif action == "empty_recycle_bin":
            segments.append((
                "meta",
                "\n该规则会永久清空回收站，不可恢复；默认不勾选。",
            ))
        if note:
            segments.append(("meta", "\n建议命令（可复制后自行执行）：\n"))
            segments.append(("note", note))
        if res is not None and getattr(res, "skipped_reason", None) and not note:
            segments.append(("meta", f"\n说明：{res.skipped_reason}"))
        if res is not None and getattr(res, "error", None):
            segments.append(("meta", f"\n扫描错误：{res.error}"))
        if res is not None and getattr(res, "total_bytes", 0) and action == "delete":
            segments.append((
                "meta",
                f"\n扫描结果：{self.engine.human_size(int(res.total_bytes))} · "
                f"{int(getattr(res, 'file_count', 0)):,} 项",
            ))
        self._set_detail_text(segments, note if action == "report" and note else "")

    def _copy_detail(self) -> None:
        text = self._detail_copy_text.strip()
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self._set_status("已复制建议命令到剪贴板")
        except Exception:
            self._set_status("复制到剪贴板失败")

    def _refresh_recycle_bin_usage(self) -> None:
        try:
            self.recycle_bin_bytes, self.recycle_bin_items = (
                self.engine.recycle_bin_size()
            )
        except Exception:
            self.recycle_bin_bytes, self.recycle_bin_items = 0, 0

    def _build_action_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        b = self.action_card.body
        b.columnconfigure(0, weight=1)

        row0 = tk.Frame(b, bg=pal.surface)
        row0.grid(row=0, column=0, sticky="ew")
        self.status_label = tk.Label(
            row0, text="", bg=pal.surface, fg=pal.text_secondary,
            font=fonts.small, anchor="w",
        )
        self.status_label.pack(side="left")
        self.selection_label = tk.Label(
            row0, text="尚未扫描", bg=pal.surface, fg=pal.accent,
            font=fonts.small_bold, anchor="e",
        )
        self.selection_label.pack(side="right")

        self.progress = widgets.ProgressBar(b, pal, height=6)
        self.progress.grid(
            row=1, column=0, sticky="ew",
            pady=(theme.sp(5 if self.compact else 8),
                  theme.sp(6 if self.compact else 10)),
        )

        row2 = tk.Frame(b, bg=pal.surface)
        row2.grid(row=2, column=0, sticky="ew")

        small_h = 32 if self.compact else 36
        primary_h = 34 if self.compact else 38
        left_actions = tk.Frame(row2, bg=pal.surface)
        left_actions.pack(side="left")
        self.recycle_btn = widgets.RoundedButton(
            left_actions, "打开回收站", self.open_recycle_bin,
            palette=pal, fonts=fonts, kind="secondary", height=small_h,
            padx=14 if self.compact else 17,
            font=fonts.small_bold,
        )
        self.recycle_btn.pack(side="left")
        self.settings_btn = widgets.RoundedButton(
            left_actions, "设置", self.open_settings_dialog,
            palette=pal, fonts=fonts, kind="secondary", height=small_h,
            padx=16 if self.compact else 20,
            font=fonts.small_bold,
        )
        self.settings_btn.pack(side="left", padx=(theme.sp(6 if self.compact else 8), 0))

        right_actions = tk.Frame(row2, bg=pal.surface)
        right_actions.pack(side="right")
        self.scan_btn = widgets.RoundedButton(
            right_actions, "开始扫描", self._on_scan_button,
            palette=pal, fonts=fonts, kind="primary", height=primary_h,
            padx=20 if self.compact else 24,
        )
        self.scan_btn.pack(side="right")
        self.clean_btn = widgets.RoundedButton(
            right_actions, "清理所选", self.clean_selected,
            palette=pal, fonts=fonts, kind="danger", height=primary_h,
            padx=18 if self.compact else 22,
        )
        self.clean_btn.pack(side="right", padx=(0, theme.sp(8 if self.compact else 10)))
        self.clean_btn.set_enabled(False)

        self._set_status("就绪 · " + self.engine.status_text)

    # ------------------------------------------------------------------
    # 规则加载 / 渲染
    # ------------------------------------------------------------------
    def _load_rules_data(self) -> None:
        """只加载数据，不碰界面；失败时由调用方决定如何提示。"""
        self._rules_load_error = ""
        try:
            self.rules = list(self.engine.load_rules())
        except Exception as exc:
            self.rules = []
            self._rules_load_error = f"{type(exc).__name__}: {exc}"
            log.exception("加载规则失败")
        self._rule_map = {
            getattr(r, "id", str(i)): r for i, r in enumerate(self.rules)
        }
        self._init_checked()
        self._render_tree()

    def _init_checked(self) -> None:
        for rule in self.rules:
            rule_id = getattr(rule, "id", "")
            if rule_id in self.checked:
                continue
            default = bool(getattr(rule, "default_enabled", True))
            risk = getattr(rule, "risk", "safe")
            self.checked[rule_id] = default and risk != "advanced"

    def _rule_is_cleanable(self, rule) -> bool:
        """delete 动作：扫描后按字节计入可清理空间。"""
        return getattr(rule, "action", "delete") == "delete"

    def _rule_is_actionable(self, rule) -> bool:
        """可勾选执行的动作：删除文件 + 清空回收站。"""
        return getattr(rule, "action", "delete") in ("delete", "empty_recycle_bin")

    def _result_needs_elevation(self, res) -> bool:
        """兼容旧 core 的字段探测：结果是否因权限/提权需求而未完成。"""
        if self.is_admin or res is None:
            return False
        rule = getattr(res, "rule", None)
        if bool(getattr(res, "requires_elevation", False)):
            return True
        if bool(getattr(rule, "requires_elevation", False)):
            return True
        text = " ".join(str(getattr(res, attr, "") or "") for attr in
                        ("skipped_reason", "error")).lower()
        hints = ("管理员", "权限", "拒绝访问", "access is denied",
                 "permission", "elevation", "privilege")
        if any(hint in text for hint in hints):
            return True
        # 旧 core 兼容：requires_admin 规则在标准权限下扫不出结果时视为需要提权
        if getattr(rule, "requires_admin", False):
            if getattr(res, "error", None) or getattr(res, "skipped_reason", None):
                return True
            if int(getattr(res, "total_bytes", 0) or 0) == 0 and \
                    int(getattr(res, "file_count", 0) or 0) == 0:
                return True
        return False

    def _rule_needs_elevation(self, rule) -> bool:
        if self.is_admin or rule is None:
            return False
        rule_id = getattr(rule, "id", "")
        if rule_id in self._elevation_rule_ids:
            return True
        return bool(getattr(rule, "requires_elevation", False))

    @staticmethod
    def _elevation_pending_of(res) -> int:
        for attr in ("pending_items", "elevation_items", "pending_count",
                     "requires_elevation_count"):
            try:
                value = int(getattr(res, attr, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                return value
        return 0

    def _refresh_elevation_from_report(self, report) -> None:
        results = list(getattr(report, "results", []) or [])
        # core 新字段 requires_elevation 才是真正被权限拦下的规则；
        # 旧引擎没有该字段时才退回 requires_admin 等启发式，避免把
        # WinSxS/休眠/还原点等“仅报告/不扫描”的规则算进横幅。
        has_field = any(hasattr(res, "requires_elevation") for res in results)
        self._elevation_is_fallback = not has_field
        ids: set[str] = set()
        pending = 0
        for res in results:
            if has_field:
                needs = bool(getattr(res, "requires_elevation", False))
            else:
                needs = self._result_needs_elevation(res)
            if not needs:
                continue
            rule_id = getattr(getattr(res, "rule", None), "id", "")
            if rule_id:
                ids.add(str(rule_id))
                # 默认不勾选需要管理员权限的规则
                self.checked[str(rule_id)] = False
            pending += self._elevation_pending_of(res)
        self._elevation_rule_ids = ids
        self._elevation_pending = pending
        self._refresh_elevation_banner()

    def _refresh_elevation_banner(self) -> None:
        banner = getattr(self, "_elevation_banner", None)
        if banner is None:
            return
        count = len(self._elevation_rule_ids)
        if self.is_admin or count <= 0:
            try:
                banner.grid_remove()
            except Exception:
                pass
            return
        names = [
            str(getattr(self._rule_map.get(rule_id), "name", ""))
            for rule_id in self._elevation_rule_ids
            if self._rule_map.get(rule_id) is not None
        ]
        names = [name for name in names if name][:3]
        pending = self._elevation_pending
        if getattr(self, "_elevation_is_fallback", False):
            text = f"{count} 条规则声明需要管理员权限（旧引擎兜底）"
            if pending:
                text += f"（约 {pending:,} 项待扫描）"
        else:
            text = f"{count} 条系统规则需要管理员权限"
            if names:
                text += f"（{'、'.join(names)}等）"
            if pending:
                text += f"（约 {pending:,} 项待扫描）"
        self._elevation_banner_text.configure(text=text)
        banner.grid()

    def _rows_for_render(self) -> list[widgets.TreeRow]:
        groups: dict[str, list] = {}
        for rule in self.rules:
            groups.setdefault(getattr(rule, "category", "其他"), []).append(rule)

        def result_of(rule):
            return self.results_by_rule.get(getattr(rule, "id", ""))

        def size_of(rule) -> int:
            res = result_of(rule)
            return int(getattr(res, "total_bytes", 0)) if res is not None else 0

        def count_of(rule) -> int:
            res = result_of(rule)
            return int(getattr(res, "file_count", 0)) if res is not None else 0

        def cat_metric(cat: str) -> Any:
            rules = groups[cat]
            if self.sort_key == "name":
                return category_index(cat)
            if self.sort_key == "risk":
                return max(
                    (RISK_ORDER.get(getattr(r, "risk", "safe"), 0) for r in rules),
                    default=0,
                )
            if self.sort_key == "count":
                return sum(count_of(r) for r in rules if self._rule_is_cleanable(r))
            return sum(size_of(r) for r in rules if self._rule_is_cleanable(r))

        categories = sorted(groups.keys(), key=category_index)
        if self.sort_key in ("size", "count", "risk"):
            categories.sort(key=cat_metric, reverse=self.sort_rev)

        def rule_metric(rule) -> Any:
            if self.sort_key == "name":
                return getattr(rule, "name", "")
            if self.sort_key == "size":
                return size_of(rule)
            if self.sort_key == "count":
                return count_of(rule)
            if self.sort_key == "risk":
                return RISK_ORDER.get(getattr(rule, "risk", "safe"), 0)
            return 0

        rows: list[widgets.TreeRow] = []
        for cat in categories:
            cat_rules = groups[cat]
            if self.sort_key:
                cat_rules = sorted(cat_rules, key=rule_metric, reverse=self.sort_rev)
            cleanable_rules = [r for r in cat_rules if self._rule_is_cleanable(r)]
            operation_rules = [
                r for r in cat_rules
                if getattr(r, "action", "delete") == "empty_recycle_bin"
            ]
            report_rules = [
                r for r in cat_rules if getattr(r, "action", "delete") == "report"
            ]
            actionable_rules = cleanable_rules + operation_rules
            cat_size = sum(size_of(r) for r in cleanable_rules)
            cat_count = sum(count_of(r) for r in cleanable_rules)
            if self.report is None:
                size_text = "—"
                count_text = "—"
            else:
                size_text = (
                    self.engine.human_size(cat_size) if cleanable_rules else "—"
                )
                count_text = f"{cat_count:,}" if cleanable_rules else "—"
            if actionable_rules:
                checked_count = sum(
                    1 for r in actionable_rules
                    if self.checked.get(getattr(r, "id", ""))
                )
                if checked_count == 0:
                    glyph = CHECK_OFF
                elif checked_count == len(actionable_rules):
                    glyph = CHECK_ON
                else:
                    glyph = CHECK_PARTIAL
            else:
                glyph = CHECK_NA
            desc = f"{len(cat_rules)} 条规则"
            if operation_rules:
                desc += f" · 不可恢复操作 {len(operation_rules)} 条"
            if report_rules:
                desc += f" · 仅报告 {len(report_rules)} 条"
            locked_cat = [
                r for r in cleanable_rules if self._rule_needs_elevation(r)
            ]
            if locked_cat:
                desc += f" · 需管理员 {len(locked_cat)} 条"
            rows.append(widgets.TreeRow(
                iid=f"cat::{cat}",
                kind="cat",
                values=(glyph, cat, size_text, count_text, "", desc),
                tags=("cat",),
            ))

            for index, rule in enumerate(cat_rules):
                rule_id = getattr(rule, "id", "")
                res = result_of(rule)
                action = getattr(rule, "action", "delete")
                risk = getattr(rule, "risk", "safe")
                needs_admin = self._rule_needs_elevation(rule)
                if action in ("delete", "empty_recycle_bin"):
                    glyph = CHECK_ON if self.checked.get(rule_id) else CHECK_OFF
                else:
                    glyph = CHECK_NA
                if needs_admin:
                    size_text = "需管理员"
                    count_text = "—"
                elif action == "empty_recycle_bin":
                    size_text = self.engine.human_size(self.recycle_bin_bytes)
                    count_text = f"{self.recycle_bin_items:,} 项"
                elif action == "delete" and res is not None:
                    size_text = self.engine.human_size(int(getattr(res, "total_bytes", 0)))
                    count_text = f"{int(getattr(res, 'file_count', 0)):,}"
                else:
                    size_text = "—"
                    count_text = "—"
                if needs_admin:
                    risk_text = "需管理员"
                elif action in ("delete", "empty_recycle_bin"):
                    risk_text = RISK_LABEL.get(risk, risk)
                else:
                    risk_text = "仅报告"
                desc = str(getattr(rule, "description", "") or "")
                if getattr(rule, "requires_admin", False):
                    desc += " · 需管理员"
                if needs_admin:
                    desc += " · 需要管理员权限"
                if action == "empty_recycle_bin":
                    desc = "永久清空回收站（不可恢复） · " + desc
                elif action != "delete":
                    desc += " · 仅报告"
                if res is not None and getattr(res, "error", None) and not needs_admin:
                    desc += " · 扫描出错"
                desc_limit = (
                    self.tree.desc_content_width()
                    if hasattr(self, "tree") else theme.sp(400)
                )
                desc = self._fit_text(desc, desc_limit)

                tags: list[str] = ["row_alt" if index % 2 else "row"]
                if needs_admin:
                    tags.append("admin_lock")
                elif action in ("delete", "empty_recycle_bin"):
                    tags.append(
                        "risk_safe" if risk == "safe"
                        else "risk_caution" if risk == "caution"
                        else "risk_advanced"
                    )
                else:
                    tags.append("report")
                rows.append(widgets.TreeRow(
                    iid=f"rule::{rule_id}",
                    kind="rule",
                    values=(glyph, str(getattr(rule, "name", rule_id)), size_text,
                            count_text, risk_text, desc),
                    tags=tuple(tags),
                ))
        return rows

    def _fit_text(self, text: str, max_px: int) -> str:
        """按像素宽度截断文字并加省略号，避免说明列被生硬裁掉。"""
        try:
            font = self.fonts.body
            if font.measure(text) <= max_px:
                return text
            while text and font.measure(text + "…") > max_px:
                text = text[:-1]
            return text + "…"
        except Exception:
            return text

    def _on_tree_columns_changed(self) -> None:
        """表格列宽变化后重绘，让说明列按新宽度重新截断。"""
        if not hasattr(self, "tree"):
            return
        try:
            self.root.after_idle(self._render_tree)
        except Exception:
            pass

    def _render_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.tree.set_sort_indicator(self.sort_key, self.sort_rev)
        self.tree.set_rows(self._rows_for_render())
        total = len(self.rules)
        actionable = sum(1 for r in self.rules if self._rule_is_actionable(r))
        if total:
            self.rules_pill.set_text(f"{actionable} 条可清理规则 · 共 {total} 条")
        else:
            self.rules_pill.set_text("无可用规则")
        self._restore_tree_selection()
        self._update_selection_summary()
        self._refresh_elevation_banner()

    def _restore_tree_selection(self) -> None:
        if not hasattr(self, "tree"):
            return
        if self.selected_rule_id:
            iid = f"rule::{self.selected_rule_id}"
            if self.tree.select_iid(iid):
                self._on_tree_select(iid)
                return
            self.selected_rule_id = None
        if self.selected_category:
            iid = f"cat::{self.selected_category}"
            if self.tree.select_iid(iid):
                self._on_tree_select(iid)
                return
        self.selected_category = None
        if hasattr(self, "detail_title"):
            self._set_detail_placeholder()

    # ------------------------------------------------------------------
    # 勾选与排序
    # ------------------------------------------------------------------
    def _on_toggle_rule(self, rule_id: str) -> None:
        rule = self._rule_map.get(rule_id)
        if rule is None:
            return
        if not self._rule_is_actionable(rule):
            self._set_status("「大文件 / 系统维护」中的仅报告项目不会删除，只提供命令建议。")
            return
        self.checked[rule_id] = not self.checked.get(rule_id, False)
        if self.checked[rule_id] and self._rule_needs_elevation(rule):
            self._set_status(
                "该规则需要管理员权限：已勾选但不会执行，清理按钮已禁用；"
                "请点顶部横幅或「以管理员身份重启（解锁系统清理）」。"
            )
        self._render_tree()

    def _on_toggle_category(self, category: str) -> None:
        rules = [
            r for r in self.rules
            if getattr(r, "category", "") == category and self._rule_is_actionable(r)
            and not self._rule_needs_elevation(r)
        ]
        if not rules:
            locked = [
                r for r in self.rules
                if getattr(r, "category", "") == category
                and self._rule_is_actionable(r)
                and self._rule_needs_elevation(r)
            ]
            if locked:
                self._set_status(
                    "该分类的系统规则需要管理员权限，请点顶部横幅先以管理员身份重启。"
                )
            else:
                self._set_status("该分类仅作报告，没有可清理项目。")
            return
        target = not all(
            self.checked.get(getattr(r, "id", ""), False) for r in rules
        )
        for rule in rules:
            self.checked[getattr(rule, "id", "")] = target
        self._render_tree()

    def _check_all(self) -> None:
        for rule in self.rules:
            if self._rule_is_actionable(rule) and not self._rule_needs_elevation(rule):
                self.checked[getattr(rule, "id", "")] = True
        self._render_tree()

    def _uncheck_all(self) -> None:
        for rule in self.rules:
            self.checked[getattr(rule, "id", "")] = False
        self._render_tree()

    def _check_safe_only(self) -> None:
        for rule in self.rules:
            if not self._rule_is_actionable(rule):
                continue
            if self._rule_needs_elevation(rule):
                continue
            self.checked[getattr(rule, "id", "")] = (
                getattr(rule, "risk", "safe") == "safe"
            )
        self._render_tree()

    def _on_sort(self, column: str) -> None:
        if column not in ("name", "size", "count", "risk"):
            return
        if self.sort_key == column:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_key = column
            self.sort_rev = column in ("size", "count")
        self._render_tree()

    # ------------------------------------------------------------------
    # 状态与数据刷新
    # ------------------------------------------------------------------
    def _set_status(self, text: str, detail: str = "") -> None:
        self._last_status = text
        self._last_status_detail = detail
        if hasattr(self, "status_label"):
            try:
                self.status_label.configure(text=text)
            except Exception:
                pass

    def _selection_stats(self) -> dict[str, Any]:
        """当前勾选项目的统计：删除字节、文件数、回收站操作。"""
        stats: dict[str, Any] = {
            "rules": [],
            "delete_rules": [],
            "empty_rules": [],
            "delete_bytes": 0,
            "file_count": 0,
        }
        for rule in self.rules:
            if not self._rule_is_actionable(rule):
                continue
            rule_id = getattr(rule, "id", "")
            if not self.checked.get(rule_id):
                continue
            if rule_id not in self.results_by_rule:
                continue
            stats["rules"].append(rule)
            if getattr(rule, "action", "delete") == "empty_recycle_bin":
                stats["empty_rules"].append(rule)
                continue
            res = self.results_by_rule.get(rule_id)
            stats["delete_rules"].append(rule)
            stats["delete_bytes"] += int(getattr(res, "total_bytes", 0))
            stats["file_count"] += int(getattr(res, "file_count", 0))
        return stats

    def _selected_rules(self) -> list:
        return list(self._selection_stats()["rules"])

    def _selected_bytes(self) -> tuple[int, int]:
        stats = self._selection_stats()
        return int(stats["delete_bytes"]), int(stats["file_count"])

    def _delete_mode(self) -> str:
        return "delete" if str(self.settings.get("delete_mode", "recycle")) == "delete" else "recycle"

    def _update_selection_summary(self) -> None:
        if not hasattr(self, "selection_label"):
            return
        if self.report is None:
            self.selection_label.configure(
                text="尚未扫描" if not self.scanning else "正在扫描…",
                fg=self.palette.text_tertiary,
            )
        else:
            stats = self._selection_stats()
            selected = stats["rules"]
            if selected:
                recycled = self._delete_mode() == "recycle"
                parts = [f"已选 {len(selected)} 条规则"]
                if stats["delete_bytes"] > 0:
                    parts.append(f"{int(stats['file_count']):,} 个文件")
                    parts.append(
                        ("预计移入回收站 " if recycled else "预计释放 ")
                        + self.engine.human_size(int(stats["delete_bytes"]))
                    )
                if stats["empty_rules"]:
                    if self.recycle_bin_bytes:
                        parts.append(
                            "另将永久清空回收站（约 "
                            + self.engine.human_size(self.recycle_bin_bytes) + "）"
                        )
                    else:
                        parts.append("另将永久清空回收站（当前为空）")
                blocked = [
                    r for r in stats["rules"] if self._rule_needs_elevation(r)
                ]
                if blocked:
                    parts.append(
                        f"含 {len(blocked)} 条需管理员权限规则（清理已禁用，请先提权）"
                    )
                self.selection_label.configure(
                    text=" · ".join(parts),
                    fg=self.palette.caution_text if blocked else self.palette.accent,
                )
            else:
                self.selection_label.configure(
                    text="未勾选任何项目", fg=self.palette.text_tertiary
                )
        can_clean = False
        if not self.scanning and not self.cleaning and self.report is not None:
            stats = self._selection_stats()
            blocked = [
                r for r in stats["rules"] if self._rule_needs_elevation(r)
            ]
            executable = [
                r for r in stats["delete_rules"]
                if self.is_admin or not getattr(r, "requires_admin", False)
            ]
            can_clean = (
                bool(executable) or bool(stats["empty_rules"])
            ) and not blocked
            if can_clean and not executable and not stats["empty_rules"]:
                can_clean = False
        if hasattr(self, "clean_btn"):
            self.clean_btn.set_enabled(can_clean)

    def _update_disk_card(self) -> None:
        if not hasattr(self, "ring"):
            return
        try:
            free, total = self.engine.drive_usage("C:")
        except Exception:
            free, total = 0, 0
        used = max(0, total - free)
        ratio = (used / total) if total else 0.0
        self.ring.set(ratio, "已用")
        self.free_label.configure(text=self.engine.human_size(free))
        self.used_label.configure(
            text=f"总容量 {self.engine.human_size(total)} · "
                 f"已用 {self.engine.human_size(used)}"
        )
        if self.report is not None:
            cleanable = int(getattr(self.report, "cleanable_bytes", 0))
            self.cleanable_label.configure(
                text=self.engine.human_size(cleanable), fg=self.palette.accent
            )
        else:
            self.cleanable_label.configure(text="—", fg=self.palette.text_tertiary)
        if self._last_scan_at:
            self.last_scan_label.configure(text=f"上次扫描 {self._last_scan_at}")
        else:
            self.last_scan_label.configure(text="尚未扫描")

    def _restore_status_after_rebuild(self) -> None:
        if self.scanning:
            self._set_status("正在扫描…")
        elif self.cleaning:
            self._set_status("正在清理…")
        elif self._last_status:
            self._set_status(self._last_status, self._last_status_detail)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _on_scan_button(self) -> None:
        if self.scanning:
            self._cancel_scan()
        else:
            self.start_scan()

    def _cancel_scan(self) -> None:
        if not self.scanning:
            return
        self._cancel.set()
        self._set_status("正在取消扫描…")
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("正在取消…")
            self.scan_btn.set_enabled(False)

    def start_scan(self, auto: bool = False) -> None:
        if self._quitting or self.scanning or self.cleaning:
            return
        if not self.rules:
            try:
                self.rules = list(self.engine.load_rules())
                self._rule_map = {
                    getattr(r, "id", str(i)): r for i, r in enumerate(self.rules)
                }
                self._init_checked()
                self._render_tree()
            except Exception:
                log.exception("重新加载规则失败")
            if not self.rules:
                self._open_error(
                    "无法扫描", "没有可用的清理规则。",
                    "请检查规则目录，或重新安装程序。",
                )
                return

        self.scanning = True
        self.cleaning = False
        self._cancel = threading.Event()
        self._scan_started = time.time()
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("取消扫描")
            self.scan_btn.set_enabled(True)
        if hasattr(self, "clean_btn"):
            self.clean_btn.set_enabled(False)
        if hasattr(self, "progress"):
            self.progress.set(0.0)
        self._set_status("正在扫描清理规则…")
        self._update_selection_summary()

        thread = threading.Thread(
            target=self._scan_worker, args=(list(self.rules),),
            name="qingfeng-scan", daemon=True,
        )
        thread.start()

    def _scan_worker(self, rules: list) -> None:
        try:
            def progress(name: str, done: int, total: int) -> None:
                self.events.put(("scan_progress", name, done, total))

            report = self.engine.scan(
                rules, progress=progress, cancel=self._cancel.is_set
            )
            self.events.put(("scan_done", report))
        except Exception:
            self.events.put(("scan_error", traceback.format_exc()))

    def _handle_scan_done(self, report) -> None:
        self.scanning = False
        self.report = report
        self.results_by_rule = {
            getattr(res.rule, "id", ""): res for res in getattr(report, "results", [])
        }
        self._last_scan_at = time.strftime("%H:%M")
        duration = float(getattr(report, "duration", 0.0))
        cleanable = int(getattr(report, "cleanable_bytes", 0))
        cancelled = bool(getattr(report, "cancelled", False))
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("重新扫描")
            self.scan_btn.set_enabled(True)
        if hasattr(self, "progress"):
            self.progress.set(1.0)
        self._refresh_recycle_bin_usage()
        self._update_disk_card()
        self._refresh_elevation_from_report(report)
        self._render_tree()
        if cancelled:
            self._set_status(
                f"扫描已取消 · 已发现可清理 {self.engine.human_size(cleanable)}"
            )
        elif self._elevation_rule_ids:
            label = (
                "条规则声明需管理员权限"
                if getattr(self, "_elevation_is_fallback", False)
                else "条系统规则需管理员权限"
            )
            self._set_status(
                f"扫描完成 · 可清理 {self.engine.human_size(cleanable)} · "
                f"{len(self._elevation_rule_ids)} {label}"
            )
        else:
            self._set_status(
                f"扫描完成 · {len(self.rules)} 条规则 · 可清理 "
                f"{self.engine.human_size(cleanable)} · 用时 {duration:.1f} 秒"
            )
        self._update_selection_summary()
        self._reset_progress_later()
        # 首次扫描完成后自动选中第一条"无扫描错误"的规则，详情面板直接可用
        if self.selected_rule_id is None and self.selected_category is None:
            children = self.tree.tree.get_children("")
            rule_iids = [iid for iid in children if str(iid).startswith("rule::")]
            first_rule = None
            for iid in rule_iids:
                res = self.results_by_rule.get(str(iid).partition("::")[2])
                if res is not None and not getattr(res, "error", None):
                    first_rule = iid
                    break
            first_rule = first_rule or (rule_iids[0] if rule_iids else None)
            if first_rule:
                self.tree.select_iid(first_rule)

    def _reset_progress_later(self, delay: int = 1200) -> None:
        """任务完成后进度条短暂保持满格，再回到空闲状态。"""

        def _reset() -> None:
            if not self.scanning and not self.cleaning and hasattr(self, "progress"):
                self.progress.set(0.0)

        try:
            self.root.after(delay, _reset)
        except Exception:
            pass

    def _handle_scan_error(self, detail: str) -> None:
        self.scanning = False
        self._cancel.clear()
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("开始扫描")
            self.scan_btn.set_enabled(True)
        if hasattr(self, "progress"):
            self.progress.set(0.0)
        self._set_status("扫描失败，请查看错误详情")
        self._update_selection_summary()
        self._open_error(
            "扫描失败",
            "扫描过程中出现问题，已停止本次扫描。",
            detail.strip().splitlines()[-1] if detail.strip() else "",
        )

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def clean_selected(self) -> None:
        if self._quitting or self.scanning or self.cleaning:
            return
        if self.report is None:
            self._open_info("尚未扫描", "请先点击「开始扫描」获取可清理项目。")
            return
        stats = self._selection_stats()
        if not stats["rules"]:
            self._open_info("未选择项目", "请先勾选要清理的项目。")
            return
        is_admin = self.is_admin or self.engine.is_admin()
        skipped_admin = 0
        delete_results = []
        delete_rules = []
        for rule in stats["delete_rules"]:
            if not is_admin and (
                getattr(rule, "requires_admin", False)
                or self._rule_needs_elevation(rule)
            ):
                skipped_admin += 1
                continue
            rule_id = getattr(rule, "id", "")
            if rule_id in self.results_by_rule:
                delete_results.append(self.results_by_rule[rule_id])
                delete_rules.append(rule)
        empty_rules = list(stats["empty_rules"])
        if not delete_results and not empty_rules:
            self._open_info(
                "需要管理员权限",
                "所选项目都需要管理员权限，请点击顶部「以管理员身份重启」后再清理。",
            )
            return

        total_bytes = sum(
            int(getattr(res, "total_bytes", 0)) for res in delete_results
        )
        file_count = sum(
            int(getattr(res, "file_count", 0)) for res in delete_results
        )
        advanced_count = sum(
            1 for r in delete_rules if getattr(r, "risk", "safe") == "advanced"
        )
        recycled = self._delete_mode() == "recycle"

        # 勾选 recycle-bin 规则时，先查真实占用用于确认框
        if empty_rules:
            self._refresh_recycle_bin_usage()

        running_programs: list[str] = []
        try:
            running_programs = self.engine.running_processes()
        except Exception:
            log.exception("检测运行中的程序失败")

        confirmed = self._with_dialog(
            lambda: dialogs.confirm_clean(
                self.root,
                palette=self.palette,
                fonts=self.fonts,
                total_bytes=total_bytes,
                file_count=file_count,
                rule_count=len(delete_results) + len(empty_rules),
                recycled=recycled,
                advanced_count=advanced_count,
                admin_count=skipped_admin,
                is_admin=is_admin,
                empties_recycle_bin=bool(empty_rules),
                recycle_bin_bytes=self.recycle_bin_bytes,
                running_programs=running_programs,
            )
        )
        if not confirmed:
            self._set_status("已取消清理")
            return
        self._start_clean(
            delete_results, empty_rules, recycled, skipped_admin, running_programs
        )

    def _start_clean(
        self,
        delete_results: list,
        empty_rules: list,
        recycled: bool,
        skipped_admin: int,
        running_programs: list[str] | None = None,
    ) -> None:
        self.cleaning = True
        self.scanning = False
        self._cancel = threading.Event()
        for attr in ("scan_btn", "clean_btn", "recycle_btn", "settings_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.set_enabled(False)
        if hasattr(self, "progress"):
            self.progress.set(0.0)
        self._set_status("正在清理，请稍候…")
        mode = "recycle" if recycled else "delete"
        recycle_size = self.recycle_bin_bytes if empty_rules else 0
        thread = threading.Thread(
            target=self._clean_worker,
            args=(delete_results, empty_rules, mode, recycled, skipped_admin,
                  recycle_size),
            name="qingfeng-clean", daemon=True,
        )
        thread.start()

    def _clean_worker(
        self,
        delete_results: list,
        empty_rules: list,
        mode: str,
        recycled: bool,
        skipped_admin: int,
        recycle_size: int,
    ) -> None:
        try:
            def progress(name: str, done: int, total: int) -> None:
                self.events.put(("clean_progress", name, done, total))

            result = None
            emptied = False
            empty_error = ""
            if delete_results:
                result = self.engine.clean(
                    delete_results, mode=mode, progress=progress,
                    cancel=self._cancel.is_set,
                )
            if empty_rules and not self._cancel.is_set():
                for index, rule in enumerate(empty_rules, start=1):
                    progress(getattr(rule, "name", "清空回收站"), index, len(empty_rules))
                emptied = bool(self.engine.empty_recycle_bin())
                if not emptied:
                    empty_error = "清空回收站失败（可能已为空、被占用或权限不足）"
            if result is None:
                result = SimpleNamespace(
                    deleted_files=0, deleted_dirs=0, freed_bytes=0,
                    recycled=recycled, failed=[], per_rule={}, duration=0.0,
                )
            if empty_error:
                try:
                    result.failed.append(("<回收站>", empty_error))
                except Exception:
                    pass
            self.events.put((
                "clean_done", result, delete_results, empty_rules, recycled,
                skipped_admin, emptied, empty_error, recycle_size,
            ))
        except Exception:
            self.events.put(("clean_error", traceback.format_exc()))

    @staticmethod
    def _is_icon_rule(rule) -> bool:
        rule_id = str(getattr(rule, "id", "")).lower()
        name = str(getattr(rule, "name", ""))
        return (
            "icon" in rule_id or "thumb" in rule_id
            or "图标" in name or "缩略图" in name
        )

    def _summary_open_recycle_bin(self) -> bool:
        ok = self.engine.open_recycle_bin()
        if ok:
            self._set_status("已在资源管理器中打开回收站")
        return ok

    def _summary_restart_explorer(self) -> bool:
        ok = self.engine.restart_explorer()
        if ok:
            self._set_status("正在重启资源管理器，桌面会短暂刷新…")
        return ok

    def _handle_clean_done(
        self,
        result,
        delete_results: list,
        empty_rules: list,
        recycled: bool,
        skipped_admin: int,
        emptied: bool,
        empty_error: str,
        recycle_size: int,
    ) -> None:
        self.cleaning = False
        for res in delete_results:
            try:
                res.items = []
            except Exception:
                pass
            rule_id = getattr(getattr(res, "rule", None), "id", "")
            self.checked[rule_id] = False
        for rule in empty_rules:
            self.checked[getattr(rule, "id", "")] = False
        if hasattr(self, "progress"):
            self.progress.set(1.0)
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("重新扫描")
            self.scan_btn.set_enabled(True)
        if hasattr(self, "recycle_btn"):
            self.recycle_btn.set_enabled(True)
        if hasattr(self, "settings_btn"):
            self.settings_btn.set_enabled(True)
        # 回收站被清空后立即归零，否则重新查询
        if emptied:
            self.recycle_bin_bytes = 0
            self.recycle_bin_items = 0
        else:
            self._refresh_recycle_bin_usage()
        self._update_disk_card()
        self._render_tree()
        freed = int(getattr(result, "freed_bytes", 0))
        failed = list(getattr(result, "failed", []) or [])
        # 权限类失败/跳过：core 的 skipped_admin 可能是 int 或 list
        permission_failed = int(skipped_admin or 0)
        raw_skipped = getattr(result, "skipped_admin", 0) or 0
        if isinstance(raw_skipped, (list, tuple, set)):
            permission_failed += len(raw_skipped)
        else:
            try:
                permission_failed += int(raw_skipped)
            except (TypeError, ValueError):
                pass
        permission_hints = ("拒绝访问", "管理员", "权限", "access is denied",
                            "permission", "elevation", "privilege")
        for entry in failed:
            try:
                err_text = str(entry[1])
            except (TypeError, IndexError):
                err_text = str(entry)
            if any(hint.lower() in err_text.lower() for hint in permission_hints):
                permission_failed += 1
        icon_cleaned = any(
            self._is_icon_rule(getattr(res, "rule", res)) for res in delete_results
        )
        status_parts = ["清理完成"]
        total_released = freed + (recycle_size if emptied else 0)
        if recycled and not emptied and freed > 0:
            status_parts.append(f"已移入回收站 {self.engine.human_size(freed)}")
            status_parts.append("清空回收站后空间才真正释放")
        elif emptied and total_released > 0:
            status_parts.append(f"已释放 {self.engine.human_size(total_released)}")
        elif not recycled and freed > 0:
            status_parts.append(f"已释放 {self.engine.human_size(freed)}")
        if emptied:
            status_parts.append("回收站已永久清空")
        elif empty_error:
            status_parts.append("清空回收站失败")
        if failed:
            status_parts.append(f"{len(failed)} 项失败（可能被占用）")
        if permission_failed:
            status_parts.append(f"{permission_failed} 项因权限跳过/失败")
        self._set_status(" · ".join(status_parts))
        self._update_selection_summary()
        self._reset_progress_later()
        self._with_dialog(lambda: dialogs.show_summary(
            self.root,
            palette=self.palette,
            fonts=self.fonts,
            result=result,
            recycled=recycled,
            skipped_count=skipped_admin,
            emptied_recycle_bin=emptied,
            recycle_bin_error=empty_error,
            recycled_bin_bytes=recycle_size,
            icon_cache_cleaned=icon_cleaned,
            permission_failed_count=permission_failed,
            on_open_recycle_bin=self._summary_open_recycle_bin,
            on_restart_explorer=self._summary_restart_explorer,
            on_restart_admin=self._restart_as_admin,
        ))

    def _handle_clean_error(self, detail: str) -> None:
        self.cleaning = False
        for attr in ("scan_btn", "recycle_btn", "settings_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.set_enabled(True)
        if hasattr(self, "scan_btn"):
            self.scan_btn.set_text("重新扫描")
        if hasattr(self, "progress"):
            self.progress.set(0.0)
        self._set_status("清理失败，请查看错误详情")
        self._update_selection_summary()
        self._open_error(
            "清理失败",
            "清理过程中出现问题，部分文件可能尚未删除。",
            detail.strip().splitlines()[-1] if detail.strip() else "",
        )

    # ------------------------------------------------------------------
    # 事件循环（主线程）
    # ------------------------------------------------------------------
    def _start_poller(self) -> None:
        self._poll_events()

    def _poll_events(self) -> None:
        if self._quitting:
            return
        self._poll_after_id = self.root.after(80, self._poll_events)
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        except Exception:
            log.exception("处理界面事件失败")

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "scan_progress":
            _kind, name, done, total = event
            if total:
                if hasattr(self, "progress"):
                    self.progress.set(done / max(1, total))
                self._set_status(f"正在扫描 · {name}（{done}/{total}）")
        elif kind == "scan_done":
            self._handle_scan_done(event[1])
        elif kind == "scan_error":
            self._handle_scan_error(event[1])
        elif kind == "clean_progress":
            _kind, name, done, total = event
            if total:
                if hasattr(self, "progress"):
                    self.progress.set(done / max(1, total))
                self._set_status(f"正在清理 · {name}（{done}/{total}）")
        elif kind == "clean_done":
            (_kind, result, delete_results, empty_rules, recycled,
             skipped, emptied, empty_error, recycle_size) = event
            self._handle_clean_done(
                result, delete_results, empty_rules, recycled,
                skipped, emptied, empty_error, recycle_size,
            )
        elif kind == "clean_error":
            self._handle_clean_error(event[1])
        elif kind == "tray_show":
            self.show()
        elif kind == "tray_scan":
            self.show()
            if not self.scanning:
                self.start_scan()
        elif kind == "tray_quit":
            self.quit_app()

    # ------------------------------------------------------------------
    # 托盘
    # ------------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            from .tray import TrayIcon

            self.tray = TrayIcon(
                on_show=lambda: self.events.put(("tray_show",)),
                on_scan=lambda: self.events.put(("tray_scan",)),
                on_quit=lambda: self.events.put(("tray_quit",)),
                icon_path=ICON_PATH,
            )
            self.tray.start()
            log.info("系统托盘已启用")
        except Exception as exc:
            self.tray = None
            log.exception("系统托盘启动失败")
            self._set_status(f"就绪 · {self.engine.status_text}（托盘不可用：{exc}）")

    def _on_close(self) -> None:
        minimize = bool(self.settings.get("minimize_to_tray", True))
        if minimize and self.tray is not None and getattr(self.tray, "available", False):
            self.hide_to_tray()
        else:
            self.quit_app()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        log.info("主窗口已最小化到托盘")

    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        if self.report is None and not self.scanning and not self.cleaning:
            self.start_scan(auto=True)

    def quit_app(self, code: int = 0) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.exit_code = code
        self._cancel.set()
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                log.exception("停止托盘失败")
            self.tray = None
        try:
            self.root.quit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 按钮行为
    # ------------------------------------------------------------------
    def open_recycle_bin(self) -> None:
        if self.engine.open_recycle_bin():
            self._set_status("已在资源管理器中打开回收站")
        else:
            self._set_status("打开回收站失败")
            self._open_error("无法打开回收站", "系统没有正确响应打开回收站的请求。")

    def open_settings_dialog(self) -> None:
        if self._dialog_depth:
            return
        result = self._with_dialog(lambda: dialogs.open_settings(
            self.root,
            palette=self.palette,
            fonts=self.fonts,
            settings=self.settings,
            engine=self.engine,
        ))
        if not result:
            return
        old_theme = str(self.settings.get("theme", "auto"))
        self.settings.update(result)
        if not save_settings(self.settings):
            log.info("设置未写入磁盘，仅本次运行生效")
        if str(self.settings.get("theme", "auto")) != old_theme:
            self._rebuild_ui()
        else:
            self._set_status("设置已保存")
            self._update_selection_summary()

    def _toggle_theme(self) -> None:
        current = theme.resolve_mode(str(self.settings.get("theme", "auto")))
        self.settings["theme"] = "light" if current == "dark" else "dark"
        save_settings(self.settings)
        self._rebuild_ui()

    def _app_paths(self) -> tuple[Path, str]:
        """返回 (启动路径, 用于命令行展示的命令)。"""
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable)
            return exe, f'"{exe}" --page {self._active_page}'
        main_py = Path(__file__).resolve().parents[2] / "main.py"
        return main_py, f'"{sys.executable}" "{main_py}" --page {self._active_page}'

    def _restart_as_admin(self) -> bool:
        page = getattr(self, "_active_page", "clean")
        ok = False
        try:
            ok = bool(self.engine.restart_as_admin(["--page", page]))
        except Exception:
            log.exception("提权重启失败")
            ok = False
        if ok:
            self.quit_app(0)
            return True
        target, command = self._app_paths()
        if getattr(sys, "frozen", False):
            step1 = f"在资源管理器里右键 {target} →「以管理员身份运行」。"
            admin_cmd = f'Start-Process -FilePath "{target}" -Verb RunAs'
        else:
            step1 = (
                "源码运行：在资源管理器里右键 "
                f"{target} →「以管理员身份运行」（或先以管理员身份打开终端再运行）。"
            )
            admin_cmd = command
        detail = (
            "可操作步骤：\n"
            f"1. {step1}\n"
            "2. 或：开始菜单搜索 PowerShell → 右键「以管理员身份运行」，粘贴：\n"
            f"   {admin_cmd}\n"
            "3. 在弹出的 UAC 窗口点「是」。\n\n"
            f"程序路径：{target}"
        )
        self._open_error(
            "无法以管理员身份重启",
            "提权请求没有完成（常见原因：UAC 被取消，或系统策略拒绝了提权）。"
            "你仍可手动按下面步骤以管理员身份运行。",
            detail,
        )
        return False

    # ------------------------------------------------------------------
    # 弹窗封装 / 异常处理
    # ------------------------------------------------------------------
    def _with_dialog(self, opener):
        """打开模态弹窗（带重入保护）。"""
        if self._dialog_depth:
            return None
        self._dialog_depth += 1
        try:
            return opener()
        except Exception:
            log.exception("弹窗显示失败")
            return None
        finally:
            self._dialog_depth -= 1
            try:
                self.root.lift()
            except Exception:
                pass

    def _open_error(self, title: str, message: str, detail: str = "") -> None:
        logging.error("%s: %s %s", title, message, detail)
        if self._dialog_depth:
            self._set_status(f"{title}：{message}")
            return
        self._with_dialog(lambda: dialogs.show_error(
            self.root, palette=self.palette, fonts=self.fonts,
            title=title, message=message, detail=detail,
        ))

    def _open_info(self, title: str, message: str) -> None:
        if self._dialog_depth:
            self._set_status(f"{title}：{message}")
            return
        self._with_dialog(lambda: dialogs.show_info(
            self.root, palette=self.palette, fonts=self.fonts,
            title=title, message=message,
        ))

    def _report_exception(self, exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("未捕获的界面异常：\n%s", detail)
        self._set_status(f"内部错误：{exc_value}")
        if self._dialog_depth:
            return
        self.root.after(0, lambda: self._open_error(
            "内部错误",
            "界面遇到一个未处理的问题，但程序仍在运行。",
            f"{exc_type.__name__}: {exc_value}",
        ))

    # ------------------------------------------------------------------
    # 主题重建
    # ------------------------------------------------------------------
    def _rebuild_ui(self) -> None:
        was_scanning = self.scanning
        was_cleaning = self.cleaning
        prev_status = self._last_status
        prev_detail = self._last_status_detail
        prev_page = getattr(self, "_active_page", "clean")
        for child in list(self.root.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self.space_page = None
        self._resolve_palette()
        self.compact = self._compute_compact()
        self._init_metrics()
        self.fonts = theme.init_fonts(self.root)
        theme.configure_defaults(self.root, self.palette, self.fonts)
        self.style = theme.apply_ttk_style(
            self.root, self.palette, self.fonts, compact=self.compact
        )
        self.root.configure(bg=self.palette.window)
        self._build_ui()
        self._update_disk_card()
        self._render_tree()
        self.scanning = was_scanning
        self.cleaning = was_cleaning
        if was_scanning:
            self.scan_btn.set_text("取消扫描")
            self.scan_btn.set_enabled(True)
            self._set_status("正在扫描…")
        elif was_cleaning:
            self.scan_btn.set_enabled(False)
            self._set_status("正在清理…")
        else:
            if self.report is not None:
                self.scan_btn.set_text("重新扫描")
            if prev_status:
                self._set_status(prev_status, prev_detail)
            else:
                self._restore_status_after_rebuild()
        self._update_selection_summary()
        if prev_page == "space":
            try:
                self._switch_page("space")
            except Exception:
                log.exception("恢复空间透视页失败")
        self.root.update_idletasks()
        theme.set_titlebar_dark(self.root, self.palette.mode == "dark")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()

    def shutdown(self) -> None:
        self._quitting = True
        self._cancel.set()
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                log.exception("关闭托盘失败")
            self.tray = None
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# launch：main.py 的入口契约
# ---------------------------------------------------------------------------


def launch(*, with_tray: bool = True, start_hidden: bool = False,
           page: str = "clean") -> int:
    """启动 GUI 并返回进程退出码。

    :param page: 启动页，``"clean"``（默认）或 ``"space"``（空间透视）。
        ``--no-tray --page space`` 组合同样有效；空间页无缓存索引时会自动后台构建。
    """
    theme.setup_dpi()
    root = tk.Tk()
    root.withdraw()
    window: MainWindow | None = None
    try:
        window = MainWindow(root, with_tray=with_tray, start_hidden=start_hidden)
        requested = str(page or "clean").strip().lower()
        if requested not in ("clean", "space"):
            log.warning("未知启动页 %r，回退到 clean", page)
            requested = "clean"
        if requested == "space":
            window._switch_page("space")
        window.run()
    except Exception:
        log.exception("主窗口启动失败")
        try:
            from tkinter import messagebox

            messagebox.showerror(
                APP_NAME,
                "程序启动失败，请查看控制台日志。\n\n" + traceback.format_exc(limit=2),
                parent=root,
            )
        except Exception:
            pass
        return 1
    finally:
        if window is not None:
            try:
                window.shutdown()
            except Exception:
                log.exception("清理主窗口失败")
        else:
            try:
                root.destroy()
            except Exception:
                pass
    return int(window.exit_code) if window is not None else 1
