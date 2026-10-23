"""清风清理 — 主题化弹窗：二次确认、清理摘要、错误提示、设置。

全部为自绘 Toplevel：沿用主窗口配色与 "Microsoft YaHei UI" 字体，
标题栏跟随深浅色（DWM immersive dark mode）。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence
import tkinter as tk
from tkinter import ttk

from . import theme, widgets
from .engine import Engine, human_size

__all__ = [
    "confirm_clean",
    "show_summary",
    "show_error",
    "show_info",
    "open_settings",
]


def _set_icon(win: tk.Toplevel) -> None:
    try:
        from qingfeng import ICON_PATH

        if ICON_PATH.exists():
            win.iconbitmap(default=str(ICON_PATH))
    except Exception:
        pass


class ThemedDialog(tk.Toplevel):
    """模态、主题化对话框基类。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        palette: theme.Palette,
        fonts: theme.Fonts,
        title: str,
        width: float = 460,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.palette = palette
        self.fonts = fonts
        self.result: Any = None
        self._parent = parent
        self._target_width = theme.sp(width)
        self.title(title)
        self.configure(bg=palette.surface)
        self.resizable(False, False)
        self.transient(parent)
        _set_icon(self)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())
        self.body = tk.Frame(self, bg=palette.surface)
        self.body.pack(fill="both", expand=True, padx=theme.sp(26), pady=theme.sp(22))

    # -- 生命周期 ------------------------------------------------------
    def _fit_size(self) -> tuple[int, int]:
        """让高度跟随内容（自绘卡片的高度是延迟计算的）。"""
        w, h = self._target_width, 0
        for _ in range(4):
            self.update_idletasks()
            w = max(w, self.winfo_reqwidth())
            h = max(h, self.winfo_reqheight())
        return int(w), int(h)

    def _place(self) -> None:
        w, h = self._fit_size()
        sw, sh = theme.screen_size()
        try:
            px, py = self._parent.winfo_rootx(), self._parent.winfo_rooty()
            pw, ph = self._parent.winfo_width(), self._parent.winfo_height()
            if pw <= 1:
                px, py, pw, ph = 0, 0, sw, sh
        except Exception:
            px, py, pw, ph = 0, 0, sw, sh
        x = px + (pw - w) // 2
        y = py + max(theme.sp(30), (ph - h) // 3)
        x = max(theme.sp(8), min(x, sw - w - theme.sp(8)))
        y = max(theme.sp(8), min(y, sh - h - theme.sp(8)))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _refit(self) -> None:
        """窗口映射后内容可能再长高，二次贴合避免按钮被裁掉。"""
        try:
            if not self.winfo_exists():
                return
            need_w, need_h = self._fit_size()
            if need_h > self.winfo_height() + 1 or need_w > self.winfo_width() + 1:
                self._place()
        except Exception:
            pass

    def show(self) -> Any:
        self._place()
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except Exception:
            pass
        theme.set_titlebar_dark(self, self.palette.mode == "dark")
        self.focus_force()
        self.after(60, self._refit)
        self.after(220, self._refit)
        self.wait_window(self)
        return self.result

    def cancel(self) -> None:
        self.result = None if self.result is None else False
        self.destroy()

    def close_with(self, value: Any) -> None:
        self.result = value
        self.destroy()

    # -- 版式组件 ------------------------------------------------------
    def header(self, kind: str, title: str, subtitle: str = "") -> tk.Frame:
        pal, fonts = self.palette, self.fonts
        frame = tk.Frame(self.body, bg=pal.surface)
        frame.pack(fill="x", anchor="w")

        badge_size = theme.sp(42)
        badge = tk.Canvas(
            frame, width=badge_size, height=badge_size,
            bg=pal.surface, highlightthickness=0, bd=0,
        )
        badge.pack(side="left", padx=(0, theme.sp(14)))
        fill, fg, glyph = {
            "warning": (pal.caution_soft, pal.caution_text, "!"),
            "danger": (pal.danger_soft, pal.danger_text, "!"),
            "success": (pal.green_soft, pal.green, "✓"),
            "info": (pal.accent_soft, pal.accent, "i"),
        }.get(kind, (pal.accent_soft, pal.accent, "i"))
        badge.create_oval(0, 0, badge_size - 1, badge_size - 1, fill=fill, outline="")
        badge.create_text(
            badge_size / 2, badge_size / 2 + theme.sp(1),
            text=glyph, fill=fg, font=fonts.h1, anchor="center",
        )

        text_wrap = tk.Frame(frame, bg=pal.surface)
        text_wrap.pack(side="left", fill="x", expand=True)
        tk.Label(
            text_wrap, text=title, bg=pal.surface, fg=pal.text,
            font=fonts.h1, anchor="w", justify="left",
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                text_wrap, text=subtitle, bg=pal.surface, fg=pal.text_secondary,
                font=fonts.small, anchor="w", justify="left",
                wraplength=theme.sp(360),
            ).pack(anchor="w", pady=(theme.sp(3), 0))
        return frame

    def detail_card(self) -> tk.Frame:
        pal = self.palette
        card = widgets.Card(
            self.body, pal, bg=pal.surface, surface=pal.surface_alt,
            radius=10, padding=(16, 10), height=40, autosize=True,
        )
        card.pack(fill="x", pady=(theme.sp(16), theme.sp(4)))
        inner = card.body
        inner.columnconfigure(1, weight=1)
        return inner

    def add_row(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        value: str,
        *,
        value_color: str | None = None,
        value_font: tk.font.Font | None = None,
        value_anchor: str = "e",
    ) -> None:
        pal, fonts = self.palette, self.fonts
        tk.Label(
            parent, text=label, bg=pal.surface_alt, fg=pal.text_secondary,
            font=fonts.small, anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=theme.sp(3))
        tk.Label(
            parent, text=value, bg=pal.surface_alt,
            fg=value_color or pal.text,
            font=value_font or fonts.body_bold,
            anchor=value_anchor, justify="right", wraplength=theme.sp(300),
        ).grid(row=row, column=1, sticky="e", pady=theme.sp(3), padx=(theme.sp(12), 0))

    def button_row(self) -> tk.Frame:
        pal = self.palette
        frame = tk.Frame(self.body, bg=pal.surface)
        frame.pack(fill="x", pady=(theme.sp(20), 0))
        return frame


# ---------------------------------------------------------------------------
# 清理二次确认
# ---------------------------------------------------------------------------


def confirm_clean(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    total_bytes: int,
    file_count: int,
    rule_count: int,
    recycled: bool = True,
    advanced_count: int = 0,
    admin_count: int = 0,
    is_admin: bool = False,
    empties_recycle_bin: bool = False,
    recycle_bin_bytes: int = 0,
    running_programs: Sequence[str] | None = None,
) -> bool:
    """清理前二次确认。返回 True 表示用户确认。

    - recycled=True 时明确写"移入回收站（可随时还原）"；
    - 勾选 recycle-bin 规则时单独警告"永久清空回收站，不可恢复"；
    - running_programs 非空时列出正在运行的相关程序（不阻止清理）。
    """
    dlg = ThemedDialog(
        parent, palette=palette, fonts=fonts, title="确认清理", width=480
    )
    pal = palette
    subtitle_parts = []
    if total_bytes:
        subtitle_parts.append("移入回收站，可随时还原" if recycled
                              else "永久删除，无法恢复")
    if empties_recycle_bin:
        subtitle_parts.append("回收站将被永久清空，不可恢复")
    dlg.header(
        "warning",
        "确认清理以下内容？",
        "；".join(subtitle_parts) + "。所有删除都会经过安全路径检查。"
        if subtitle_parts else "所有删除都会经过安全路径检查。",
    )

    inner = dlg.detail_card()
    row = 0
    if total_bytes:
        if recycled:
            dlg.add_row(inner, row, "移入回收站",
                        f"约 {human_size(total_bytes)}（可随时还原）",
                        value_color=pal.green)
        else:
            dlg.add_row(inner, row, "永久删除",
                        f"约 {human_size(total_bytes)}（不可恢复）",
                        value_color=pal.danger_text)
        row += 1
    if empties_recycle_bin:
        if recycle_bin_bytes > 0:
            size_text = f"约 {human_size(recycle_bin_bytes)}（不可恢复）"
        else:
            size_text = "回收站当前为空（0 B，不可恢复）"
        dlg.add_row(inner, row, "永久清空回收站", size_text,
                    value_color=pal.danger_text)
        row += 1
    dlg.add_row(inner, row, "项目数量", f"{rule_count} 条规则 · {file_count:,} 个文件")
    row += 1
    if advanced_count:
        dlg.add_row(inner, row, "高级风险规则",
                    f"{advanced_count} 条", value_color=pal.danger_text)
        row += 1
    if admin_count:
        if is_admin:
            dlg.add_row(inner, row, "需要管理员", f"{admin_count} 条")
        else:
            dlg.add_row(inner, row, "需要管理员",
                        f"{admin_count} 条（非管理员，将跳过）",
                        value_color=pal.caution_text)

    # 运行中的程序（浏览器 / Office）——只提示，不阻止
    if running_programs:
        warn = tk.Frame(dlg.body, bg=pal.caution_soft)
        warn.pack(fill="x", pady=(theme.sp(12), 0))
        box = tk.Frame(warn, bg=pal.caution_soft)
        box.pack(fill="x", padx=theme.sp(12), pady=theme.sp(9))
        tk.Label(
            box, text=f"检测到 {len(running_programs)} 个程序正在运行",
            bg=pal.caution_soft, fg=pal.caution_text,
            font=fonts.small_bold, anchor="w",
        ).pack(fill="x")
        tk.Label(
            box, text="、".join(running_programs),
            bg=pal.caution_soft, fg=pal.text,
            font=fonts.body, anchor="w", justify="left",
            wraplength=theme.sp(400),
        ).pack(fill="x", pady=(theme.sp(2), 0))
        tk.Label(
            box,
            text="缓存文件可能因占用而删除失败，建议先完全退出这些程序。",
            bg=pal.caution_soft, fg=pal.text_secondary,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(400),
        ).pack(fill="x", pady=(theme.sp(2), 0))

    require_ack = bool(advanced_count) or empties_recycle_bin
    state: dict[str, Any] = {"ack": not require_ack}

    def _toggle(enabled: bool) -> None:
        state["ack"] = enabled
        if "confirm" in state:
            state["confirm"].set_enabled(enabled)

    if require_ack:
        if advanced_count and empties_recycle_bin:
            ack_text = "我了解高级风险项与清空回收站均不可恢复，确认继续"
        elif empties_recycle_bin:
            ack_text = "我了解清空回收站不可恢复，确认继续"
        else:
            ack_text = "我了解上述高级风险项，确认继续清理"
        widgets.CheckRow(
            dlg.body, ack_text, False,
            palette=pal, fonts=fonts, bg=pal.surface, command=_toggle,
        ).pack(fill="x", pady=(theme.sp(12), 0))

    buttons = dlg.button_row()

    def _accept() -> None:
        if state["ack"]:
            dlg.close_with(True)

    confirm_btn = widgets.RoundedButton(
        buttons, "确认清理", _accept,
        palette=pal, fonts=fonts,
        kind="danger" if (not recycled or empties_recycle_bin) else "primary",
        height=36, padx=20,
    )
    confirm_btn.set_enabled(state["ack"])
    confirm_btn.pack(side="right")
    state["confirm"] = confirm_btn
    widgets.RoundedButton(
        buttons, "取消", dlg.cancel,
        palette=pal, fonts=fonts, kind="secondary", height=36, padx=20,
    ).pack(side="right", padx=(0, theme.sp(10)))

    dlg.bind("<Return>", lambda _e: _accept())
    return bool(dlg.show())


# ---------------------------------------------------------------------------
# 清理结果摘要
# ---------------------------------------------------------------------------


def show_summary(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    result,
    recycled: bool = True,
    skipped_count: int = 0,
    emptied_recycle_bin: bool = False,
    recycle_bin_error: str = "",
    recycled_bin_bytes: int = 0,
    icon_cache_cleaned: bool = False,
    permission_failed_count: int = 0,
    on_open_recycle_bin: Callable[[], bool] | None = None,
    on_restart_explorer: Callable[[], bool] | None = None,
    on_restart_admin: Callable[[], bool] | None = None,
) -> None:
    """清理完成后的摘要弹窗。

    回收站模式下必须让用户明白"磁盘空间尚未释放"，并提供打开回收站的入口；
    图标缓存被清理时提示可能需要重启资源管理器。
    """
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts, title="清理完成", width=470)
    pal = palette
    failed = list(getattr(result, "failed", []) or [])
    files = int(getattr(result, "deleted_files", 0))
    freed = int(getattr(result, "freed_bytes", 0))
    if recycled and emptied_recycle_bin:
        subtitle = f"已处理 {files:,} 个文件，并永久清空回收站。"
    elif recycled:
        subtitle = f"已使用「回收站」方式处理 {files:,} 个文件。"
    else:
        subtitle = f"已永久删除 {files:,} 个文件。"
    if emptied_recycle_bin and files == 0:
        subtitle = "回收站已永久清空。"
    dlg.header("success", "清理完成", subtitle)

    # -- 关键数字（区分"移入回收站"与"真正释放"）---------------------
    total_released = freed + (recycled_bin_bytes if emptied_recycle_bin else 0)
    if recycled and not emptied_recycle_bin and freed > 0:
        headline = ("已移入回收站", human_size(freed), pal.accent,
                    "磁盘可用空间尚未变化；清空回收站后才会真正释放。")
    elif (not recycled or emptied_recycle_bin) and total_released > 0:
        headline = ("已释放空间", human_size(total_released), pal.green, "")
    elif emptied_recycle_bin:
        headline = ("回收站已永久清空", human_size(0), pal.green, "")
    else:
        headline = ("完成处理", human_size(0), pal.text_secondary, "")
    total_frame = tk.Frame(dlg.body, bg=pal.surface)
    total_frame.pack(fill="x", pady=(theme.sp(18), 0))
    tk.Label(
        total_frame, text=headline[0], bg=pal.surface, fg=pal.text_secondary,
        font=fonts.small, anchor="w",
    ).pack(anchor="w")
    tk.Label(
        total_frame, text=headline[1], bg=pal.surface, fg=headline[2],
        font=fonts.num_lg, anchor="w",
    ).pack(anchor="w", pady=(theme.sp(2), 0))
    if headline[3]:
        tk.Label(
            total_frame, text=headline[3], bg=pal.surface, fg=pal.caution_text,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(420),
        ).pack(anchor="w", pady=(theme.sp(2), 0))

    inner = dlg.detail_card()
    row = 0
    dlg.add_row(inner, row, "处理文件", f"{files:,} 个")
    row += 1
    dlg.add_row(inner, row, "处理目录", f"{getattr(result, 'deleted_dirs', 0):,} 个")
    row += 1
    dlg.add_row(inner, row, "耗时", f"{float(getattr(result, 'duration', 0.0)):.1f} 秒")
    row += 1
    if skipped_count:
        dlg.add_row(inner, row, "跳过规则",
                    f"{skipped_count} 条（需要管理员权限）",
                    value_color=pal.caution_text)
        row += 1
    if recycled and not emptied_recycle_bin and freed > 0:
        dlg.add_row(inner, row, "空间释放",
                    "需清空回收站后生效", value_color=pal.caution_text)
        row += 1
    if emptied_recycle_bin:
        dlg.add_row(inner, row, "清空回收站", "已永久清空", value_color=pal.green)
        row += 1
    elif recycle_bin_error:
        dlg.add_row(inner, row, "清空回收站", "失败", value_color=pal.danger_text)
        row += 1
    if failed:
        dlg.add_row(inner, row, "失败项", f"{len(failed)} 个", value_color=pal.danger_text)
        preview = "；".join(str(p) for p, _err in failed[:3])
        tk.Label(
            dlg.body,
            text=f"部分文件无法删除（可能被占用）：{preview}",
            bg=pal.surface, fg=pal.danger_text, font=fonts.tiny,
            anchor="w", justify="left", wraplength=theme.sp(420),
        ).pack(fill="x", pady=(theme.sp(8), 0))

    # -- 权限失败 / 跳过：提权重试入口 -------------------------------
    if permission_failed_count:
        perm = tk.Frame(dlg.body, bg=pal.danger_soft)
        perm.pack(fill="x", pady=(theme.sp(12), 0))
        perm_inner = tk.Frame(perm, bg=pal.danger_soft)
        perm_inner.pack(fill="x", padx=theme.sp(12), pady=theme.sp(9))
        tk.Label(
            perm_inner,
            text=f"有 {permission_failed_count} 项因权限失败 / 跳过",
            bg=pal.danger_soft, fg=pal.danger_text,
            font=fonts.small_bold, anchor="w",
        ).pack(fill="x")
        tk.Label(
            perm_inner,
            text="系统临时文件、更新缓存、CBS 日志等需要管理员权限；"
                 "重启到管理员模式后再试一次即可处理这些项目。",
            bg=pal.danger_soft, fg=pal.text_secondary,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(380),
        ).pack(fill="x", pady=(theme.sp(2), theme.sp(6)))
        retry_btn = widgets.RoundedButton(
            perm_inner, "以管理员身份重启并重试", None,
            palette=pal, fonts=fonts, kind="primary", height=30, padx=14,
            bg=pal.danger_soft, font=fonts.small_bold,
        )
        retry_btn.pack(anchor="w")

        def _do_restart_admin() -> None:
            if on_restart_admin is None:
                retry_btn.set_text("不可用")
                retry_btn.set_enabled(False)
                return
            dlg.close_with(True)
            try:
                on_restart_admin()
            except Exception:
                pass

        retry_btn._command = _do_restart_admin  # noqa: SLF001

    # -- 图标缓存提示 + 一键重启资源管理器 ----------------------------
    restart_btn = None
    restart_hint = None
    if icon_cache_cleaned:
        hint = tk.Frame(dlg.body, bg=pal.caution_soft)
        hint.pack(fill="x", pady=(theme.sp(12), 0))
        hint_inner = tk.Frame(hint, bg=pal.caution_soft)
        hint_inner.pack(fill="x", padx=theme.sp(12), pady=theme.sp(9))
        tk.Label(
            hint_inner, text="缩略图 / 图标缓存已清理",
            bg=pal.caution_soft, fg=pal.caution_text,
            font=fonts.small_bold, anchor="w",
        ).pack(fill="x")
        restart_hint = tk.Label(
            hint_inner,
            text="资源管理器可能显示白色图标，建议重启资源管理器以重建缓存。",
            bg=pal.caution_soft, fg=pal.text_secondary,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(360),
        )
        restart_hint.pack(fill="x", pady=(theme.sp(2), theme.sp(6)))
        restart_btn = widgets.RoundedButton(
            hint_inner, "重启资源管理器", None,
            palette=pal, fonts=fonts, kind="secondary", height=30, padx=14,
            bg=pal.caution_soft, font=fonts.small_bold,
        )
        restart_btn.pack(anchor="w")
        if on_restart_explorer is not None:
            def _do_restart() -> None:
                try:
                    ok = bool(on_restart_explorer())
                except Exception:
                    ok = False
                if ok:
                    restart_btn.set_text("正在重启…")
                    restart_btn.set_enabled(False)
                    if restart_hint is not None:
                        restart_hint.configure(text="已发送重启命令，桌面会短暂刷新。")
                else:
                    restart_btn.set_text("重启失败")
            restart_btn._command = _do_restart  # noqa: SLF001 - 同包内部协作
        else:
            restart_btn.set_enabled(False)

    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "完成", lambda: dlg.close_with(True),
        palette=pal, fonts=fonts, kind="primary", height=36, padx=26,
    ).pack(side="right")
    if recycled and on_open_recycle_bin is not None:
        open_btn = widgets.RoundedButton(
            buttons, "打开回收站", None,
            palette=pal, fonts=fonts, kind="secondary", height=36, padx=20,
        )
        open_btn.pack(side="left")

        def _do_open() -> None:
            try:
                ok = bool(on_open_recycle_bin())
            except Exception:
                ok = False
            open_btn.set_text("已打开回收站" if ok else "打开失败")

        open_btn._command = _do_open  # noqa: SLF001
    dlg.bind("<Return>", lambda _e: dlg.close_with(True))
    dlg.show()


