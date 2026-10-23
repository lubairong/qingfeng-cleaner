"""清风清理 v2 — 自绘 Squarified Treemap。

纯 tk.Canvas，无第三方依赖：
- 面积与 size 严格成正比（Squarified 算法，200 块 <200ms）；
- 悬停 tooltip（名称/路径/大小/修改时间）；
- 单击选中、双击下钻/打开；顶部标题 + 返回上级；
- 深/浅色两套配色（跟随 theme.Palette）。

输入（契约 §4.1）：list[(name, size, kind, path)]，额外可带 mtime/is_dir。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
import tkinter as tk

from . import theme
from .widgets import rounded_rect


@dataclass
class TreemapItem:
    name: str
    size: int
    kind: str = "file"      # dir/system/installer/video/archive/vm/keep/file...
    path: str = ""
    mtime: float = 0.0
    is_dir: bool = False


# 色板：目录蓝灰、安装包橙、视频紫、镜像深蓝、保留灰、可清缓存青绿
_KIND_COLORS_LIGHT: dict[str, str] = {
    "dir": "#5B7FA8",
    "system": "#64748B",
    "installer": "#D97706",
    "app-version-dup": "#C2410C",
    "backup": "#B45309",
    "devcache": "#0F766E",
    "video": "#7C3AED",
    "archive": "#0E7490",
    "vm": "#1D4ED8",
    "keep": "#6B7280",
    "cleanable": "#0E9F6E",
    "file": "#94A3B8",
}
_KIND_COLORS_DARK: dict[str, str] = {
    "dir": "#6E93BE",
    "system": "#8492A6",
    "installer": "#F59E0B",
    "app-version-dup": "#FB923C",
    "backup": "#F0A63C",
    "devcache": "#2DD4BF",
    "video": "#A78BFA",
    "archive": "#22B8CF",
    "vm": "#60A5FA",
    "keep": "#9CA3AF",
    "cleanable": "#4AD295",
    "file": "#B0BAC6",
}


def _human_size(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(0, n))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} B"


def _fmt_time(ts: float) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return "—"


# ---------------------------------------------------------------------------
# Squarified Treemap 算法
# ---------------------------------------------------------------------------


def _worst(row: list[float], length: float) -> float:
    if not row or length <= 0:
        return float("inf")
    total = sum(row)
    if total <= 0:
        return float("inf")
    side = total / length
    worst_ratio = 1.0
    for area in row:
        if area <= 0:
            continue
        ratio = max(side / (area / side if (area / side) else 1e-9),
                    (area / side) / side)
        worst_ratio = max(worst_ratio, ratio)
    return worst_ratio


def squarify(sizes: list[float], x: float, y: float,
             width: float, height: float) -> list[tuple[float, float, float, float]]:
    """标准 Squarified Treemap：返回与 sizes 等长的矩形 (x,y,w,h)。"""
    rects: list[tuple[float, float, float, float]] = [
        (x, y, 0, 0) for _ in sizes
    ]
    total = sum(s for s in sizes if s > 0)
    if total <= 0 or width <= 0 or height <= 0:
        return rects
    scale = (width * height) / total
    areas = [max(0.0, s) * scale for s in sizes]
    order = list(range(len(areas)))
    remaining = [(i, areas[i]) for i in order if areas[i] > 0]

    def worst_for(row: list[tuple[int, float]], side: float) -> float:
        row_sum = sum(a for _i, a in row)
        if row_sum <= 0 or side <= 0:
            return float("inf")
        side_len = row_sum / side
        worst = 1.0
        for _i, a in row:
            other = a / side_len if side_len else 1e-9
            worst = max(worst, max(side_len / other if other else 1e9,
                                   other / side_len if side_len else 1e9))
        return worst

    cx, cy, cw, ch = x, y, width, height
    row: list[tuple[int, float]] = []
    idx = 0
    while idx < len(remaining) or row:
        if not row:
            row = [remaining[idx]]
            idx += 1
            continue
        short = min(cw, ch)
        current = worst_for(row, short)
        candidate = worst_for(row + [remaining[idx]], short) if idx < len(remaining) else float("inf")
        if idx < len(remaining) and candidate <= current:
            row.append(remaining[idx])
            idx += 1
            continue
        # 布局这一行
        row_sum = sum(a for _i, a in row)
        if cw <= ch:
            # 行横向排列，占据顶部 cw 宽度
            row_h = row_sum / cw if cw else 0
            rx = cx
            for item_idx, area in row:
                rw = area / row_h if row_h else 0
                rects[item_idx] = (rx, cy, max(0.0, rw), max(0.0, row_h))
                rx += rw
            cy += row_h
            ch -= row_h
        else:
            row_w = row_sum / ch if ch else 0
            ry = cy
            for item_idx, area in row:
                rh = area / row_w if row_w else 0
                rects[item_idx] = (cx, ry, max(0.0, row_w), max(0.0, rh))
                ry += rh
            cx += row_w
            cw -= row_w
        row = []
        if cw <= 0.5 or ch <= 0.5:
            break
    return rects


# ---------------------------------------------------------------------------
# 画布组件
# ---------------------------------------------------------------------------


class Treemap(tk.Canvas):
    """Squarified Treemap 画布。"""

    HEADER_HEIGHT = 26

    def __init__(
        self,
        master: tk.Misc,
        palette: theme.Palette,
        fonts: theme.Fonts,
        *,
        on_select: Callable[[TreemapItem], None] | None = None,
        on_open: Callable[[TreemapItem], None] | None = None,
        bg: str | None = None,
        title: str = "",
        on_back: Callable[[], None] | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        super().__init__(
            master,
            bg=bg or palette.surface,
            highlightthickness=0,
            bd=0,
            cursor="arrow",
            width=width if width is not None else theme.sp(320),
            height=height if height is not None else theme.sp(160),
        )
        self.palette = palette
        self.fonts = fonts
        self._on_select = on_select
        self._on_open = on_open
        self._title = title
        self._on_back = on_back
        self._items: list[TreemapItem] = []
        self._rects: list[tuple[TreemapItem, tuple[float, float, float, float]]] = []
        self._selected_path: str | None = None
        self._hover_i: int = -1
        self._last_layout_size: tuple[int, int] = (0, 0)
        self._render_ms = 0.0
        self.bind("<Configure>", lambda _e: self._schedule_redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _e: self._clear_tooltip())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Double-1>", self._on_double_click)
        self._redraw_job: str | None = None

    # -- 数据 ----------------------------------------------------------
    def set_items(
        self,
        items: list[TreemapItem],
        *,
        title: str | None = None,
        on_back: Callable[[], None] | None = None,
    ) -> None:
        if title is not None:
            self._title = title
        if on_back is not None:
            self._on_back = on_back
        # 过滤 0 字节与极小项，按大小降序
        cleaned = []
        for item in items:
            if item.size <= 0 and len(items) > 40:
                continue
            cleaned.append(item)
        cleaned.sort(key=lambda it: it.size, reverse=True)
        self._items = cleaned
        self._selected_path = None
        self._hover_i = -1
        self._last_layout_size = (0, 0)
        self.render()

    def set_title(self, title: str, on_back: Callable[[], None] | None = None) -> None:
        self._title = title
        if on_back is not None:
            self._on_back = on_back
        self.render()

    @property
    def render_ms(self) -> float:
        return self._render_ms

    # -- 绘制 ----------------------------------------------------------
    def _schedule_redraw(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except Exception:
                pass
        self._redraw_job = self.after(30, self.render)

    def _kind_color(self, kind: str) -> str:
        table = _KIND_COLORS_DARK if self.palette.mode == "dark" else _KIND_COLORS_LIGHT
        return table.get(kind, table["file"])

    def render(self) -> None:
        started = time.perf_counter()
        self._redraw_job = None
        if not self.winfo_exists():
            return
        width = int(self.winfo_width())
        height = int(self.winfo_height())
        if width <= 2 or height <= 2:
            return
        self._last_layout_size = (width, height)
        self.delete("all")
        pal = self.palette
        header_h = theme.sp(self.HEADER_HEIGHT) if self._title else 0
        body_y = header_h

        if header_h:
            self.create_rectangle(0, 0, width, header_h, fill=pal.surface, outline="")
            self.create_text(
                theme.sp(10), header_h / 2,
                text=self._title, fill=pal.text, font=self.fonts.small_bold,
                anchor="w", tags="title",
            )
            if self._on_back is not None:
                self.create_text(
                    width - theme.sp(10), header_h / 2,
                    text="↑ 返回上级", fill=pal.accent,
                    font=self.fonts.small_bold, anchor="e", tags="back",
                )
            self.create_line(
                0, header_h, width, header_h,
                fill=pal.border, width=max(1, theme.sp(1)),
            )

        plot_w = width - theme.sp(2)
        plot_h = height - body_y - theme.sp(2)
        if plot_w <= 4 or plot_h <= 4 or not self._items:
            if not self._items:
                self.create_text(
                    width / 2, height / 2, text="该目录没有可展示的内容",
                    fill=pal.text_tertiary, font=self.fonts.small,
                )
            self._render_ms = (time.perf_counter() - started) * 1000
            return

        sizes = [float(item.size) for item in self._items]
        rects = squarify(sizes, theme.sp(1), body_y + theme.sp(1), plot_w, plot_h)
        self._rects = []
        for item, (rx, ry, rw, rh) in zip(self._items, rects):
            if rw < 1 or rh < 1:
                continue
            color = self._kind_color(item.kind)
            selected = bool(self._selected_path and item.path == self._selected_path)
            self.create_rectangle(
                rx, ry, rx + rw, ry + rh,
                fill=color,
                outline=self.palette.accent if selected else self.palette.surface,
                width=theme.sp(2) if selected else max(1, theme.sp(1)),
            )
            # 只给足够大的块画标签，保证 200 块渲染速度
            if rw > theme.sp(54) and rh > theme.sp(20):
                label = item.name
                max_chars = max(2, int(rw / theme.sp(8)))
                if len(label) > max_chars:
                    label = label[: max(1, max_chars - 1)] + "…"
                text_color = "#FFFFFF"
                self.create_text(
                    rx + theme.sp(5), ry + theme.sp(4),
                    text=label, fill=text_color, font=self.fonts.tiny,
                    anchor="nw",
                )
                if rh > theme.sp(34) and rw > theme.sp(70):
                    self.create_text(
                        rx + theme.sp(5), ry + rh - theme.sp(4),
                        text=_human_size(item.size), fill="#E8EEF6",
                        font=self.fonts.tiny, anchor="sw",
                    )
            self._rects.append((item, (rx, ry, rw, rh)))

        self._render_ms = (time.perf_counter() - started) * 1000

    # -- 交互 ----------------------------------------------------------
    def _item_at(self, x: float, y: float) -> tuple[int, TreemapItem | None]:
        for i, (item, (rx, ry, rw, rh)) in enumerate(self._rects):
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return i, item
        return -1, None

    def _on_motion(self, event: tk.Event) -> None:
        header_h = theme.sp(self.HEADER_HEIGHT) if self._title else 0
        if event.y < header_h:
            self._clear_tooltip()
            self.configure(cursor="hand2" if self._on_back is not None else "arrow")
            return
        idx, item = self._item_at(event.x, event.y)
        if idx == self._hover_i:
            return
        self._hover_i = idx
        self._clear_tooltip()
        if item is None:
            self.configure(cursor="arrow")
            return
        self.configure(cursor="hand2")
        self._draw_tooltip(event.x, event.y, item)

    def _clear_tooltip(self) -> None:
        self.delete("tooltip")
        self._hover_i = -1

    def _draw_tooltip(self, x: float, y: float, item: TreemapItem) -> None:
        pal = self.palette
        pad = theme.sp(8)
        lines = [
            item.name,
            _human_size(item.size) + (" · 文件夹" if item.is_dir else " · 文件"),
            item.path,
            "修改时间 " + _fmt_time(item.mtime),
        ]
        font_small = self.fonts.tiny
        widths = []
        for line in lines:
            try:
                widths.append(font_small.measure(line))
            except Exception:
                widths.append(len(line) * theme.sp(7))
        box_w = max(widths) + pad * 2
        line_h = theme.sp(16)
        box_h = line_h * len(lines) + pad * 2
        tx = x + theme.sp(14)
        ty = y + theme.sp(14)
        if tx + box_w > self.winfo_width():
            tx = x - box_w - theme.sp(8)
        if ty + box_h > self.winfo_height():
            ty = y - box_h - theme.sp(8)
        tx = max(theme.sp(2), tx)
        ty = max(theme.sp(2), ty)
        rounded_rect(
            self, tx, ty, tx + box_w, ty + box_h, theme.sp(6),
            fill=pal.surface_alt, outline=pal.border_strong, width=1,
            tags="tooltip",
        )
        for i, line in enumerate(lines):
            color = pal.text if i == 0 else (
                pal.text_secondary if i < 3 else pal.text_tertiary
            )
            fnt = self.fonts.small_bold if i == 0 else font_small
            self.create_text(
                tx + pad, ty + pad + i * line_h,
                text=line, fill=color, font=fnt, anchor="nw", tags="tooltip",
            )

    def _on_click(self, event: tk.Event) -> None:
        header_h = theme.sp(self.HEADER_HEIGHT) if self._title else 0
        if header_h and event.y < header_h:
            if event.x > self.winfo_width() - theme.sp(120) and self._on_back is not None:
                self._on_back()
            return
        _idx, item = self._item_at(event.x, event.y)
        if item is None:
            return
        self._selected_path = item.path
        self.render()
        if self._on_select is not None:
            self._on_select(item)

    def _on_double_click(self, event: tk.Event) -> None:
        header_h = theme.sp(self.HEADER_HEIGHT) if self._title else 0
        if event.y < header_h:
            return
        _idx, item = self._item_at(event.x, event.y)
        if item is not None and self._on_open is not None:
            self._on_open(item)
