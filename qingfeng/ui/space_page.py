"""清风清理 v2 — 空间透视页。

布局（契约 §4.1）：
  人话总结
  磁盘构成条 + 图例
  左：目录树（大小降序）        右：Treemap（当前目录下一层，面包屑/返回）
  大文件决策区（分组 chips + 表格 + 回收站/迁移/打开位置）
  系统占用卡片（仅展示 + 复制命令 + 打开管理员 PowerShell，绝不代执行）
  迁移确认/进度/结果（复制成功后才删源，可取消）

线程约定：索引构建、回收站、迁移均在后台线程，只通过 queue + root.after 回主线程。
"""

from __future__ import annotations

import queue
import os
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, ttk
import tkinter as tk

from . import actions, dialogs, space_mock, theme, widgets
from .space_engine import SpaceEngine
from .treemap import Treemap, TreemapItem

MB = 1024 * 1024
GB = 1024 * MB

# chip key -> (label, set(group keys))
_CHIP_DEFS: list[tuple[str, str, set[str] | None]] = [
    ("all", "全部", None),
    ("installer", "安装包", {"installer"}),
    ("app-version-dup", "旧版本副本", {"app-version-dup"}),
    ("backup", "备份", {"backup"}),
    ("devcache", "开发缓存", {"devcache"}),
    ("media", "视频/压缩包/镜像", {"video", "archive", "vm"}),
    ("large-misc", "其它大文件", {"large-misc"}),
    ("keep", "建议保留", {"keep"}),
]

_ADVICE_LABEL = {
    "safe_delete": "建议清理",
    "review": "你来判断",
    "keep": "建议保留",
    "move": "建议迁移",
}


def _deny_short_label(item, policy) -> str:
    """按授权矩阵的拒绝原因给表格里的短标签；完整原因走 tooltip。"""
    if not policy.deny_reason:
        if not policy.can_delete and policy.can_move:
            return "仅可迁移"
        if policy.can_delete and not policy.can_move:
            return "仅可清理"
        return ""
    # 部分允许（例如 ProgramData 下的安装包只允许回收站删除，不允许迁移）
    if policy.can_delete and not policy.can_move:
        return "仅可清理"
    if policy.can_move and not policy.can_delete:
        return "仅可迁移"
    path = str(getattr(item, "path", "")).lower().replace("/", "\\")
    if "\\appdata\\local\\packages" in path:
        return "系统应用数据·不可操作"
    if ("$recycle.bin" in path or "system volume information" in path
            or "\\recovery" in path or "\\efi" in path or "\\boot" in path):
        return "受保护·不可操作"
    if "\\windows" in path:
        return "系统目录·不可操作"
    if "\\program files" in path or "\\programdata" in path:
        return "程序目录·不可操作"
    if not policy.can_delete and not policy.can_move:
        return "受保护·不可操作"
    return ""

_COMP_SPECS = [
    ("system", "系统", "#5B7FA8"),
    ("users", "用户文件", "#0E9F6E"),
    ("programs", "程序", "#7C3AED"),
    ("hiber", "休眠/交换", "#D97706"),
    ("protected", "受保护/无法统计", "#94A3B8"),
]


class _HoverTip:
    """轻量 tooltip：鼠标悬停时在指针下方显示一条说明（不影响主界面）。"""

    def __init__(self, widget: tk.Misc, text: str, *, palette: theme.Palette,
                 fonts: theme.Fonts, auto: bool = True) -> None:
        self.widget = widget
        self.text = text
        self.palette = palette
        self.fonts = fonts
        self._tip: tk.Toplevel | None = None
        self._label: tk.Label | None = None
        if auto:
            widget.bind("<Enter>", self._show, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event) -> None:
        self.show_at(self.text, event.x_root, event.y_root)

    def show_at(self, text: str, x_root: int, y_root: int) -> None:
        if self._tip is None:
            tip = tk.Toplevel(self.widget.winfo_toplevel())
            tip.overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except Exception:
                pass
            frame = tk.Frame(tip, bg=self.palette.border_strong, bd=1)
            frame.pack(fill="both", expand=True)
            self._label = tk.Label(
                frame, text="", bg=self.palette.surface_alt,
                fg=self.palette.text, font=self.fonts.tiny,
                justify="left", anchor="w", wraplength=theme.sp(300),
                padx=theme.sp(8), pady=theme.sp(5),
            )
            self._label.pack()
            self._tip = tip
        if self._label is not None:
            self._label.configure(text=text)
        self._tip.update_idletasks()
        x = int(x_root) + theme.sp(14)
        y = int(y_root) + theme.sp(18)
        width = self._tip.winfo_reqwidth()
        try:
            screen_w = self.widget.winfo_screenwidth()
            screen_h = self.widget.winfo_screenheight()
            if x + width > screen_w:
                x = max(theme.sp(4), screen_w - width - theme.sp(8))
            if y + self._tip.winfo_reqheight() > screen_h:
                y = max(theme.sp(4), int(y_root) - self._tip.winfo_reqheight() - theme.sp(8))
        except Exception:
            pass
        self._tip.geometry(f"+{x}+{y}")
        self._tip.deiconify()

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
            self._label = None

    hide = _hide