# ---------------------------------------------------------------------------
# 信息 / 错误
# ---------------------------------------------------------------------------


def show_info(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    title: str,
    message: str,
    detail: str = "",
) -> None:
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts, title=title, width=420)
    dlg.header("info", title)
    tk.Label(
        dlg.body, text=message, bg=palette.surface, fg=palette.text,
        font=fonts.body, anchor="w", justify="left", wraplength=theme.sp(380),
    ).pack(fill="x", pady=(theme.sp(14), 0))
    if detail:
        tk.Label(
            dlg.body, text=detail, bg=palette.surface, fg=palette.text_tertiary,
            font=fonts.tiny, anchor="w", justify="left", wraplength=theme.sp(380),
        ).pack(fill="x", pady=(theme.sp(6), 0))
    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "知道了", lambda: dlg.close_with(True),
        palette=palette, fonts=fonts, kind="primary", height=34, padx=22,
    ).pack(side="right")
    dlg.bind("<Return>", lambda _e: dlg.close_with(True))
    dlg.show()


def show_error(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    title: str,
    message: str,
    detail: str = "",
) -> None:
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts, title=title, width=440)
    dlg.header("danger", title)
    tk.Label(
        dlg.body, text=message, bg=palette.surface, fg=palette.text,
        font=fonts.body, anchor="w", justify="left", wraplength=theme.sp(390),
    ).pack(fill="x", pady=(theme.sp(14), 0))
    if detail:
        box = tk.Frame(dlg.body, bg=palette.surface_alt)
        box.pack(fill="x", pady=(theme.sp(10), 0))
        tk.Label(
            box, text=detail, bg=palette.surface_alt, fg=palette.text_secondary,
            font=fonts.mono, anchor="w", justify="left",
            wraplength=theme.sp(390), padx=theme.sp(10), pady=theme.sp(8),
        ).pack(fill="x")
    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "关闭", lambda: dlg.close_with(True),
        palette=palette, fonts=fonts, kind="primary", height=34, padx=22,
    ).pack(side="right")
    dlg.show()


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------


