"""清风清理 — 自绘控件库。

tkinter 默认控件在 Windows 上要么是灰扑扑的 90 年代外观（tk），要么无法自定义
深色（ttk vista 主题）。这里用 Canvas 自绘卡片、圆环仪表、圆角按钮、进度条与
分段徽标，配合 ttk.Treeview 的 clam 样式，得到接近 Windows 11 原生工具的观感。

约定：所有尺寸都经 theme.sp() 按 DPI 缩放；所有颜色来自 theme.Palette。
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence
import tkinter as tk
from tkinter import ttk

from . import theme


# ---------------------------------------------------------------------------
# 基础绘制
# ---------------------------------------------------------------------------


def rounded_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs,
) -> int:
    """用 smooth polygon 画圆角矩形（Tk Canvas 无原生抗锯齿圆角）。"""
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=32, **kwargs)


def clear_canvas(canvas: tk.Canvas, tag: str | None = None) -> None:
    canvas.delete(tag if tag else "all")


# ---------------------------------------------------------------------------
# 圆角卡片
# ---------------------------------------------------------------------------


class Card(tk.Canvas):
    """带圆角与细描边的卡片，内容放在 self.body（tk.Frame）里。"""

    def __init__(
        self,
        master: tk.Misc,
        palette: theme.Palette,
        *,
        bg: str | None = None,
        surface: str | None = None,
        radius: float = 12,
        padding: tuple[float, float] = (0, 0),
        bordered: bool = True,
        height: int | None = None,
        autosize: bool = False,
        **kwargs,
    ) -> None:
        self.palette = palette
        self._card_bg = bg or palette.window
        self._surface = surface or palette.surface
        self._radius = theme.sp(radius)
        self._padx = theme.sp(padding[0])
        self._pady = theme.sp(padding[1])
        self._bordered = bordered
        self._autosize = autosize
        self._auto_h = 0
        super().__init__(
            master,
            bg=self._card_bg,
            highlightthickness=0,
            bd=0,
            height=theme.sp(height) if height else theme.sp(1),
            **kwargs,
        )
        self.body = tk.Frame(self, bg=self._surface, bd=0, highlightthickness=0)
        self._body_id = self.create_window(
            self._padx, self._pady, window=self.body, anchor="nw"
        )
        self._last: tuple[int, int] = (0, 0)
        self.bind("<Configure>", self._on_configure)
        if autosize:
            self.body.bind("<Configure>", self._on_body_configure)
            # 未映射的窗口（如模态弹窗预排版）不会触发 body 的 Configure，
            # 用 idle 回调先把高度按内容算出来。
            self.after_idle(self._on_body_configure)

    def _on_body_configure(self, _event: tk.Event | None = None) -> None:
        if not self._autosize:
            return
        needed = self.body.winfo_reqheight() + 2 * self._pady
        if needed > 1 and abs(needed - self._auto_h) > 1:
            self._auto_h = needed
            self.configure(height=needed)

    def _on_configure(self, event: tk.Event) -> None:
        w, h = event.width, event.height
        if w <= 1 or h <= 1 or (w, h) == self._last:
            return
        self._last = (w, h)
        self.delete("card_bg")
        rounded_rect(
            self,
            1,
            1,
            w - 1,
            h - 1,
            self._radius,
            fill=self._surface,
            outline=self.palette.border if self._bordered else self._surface,
            width=1,
            tags="card_bg",
        )
        self.tag_lower("card_bg")
        self.coords(self._body_id, self._padx, self._pady)
        if self._autosize:
            self.itemconfigure(
                self._body_id, width=max(1, w - 2 * self._padx)
            )
        else:
            self.itemconfigure(
                self._body_id,
                width=max(1, w - 2 * self._padx),
                height=max(1, h - 2 * self._pady),
            )

    def recolor(self) -> None:
        """主题切换后（若复用卡片）刷新底色。"""
        self.configure(bg=self.palette.window)
        self.body.configure(bg=self.palette.surface)
        self.delete("card_bg")
        self._last = (0, 0)
        self.event_generate("<Configure>")


# ---------------------------------------------------------------------------
# 圆环仪表盘（磁盘占用）
# ---------------------------------------------------------------------------


class RingGauge(tk.Canvas):
    """环形占用率仪表：渐变弧 + 中心大字。"""

    def __init__(
        self,
        master: tk.Misc,
        palette: theme.Palette,
        fonts: theme.Fonts,
        *,
        size: float = 112,
        thickness: float = 10,
        bg: str | None = None,
    ) -> None:
        self.size = theme.sp(size)
        self.thickness = theme.sp(thickness)
        super().__init__(
            master,
            width=self.size,
            height=self.size,
            bg=bg or palette.surface,
            highlightthickness=0,
            bd=0,
        )
        self.palette = palette
        self.fonts = fonts
        self._ratio = 0.0
        self._caption = ""
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set(self, ratio: float, caption: str = "已用") -> None:
        self._ratio = max(0.0, min(1.0, float(ratio)))
        self._caption = caption
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = self.size
        pad = self.thickness / 2 + 1
        box = (pad, pad, w - pad, w - pad)
        pal = self.palette

        # 轨道
        self.create_oval(*box, outline=pal.ring_track, width=int(self.thickness))

        # 渐变弧：绿 -> 蓝，按占用率绘制
        ratio = self._ratio
        if ratio > 0.001:
            segments = 36
            for i in range(segments):
                t0 = i / segments
                t1 = (i + 1) / segments
                start = 90.0 - 360.0 * ratio * t0
                extent = -360.0 * ratio * (t1 - t0)
                color = theme.mix(pal.green, pal.accent, t0)
                self.create_arc(
                    *box,
                    start=start,
                    extent=extent - 0.8 if extent < 0 else extent + 0.8,
                    style="arc",
                    outline=color,
                    width=int(self.thickness),
                )
        # 中心文字
        center = w / 2
        pct = f"{int(round(ratio * 100))}%"
        if ratio >= 0.92:
            color = pal.danger_text
        elif ratio >= 0.80:
            color = pal.caution_text
        else:
            color = pal.text
        self.create_text(
            center,
            center - theme.sp(10),
            text=pct,
            fill=color,
            font=self.fonts.num_md,
            anchor="center",
        )
        self.create_text(
            center,
            center + theme.sp(17),
            text=self._caption,
            fill=pal.text_tertiary,
            font=self.fonts.small,
            anchor="center",
        )


# ---------------------------------------------------------------------------
# 圆角按钮
# ---------------------------------------------------------------------------

_BUTTON_KINDS = {
    "primary": dict(fill="accent", hover="accent_hover", pressed="accent_pressed",
                    fg="on_accent", border=None),
    "danger": dict(fill="danger", hover="danger_hover", pressed="danger_pressed",
                   fg="#FFFFFF", border=None),
    "secondary": dict(fill="surface", hover="hover", pressed="accent_soft",
                      fg="text", border="border_strong"),
    "subtle": dict(fill=None, hover="hover", pressed="accent_soft",
                   fg="text", border=None),
    "link": dict(fill=None, hover="accent_soft", pressed="accent_soft",
                 fg="accent", border=None),
}


class RoundedButton(tk.Canvas):
    """自绘圆角按钮，支持 primary / danger / secondary / subtle / link。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        kind: str = "secondary",
        bg: str | None = None,
        height: float = 36,
        padx: float = 20,
        min_width: float = 0,
        radius: float = 7,
        font: tk.font.Font | None = None,
    ) -> None:
        self.palette = palette
        self.fonts = fonts
        self.kind = kind if kind in _BUTTON_KINDS else "secondary"
        self._text = text
        self._command = command
        self._bg = bg or palette.surface
        self._height = theme.sp(height)
        self._padx = theme.sp(padx)
        self._min_width = theme.sp(min_width)
        self._radius = theme.sp(radius)
        self._text_font = font or fonts.body_bold
        self._enabled = True
        self._hover = False
        self._pressed = False
        width = self._calc_width()
        super().__init__(
            master,
            width=width,
            height=self._height,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=1,
        )
        self._width = width
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._draw())

    # -- 尺寸 / 状态 ---------------------------------------------------
    def _calc_width(self) -> int:
        try:
            text_w = self._text_font.measure(self._text)
        except Exception:
            text_w = len(self._text) * theme.sp(12)
        return max(self._min_width, text_w + 2 * self._padx)

    def _style(self) -> tuple[str | None, str]:
        pal = self.palette
        spec = _BUTTON_KINDS[self.kind]
        if not self._enabled:
            if spec["fill"] is None:
                return None, pal.disabled_fg
            return pal.disabled_bg, pal.disabled_fg
        if self._pressed:
            fill_key = spec["pressed"]
        elif self._hover:
            fill_key = spec["hover"]
        else:
            fill_key = spec["fill"]
        fill = getattr(pal, fill_key) if fill_key else None
        fg_key = spec["fg"]
        fg = fg_key if fg_key.startswith("#") else getattr(pal, fg_key)
        return fill, fg

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = int(self._width)
        h = self.winfo_height()
        if h <= 1:
            h = self._height
        fill, fg = self._style()
        spec = _BUTTON_KINDS[self.kind]
        border_key = spec["border"]
        if self._enabled and border_key:
            border = getattr(self.palette, border_key)
        else:
            border = fill
        if fill is not None:
            rounded_rect(
                self, 1, 1, w - 1, h - 1, self._radius,
                fill=fill, outline=border, width=1,
            )
        elif self._hover and self._enabled:
            rounded_rect(
                self, 1, 1, w - 1, h - 1, self._radius,
                fill=getattr(self.palette, spec["hover"]), outline="", width=0,
            )
        offset = theme.sp(1) if (self._pressed and self._enabled) else 0
        self.create_text(
            w / 2,
            h / 2 + offset,
            text=self._text,
            fill=fg,
            font=self._text_font,
            anchor="center",
        )

    # -- 事件 ----------------------------------------------------------
    def _on_enter(self, _event: tk.Event) -> None:
        if not self._enabled:
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, event: tk.Event) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        if was_pressed and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self._command is not None:
                self._command()

    # -- 公开 API ------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._width = self._calc_width()
        self.configure(width=self._width)
        self._draw()

    def set_kind(self, kind: str) -> None:
        if kind in _BUTTON_KINDS:
            self.kind = kind
            self._draw()

    @property
    def enabled(self) -> bool:
        return self._enabled