class SpacePage(tk.Frame):
    """空间透视页（独立 Frame，由 MainWindow 切换显示）。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        engine,
        root: tk.Misc,
        compact: bool = False,
        on_restart_as_admin=None,
        on_open_settings=None,
        cleanable_provider=None,
    ) -> None:
        super().__init__(master, bg=palette.window)
        self.palette = palette
        self.fonts = fonts
        self.engine = engine
        self.root = root
        self.compact = compact
        self.on_restart_as_admin = on_restart_as_admin
        self.on_open_settings = on_open_settings
        self.cleanable_provider = cleanable_provider

        self.space = SpaceEngine(engine)
        self.index = None
        self.review_items: list = []
        self.groups: list = []
        self.selected_paths: set[str] = set()
        self.policies: dict[str, object] = {}
        self.chip: str = "all"
        self.current_path: str = "C:\\"
        self.targets: list[dict] = []
        self.selected_target: dict | None = None
        self._custom_target: dict | None = None

        self.events: queue.Queue = queue.Queue()
        self._poll_id: str | None = None
        self._pulse_id: str | None = None
        self._pulse_value = 0.0
        self._indexing = False
        self._moving = False
        self._recycling = False
        self._cancel = threading.Event()
        self._destroyed = False
        self._started = False
        self._summary_text = ""
        self._probe: dict = {}
        self._segment_values: dict[str, int] = {}
        self._target_buttons: list = []
        self._chip_buttons: dict[str, widgets.RoundedButton] = {}
        self._updating_table = False

        self.bind("<Destroy>", self._on_destroy)
        self._build_ui()
        # 主线程事件循环（后台线程只写 queue，绝不碰 tkinter）
        self._poll_events()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(4, weight=2)
        self._build_summary_row()
        self._build_progress_row()
        self._build_composition_card()
        self._build_middle()
        self._build_bigfiles_card()
        self._build_system_card()

    def _build_summary_row(self) -> None:
        pal, fonts = self.palette, self.fonts
        frame = tk.Frame(self, bg=pal.window)
        frame.grid(row=0, column=0, sticky="ew", pady=(0, theme.sp(4)))
        frame.columnconfigure(0, weight=1)
        self.summary_label = tk.Label(
            frame, text="正在准备空间索引…", bg=pal.window, fg=pal.text,
            font=fonts.small, anchor="w", justify="left",
            wraplength=theme.sp(760),
        )
        self.summary_label.grid(row=0, column=0, sticky="w")
        controls = tk.Frame(frame, bg=pal.window)
        controls.grid(row=0, column=1, sticky="e", padx=(theme.sp(10), 0))
        self.summary_controls = controls

        def _summary_resize(event) -> None:
            try:
                available = max(theme.sp(420),
                                event.width - controls.winfo_width() - theme.sp(14))
                self.summary_label.configure(wraplength=available)
            except Exception:
                pass

        frame.bind("<Configure>", _summary_resize)
        self.space_status_label = tk.Label(
            controls, text=self.space.status_text, bg=pal.window,
            fg=pal.text_tertiary, font=fonts.tiny, anchor="e",
        )
        self.space_status_label.pack(side="left", padx=(0, theme.sp(8)))
        self.rescan_btn = widgets.RoundedButton(
            controls, "重新扫描", self._on_rescan_clicked,
            palette=pal, fonts=fonts, kind="secondary", bg=pal.window,
            height=28, padx=12, font=fonts.small,
        )
        self.rescan_btn.pack(side="left")
        self.cancel_index_btn = widgets.RoundedButton(
            controls, "取消", self._cancel_index,
            palette=pal, fonts=fonts, kind="subtle", bg=pal.window,
            height=28, padx=10, font=fonts.small,
        )
        self.cancel_index_btn.configure(width=0)
        self.cancel_index_btn.pack(side="left", padx=(theme.sp(4), 0))
        self.cancel_index_btn.pack_forget()

    def _build_progress_row(self) -> None:
        pal = self.palette
        self.progress_row = tk.Frame(self, bg=pal.window)
        self.progress_row.grid(row=1, column=0, sticky="ew", pady=(0, theme.sp(4)))
        self.progress_row.columnconfigure(0, weight=1)
        self.progress_bar = widgets.ProgressBar(
            self.progress_row, pal, height=5, bg=pal.window
        )
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_text = tk.Label(
            self.progress_row, text="", bg=pal.window, fg=pal.text_tertiary,
            font=self.fonts.tiny, anchor="e",
        )
        self.progress_text.grid(row=0, column=1, sticky="e", padx=(theme.sp(8), 0))
        self.progress_row.grid_remove()

    def _build_composition_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        card = widgets.Card(
            self, pal, height=58, radius=10, padding=(14, 8),
        )
        card.grid(row=2, column=0, sticky="ew", pady=(0, theme.sp(6)))
        body = card.body
        body.columnconfigure(0, weight=1)
        self.comp_canvas = tk.Canvas(
            body, height=theme.sp(12), bg=pal.surface,
            highlightthickness=0, bd=0,
        )
        self.comp_canvas.grid(row=0, column=0, sticky="ew")
        self.comp_canvas.bind("<Configure>", lambda _e: self._draw_composition_bar())
        legend = tk.Frame(body, bg=pal.surface)
        legend.grid(row=1, column=0, sticky="w", pady=(theme.sp(4), 0))
        self._legend_labels: dict[str, tk.Label] = {}
        for key, label, color in _COMP_SPECS:
            lbl = tk.Label(
                legend, text=f"● {label}", bg=pal.surface, fg=color,
                font=fonts.tiny, anchor="w",
            )
            lbl.pack(side="left", padx=(0, theme.sp(10)))
            self._legend_labels[key] = lbl
        self.dedup_note = tk.Label(
            legend, text="已按硬链接去重（资源管理器会重复计算）",
            bg=pal.surface, fg=pal.text_tertiary, font=fonts.tiny, anchor="w",
        )
        self.dedup_note.pack(side="left", padx=(theme.sp(6), 0))
        self._protected_tip = _HoverTip(
            self._legend_labels["protected"],
            "受保护/无法统计：包含还原点、回收站、无权限目录，"
            "以及页面/休眠文件之外的系统占用。",
            palette=pal, fonts=fonts,
        )

    def _build_middle(self) -> None:
        pal, fonts = self.palette, self.fonts
        middle = tk.Frame(self, bg=pal.window)
        middle.grid(row=3, column=0, sticky="nsew", pady=(0, theme.sp(6)))
        middle.columnconfigure(0, weight=34, uniform="mid")
        middle.columnconfigure(1, weight=66, uniform="mid")
        middle.rowconfigure(0, weight=1)
        self.middle = middle

        tree_card = widgets.Card(middle, pal, radius=10, padding=(0, 0))
        tree_card.grid(row=0, column=0, sticky="nsew", padx=(0, theme.sp(6)))
        tree_card.body.columnconfigure(0, weight=1)
        tree_card.body.rowconfigure(0, weight=1)
        self.dir_tree = ttk.Treeview(
            tree_card.body, columns=("size",), show="tree headings",
            selectmode="browse", style="Treeview", height=7,
        )
        self.dir_tree.heading("#0", text="目录", anchor="w")
        self.dir_tree.heading("size", text="大小", anchor="e")
        self.dir_tree.column("#0", width=theme.sp(170), minwidth=theme.sp(90),
                            anchor="w", stretch=True)
        self.dir_tree.column("size", width=theme.sp(78), minwidth=theme.sp(50),
                            anchor="e", stretch=False)
        self.dir_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(tree_card.body, orient="vertical",
                                    command=self.dir_tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.dir_tree.configure(yscrollcommand=tree_scroll.set)
        self.dir_tree.tag_configure("system", foreground=pal.caution_text)
        self.dir_tree.tag_configure("protected", foreground=pal.text_tertiary)
        self.dir_tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.dir_tree.bind("<Double-1>", self._on_tree_double_click)

        map_card = widgets.Card(middle, pal, radius=10, padding=(2, 2))
        map_card.grid(row=0, column=1, sticky="nsew")
        self.treemap = Treemap(
            map_card.body, pal, fonts,
            on_select=self._on_treemap_select,
            on_open=self._on_treemap_open,
            width=theme.sp(300), height=theme.sp(110),
        )
        self.treemap.pack(fill="both", expand=True)

    def _build_bigfiles_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        card = widgets.Card(
            self, pal, radius=10, padding=(12, 7), height=150,
        )
        card.grid(row=4, column=0, sticky="nsew", pady=(0, theme.sp(6)))
        self.bigfiles_card = card
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        header = tk.Frame(body, bg=pal.surface)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text="大文件决策", bg=pal.surface, fg=pal.text,
            font=fonts.h2, anchor="w",
        ).pack(side="left")
        self.bigfiles_pill = widgets.Pill(
            header, "正在统计…", palette=pal, fonts=fonts, kind="neutral",
            bg=pal.surface, height=20,
        )
        self.bigfiles_pill.pack(side="left", padx=(theme.sp(8), 0))
        self.selected_stats_label = tk.Label(
            header, text="未选择", bg=pal.surface, fg=pal.text_tertiary,
            font=fonts.small_bold, anchor="e",
        )
        self.selected_stats_label.pack(side="right")

        self.chips_frame = tk.Frame(body, bg=pal.surface)
        self.chips_frame.grid(row=1, column=0, sticky="ew",
                              pady=(theme.sp(3), theme.sp(2)))

        table_frame = tk.Frame(body, bg=pal.surface)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("check", "name", "parent", "size", "mtime", "age",
                   "type", "advice", "action")
        self.big_table = ttk.Treeview(
            table_frame, columns=columns, show="headings",
            selectmode="none", style="Treeview", height=1,
        )
        headings = [
            ("check", "☐", 30, "center"), ("name", "名称", 250, "w"),
            ("parent", "位置", 250, "w"),
            ("size", "大小", 80, "e"), ("mtime", "修改时间", 108, "center"),
            ("age", "年龄", 54, "e"), ("type", "类型", 64, "center"),
            ("advice", "建议", 136, "center"), ("action", "操作", 76, "center"),
        ]
        for col, label, width, anchor in headings:
            self.big_table.heading(col, text=label, anchor=anchor)
            self.big_table.column(
                col, width=theme.sp(width), minwidth=theme.sp(30),
                anchor=anchor, stretch=(col == "name"),
            )
        self.big_table.grid(row=0, column=0, sticky="nsew")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical",
                                     command=self.big_table.yview)
        table_scroll.grid(row=0, column=1, sticky="ns")
        self.big_table.configure(yscrollcommand=table_scroll.set)
        self.big_table.tag_configure("safe", foreground=pal.green)
        self.big_table.tag_configure("review", foreground=pal.text)
        self.big_table.tag_configure("keep", foreground=pal.danger_text)
        self.big_table.tag_configure("row_alt", background=pal.row_alt)
        self.big_table.bind("<Button-1>", self._on_table_click)
        self.big_table.bind("<Double-1>", self._on_table_double_click)
        self.big_table.bind("<Motion>", self._on_table_motion)
        self.big_table.bind("<Leave>", self._on_table_leave)
        self._row_tip = _HoverTip(
            self.big_table, "", palette=pal, fonts=fonts, auto=False
        )

        footer = tk.Frame(body, bg=pal.surface)
        footer.grid(row=3, column=0, sticky="ew", pady=(theme.sp(4), 0))
        self.target_frame = tk.Frame(footer, bg=pal.surface)
        self.target_frame.pack(side="left")
        self.custom_target_btn = widgets.RoundedButton(
            self.target_frame, "自定义目录…", self._choose_custom_target,
            palette=pal, fonts=fonts, kind="link", bg=pal.surface,
            height=26, padx=8, font=fonts.small,
        )
        self.custom_target_btn.pack(side="left", padx=(theme.sp(4), theme.sp(6)))
        self.target_note = tk.Label(
            footer, text="", bg=pal.surface, fg=pal.text_tertiary,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(430),
        )
        self.target_note.pack(side="left", padx=(theme.sp(4), theme.sp(6)))
        self.admin_hint_btn = widgets.RoundedButton(
            footer, "以管理员身份重启", self._on_admin_restart,
            palette=pal, fonts=fonts, kind="link", bg=pal.surface,
            height=26, padx=6, font=fonts.small,
        )

        buttons = tk.Frame(footer, bg=pal.surface)
        buttons.pack(side="right")
        self.open_btn = widgets.RoundedButton(
            buttons, "打开所在位置", self._on_open_selected,
            palette=pal, fonts=fonts, kind="secondary", height=30, padx=11,
            font=fonts.small,
        )
        self.open_btn.pack(side="left", padx=(0, theme.sp(6)))
        self.recycle_btn = widgets.RoundedButton(
            buttons, "移入回收站", self._on_recycle_selected,
            palette=pal, fonts=fonts, kind="secondary", height=30, padx=11,
            font=fonts.small,
        )
        self.recycle_btn.pack(side="left", padx=(0, theme.sp(6)))
        self.migrate_btn = widgets.RoundedButton(
            buttons, "迁移到目标", self._on_migrate_selected,
            palette=pal, fonts=fonts, kind="primary", height=30, padx=14,
            font=fonts.small,
        )
        self.migrate_btn.pack(side="left")
        self._migrate_tip = _HoverTip(
            self.migrate_btn, "", palette=pal, fonts=fonts
        )
        self.migrate_btn.set_enabled(False)
        self.recycle_btn.set_enabled(False)
        self.open_btn.set_enabled(False)

    def _build_system_card(self) -> None:
        pal, fonts = self.palette, self.fonts
        card = widgets.Card(
            self, pal, height=78, radius=10, padding=(10, 6),
        )
        card.grid(row=5, column=0, sticky="ew")
        body = card.body
        for i in range(4):
            body.columnconfigure(i, weight=1, uniform="sys")
        self.system_blocks: dict[str, dict] = {}
        specs = [
            ("winsxs", "WinSxS 组件库",
             "Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase"),
            ("hiberfil", "休眠文件",
             "powercfg /h off"),
            ("restore_points", "还原点 / 受保护",
             "vssadmin resize shadowstorage /for=C: /on=C: /maxsize=10%"),
            ("recycle_bin", "回收站",
             "PowerShell -Command Clear-RecycleBin -DriveLetter C -Force"),
        ]
        for i, (key, title, command) in enumerate(specs):
            block = tk.Frame(body, bg=pal.surface)
            block.grid(row=0, column=i, sticky="nsew",
                       padx=(0 if i == 0 else theme.sp(8), 0))
            head = tk.Frame(block, bg=pal.surface)
            head.pack(fill="x")
            tk.Label(head, text=title, bg=pal.surface, fg=pal.text,
                     font=fonts.small_bold, anchor="w").pack(side="left")
            size_lbl = tk.Label(head, text="—", bg=pal.surface, fg=pal.accent,
                                font=fonts.small_bold, anchor="e")
            size_lbl.pack(side="right")
            desc = tk.Label(block, text="", bg=pal.surface, fg=pal.text_tertiary,
                            font=fonts.tiny, anchor="w", justify="left",
                            wraplength=theme.sp(250))
            desc.pack(fill="x")
            btn_row = tk.Frame(block, bg=pal.surface)
            btn_row.pack(fill="x", pady=(theme.sp(2), 0))
            widgets.RoundedButton(
                btn_row, "复制命令",
                lambda c=command: self._copy_command(c),
                palette=pal, fonts=fonts, kind="link", bg=pal.surface,
                height=22, padx=6, font=fonts.tiny,
            ).pack(side="left")
            widgets.RoundedButton(
                btn_row, "管理员 PowerShell", self._open_admin_ps,
                palette=pal, fonts=fonts, kind="link", bg=pal.surface,
                height=22, padx=6, font=fonts.tiny,
            ).pack(side="left", padx=(theme.sp(4), 0))
            self.system_blocks[key] = {
                "title": title, "command": command,
                "size": size_lbl, "desc": desc,
            }

    # ------------------------------------------------------------------
    # 生命周期 / 启动
    # ------------------------------------------------------------------
    def _on_destroy(self, event=None) -> None:
        if getattr(event, "widget", None) is not self:
            return
        self._destroyed = True
        self._cancel.set()
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        if self._pulse_id is not None:
            try:
                self.root.after_cancel(self._pulse_id)
            except Exception:
                pass
            self._pulse_id = None
        for attr in ("_row_tip", "_protected_tip"):
            tip = getattr(self, attr, None)
            if tip is not None:
                try:
                    tip.hide()
                except Exception:
                    pass

    def on_show(self) -> None:
        self.apply_row_style()
        if self._started:
            return
        self._started = True
        self._startup()

    def apply_row_style(self) -> None:
        """空间页用更紧凑的行高，给目录树和大文件表同时留出足够行数。"""
        try:
            ttk.Style(self).configure("Treeview", rowheight=theme.sp(19))
        except Exception:
            pass

    def _startup(self) -> None:
        if self._destroyed:
            return
        cached = self.space.load_cached_index(24)
        if cached is not None:
            self._apply_index(cached, from_cache=True)
            self._load_targets()
            return
        self._load_targets()
        self._start_index_build()

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------
    def _on_rescan_clicked(self) -> None:
        if self._indexing:
            self._cancel_index()
            return
        if self._moving:
            self._cancel.set()
            self.rescan_btn.set_text("正在取消迁移…")
            self._set_status("正在取消迁移：已完成的保留、未完成的源文件不动")
            return
        if self._recycling:
            return
        self.space.refresh()
        self._start_index_build()

    def _start_index_build(self) -> None:
        if self._indexing or self._moving or self._recycling:
            return
        self._indexing = True
        self._cancel = threading.Event()
        self.rescan_btn.set_text("取消扫描")
        self.cancel_index_btn.pack_forget()
        self.space_status_label.configure(text="正在统计空间…")
        self._show_progress("正在建立空间索引…")
        self._reset_pulse()
        thread = threading.Thread(target=self._index_worker, daemon=True,
                                  name="qingfeng-space-index")
        thread.start()

    def _cancel_index(self) -> None:
        if self._indexing:
            self._cancel.set()
            self.space_status_label.configure(text="正在取消…")

    def _index_worker(self) -> None:
        try:
            def progress(path: str, count: int) -> None:
                self.events.put(("index_progress", path, count))

            index = self.space.build_index(
                "C:\\", progress=progress, cancel=self._cancel.is_set
            )
            # 缓存写入放在工作线程，避免大 JSON 卡住 UI
            self.space.save_index(index)
            self.events.put(("index_done", index))
        except Exception:
            self.events.put(("index_error", traceback.format_exc()))

    def _fast_probe(self, index) -> dict:
        """从索引节点 + 快速系统调用得出占用（避免同步遍历 Windows 目录）。"""
        def node_size(path: str) -> int:
            try:
                node = index.find(path) if index else None
                return int(node.size) if node is not None else 0
            except Exception:
                return 0

        raw_tree = getattr(index, "raw_tree_bytes", {}) or {} if index else {}

        def raw_size(path: str) -> int:
            target = os.path.normcase(path)
            for raw_path, value in raw_tree.items():
                if os.path.normcase(str(raw_path)) == target:
                    return int(value)
            return 0

        def file_size(path: str) -> int:
            try:
                return int(os.path.getsize(path))
            except Exception:
                return 0

        try:
            recycle_bytes, recycle_items = self.engine.recycle_bin_size()
        except Exception:
            recycle_bytes, recycle_items = 0, 0
        windows_old = index.find("C:\\Windows.old") if index else None
        return {
            "recycle_bin": {"count": int(recycle_items), "bytes": int(recycle_bytes)},
            "hiberfil": {"exists": True, "bytes": file_size("C:\\hiberfil.sys")},
            "pagefile": {"exists": True, "bytes": file_size("C:\\pagefile.sys")},
            "swapfile": {"exists": True, "bytes": file_size("C:\\swapfile.sys")},
            "winsxs": {
                "bytes": node_size("C:\\Windows\\WinSxS"),
                "bytes_raw": raw_size("C:\\Windows\\WinSxS"),
            },
            "windows": {
                "bytes": node_size("C:\\Windows"),
                "bytes_raw": raw_size("C:\\Windows"),
            },
            "windows_old": {
                "exists": windows_old is not None,
                "bytes": int(windows_old.size) if windows_old is not None else 0,
            },
            "installer_cache": {
                "bytes": node_size("C:\\Windows\\Installer"),
                "bytes_raw": raw_size("C:\\Windows\\Installer"),
            },
            "restore_points": {
                "available": False, "bytes": None,
                "note": "非管理员无法读取还原点占用；可在“系统保护”中查看",
            },
            "fast": True,
        }

    def _start_probe_background(self) -> None:
        if not self.space.is_native:
            return

        def worker() -> None:
            try:
                probe = self.space.probe_system_space(index=self.index)
                self.events.put(("probe_done", probe))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="qingfeng-space-probe").start()

    def _on_probe_done(self, probe: dict) -> None:
        if not isinstance(probe, dict) or not probe:
            return
        merged = dict(self._probe)
        for key in ("recycle_bin", "hiberfil", "pagefile", "swapfile",
                    "winsxs", "windows", "windows_old", "installer_cache",
                    "restore_points"):
            value = probe.get(key)
            if isinstance(value, dict):
                merged[key] = value
        self._probe = merged
        self._update_summary()
        self._update_composition()
        self._update_system_cards()

    def _on_index_done(self, index, error: str | None = None) -> None:
        self._indexing = False
        self.rescan_btn.set_text("重新扫描")
        self._hide_progress()
        if error:
            self.space_status_label.configure(text="索引失败")
            self.summary_label.configure(
                text="空间索引失败，可点「重新扫描」重试；不影响「清理」页。"
            )
            self._set_status("空间索引失败：" + (error.strip().splitlines()[-1] if error else ""))
            return
        self._apply_index(index, from_cache=False)
        self.space_status_label.configure(
            text=f"{self.space.status_text} · {index.duration:.1f}s"
        )

    # ------------------------------------------------------------------
    # 应用索引 -> 页面
    # ------------------------------------------------------------------
    def _apply_index(self, index, *, from_cache: bool) -> None:
        self.index = index
        self._probe = self._fast_probe(index)
        self.review_items = self.space.collect_review_items(index, limit=800)
        self.groups = self.space.group_review_items(self.review_items)
        # 动作策略直接读 core.decide.action_policy（缺省时保守拒绝，见 space_mock）
        self.policies = {
            item.path: self.space.action_policy(item) for item in self.review_items
        }
        # 默认只勾选「安装包」且策略允许删除的项；建议保留/deny 项一律不勾选
        self.selected_paths = {
            item.path for item in self.review_items
            if item.group == "installer" and self._policy_for(item).can_delete
        }
        self._update_summary()
        self._update_composition()
        self._update_system_cards()
        self._refresh_chips()
        self._render_table()
        self._populate_tree()
        self.current_path = index.root_node.path if index.root_node else "C:\\"
        self._navigate(self.current_path, update_tree=False)
        self.bigfiles_pill.set_text(
            f"{len(self.review_items)} 个大文件 · "
            f"{self.space.human_size(sum(it.size for it in self.review_items))}"
        )
        if from_cache:
            self._set_status("已载入 24 小时内的空间索引缓存（秒开）")
        else:
            self._set_status(
                f"索引完成：{index.duration:.1f}s · "
                f"{self.space.human_size(index.accessible_bytes)} 已统计"
            )
        # Windows 目录占用等交给后台 probe（不阻塞 UI）
        self._start_probe_background()

    def _update_summary(self) -> None:
        probe = self._probe or {}
        used = int(getattr(self.index, "total_bytes", 0))
        windows = int(probe.get("windows", {}).get("bytes", 0))
        winsxs = int(probe.get("winsxs", {}).get("bytes", 0))
        hiber = int(probe.get("hiberfil", {}).get("bytes", 0)) + int(
            probe.get("pagefile", {}).get("bytes", 0)
        ) + int(probe.get("swapfile", {}).get("bytes", 0))

        def node_size(path: str) -> int:
            try:
                node = self.index.find(path) if self.index else None
                return int(node.size) if node is not None else 0
            except Exception:
                return 0

        users = node_size("C:\\Users")
        programs = node_size("C:\\Program Files") + node_size("C:\\Program Files (x86)")
        if not users:
            users = int(36.8 * GB)
        if not programs:
            programs = int(12.5 * GB)
        if not windows:
            windows = int(35.4 * GB)
        if not hiber:
            hiber = int(7.0 * GB)
        installers = sum(
            it.size for it in self.review_items if it.group == "installer"
        )
        media = sum(
            it.size for it in self.review_items
            if it.group in ("video", "archive", "vm")
        )
        hv = self.space.human_size
        windows_raw = int(probe.get("windows", {}).get("bytes_raw", 0) or 0)
        dedup_note = ""
        if windows_raw > windows > 0:
            dedup_note = f"（硬链接去重后；资源管理器约 {hv(windows_raw)}）"
        self._summary_text = (
            f"C 盘 {hv(used)} 已用：系统 {hv(windows)}{dedup_note}"
            f"（其中 {hv(winsxs)} 是组件库，可 DISM 回收）、你的文件 {hv(users)}、"
            f"程序 {hv(programs)}、休眠/交换 {hv(hiber)}。"
            f"发现 {len(self.review_items)} 个大文件合计 "
            f"{hv(sum(it.size for it in self.review_items))}，其中 "
            f"{sum(1 for it in self.review_items if it.group == 'installer')} 个是升级残留"
            f"（约 {hv(installers)}，建议清理）、"
            f"{sum(1 for it in self.review_items if it.group in ('video','archive','vm'))} 个"
            f"视频/压缩包/镜像（约 {hv(media)}，建议迁移到 D/E）。"
        )
        self.summary_label.configure(text=self._summary_text)

    def _composition_values(self) -> dict[str, int]:
        probe = self._probe or {}
        used = int(getattr(self.index, "total_bytes", 0))

        def node_size(path: str) -> int:
            try:
                node = self.index.find(path) if self.index else None
                return int(node.size) if node is not None else 0
            except Exception:
                return 0

        windows = int(probe.get("windows", {}).get("bytes", 0)) or node_size("C:\\Windows")
        users = node_size("C:\\Users")
        programs = (node_size("C:\\Program Files")
                    + node_size("C:\\Program Files (x86)"))
        hiber = sum(int(probe.get(k, {}).get("bytes", 0) or 0)
                    for k in ("hiberfil", "pagefile", "swapfile"))
        # 不再单列「可清缓存」：它可能包含在用户/程序目录里，单列会重复计数。
        # 最后一段直接取差额，保证所有分段之和严格等于磁盘已用。
        protected = max(0, used - windows - users - programs - hiber)
        return {
            "system": windows, "users": users, "programs": programs,
            "hiber": hiber, "protected": protected,
        }

    def _update_composition(self) -> None:
        self._segment_values = self._composition_values()
        total = sum(self._segment_values.values()) or 1
        hv = self.space.human_size
        for key, label, color in _COMP_SPECS:
            value = self._segment_values.get(key, 0)
            self._legend_labels[key].configure(
                text=f"● {label} {hv(value)}", fg=color
            )
        self._draw_composition_bar()

    def _draw_composition_bar(self) -> None:
        canvas = getattr(self, "comp_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = int(canvas.winfo_width())
        if width <= 4:
            return
        height = max(theme.sp(10), int(canvas.winfo_height()))
        values = self._segment_values or self._composition_values()
        total = sum(values.values()) or 1
        x = 0.0
        colors = {key: color for key, _label, color in _COMP_SPECS}
        for key, _label, _color in _COMP_SPECS:
            value = values.get(key, 0)
            if value <= 0:
                continue
            seg_w = width * value / total
            if seg_w < 1:
                continue
            canvas.create_rectangle(
                x, 0, x + seg_w, height * 0.62,
                fill=colors[key], outline="",
            )
            x += seg_w
        canvas.create_rectangle(
            0, height * 0.62, width, height,
            fill=self.palette.trough, outline="",
        )

    def _update_system_cards(self) -> None:
        probe = self._probe or {}
        hv = self.space.human_size
        winsxs = probe.get("winsxs", {}) or {}
        hiber = probe.get("hiberfil", {}) or {}
        restore = probe.get("restore_points", {}) or {}
        recycle = probe.get("recycle_bin", {}) or {}

        blocks = self.system_blocks
        blocks["winsxs"]["size"].configure(text=hv(int(winsxs.get("bytes", 0) or 0)))
        blocks["winsxs"]["desc"].configure(
            text="组件库，已按硬链接去重；可用 DISM 回收"
        )
        blocks["hiberfil"]["size"].configure(
            text=hv(int(hiber.get("bytes", 0) or 0))
        )
        blocks["hiberfil"]["desc"].configure(
            text="休眠/快速启动文件；关闭休眠可释放"
        )
        if restore.get("available") and restore.get("bytes") is not None:
            rtext = hv(int(restore.get("bytes") or 0))
        else:
            rtext = "需管理员"
        blocks["restore_points"]["size"].configure(text=rtext)
        blocks["restore_points"]["desc"].configure(
            text="系统还原点与受保护目录，需管理员查看/调整"
        )
        blocks["recycle_bin"]["size"].configure(
            text=hv(int(recycle.get("bytes", 0) or 0))
        )
        count = int(recycle.get("count", 0) or 0)
        blocks["recycle_bin"]["desc"].configure(
            text=f"{count} 项；清空不可恢复，移入回收站可还原"
        )

    # ------------------------------------------------------------------
    # 目录树 + Treemap
    # ------------------------------------------------------------------
    def _populate_tree(self) -> None:
        tree = self.dir_tree
        tree.delete(*tree.get_children(""))
        if self.index is None or self.index.root_node is None:
            return
        root_node = self.index.root_node
        root_iid = root_node.path
        hv = self.space.human_size
        protected = set(getattr(self.index, "protected_hint", {}) or {})

        def tag_for(node) -> tuple[str, ...]:
            name = node.name
            if name in protected:
                return ("protected",)
            if name in ("hiberfil.sys", "pagefile.sys", "swapfile.sys"):
                return ("system",)
            return ()

        def insert(node, parent: str, depth: int) -> None:
            children = sorted(node.children, key=lambda c: c.size, reverse=True)
            for child in children:
                if not getattr(child, "is_dir", True):
                    continue
                iid = child.path or f"{parent}/{child.name}"
                try:
                    tree.insert(
                        parent, "end", iid=iid, text=child.name,
                        values=(hv(child.size),), tags=tag_for(child),
                    )
                except Exception:
                    continue
                if depth < 1 and child.children:
                    insert(child, iid, depth + 1)

        tree.insert("", "end", iid=root_iid, text=root_node.name,
                    values=(hv(root_node.size),), open=True)
        insert(root_node, root_iid, 0)
        try:
            # 默认只展开根节点，让全部顶层目录（按大小降序）第一眼可见
            tree.item(root_iid, open=True)
        except Exception:
            pass

    def _on_tree_select(self, _event=None) -> None:
        selection = self.dir_tree.selection()
        if not selection or self.index is None:
            return
        path = selection[0]
        node = self.index.find(path)
        if node is not None and node.is_dir:
            self._navigate(node.path, update_tree=False)

    def _on_tree_double_click(self, event: tk.Event) -> None:
        iid = self.dir_tree.identify_row(event.y)
        if not iid or self.index is None:
            return
        node = self.index.find(iid)
        if node is not None and node.is_dir:
            self._navigate(node.path, update_tree=True)

    def _navigate(self, path: str, *, update_tree: bool = True) -> None:
        if self.index is None:
            return
        node = self.index.find(path)
        if node is None:
            return
        self.current_path = node.path
        title = self._breadcrumb(node.path)
        parent = node.path.rstrip("\\")
        parent_path = str(Path(parent).parent) if parent.lower() != "c:" else ""
        on_back = None
        if parent_path:
            on_back = lambda p=parent_path: self._navigate(p, update_tree=True)
        items: list[TreemapItem] = []
        for child in sorted(node.children, key=lambda c: c.size, reverse=True)[:200]:
            if child.size <= 0:
                continue
            items.append(TreemapItem(
                name=child.name, size=child.size,
                kind=self._treemap_kind(child), path=child.path,
                mtime=child.mtime, is_dir=child.is_dir,
            ))
        if not items:
            # 当前目录没有子项：用大文件作为 treemap 内容
            for item in self.review_items[:200]:
                items.append(TreemapItem(
                    name=item.name, size=item.size, kind=item.group,
                    path=item.path, mtime=item.mtime, is_dir=item.is_dir,
                ))
        self.treemap.set_items(items, title=title, on_back=on_back)
        if update_tree:
            try:
                if self.dir_tree.exists(node.path):
                    self.dir_tree.selection_set(node.path)
                    self.dir_tree.see(node.path)
            except Exception:
                pass

    def _breadcrumb(self, path: str) -> str:
        try:
            parts = [p for p in path.replace("/", "\\").split("\\") if p]
        except Exception:
            return path
        if not parts:
            return "C:"
        if len(parts) == 1:
            return f"{parts[0]} 盘 · 空间构成"
        return " > ".join(parts)

    def _treemap_kind(self, node) -> str:
        name = str(getattr(node, "name", "")).lower()
        path = str(getattr(node, "path", "")).lower()
        if name in ("hiberfil.sys", "pagefile.sys", "swapfile.sys") or \
                "system volume information" in name or "$recycle.bin" in name:
            return "system"
        if "windows" in path or "winsxs" in path:
            return "system"
        if getattr(node, "is_dir", True):
            return "dir"
        ext = Path(name).suffix.lower()
        if ext in (".iso", ".img", ".vhd", ".vhdx", ".vmdk"):
            return "vm"
        if ext in (".mp4", ".mkv", ".mov", ".avi"):
            return "video"
        if ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
            return "archive"
        if ext in (".exe", ".msi", ".appx", ".msix"):
            return "installer"
        return "file"

    def _on_treemap_select(self, item: TreemapItem) -> None:
        self._set_status(f"已选中：{item.name} · {self.space.human_size(item.size)}")

    def _on_treemap_open(self, item: TreemapItem) -> None:
        if item.is_dir and self.index is not None:
            node = self.index.find(item.path)
            if node is not None:
                self._navigate(node.path, update_tree=True)
                return
        actions.open_location(item.path)

    # ------------------------------------------------------------------
    # 大文件决策区
    # ------------------------------------------------------------------
    def _chip_counts(self) -> dict[str, tuple[int, int]]:
        counts: dict[str, tuple[int, int]] = {}
        for key, _label, group_keys in _CHIP_DEFS:
            if group_keys is None:
                items = self.review_items
            else:
                items = [it for it in self.review_items if it.group in group_keys]
            counts[key] = (len(items), sum(it.size for it in items))
        return counts

    def _refresh_chips(self) -> None:
        pal, fonts = self.palette, self.fonts
        for child in list(self.chips_frame.winfo_children()):
            child.destroy()
        self._chip_buttons = {}
        counts = self._chip_counts()
        for key, label, _groups in _CHIP_DEFS:
            count, size = counts.get(key, (0, 0))
            text = f"{label} {count}"
            btn = widgets.RoundedButton(
                self.chips_frame, text, lambda k=key: self._select_chip(k),
                palette=pal, fonts=fonts,
                kind="primary" if key == self.chip else "secondary",
                bg=pal.surface, height=24, padx=9, font=fonts.tiny,
            )
            btn.pack(side="left", padx=(0, theme.sp(4)))
            self._chip_buttons[key] = btn

    def _select_chip(self, key: str) -> None:
        self.chip = key
        for chip_key, btn in self._chip_buttons.items():
            btn.set_kind("primary" if chip_key == key else "secondary")
        self._render_table()

    def _visible_items(self) -> list:
        chip_def = next((d for d in _CHIP_DEFS if d[0] == self.chip), None)
        if chip_def is None or chip_def[2] is None:
            items = list(self.review_items)
        else:
            items = [it for it in self.review_items if it.group in chip_def[2]]
        items.sort(key=lambda it: it.size, reverse=True)
        return items

    def _render_table(self) -> None:
        if not hasattr(self, "big_table"):
            return
        self._updating_table = True
        table = self.big_table
        try:
            scroll = table.yview()
            table.delete(*table.get_children(""))
            hv = self.space.human_size
            for index, item in enumerate(self._visible_items()):
                checked = item.path in self.selected_paths
                mtime = time.strftime("%Y-%m-%d", time.localtime(item.mtime)) \
                    if item.mtime else "—"
                age = f"{item.age_days} 天" if item.age_days >= 0 else "—"
                type_text = (item.ext.upper() if item.ext else "目录")
                policy = self._policy_for(item)
                denied = not policy.can_delete and not policy.can_move
                base_advice = _ADVICE_LABEL.get(item.advice, item.advice)
                short = _deny_short_label(item, policy)
                advice = short if short else base_advice
                tags = ["row_alt" if index % 2 else "row"]
                if denied or item.advice == "keep":
                    tags.append("keep")
                elif item.advice == "safe_delete":
                    tags.append("safe")
                else:
                    tags.append("review")
                table.insert(
                    "", "end", iid=item.path,
                    values=(
                        "☑" if checked else "☐", item.name,
                        self._short_parent(item.path), hv(item.size),
                        mtime, age, type_text, advice, "打开位置",
                    ),
                    tags=tuple(tags),
                )
            try:
                table.yview_moveto(scroll[0])
            except Exception:
                pass
        finally:
            self._updating_table = False
        self._update_selected_stats()

    def _on_table_click(self, event: tk.Event) -> str | None:
        if self._updating_table:
            return None
        region = self.big_table.identify_region(event.x, event.y)
        if region != "cell":
            return None
        iid = self.big_table.identify_row(event.y)
        column = self.big_table.identify_column(event.x)
        if not iid:
            return None
        if column == "#1":
            self._toggle_item(iid)
            return "break"
        if column == "#2":
            item = self._item_by_path(iid)
            if item is not None:
                policy = self._policy_for(item)
                reason = policy.deny_reason or item.reason or ""
                if policy.deny_reason:
                    self._set_status(f"不可操作：{reason}")
                elif item.reason:
                    self._set_status(f"{item.advice}：{reason}")
            return None
        if column == "#9":
            item = self._item_by_path(iid)
            if item is not None:
                actions.open_location(item.path)
            return "break"
        return None

    def _on_table_double_click(self, event: tk.Event) -> str | None:
        iid = self.big_table.identify_row(event.y)
        if not iid:
            return None
        item = self._item_by_path(iid)
        if item is not None:
            actions.open_location(item.path)
        return "break"

    def _item_by_path(self, path: str):
        for item in self.review_items:
            if item.path == path:
                return item
        return None

    @staticmethod
    def _short_parent(path: str) -> str:
        """位置列：保留最有辨识度的末两级目录，完整路径在行悬停 tooltip 中显示。"""
        parent = os.path.dirname(str(path).rstrip("\\")) or str(path)
        drive, tail = os.path.splitdrive(parent)
        parts = [part for part in tail.split("\\") if part]
        if not parts:
            return parent
        if len(parts) <= 2 and len(parent) <= 32:
            return parent
        prefix = "…\\" if len(parts) > 2 else ""
        last = parts[-1]
        first_tail = parts[-2] if len(parts) > 1 else ""
        text = f"{prefix}{first_tail}\\{last}" if first_tail else f"{prefix}{last}"
        if len(text) > 30:
            available = max(4, 30 - len(prefix) - len(last) - 2)
            text = f"{prefix}{first_tail[:available]}…\\{last}"
        return text

    def _policy_for(self, item):
        path = item.path if hasattr(item, "path") else str(item)
        policy = self.policies.get(path)
        if policy is None:
            found = self._item_by_path(path) if not hasattr(item, "path") else item
            policy = self.space.action_policy(found) if found is not None else \
                space_mock.ActionPolicy(
                    can_delete=False, can_move=False,
                    deny_reason="未评估的项目，暂不可操作",
                )
            self.policies[path] = policy
        return policy

    def _on_table_motion(self, event: tk.Event) -> None:
        iid = self.big_table.identify_row(event.y)
        if not iid:
            self._row_tip.hide()
            return
        item = self._item_by_path(iid)
        if item is None:
            self._row_tip.hide()
            return
        policy = self._policy_for(item)
        text = ""
        if policy.deny_reason:
            text = "不可操作：" + policy.deny_reason + "\n"
        text += "完整路径：" + item.path
        self._row_tip.show_at(text, event.x_root, event.y_root)

    def _on_table_leave(self, _event=None) -> None:
        self._row_tip.hide()
        self._update_selected_stats()

    def _toggle_item(self, path: str) -> None:
        if path in self.selected_paths:
            self.selected_paths.discard(path)
        else:
            item = self._item_by_path(path)
            policy = self._policy_for(item) if item is not None else None
            if policy is not None and not policy.can_delete and not policy.can_move:
                self._set_status(
                    "不可操作：" + (policy.deny_reason or "该项目已受保护")
                )
                return
            self.selected_paths.add(path)
        values = list(self.big_table.item(path, "values"))
        if values:
            values[0] = "☑" if path in self.selected_paths else "☐"
            self.big_table.item(path, values=values)
        self._update_selected_stats()

    def _selected_items(self) -> list:
        return [it for it in self.review_items if it.path in self.selected_paths]

    def _update_selected_stats(self) -> None:
        items = self._selected_items()
        total = sum(it.size for it in items)
        hv = self.space.human_size
        can_recycle = bool(items) and all(
            self._policy_for(it).can_delete for it in items
        )
        can_move = bool(items) and all(
            self._policy_for(it).can_move for it in items
        )
        denied_count = sum(
            1 for it in items
            if not self._policy_for(it).can_delete
            and not self._policy_for(it).can_move
        )
        if items:
            suffix = f" · 含 {denied_count} 项不可操作" if denied_count else ""
            self.selected_stats_label.configure(
                text=f"已选 {len(items)} 项 · 合计 {hv(total)}{suffix}",
                fg=self.palette.accent,
            )
        else:
            self.selected_stats_label.configure(
                text="未选择", fg=self.palette.text_tertiary
            )
        enabled = bool(items) and not self._moving and not self._recycling \
            and not self._indexing
        self.recycle_btn.set_enabled(enabled and can_recycle)
        self.open_btn.set_enabled(bool(items))
        self._update_migrate_state()

    # ------------------------------------------------------------------
    # 迁移目标
    # ------------------------------------------------------------------
    def _load_targets(self) -> None:
        try:
            self.targets = self.space.suggest_targets()
        except Exception:
            self.targets = []
        writable = [t for t in self.targets if t.get("writable")]
        self.selected_target = writable[0] if writable else (
            self.targets[0] if self.targets else None
        )
        self._render_targets()

    def _render_targets(self) -> None:
        pal, fonts = self.palette, self.fonts
        for btn in self._target_buttons:
            try:
                btn.destroy()
            except Exception:
                pass
        self._target_buttons = []
        old_box = getattr(self, "_target_box", None)
        if old_box is not None:
            try:
                old_box.destroy()
            except Exception:
                pass
            self._target_box = None
        if not self.targets:
            self.target_note.configure(
                text="未发现可用的固定磁盘；请选择「自定义目录…」或插入 D/E 盘。",
                fg=pal.caution_text,
            )
            self._update_migrate_state()
            return
        box = tk.Frame(self.target_frame, bg=pal.surface)
        box.pack(side="left", before=self.custom_target_btn)
        self._target_box = box
        for target in self.targets:
            drive = target.get("drive", "?")
            free = self.space.human_size(int(target.get("free", 0)))
            writable = bool(target.get("writable"))
            text = f"{drive} {free} · {'可写' if writable else '需管理员'}"
            selected = bool(self.selected_target and
                            self.selected_target.get("path") == target.get("path"))
            btn = widgets.RoundedButton(
                box, text, lambda t=target: self._select_target(t),
                palette=pal, fonts=fonts,
                kind="primary" if selected else "secondary",
                bg=pal.surface, height=24, padx=8, font=fonts.tiny,
            )
            btn.pack(side="left", padx=(0, theme.sp(4)))
            self._target_buttons.append(btn)
        self._update_target_note()

    def _select_target(self, target: dict) -> None:
        self.selected_target = target
        self._custom_target = None
        self._render_targets()

    def _choose_custom_target(self) -> None:
        try:
            chosen = filedialog.askdirectory(
                parent=self.root, title="选择迁移目标目录",
                initialdir=self.selected_target.get("path", "D:\\")
                if self.selected_target else "D:\\",
            )
        except Exception:
            chosen = ""
        if not chosen:
            return
        free = actions.target_free_bytes(chosen)
        writable = actions.can_write_dir(chosen)
        note = "自定义目录"
        if not writable:
            note = "该目录不可写，请选择其它目录或以管理员身份重启"
        target = {
            "drive": str(Path(chosen).drive or chosen), "path": chosen,
            "free": free, "total": free, "writable": writable, "note": note,
        }
        self._custom_target = target
        self.targets = [target] + [t for t in self.targets
                                   if t.get("path") != chosen]
        self.selected_target = target
        self._render_targets()

    def _update_target_note(self) -> None:
        pal = self.palette
        target = self.selected_target
        if target is None:
            self.target_note.configure(text="")
            return
        note = str(target.get("note") or "")
        writable = bool(target.get("writable"))
        if writable:
            text = note or "目标目录可写"
            self.target_note.configure(text=text, fg=pal.text_tertiary)
            self.admin_hint_btn.pack_forget()
        else:
            text = note or "目标目录不可写：请选择其它目录，或以管理员身份重启"
            self.target_note.configure(text=text, fg=pal.caution_text)
            self.admin_hint_btn.pack(side="left", padx=(theme.sp(2), theme.sp(6)))
        try:
            self.migrate_btn.set_text(
                f"迁移到 {target.get('drive', '目标')}"
                if writable else "迁移不可用"
            )
        except Exception:
            pass
        self._update_migrate_state()

    def _update_migrate_state(self) -> None:
        items = self._selected_items()
        target = self.selected_target
        writable = bool(target and target.get("writable"))
        policies = [(it, self._policy_for(it)) for it in items]
        movable = [(it, p) for it, p in policies if p.can_move]
        protected = [
            (it, p) for it, p in policies
            if not p.can_delete and not p.can_move
        ]
        clean_only = [
            (it, p) for it, p in policies
            if p.can_delete and not p.can_move
        ]
        move_only = [
            (it, p) for it, p in policies
            if not p.can_delete and p.can_move
        ]
        blocked = protected + clean_only + move_only
        enabled = bool(items) and bool(movable) and writable \
            and not self._moving and not self._recycling and not self._indexing
        self.migrate_btn.set_enabled(enabled)

        tip_lines: list[str] = []
        for it, p in blocked[:8]:
            reason = p.deny_reason or "该区域不允许迁移"
            tip_lines.append(f"{it.name} — {reason}")
        if len(blocked) > 8:
            tip_lines.append(f"…等 {len(blocked)} 项")

        if target and writable and items and not movable:
            if protected and clean_only:
                text = "迁移不可用（含受保护项/仅可清理项）"
            elif protected:
                text = "迁移不可用（含受保护项）"
            elif clean_only:
                text = "迁移不可用（含不可迁移项·仅可清理）"
            else:
                text = "迁移不可用（含不可迁移项）"
            tip = "不可迁移的项目：\n" + "\n".join(tip_lines)
        elif target and writable and blocked:
            text = (
                f"迁移选中项（{len(movable)} 项可迁移 / "
                f"{len(blocked)} 项将跳过）"
            )
            tip = (
                f"将迁移 {len(movable)} 项；以下 {len(blocked)} 项会跳过：\n"
                + "\n".join(tip_lines)
            )
        elif target and writable:
            text = f"迁移到 {target.get('drive')}"
            tip = "把已勾选的项目迁移到目标盘；复制成功后才删除源文件"
        else:
            text = "迁移不可用"
            tip = str((target or {}).get("note") or "请选择可写的迁移目标目录")
        self.migrate_btn.set_text(text)
        if getattr(self, "_migrate_tip", None) is not None:
            self._migrate_tip.text = tip

    def _on_admin_restart(self) -> None:
        if self.on_restart_as_admin is not None:
            self.on_restart_as_admin()

    # ------------------------------------------------------------------
    # 回收站动作
    # ------------------------------------------------------------------
    def _on_recycle_selected(self) -> None:
        if self._moving or self._recycling or self._indexing:
            return
        items = self._selected_items()
        if not items:
            self._set_status("请先勾选要移入回收站的项目")
            return
        denied = [it for it in items if not self._policy_for(it).can_delete]
        if denied:
            reason = self._policy_for(denied[0]).deny_reason or "受保护项目"
            self._set_status(f"不可操作：{reason}")
            return
        total = sum(it.size for it in items)
        keep_count = sum(1 for it in items if it.advice == "keep")
        confirmed = dialogs.confirm_recycle_items(
            self.root, palette=self.palette, fonts=self.fonts,
            item_names=[it.name for it in items],
            total_bytes=total, keep_count=keep_count,
        )
        if not confirmed:
            self._set_status("已取消移入回收站")
            return
        self._recycling = True
        self._cancel = threading.Event()
        self._set_status(f"正在把 {len(items)} 项移入回收站…")
        self._show_progress("正在移入回收站…")
        self._reset_pulse()
        self._update_selected_stats()
        thread = threading.Thread(
            target=self._recycle_worker, args=(list(items),), daemon=True,
            name="qingfeng-recycle",
        )
        thread.start()

    def _recycle_worker(self, items: list) -> None:
        try:
            allow_real = self.space.is_native
            paths = []
            failures: list[tuple[str, str]] = []
            for item in items:
                policy = self._policy_for(item)
                if not policy.can_delete:
                    failures.append((item.path, policy.deny_reason or "受保护项目"))
                else:
                    paths.append(item.path)
            if paths:
                failures.extend(actions.recycle_items(paths, allow_real=allow_real))
            self.events.put(("recycle_done", paths, failures, not allow_real))
        except Exception:
            self.events.put(("recycle_error", traceback.format_exc()))

    def _on_recycle_done(self, paths: list, failures: list, simulated: bool) -> None:
        self._recycling = False
        self._hide_progress()
        failed_paths = {p for p, _e in failures}
        for path in paths:
            if path in failed_paths:
                continue
            self.selected_paths.discard(path)
            self.review_items = [it for it in self.review_items if it.path != path]
        self.groups = self.space.group_review_items(self.review_items)
        self._refresh_chips()
        self._render_table()
        if failures:
            detail = "\n".join(f"{p} — {e}" for p, e in failures[:6])
            if simulated:
                detail = "演示模式：未实际删除文件。"
            dialogs.show_error(
                self.root, palette=self.palette, fonts=self.fonts,
                title="部分项目未能移入回收站",
                message=f"{len(failures)} 项失败，其余已处理。",
                detail=detail,
            )
        self._set_status(
            f"已处理 {len(paths) - len(failures)} 项移入回收站"
            + ("（演示模式，未真正删除）" if simulated else "")
        )
        self._update_selected_stats()

    # ------------------------------------------------------------------
    # 迁移动作
    # ------------------------------------------------------------------
    def _ensure_target_dir(self, target: dict) -> bool:
        """确认目标目录存在且可写（必要时创建 QingFengMoved）。"""
        dest = str(target.get("path") or "")
        if not dest:
            return False
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as exc:
            target["writable"] = False
            target["note"] = f"目标目录无法创建：{exc.__class__.__name__}，请换目录或管理员运行"
            return False
        if not actions.can_write_dir(dest):
            target["writable"] = False
            target["note"] = "目标目录不可写，请选择其它目录或以管理员身份重启"
            return False
        target["free"] = actions.target_free_bytes(dest)
        return True

    def _on_migrate_selected(self) -> None:
        if self._moving or self._recycling or self._indexing:
            return
        items = self._selected_items()
        if not items:
            self._set_status("请先勾选要迁移的项目")
            return
        movable = []
        blocked: list[tuple[object, str]] = []
        for item in items:
            policy = self._policy_for(item)
            if policy.can_move:
                movable.append(item)
            else:
                blocked.append((item, policy.deny_reason or "该区域不允许迁移"))
        if not movable:
            first_reason = blocked[0][1] if blocked else "全部项目均不可迁移"
            self._set_status(f"不可迁移：{first_reason}")
            return
        target = self.selected_target
        if target is None or not target.get("writable"):
            self._set_status("迁移目标不可写：请选择其它目录，或以管理员身份重启")
            return
        if not self._ensure_target_dir(target):
            self._render_targets()
            self._set_status(str(target.get("note") or "目标目录不可写"))
            return
        dest_root = str(target.get("path") or "")
        total = sum(it.size for it in movable)
        keep_count = sum(1 for it in movable if it.advice == "keep")
        source_root = "C:\\"
        estimate = actions.estimate_minutes(total, speed_mb_s=300.0)
        confirmed = dialogs.confirm_move(
            self.root, palette=self.palette, fonts=self.fonts,
            item_names=[it.name for it in movable],
            total_bytes=total,
            file_count=sum(int(getattr(it, "file_count", 1) or 1) for it in movable),
            source_root=source_root, dest_root=dest_root,
            target_free=int(target.get("free", 0)),
            estimate_minutes=estimate, keep_count=keep_count,
            skipped_items=[(it.name, reason) for it, reason in blocked],
        )
        if not confirmed:
            self._set_status("已取消迁移")
            return
        self._moving = True
        self._cancel = threading.Event()
        self.rescan_btn.set_text("取消迁移")
        self._set_status(
            f"正在迁移 {len(movable)} 项到 {dest_root}…"
            + (f"（{len(blocked)} 项不迁移）" if blocked else "")
        )
        self._show_progress("正在复制并校验…")
        self._reset_pulse()
        self._update_selected_stats()
        thread = threading.Thread(
            target=self._move_worker,
            args=(list(movable), dest_root, list(blocked)), daemon=True,
            name="qingfeng-move",
        )
        thread.start()

    def _move_worker(self, items: list, dest_root: str,
                     blocked: list[tuple[object, str]] | None = None) -> None:
        try:
            allowed: list[str] = []
            denied: list[tuple[str, str]] = []
            for item in items:
                policy = self._policy_for(item)
                if policy.can_move:
                    allowed.append(item.path)
                else:
                    denied.append((item.path, policy.deny_reason or "受保护项目"))
            for item, reason in blocked or []:
                denied.append((getattr(item, "path", ""), reason))

            def progress(path: str, done: int, total: int) -> None:
                self.events.put(("move_progress", path, done, total))

            result = self.space.move_many(
                allowed, dest_root, progress=progress,
                cancel=self._cancel.is_set,
            )
            for path, reason in denied:
                result.skipped.append((path, reason))
            self.events.put(("move_done", result, items, dest_root))
        except Exception:
            self.events.put(("move_error", traceback.format_exc()))

    def _on_move_done(self, result, items: list, dest_root: str) -> None:
        self._moving = False
        self._hide_progress()
        self.rescan_btn.set_text("重新扫描")
        moved = int(getattr(result, "moved", 0))
        simulated = not self.space.is_native
        failed_paths = {
            str(path) for path, _reason in (getattr(result, "failed", []) or [])
        } | {
            str(path) for path, _reason in (getattr(result, "skipped", []) or [])
        }
        moved_paths = {
            it.path for it in items if it.path not in failed_paths
        }
        if moved_paths:
            self.review_items = [
                it for it in self.review_items if it.path not in moved_paths
            ]
            self.selected_paths -= moved_paths
            self.groups = self.space.group_review_items(self.review_items)
            self._refresh_chips()
            self._render_table()
        self._update_selected_stats()
        self._set_status(
            f"迁移完成：成功 {moved} 项 · "
            f"{self.space.human_size(int(getattr(result, 'moved_bytes', 0)))}"
            + ("（演示模式，未实际复制）" if simulated else "")
        )
        dialogs.show_move_summary(
            self.root, palette=self.palette, fonts=self.fonts,
            result=result, target_root=dest_root,
            on_open_target=lambda: actions.open_location(dest_root),
        )

    # ------------------------------------------------------------------
    # 打开 / 复制 / 管理员
    # ------------------------------------------------------------------
    def _on_open_selected(self) -> None:
        items = self._selected_items()
        if not items:
            self._set_status("请先选中要打开位置的项目")
            return
        if actions.open_location(items[0].path):
            self._set_status(f"已在资源管理器中打开：{items[0].name}")
        else:
            self._set_status("打开位置失败")

    def _copy_command(self, command: str) -> None:
        if actions.copy_text(self.root, command):
            self._set_status("命令已复制到剪贴板（工具不会代执行）")
        else:
            self._set_status("复制命令失败")

    def _open_admin_ps(self) -> None:
        if actions.open_admin_powershell():
            self._set_status("已打开管理员 PowerShell，请粘贴已复制的命令")
        else:
            self._set_status("打开管理员 PowerShell 失败（可能取消了 UAC）")

    # ------------------------------------------------------------------
    # 事件循环 / 状态
    # ------------------------------------------------------------------
    def _poll_events(self) -> None:
        if self._destroyed or not self.winfo_exists():
            return
        self._poll_id = self.root.after(80, self._poll_events)
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "index_progress":
            _, path, count = event
            path = str(path)
            if len(path) > 52:
                path = "…" + path[-51:]
            self.progress_text.configure(text=f"{path} · 已处理 {count:,} 项")
            self._set_status(f"正在统计：{path}")
        elif kind == "index_done":
            self._on_index_done(event[1])
        elif kind == "index_error":
            self._on_index_done(None, event[1])
        elif kind == "probe_done":
            self._on_probe_done(event[1])
        elif kind == "recycle_done":
            _, paths, failures, simulated = event
            self._on_recycle_done(paths, failures, simulated)
        elif kind == "recycle_error":
            self._recycling = False
            self._hide_progress()
            self._set_status("移入回收站失败")
            dialogs.show_error(
                self.root, palette=self.palette, fonts=self.fonts,
                title="移入回收站失败", message="操作过程中出现问题。",
                detail=event[1].strip().splitlines()[-1] if event[1].strip() else "",
            )
        elif kind == "move_progress":
            _, path, done, total = event
            if total:
                self.progress_bar.set(done / max(1, total))
            name = str(path)
            if len(name) > 48:
                name = "…" + name[-47:]
            self.progress_text.configure(text=f"{name} · {done}/{total}")
            self._set_status(f"正在迁移：{name}（{done}/{total}）")
        elif kind == "move_done":
            _, result, items, dest_root = event
            self._on_move_done(result, items, dest_root)
        elif kind == "move_error":
            self._moving = False
            self._hide_progress()
            self._set_status("迁移失败")
            dialogs.show_error(
                self.root, palette=self.palette, fonts=self.fonts,
                title="迁移失败", message="迁移过程中出现问题，源文件未被删除。",
                detail=event[1].strip().splitlines()[-1] if event[1].strip() else "",
            )
        self._update_selected_stats()

    def _set_status(self, text: str) -> None:
        if hasattr(self, "space_status_label"):
            try:
                self.space_status_label.configure(text=text)
            except Exception:
                pass

    def _show_progress(self, label: str) -> None:
        self.progress_row.grid()
        self.progress_bar.set(0.0)
        self.progress_text.configure(text=label)
        if self._pulse_id is None:
            self._pulse_id = self.root.after(120, self._pulse)

    def _hide_progress(self) -> None:
        try:
            self.progress_row.grid_remove()
        except Exception:
            pass
        if self._pulse_id is not None:
            try:
                self.root.after_cancel(self._pulse_id)
            except Exception:
                pass
            self._pulse_id = None

    def _reset_pulse(self) -> None:
        self._pulse_value = 0.0

    def _pulse(self) -> None:
        self._pulse_id = None
        if self._destroyed or not self.winfo_exists():
            return
        if not (self._indexing or self._moving or self._recycling):
            return
        self._pulse_value = (self._pulse_value + 0.04) % 1.0
        # 索引用脉冲式不确定进度；迁移/回收站由事件设置真实进度
        if self._indexing:
            value = 0.15 + 0.7 * (self._pulse_value)
            self.progress_bar.set(value)
        self._pulse_id = self.root.after(160, self._pulse)