def open_settings(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    settings: dict,
    engine: Engine | None = None,
) -> dict | None:
    """返回新的设置字典；取消返回 None。"""
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts, title="设置", width=520)
    pal = palette
    dlg.header("info", "设置", "偏好会尽量保存，下次启动继续生效。")

    state = {
        "theme": settings.get("theme", "auto"),
        "delete_mode": settings.get("delete_mode", "recycle"),
        "minimize_to_tray": bool(settings.get("minimize_to_tray", True)),
        "scan_on_start": bool(settings.get("scan_on_start", True)),
    }

    # -- 外观 ----------------------------------------------------------
    _section(dlg.body, "外观", pal, fonts)
    widgets.SegmentedControl(
        dlg.body,
        [("auto", "跟随系统"), ("light", "浅色"), ("dark", "深色")],
        state["theme"],
        palette=pal,
        fonts=fonts,
        command=lambda v: state.__setitem__("theme", v),
    ).pack(anchor="w", pady=(theme.sp(4), 0))

    # -- 清理方式 ------------------------------------------------------
    _section(dlg.body, "清理方式", pal, fonts)
    widgets.SegmentedControl(
        dlg.body,
        [("recycle", "移到回收站（安全）"), ("delete", "永久删除（不可恢复）")],
        state["delete_mode"],
        palette=pal,
        fonts=fonts,
        command=lambda v: state.__setitem__("delete_mode", v),
    ).pack(anchor="w", pady=(theme.sp(4), 0))
    tk.Label(
        dlg.body,
        text="永久删除不经回收站，清理前的确认框会再次提醒。",
        bg=pal.surface, fg=pal.text_tertiary, font=fonts.tiny, anchor="w",
    ).pack(fill="x", pady=(theme.sp(4), 0))

    # -- 窗口行为 ------------------------------------------------------
    _section(dlg.body, "窗口行为", pal, fonts)
    widgets.CheckRow(
        dlg.body, "关闭主窗口时最小化到系统托盘", state["minimize_to_tray"],
        palette=pal, fonts=fonts, bg=pal.surface,
        command=lambda v: state.__setitem__("minimize_to_tray", v),
    ).pack(anchor="w", pady=(theme.sp(3), theme.sp(4)))
    widgets.CheckRow(
        dlg.body, "启动后自动扫描一次", state["scan_on_start"],
        palette=pal, fonts=fonts, bg=pal.surface,
        command=lambda v: state.__setitem__("scan_on_start", v),
    ).pack(anchor="w", pady=(theme.sp(3), 0))

    # -- 关于 ----------------------------------------------------------
    _section(dlg.body, "关于", pal, fonts)
    try:
        from qingfeng import APP_NAME, __version__

        about = f"{APP_NAME} v{__version__} · 仅在本机运行，不上传任何数据"
    except Exception:
        about = "清风清理 · 仅在本机运行，不上传任何数据"
    tk.Label(
        dlg.body, text=about, bg=pal.surface, fg=pal.text_secondary,
        font=fonts.small, anchor="w", justify="left", wraplength=theme.sp(460),
    ).pack(fill="x", pady=(theme.sp(2), 0))
    if engine is not None:
        tk.Label(
            dlg.body, text=engine.status_detail, bg=pal.surface, fg=pal.text_tertiary,
            font=fonts.tiny, anchor="w", justify="left", wraplength=theme.sp(460),
        ).pack(fill="x", pady=(theme.sp(2), 0))

    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "保存", lambda: dlg.close_with(dict(state)),
        palette=pal, fonts=fonts, kind="primary", height=36, padx=24,
    ).pack(side="right")
    widgets.RoundedButton(
        buttons, "取消", dlg.cancel,
        palette=pal, fonts=fonts, kind="secondary", height=36, padx=22,
    ).pack(side="right", padx=(0, theme.sp(10)))
    dlg.bind("<Return>", lambda _e: dlg.close_with(dict(state)))
    result = dlg.show()
    return dict(result) if result else None


