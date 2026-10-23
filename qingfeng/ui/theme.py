"""清风清理 — 主题、字体、DPI 与 ttk 样式。

设计目标：Windows 11 / Fluent 风格的浅色界面（默认）与深色界面（跟随系统）。
本模块只依赖标准库 + tkinter，不导入 Pillow。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from types import SimpleNamespace
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DPI / 缩放
# ---------------------------------------------------------------------------

DPI = 96
SCALE = 1.0
_geometry_scale = 1.0   # 每个 Tk 几何单位对应的物理像素（用于窗口定位/尺寸校验）
_frame_w = 0            # 窗口外框与客户区宽度差（物理像素）
_frame_h = 0
_dpi_ready = False

FONT_FAMILY_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "Tahoma",
)
_family = "Microsoft YaHei UI"


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _init_winapi() -> None:
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    except Exception:
        pass


def setup_dpi() -> float:
    """在创建 Tk 之前调用：声明 DPI 感知并给出初始缩放（创建窗口后可用 refine_dpi 校正）。"""
    global DPI, SCALE, _dpi_ready
    if _dpi_ready:
        return SCALE
    _dpi_ready = True
    if sys.platform != "win32":
        return SCALE
    _init_winapi()
    try:
        # 1 = PROCESS_SYSTEM_DPI_AWARE；Tk 8.6 对系统级 DPI 支持最稳
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    dpi = 96
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            dc = user32.GetDC(0)
            dpi = int(gdi32.GetDeviceCaps(dc, 88))  # LOGPIXELSX
            user32.ReleaseDC(0, dc)
        except Exception:
            dpi = 96
    if 72 <= dpi <= 480:
        DPI = dpi
    SCALE = max(0.75, min(3.0, DPI / 96.0))
    return SCALE


def refine_dpi(root: tk.Misc) -> float:
    """Tk 创建后调用：以 Tk 自身的 DPI/几何单位为准校正缩放。

    打包/不同启动方式下 ``GetDeviceCaps`` 可能给出错误 DPI（例如 175% 屏
    却返回 96），导致窗口按 1x 尺寸创建、显得又小又挤。这里用
    ``winfo_fpixels('1i')``（Tk 单位/英寸）校正，并测量「1 个 Tk 单位
    对应多少物理像素」，同时得到窗口外框增量用于居中/贴边校验。
    """
    global DPI, SCALE, _geometry_scale, _frame_w, _frame_h, _dpi_ready
    _dpi_ready = True
    if sys.platform != "win32":
        return SCALE
    _init_winapi()
    tk_dpi = float(DPI or 96)
    try:
        tk_dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        pass
    if not (72 <= tk_dpi <= 480):
        tk_dpi = float(DPI or 96)

    # 用一次隐藏窗口的几何探测，求 Tk 单位->物理像素比例与外框增量
    try:
        root.geometry("400x300+0+0")
        root.update_idletasks()
        hwnd = _toplevel_hwnd(root)
        if hwnd:
            client = _RECT()
            outer = _RECT()
            ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(client))
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(outer))
            cw = max(1, client.right - client.left)
            ch = max(1, client.bottom - client.top)
            _geometry_scale = max(0.5, min(3.0, cw / 400.0))
            _frame_w = max(0, (outer.right - outer.left) - cw)
            _frame_h = max(0, (outer.bottom - outer.top) - ch)
    except Exception:
        logger.debug("几何单位探测失败，按 1:1 处理", exc_info=True)
        _geometry_scale = 1.0
        _frame_w = 0
        _frame_h = 0

    # Tk 的 winfo_fpixels 已经是「Tk 单位/英寸」，设计尺寸按 Tk 单位计
    DPI = int(round(tk_dpi))
    SCALE = max(0.75, min(3.0, tk_dpi / 96.0))
    logger.info(
        "DPI 校正：tk_dpi=%s geometry_scale=%.3f frame=%sx%s scale=%.3f",
        DPI, _geometry_scale, _frame_w, _frame_h, SCALE,
    )
    return SCALE


def _toplevel_hwnd(root: tk.Misc) -> int | None:
    try:
        user32 = ctypes.windll.user32
        child = root.winfo_id()
        parent = user32.GetParent(ctypes.c_void_p(child))
        return int(parent or child)
    except Exception:
        return None


def sp(px: float) -> int:
    """把 96 DPI 下的逻辑像素换算成 Tk 单位。"""
    return max(1, int(round(px * SCALE)))


def screen_size() -> tuple[int, int]:
    """屏幕尺寸（与当前进程 DPI 感知一致的单位）。"""
    try:
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def work_area() -> tuple[int, int, int, int]:
    """返回工作区 (left, top, right, bottom)，自动排除任务栏（SPI_GETWORKAREA）。"""
    if sys.platform == "win32":
        try:
            rect = _RECT()
            ok = ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0  # SPI_GETWORKAREA
            )
            if ok and rect.right > rect.left + 100 and rect.bottom > rect.top + 100:
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            pass
    sw, sh = screen_size()
    # 兜底：预留约 6% 给任务栏
    return 0, 0, sw, sh - int(sh * 0.06)


def fit_window(root: tk.Misc, width: int, height: int) -> dict[str, int]:
    """把 *客户区* 尺寸为 width x height 的窗口放到工作区中央并保证完整可见。

    尺寸单位与 Tk 几何单位一致（系统 DPI 感知下通常等于物理像素）；
    返回实际外框/工作区数据，便于日志与验证。
    """
    left, top, right, bottom = work_area()
    avail_w = max(320, right - left)
    avail_h = max(240, bottom - top)
    client_w = max(320, min(int(width), avail_w - _frame_w - 8))
    client_h = max(240, min(int(height), avail_h - _frame_h - 8))
    outer_w = client_w + _frame_w
    outer_h = client_h + _frame_h
    x = left + max(0, (avail_w - outer_w) // 2)
    y = top + max(0, (avail_h - outer_h) // 2)
    if y + outer_h > bottom:
        y = bottom - outer_h
    if x + outer_w > right:
        x = right - outer_w
    x = max(left, x)
    y = max(top, y)
    root.geometry(f"{client_w}x{client_h}+{x}+{y}")
    try:
        root.update_idletasks()
        hwnd = _toplevel_hwnd(root)
        if hwnd:
            rect = _RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            # 二次校正：外框越界时上移/左移
            dx = dy = 0
            if rect.bottom > bottom:
                dy = bottom - rect.bottom
            if rect.right > right:
                dx = right - rect.right
            if rect.left + dx < left:
                dx = left - rect.left
            if rect.top + dy < top:
                dy = top - rect.top
            if dx or dy:
                x += dx
                y += dy
                root.geometry(f"+{x}+{y}")
                root.update_idletasks()
    except Exception:
        pass
    info = {
        "work_left": left, "work_top": top, "work_right": right,
        "work_bottom": bottom, "client_w": client_w, "client_h": client_h,
        "outer_w": outer_w, "outer_h": outer_h,
        "x": x, "y": y,
    }
    logger.info("窗口适配：%s", info)
    return info


def apply_tk_scaling(root: tk.Misc) -> None:
    try:
        # 使用 Tk 自己的 DPI（单位一致，避免在高 DPI 下二次放大）
        root.tk.call("tk", "scaling", DPI / 72.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """一套完整配色。字段语义见 light/dark 实例。"""

    mode: str
    window: str            # 窗口底色
    surface: str           # 卡片
    surface_alt: str       # 卡片内的次级区域
    border: str            # 细分隔线
    border_strong: str     # 控件描边
    text: str              # 主文字
    text_secondary: str    # 次级文字
    text_tertiary: str     # 弱化文字 / 占位
    accent: str            # 主色
    accent_hover: str
    accent_pressed: str
    on_accent: str         # 主色上的文字
    accent_soft: str       # 主色淡底
    green: str
    green_soft: str
    caution: str
    caution_soft: str
    caution_text: str      # 橙色文字（风险标签）
    danger: str
    danger_hover: str
    danger_pressed: str
    danger_soft: str
    danger_text: str       # 红色文字（风险标签）
    selected: str          # 列表选中行底色
    hover: str             # 悬停底色
    header: str            # 表头 / 分类行底色
    trough: str            # 进度条 / 滚动槽
    ring_track: str        # 圆环轨道
    row_alt: str           # 斑马纹
    disabled_bg: str
    disabled_fg: str
    scrollbar: str


LIGHT = Palette(
    mode="light",
    window="#F3F3F3",
    surface="#FFFFFF",
    surface_alt="#F8F9FA",
    border="#E3E3E3",
    border_strong="#D4D4D4",
    text="#1B1B1B",
    text_secondary="#5C5C5C",
    text_tertiary="#8B8B8B",
    accent="#0F6CBD",
    accent_hover="#115EA3",
    accent_pressed="#0B4C87",
    on_accent="#FFFFFF",
    accent_soft="#E8F1FB",
    green="#0E9F6E",
    green_soft="#E4F6EE",
    caution="#D97706",
    caution_soft="#FFF4E0",
    caution_text="#A85B00",
    danger="#C42B1C",
    danger_hover="#A8261A",
    danger_pressed="#8A1F15",
    danger_soft="#FCE9E7",
    danger_text="#B3261E",
    selected="#EAF2FB",
    hover="#F4F8FD",
    header="#F7F8FA",
    trough="#E6E8EB",
    ring_track="#EAECEF",
    row_alt="#FCFCFD",
    disabled_bg="#F0F1F3",
    disabled_fg="#A9ADB3",
    scrollbar="#C9CDD2",
)

DARK = Palette(
    mode="dark",
    window="#1E1E1E",
    surface="#282828",
    surface_alt="#2E2E2E",
    border="#3A3A3A",
    border_strong="#4A4A4A",
    text="#F5F5F5",
    text_secondary="#C4C4C4",
    text_tertiary="#909090",
    accent="#3B9EFF",
    accent_hover="#57AEFF",
    accent_pressed="#2A88E6",
    on_accent="#06121D",
    accent_soft="#1D3448",
    green="#4AD295",
    green_soft="#16352A",
    caution="#F0A63C",
    caution_soft="#3A2C17",
    caution_text="#FFC266",
    danger="#E5534B",
    danger_hover="#F26A62",
    danger_pressed="#C9423B",
    danger_soft="#3B2220",
    danger_text="#FF938C",
    selected="#26384B",
    hover="#323232",
    header="#242424",
    trough="#3A3A3A",
    ring_track="#3B3B3B",
    row_alt="#2C2C2C",
    disabled_bg="#333333",
    disabled_fg="#6C6C6C",
    scrollbar="#5A5A5A",
)


def system_prefers_dark() -> bool:
    """读取注册表 AppsUseLightTheme 判断系统深浅色。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return False