# ---------------------------------------------------------------------------
# 进度条
# ---------------------------------------------------------------------------


class ProgressBar(tk.Canvas):
    """细圆角进度条；value 取值 0.0 ~ 1.0。"""

    def __init__(
        self,
        master: tk.Misc,
        palette: theme.Palette,
        *,
        height: float = 6,
        bg: str | None = None,
        fill_color: str | None = None,
    ) -> None:
        self._h = theme.sp(height)
        super().__init__(
            master,
            height=self._h,
            bg=bg or palette.surface,
            highlightthickness=0,
            bd=0,
        )
        self.palette = palette
        self._fill_override = fill_color
        self._value = 0.0
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._value) < 0.0005:
            return
        self._value = value
        self._draw()

    @property
    def value(self) -> float:
        return self._value

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            return
        h = self._h
        r = h / 2
        pal = self.palette
        rounded_rect(self, 0, 0, w, h, r, fill=pal.trough, outline="", width=0)
        if self._value > 0.001:
            fw = max(h, w * self._value)
            color = self._fill_override or pal.accent
            rounded_rect(self, 0, 0, fw, h, r, fill=color, outline="", width=0)


# ---------------------------------------------------------------------------
# 小型徽标（版本号 / 引擎状态 / 仅报告）
# ---------------------------------------------------------------------------