def _section(parent: tk.Misc, text: str, pal: theme.Palette, fonts: theme.Fonts) -> None:
    tk.Label(
        parent, text=text, bg=pal.surface, fg=pal.text_secondary,
        font=fonts.small_bold, anchor="w",
    ).pack(fill="x", pady=(theme.sp(16), 0))


# ---------------------------------------------------------------------------
# v2 空间透视：回收站 / 迁移 / 迁移结果
# ---------------------------------------------------------------------------


def _names_preview(names: Sequence[str], limit: int = 4) -> str:
    names = list(names)
    if not names:
        return "—"
    shown = "、".join(names[:limit])
    if len(names) > limit:
        shown += f" 等 {len(names)} 项"
    return shown


def confirm_recycle_items(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    item_names: Sequence[str],
    total_bytes: int,
    file_count: int = 0,
    keep_count: int = 0,
) -> bool:
    """大文件决策区的「移入回收站」二次确认。"""
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts,
                       title="移入回收站", width=480)
    pal = palette
    dlg.header(
        "warning",
        "确认把所选项目移入回收站？",
        "删除可撤销：清空回收站之前，都可以还原。",
    )
    inner = dlg.detail_card()
    dlg.add_row(inner, 0, "移入回收站",
                f"约 {human_size(total_bytes)}（可随时还原）",
                value_color=pal.green)
    dlg.add_row(inner, 1, "项目数量",
                f"{len(item_names)} 项" + (f" · {file_count:,} 个文件" if file_count else ""))
    dlg.add_row(inner, 2, "项目预览", _names_preview(item_names),
                value_color=pal.text_secondary, value_font=fonts.small)
    if keep_count:
        dlg.add_row(inner, 3, "建议保留项",
                    f"{keep_count} 项（请再次确认不需要）",
                    value_color=pal.danger_text)
    tk.Label(
        dlg.body,
        text="提示：移入回收站不会立即释放磁盘空间，清空回收站后才会真正释放。",
        bg=pal.surface, fg=pal.text_tertiary, font=fonts.tiny,
        anchor="w", justify="left", wraplength=theme.sp(420),
    ).pack(fill="x", pady=(theme.sp(10), 0))
    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "移入回收站", lambda: dlg.close_with(True),
        palette=pal, fonts=fonts, kind="danger", height=36, padx=20,
    ).pack(side="right")
    widgets.RoundedButton(
        buttons, "取消", dlg.cancel,
        palette=pal, fonts=fonts, kind="secondary", height=36, padx=20,
    ).pack(side="right", padx=(0, theme.sp(10)))
    dlg.bind("<Return>", lambda _e: dlg.close_with(True))
    return bool(dlg.show())