def resolve_mode(mode: str) -> str:
    """mode: auto / light / dark -> light / dark。"""
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return "dark" if system_prefers_dark() else "light"


def get_palette(mode: str = "auto") -> Palette:
    return DARK if resolve_mode(mode) == "dark" else LIGHT


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[str, int, str], tkfont.Font] = {}
_font_family_cache: str | None = None


def pick_family(root: tk.Misc) -> str:
    global _font_family_cache
    if _font_family_cache:
        return _font_family_cache
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in FONT_FAMILY_CANDIDATES:
        if name in families:
            _font_family_cache = name
            break
    else:
        _font_family_cache = "TkDefaultFont"
    return _font_family_cache


def font(size: int, weight: str = "normal") -> tkfont.Font:
    """按 (字号, 字重) 缓存字体对象。必须在 Tk 创建之后调用。"""
    key = (family(), size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    created = tkfont.Font(family=family(), size=size, weight=weight)
    _font_cache[key] = created
    return created


def family() -> str:
    return _font_family_cache or "Microsoft YaHei UI"


class Fonts(SimpleNamespace):
    """一组语义化字体，由 init_fonts() 填充。"""


FONT_SIZES = SimpleNamespace(
    title=17,
    subtitle=9,
    h1=14,
    h2=11,
    body=10,
    small=9,
    tiny=8,
    num_lg=22,
    num_md=15,
    num_sm=11,
)


def init_fonts(root: tk.Misc) -> Fonts:
    global _font_family_cache
    _font_family_cache = pick_family(root)
    fam = _font_family_cache

    def mk(size: int, weight: str = "normal", family: str | None = None) -> tkfont.Font:
        key = ("__named__" + str(size), weight, family or fam)
        cached = _font_cache.get(key)
        if cached is not None:
            return cached
        f = tkfont.Font(root=root, family=family or fam, size=size, weight=weight)
        _font_cache[key] = f
        return f

    fonts = Fonts(
        family=fam,
        title=mk(FONT_SIZES.title, "bold"),
        subtitle=mk(FONT_SIZES.subtitle),
        h1=mk(FONT_SIZES.h1, "bold"),
        h2=mk(FONT_SIZES.h2, "bold"),
        body=mk(FONT_SIZES.body),
        body_bold=mk(FONT_SIZES.body, "bold"),
        small=mk(FONT_SIZES.small),
        small_bold=mk(FONT_SIZES.small, "bold"),
        tiny=mk(FONT_SIZES.tiny),
        num_lg=mk(FONT_SIZES.num_lg, "bold"),
        num_md=mk(FONT_SIZES.num_md, "bold"),
        num_sm=mk(FONT_SIZES.num_sm, "bold"),
        mono=mk(FONT_SIZES.small, family="Consolas"),
    )
    return fonts


# ---------------------------------------------------------------------------
# 全局 Tk / ttk 样式
# ---------------------------------------------------------------------------


def configure_defaults(root: tk.Misc, pal: Palette, fonts: Fonts) -> None:
    """设置 Tk 默认字体与基础选项。"""
    for name, f in (
        ("TkDefaultFont", fonts.body),
        ("TkTextFont", fonts.body),
        ("TkMenuFont", fonts.body),
        ("TkHeadingFont", fonts.small_bold),
        ("TkCaptionFont", fonts.small_bold),
        ("TkSmallCaptionFont", fonts.small),
        ("TkIconFont", fonts.small),
        ("TkTooltipFont", fonts.small),
    ):
        try:
            tkfont.nametofont(name).configure(family=fonts.family, size=f.cget("size"))
            if name in ("TkCaptionFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(weight="bold")
        except Exception:
            pass

    root.option_add("*Font", fonts.body)
    root.option_add("*Menu.font", fonts.body)
    root.option_add("*Menu.background", pal.surface)
    root.option_add("*Menu.foreground", pal.text)
    root.option_add("*Menu.activeBackground", pal.accent_soft)
    root.option_add("*Menu.activeForeground", pal.text)
    root.option_add("*Menu.borderWidth", 0)
    root.option_add("*Menu.relief", "flat")
    try:
        root.configure(background=pal.window)
    except Exception:
        pass


def apply_ttk_style(
    root: tk.Misc, pal: Palette, fonts: Fonts, *, compact: bool = False
) -> ttk.Style:
    """使用 clam 主题做完全自定义，替换 Windows 原生主题，保证深色可用。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=pal.window, foreground=pal.text, font=fonts.body)

    # Treeview --------------------------------------------------------------
    style.configure(
        "Treeview",
        background=pal.surface,
        fieldbackground=pal.surface,
        foreground=pal.text,
        rowheight=sp(30 if compact else 34),
        font=fonts.body,
        borderwidth=0,
        relief="flat",
    )
    style.map(
        "Treeview",
        background=[("selected", pal.selected)],
        foreground=[("selected", pal.text)],
    )
    style.configure(
        "Treeview.Heading",
        background=pal.header,
        foreground=pal.text_secondary,
        font=fonts.small_bold,
        relief="flat",
        borderwidth=0,
        padding=(sp(8), sp(5 if compact else 7)),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", pal.hover)],
        foreground=[("active", pal.text)],
    )
    # 去掉 Treeview 外框，让内容贴合卡片
    try:
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    except Exception:
        pass

    # Scrollbar（去掉箭头，细条）------------------------------------------
    for orient in ("Vertical", "Horizontal"):
        name = f"{orient}.TScrollbar"
        style.configure(
            name,
            background=pal.scrollbar,
            troughcolor=pal.surface,
            bordercolor=pal.surface,
            darkcolor=pal.scrollbar,
            lightcolor=pal.scrollbar,
            arrowcolor=pal.scrollbar,
            gripcount=0,
            width=sp(10),
        )
        style.map(name, background=[("active", pal.text_tertiary)])
        layout_name = "Vertical.Scrollbar" if orient == "Vertical" else "Horizontal.Scrollbar"
        thumb = f"{layout_name}.thumb"
        try:
            if orient == "Vertical":
                style.layout(
                    name,
                    [
                        (
                            "Vertical.Scrollbar.trough",
                            {
                                "sticky": "ns",
                                "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
                            },
                        )
                    ],
                )
            else:
                style.layout(
                    name,
                    [
                        (
                            "Horizontal.Scrollbar.trough",
                            {
                                "sticky": "ew",
                                "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
                            },
                        )
                    ],
                )
        except Exception:
            pass

    # 复选框 / 单选框（设置与确认弹窗使用）--------------------------------
    style.configure(
        "TCheckbutton",
        background=pal.surface,
        foreground=pal.text,
        font=fonts.body,
        focuscolor=pal.surface,
        indicatorbackground=pal.surface,
        indicatorforeground=pal.accent,
        upperbordercolor=pal.border_strong,
        lowerbordercolor=pal.border_strong,
        bordercolor=pal.border_strong,
        padding=(0, sp(2)),
    )
    style.map(
        "TCheckbutton",
        background=[("active", pal.surface)],
        foreground=[("disabled", pal.text_tertiary)],
        indicatorbackground=[("selected", pal.accent), ("active", pal.accent_soft)],
    )
    style.configure(
        "TRadiobutton",
        background=pal.surface,
        foreground=pal.text,
        font=fonts.body,
        focuscolor=pal.surface,
        indicatorbackground=pal.surface,
        indicatorforeground=pal.accent,
        upperbordercolor=pal.border_strong,
        lowerbordercolor=pal.border_strong,
        padding=(0, sp(2)),
    )
    style.map(
        "TRadiobutton",
        background=[("active", pal.surface)],
        foreground=[("disabled", pal.text_tertiary)],
        indicatorbackground=[("selected", pal.accent)],
    )

    # 进度条（少量 ttk 场景备用）------------------------------------------
    style.configure(
        "Accent.Horizontal.TProgressbar",
        troughcolor=pal.trough,
        background=pal.accent,
        bordercolor=pal.trough,
        lightcolor=pal.accent,
        darkcolor=pal.accent,
        thickness=sp(6),
    )
    return style


def set_titlebar_dark(window: tk.Misc, dark: bool) -> bool:
    """让系统标题栏与深色主题一致（Win10 1809+ / Win11）。返回是否成功。"""
    if sys.platform != "win32":
        return False
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.windll.dwmapi
        try:
            dwm.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
            ]
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        except Exception:
            pass
        value = ctypes.c_int(1 if dark else 0)
        ok = False
        for attr in (20, 19):  # 20 = Win10 2004+ / Win11，19 = 旧版本
            try:
                hr = dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                continue
            if hr == 0:
                ok = True
                break
        # 触发标题栏重绘
        try:
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0, 0x0020 | 0x0001 | 0x0002 | 0x0004
            )
        except Exception:
            pass
        logger.debug("标题栏深色=%s ok=%s hwnd=%s", dark, ok, hex(int(hwnd)))
        return ok
    except Exception:
        logger.debug("设置标题栏颜色失败", exc_info=True)
        return False


def center(root: tk.Misc, width: int, height: int) -> None:
    """按物理像素把窗口居中（width/height 为物理像素）。"""
    root.update_idletasks()
    sw, sh = screen_size()
    if sw <= 1:
        sw, sh = 1920, 1080
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    root.geometry(f"{width}x{height}+{x}+{y}")


# ---------------------------------------------------------------------------
# Canvas 颜色工具
# ---------------------------------------------------------------------------


def mix(color_a: str, color_b: str, t: float) -> str:
    """线性混合两个 #RRGGBB 颜色。"""
    t = max(0.0, min(1.0, t))
    a = color_a.lstrip("#")
    b = color_b.lstrip("#")
    try:
        ca = tuple(int(a[i : i + 2], 16) for i in (0, 2, 4))
        cb = tuple(int(b[i : i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return color_a
    return "#%02X%02X%02X" % tuple(int(round(ca[i] + (cb[i] - ca[i]) * t)) for i in range(3))