class Pill(tk.Canvas):
    """圆角小徽标。kind: accent / green / caution / danger / neutral。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        kind: str = "neutral",
        bg: str | None = None,
        padx: float = 8,
        height: float = 20,
        font: tk.font.Font | None = None,
    ) -> None:
        self.palette = palette
        self._text = text
        self.kind = kind
        self._font = font or fonts.tiny
        self._h = theme.sp(height)
        self._padx = theme.sp(padx)
        self._bg = bg or palette.surface
        self._width = self._font.measure(text) + 2 * self._padx
        super().__init__(
            master,
            width=self._width,
            height=self._h,
            bg=self._bg,
            highlightthickness=0,
            bd=0,
        )
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _colors(self) -> tuple[str, str]:
        pal = self.palette
        if self.kind == "accent":
            return pal.accent_soft, pal.accent
        if self.kind == "green":
            return pal.green_soft, pal.green
        if self.kind == "caution":
            return pal.caution_soft, pal.caution_text
        if self.kind == "danger":
            return pal.danger_soft, pal.danger_text
        return pal.surface_alt, pal.text_secondary

    def set_text(self, text: str) -> None:
        self._text = text
        self._width = self._font.measure(text) + 2 * self._padx
        self.configure(width=self._width)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = self._width
        fill, fg = self._colors()
        rounded_rect(
            self, 0, 0, w, self._h, self._h / 2,
            fill=fill, outline="", width=0,
        )
        self.create_text(
            w / 2, self._h / 2, text=self._text, fill=fg, font=self._font, anchor="center"
        )


# ---------------------------------------------------------------------------
# 可勾选规则树
# ---------------------------------------------------------------------------


class TreeRow(dict):
    """一行数据的描述字典（纯数据，便于排序/重绘）。

    键：iid, kind(cat|rule), values(tuple), tags(tuple)
    """


class RuleTree(tk.Frame):
    """两层 ttk.Treeview：分类父节点 + 规则子节点，表头可排序。"""

    COLUMNS: tuple[tuple[str, str, float, str], ...] = (
        ("check", "", 48, "center"),
        ("name", "名称", 250, "w"),
        ("size", "大小", 110, "e"),
        ("count", "项数", 78, "e"),
        ("risk", "风险", 82, "center"),
        ("desc", "说明", 320, "w"),
    )

    def __init__(
        self,
        master: tk.Misc,
        palette: theme.Palette,
        fonts: theme.Fonts,
        *,
        on_toggle_rule: Callable[[str], None],
        on_toggle_category: Callable[[str], None],
        on_sort: Callable[[str], None],
        on_select_rule: Callable[[str], None] | None = None,
        on_columns_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, bg=palette.surface, bd=0, highlightthickness=0)
        self.palette = palette
        self.fonts = fonts
        self._on_toggle_rule = on_toggle_rule
        self._on_toggle_category = on_toggle_category
        self._on_sort = on_sort
        self._on_select_rule = on_select_rule
        self._on_columns_changed = on_columns_changed
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._last_fit_width = 0
        self._resize_job: str | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        column_ids = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(
            self,
            columns=column_ids,
            show="headings",
            selectmode="browse",
            style="Treeview",
            height=2,  # 请求高度尽量小，由外层 grid 拉伸，避免小窗口把下方内容挤掉
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.scroll.grid(row=0, column=1, sticky="ns", padx=(theme.sp(2), 0))
        self.tree.configure(yscrollcommand=self.scroll.set)

        for col_id, label, width, anchor in self.COLUMNS:
            self.tree.heading(
                col_id,
                text=label,
                anchor=anchor,
                command=lambda c=col_id: self._on_sort(c),
            )
            stretch = col_id == "desc"
            self.tree.column(
                col_id,
                width=theme.sp(width),
                minwidth=theme.sp(44),
                anchor=anchor,
                stretch=stretch,
            )

        self._configure_tags()
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Return>", self._on_space)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_changed)
        self.bind("<Configure>", self._on_frame_configure)
        self.after(200, self._apply_column_layout)

    # -- 列宽自适应 ----------------------------------------------------
    def _on_frame_configure(self, event: tk.Event) -> None:
        if abs(event.width - self._last_fit_width) < theme.sp(20):
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(160, self._apply_column_layout)

    def _apply_column_layout(self) -> None:
        self._resize_job = None
        try:
            changed = self.fit_columns()
        except Exception:
            changed = False
        if changed and self._on_columns_changed is not None:
            self._on_columns_changed()

    def fit_columns(self) -> bool:
        """按当前树宽分配列宽：说明列弹性伸缩，名称列保底。"""
        try:
            avail = int(self.tree.winfo_width())
        except Exception:
            return False
        if avail < theme.sp(240):
            return False
        check = theme.sp(46)
        risk = theme.sp(74)
        count = theme.sp(68)
        size_w = theme.sp(96)
        name = max(theme.sp(150), min(theme.sp(300), int(avail * 0.24)))
        desc = avail - check - risk - count - size_w - name - theme.sp(10)
        if desc < theme.sp(150):
            deficit = theme.sp(150) - desc
            name = max(theme.sp(120), name - deficit)
            desc = avail - check - risk - count - size_w - name - theme.sp(10)
        if desc < theme.sp(90):
            size_w = max(theme.sp(70), size_w - (theme.sp(90) - desc))
            desc = avail - check - risk - count - size_w - name - theme.sp(10)
        desc = max(theme.sp(80), desc)
        widths = {
            "check": check, "name": name, "size": size_w,
            "count": count, "risk": risk, "desc": desc,
        }
        changed = abs(avail - self._last_fit_width) >= theme.sp(20)
        self._last_fit_width = avail
        for column, width in widths.items():
            self.tree.column(column, width=width, minwidth=theme.sp(44))
        return changed

    def desc_content_width(self) -> int:
        """说明列可用的内容宽度（用于省略号截断）。"""
        try:
            width = int(self.tree.column("desc", "width"))
        except Exception:
            width = theme.sp(400)
        return max(theme.sp(80), width - theme.sp(16))

    # -- 样式 ----------------------------------------------------------
    def _configure_tags(self) -> None:
        pal, fonts = self.palette, self.fonts
        t = self.tree
        t.tag_configure("cat", background=pal.header, foreground=pal.text,
                        font=fonts.body_bold)
        t.tag_configure("row", background=pal.surface, foreground=pal.text)
        t.tag_configure("row_alt", background=pal.row_alt, foreground=pal.text)
        t.tag_configure("risk_safe", foreground=pal.green)
        t.tag_configure("risk_caution", foreground=pal.caution_text)
        t.tag_configure("risk_advanced", foreground=pal.danger_text)
        t.tag_configure("report", foreground=pal.text_tertiary)
        t.tag_configure("admin_lock", foreground=pal.caution_text)
        t.tag_configure("empty", foreground=pal.text_tertiary, font=fonts.small)

    def set_sort_indicator(self, column: str | None, reverse: bool = False) -> None:
        self._sort_column = column
        self._sort_reverse = reverse
        for col_id, label, _w, _a in self.COLUMNS:
            if col_id == "check":
                text = ""
            elif col_id == column:
                text = f"{label} {'▼' if reverse else '▲'}"
            else:
                text = label
            self.tree.heading(col_id, text=text)

    # -- 数据 ----------------------------------------------------------
    def set_rows(self, rows: Iterable[TreeRow]) -> None:
        tree = self.tree
        scroll = tree.yview()
        tree.delete(*tree.get_children(""))
        for row in rows:
            tree.insert(
                "",
                "end",
                iid=row["iid"],
                values=tuple(row["values"]),
                tags=tuple(row.get("tags", ())),
            )
        if rows:
            try:
                tree.yview_moveto(scroll[0])
            except Exception:
                pass

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children(""))

    def focus_first(self) -> None:
        children = self.tree.get_children("")
        if children:
            self.tree.focus(children[0])

    # -- 选中与详情 ----------------------------------------------------
    def _on_selection_changed(self, _event: tk.Event | None = None) -> None:
        if self._on_select_rule is None:
            return
        selection = self.tree.selection()
        if selection:
            self._on_select_rule(selection[0])

    def select_iid(self, iid: str) -> bool:
        """按 iid 选中一行（用于换肤/重绘后恢复选中）。"""
        children = self.tree.get_children("")
        if iid not in children:
            return False
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.tree.see(iid)
        except Exception:
            pass
        return True

    def clear_selection(self) -> None:
        selection = self.tree.selection()
        if selection:
            self.tree.selection_remove(*selection)

    # -- 交互 ----------------------------------------------------------
    def _row_key(self, iid: str) -> tuple[str, str]:
        kind, _, key = iid.partition("::")
        return kind, key

    def _on_click(self, event: tk.Event) -> str | None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        column = self.tree.identify_column(event.x)
        if column != "#1":
            return None
        self._toggle(iid)
        return "break"

    def _on_double_click(self, event: tk.Event) -> str | None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self._toggle(iid)
        return "break"

    def _on_space(self, _event: tk.Event) -> str:
        iid = self.tree.focus()
        if iid:
            self._toggle(iid)
        return "break"

    def _on_motion(self, event: tk.Event) -> None:
        inside_check = (
            self.tree.identify_region(event.x, event.y) == "cell"
            and self.tree.identify_column(event.x) == "#1"
            and bool(self.tree.identify_row(event.y))
        )
        cursor = "hand2" if inside_check else ""
        try:
            if self.tree.cget("cursor") != cursor:
                self.tree.configure(cursor=cursor)
        except Exception:
            pass

    def _toggle(self, iid: str) -> None:
        kind, key = self._row_key(iid)
        if kind == "cat":
            self._on_toggle_category(key)
        else:
            self._on_toggle_rule(key)

    def toggle_focused(self) -> None:
        iid = self.tree.focus()
        if iid:
            self._toggle(iid)


# ---------------------------------------------------------------------------
# 分段选择器 / 自定义复选框（设置与弹窗用）
# ---------------------------------------------------------------------------


class SegmentedControl(tk.Frame):
    """一组互斥的圆角按钮，替代 ttk.Radiobutton。"""

    def __init__(
        self,
        master: tk.Misc,
        options: Sequence[tuple[str, str]],
        value: str,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        command: Callable[[str], None] | None = None,
        bg: str | None = None,
        height: float = 32,
        padx: float = 14,
    ) -> None:
        super().__init__(master, bg=bg or palette.surface)
        self.palette = palette
        self.options = list(options)
        self.value = value
        self._command = command
        self.buttons: list[RoundedButton] = []
        for index, (val, label) in enumerate(self.options):
            btn = RoundedButton(
                self,
                label,
                lambda v=val: self.select(v),
                palette=palette,
                fonts=fonts,
                kind="primary" if val == value else "secondary",
                bg=bg or palette.surface,
                height=height,
                padx=padx,
                radius=6,
                font=fonts.small_bold,
            )
            btn.pack(side="left", padx=(0, theme.sp(8) if index < len(self.options) - 1 else 0))
            self.buttons.append(btn)

    def select(self, value: str) -> None:
        if value == self.value:
            return
        self.value = value
        for (val, _label), btn in zip(self.options, self.buttons):
            btn.set_kind("primary" if val == value else "secondary")
        if self._command is not None:
            self._command(value)


class CheckRow(tk.Canvas):
    """自绘复选框 + 文字，整行可点击。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        value: bool = False,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        command: Callable[[bool], None] | None = None,
        bg: str | None = None,
        font: tk.font.Font | None = None,
    ) -> None:
        self.palette = palette
        self._text = text
        self._font = font or fonts.body
        self.checked = bool(value)
        self._command = command
        self._box = theme.sp(18)
        self._bg = bg or palette.surface
        w = self._box + theme.sp(10) + self._font.measure(text)
        h = max(self._box + theme.sp(6), self._font.metrics("linespace") + theme.sp(4))
        super().__init__(master, width=w, height=h, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", lambda _e: self.toggle())
        self._draw()

    def toggle(self) -> None:
        self.set(not self.checked)

    def set(self, checked: bool) -> None:
        self.checked = bool(checked)
        self._draw()
        if self._command is not None:
            self._command(self.checked)

    def _draw(self) -> None:
        self.delete("all")
        h = self.winfo_height() or theme.sp(24)
        if h <= 1:
            h = theme.sp(24)
        b = self._box
        top = (h - b) / 2
        pal = self.palette
        if self.checked:
            rounded_rect(self, 0, top, b, top + b, theme.sp(4),
                         fill=pal.accent, outline=pal.accent, width=1)
            self.create_text(b / 2, top + b / 2 + theme.sp(1), text="✓",
                             fill=pal.on_accent, font=self._font, anchor="center")
        else:
            rounded_rect(self, 0, top, b, top + b, theme.sp(4),
                         fill=pal.surface, outline=pal.border_strong, width=1)
        self.create_text(b + theme.sp(10), h / 2, text=self._text, fill=pal.text,
                         font=self._font, anchor="w")


# ---------------------------------------------------------------------------
# 其他小工具
# ---------------------------------------------------------------------------


class HLine(tk.Frame):
    """1px 分隔线。"""

    def __init__(self, master: tk.Misc, palette: theme.Palette, *,
                 color: str | None = None) -> None:
        super().__init__(master, bg=color or palette.border, height=max(1, theme.sp(1)))
        self.pack_propagate(False)


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"