def confirm_move(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    item_names: Sequence[str],
    total_bytes: int,
    file_count: int,
    source_root: str,
    dest_root: str,
    target_free: int,
    estimate_minutes: float,
    keep_count: int = 0,
    skipped_items: Sequence[tuple[str, str]] | None = None,
) -> bool:
    """迁移到其它盘的二次确认（复制成功后才删除源）。"""
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts,
                       title="迁移确认", width=540)
    pal = palette
    dlg.header(
        "warning",
        "确认迁移所选项目？",
        "跨盘复制并校验成功后，才会删除源文件。",
    )
    inner = dlg.detail_card()
    dlg.add_row(inner, 0, "源", source_root, value_font=fonts.small,
                value_color=pal.text_secondary)
    dlg.add_row(inner, 1, "目标", dest_root, value_font=fonts.small,
                value_color=pal.accent)
    dlg.add_row(inner, 2, "文件数 / 大小",
                f"{file_count:,} 个 · {human_size(total_bytes)}")
    dlg.add_row(inner, 3, "目标剩余空间", human_size(target_free))
    dlg.add_row(inner, 4, "预计耗时",
                f"约 {estimate_minutes:.1f} 分钟（按 300 MB/s 估算）")
    if keep_count:
        dlg.add_row(inner, 5, "建议保留项",
                    f"{keep_count} 项（请再次确认不需要）",
                    value_color=pal.danger_text)
    dlg.add_row(inner, 6, "项目预览", _names_preview(item_names),
                value_font=fonts.small, value_color=pal.text_secondary)
    if skipped_items:
        skip = tk.Frame(dlg.body, bg=pal.caution_soft)
        skip.pack(fill="x", pady=(theme.sp(10), 0))
        skip_inner = tk.Frame(skip, bg=pal.caution_soft)
        skip_inner.pack(fill="x", padx=theme.sp(12), pady=theme.sp(8))
        tk.Label(
            skip_inner,
            text=f"将跳过 {len(skipped_items)} 项（不可迁移）",
            bg=pal.caution_soft, fg=pal.caution_text,
            font=fonts.small_bold, anchor="w", justify="left",
        ).pack(fill="x")
        skip_preview = "；".join(
            f"{name}（{reason}）" for name, reason in list(skipped_items)[:4]
        )
        if len(skipped_items) > 4:
            skip_preview += f" 等 {len(skipped_items)} 项"
        tk.Label(
            skip_inner, text=skip_preview,
            bg=pal.caution_soft, fg=pal.text_secondary,
            font=fonts.tiny, anchor="w", justify="left",
            wraplength=theme.sp(450),
        ).pack(fill="x", pady=(theme.sp(2), 0))

    warn = tk.Frame(dlg.body, bg=pal.caution_soft)
    warn.pack(fill="x", pady=(theme.sp(12), 0))
    tk.Label(
        warn,
        text="复制成功后才删除源文件；取消时已完成的保留、未完成的源文件不动。",
        bg=pal.caution_soft, fg=pal.caution_text, font=fonts.small_bold,
        anchor="w", justify="left", wraplength=theme.sp(470),
        padx=theme.sp(12), pady=theme.sp(8),
    ).pack(fill="x")

    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "开始迁移", lambda: dlg.close_with(True),
        palette=pal, fonts=fonts, kind="primary", height=36, padx=22,
    ).pack(side="right")
    widgets.RoundedButton(
        buttons, "取消", dlg.cancel,
        palette=pal, fonts=fonts, kind="secondary", height=36, padx=20,
    ).pack(side="right", padx=(0, theme.sp(10)))
    dlg.bind("<Return>", lambda _e: dlg.close_with(True))
    return bool(dlg.show())


def show_move_summary(
    parent: tk.Misc,
    *,
    palette: theme.Palette,
    fonts: theme.Fonts,
    result,
    target_root: str,
    on_open_target: Callable[[], bool] | None = None,
) -> None:
    """迁移完成摘要：成功/跳过/失败，失败原因可展开，可打开目标目录。"""
    dlg = ThemedDialog(parent, palette=palette, fonts=fonts,
                       title="迁移完成", width=520)
    pal = palette
    moved = int(getattr(result, "moved", 0))
    moved_bytes = int(getattr(result, "moved_bytes", 0))
    skipped = list(getattr(result, "skipped", []) or [])
    failed = list(getattr(result, "failed", []) or [])
    ok = not failed
    dlg.header(
        "success" if ok else "warning",
        "迁移完成",
        f"已成功迁移 {moved} 项 · {human_size(moved_bytes)} 到目标目录。",
    )
    inner = dlg.detail_card()
    dlg.add_row(inner, 0, "成功", f"{moved} 项 · {human_size(moved_bytes)}",
                value_color=pal.green)
    dlg.add_row(inner, 1, "跳过", f"{len(skipped)} 项",
                value_color=pal.caution_text if skipped else None)
    dlg.add_row(inner, 2, "失败", f"{len(failed)} 项",
                value_color=pal.danger_text if failed else None)
    dlg.add_row(inner, 3, "耗时",
                f"{float(getattr(result, 'duration', 0.0)):.1f} 秒")
    dlg.add_row(inner, 4, "目标目录", target_root, value_font=fonts.small,
                value_color=pal.text_secondary)

    details_box = tk.Frame(dlg.body, bg=pal.surface)
    text = tk.Text(
        details_box, height=7, wrap="word", bg=pal.surface_alt, fg=pal.text,
        font=fonts.mono, relief="flat", bd=0, highlightthickness=1,
        highlightbackground=pal.border, padx=theme.sp(8), pady=theme.sp(6),
    )
    reasons: list[str] = []
    for path, reason in skipped:
        reasons.append(f"[跳过] {path} — {reason}")
    for path, reason in failed:
        reasons.append(f"[失败] {path} — {reason}")
    text.insert("1.0", "\n".join(reasons) if reasons else "没有跳过或失败的项目。")
    text.configure(state="disabled")
    scroll = ttk.Scrollbar(details_box, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    state = {"expanded": False}

    def _toggle_details() -> None:
        state["expanded"] = not state["expanded"]
        if state["expanded"]:
            details_box.pack(fill="x", pady=(theme.sp(8), 0))
            toggle_btn.set_text("收起详情")
        else:
            details_box.pack_forget()
            toggle_btn.set_text(f"展开详情（{len(reasons)}）")
        dlg.update_idletasks()
        dlg._place()  # noqa: SLF001 - 调整弹窗高度

    buttons = dlg.button_row()
    widgets.RoundedButton(
        buttons, "完成", lambda: dlg.close_with(True),
        palette=pal, fonts=fonts, kind="primary", height=36, padx=26,
    ).pack(side="right")
    if on_open_target is not None:
        open_btn = widgets.RoundedButton(
            buttons, "打开目标目录", lambda: None,
            palette=pal, fonts=fonts, kind="secondary", height=36, padx=18,
        )
        open_btn.pack(side="left")

        def _open() -> None:
            try:
                ok_open = bool(on_open_target())
            except Exception:
                ok_open = False
            open_btn.set_text("已打开" if ok_open else "打开失败")

        open_btn._command = _open  # noqa: SLF001
    if reasons:
        toggle_btn = widgets.RoundedButton(
            dlg.body, f"展开详情（{len(reasons)}）", _toggle_details,
            palette=pal, fonts=fonts, kind="link", bg=pal.surface,
            height=28, padx=8, font=fonts.small,
        )
        toggle_btn.pack(anchor="w", pady=(theme.sp(6), 0))
    dlg.bind("<Return>", lambda _e: dlg.close_with(True))
    dlg.show()
