"""生成清风清理的应用图标（开发期工具）。

运行时程序**不依赖 Pillow**：本脚本只在开发/打包阶段运行一次，
产出 `assets/app.ico`（256/128/64/48/32/16 多尺寸）与 `assets/app_256.png`。

设计：深墨绿圆角底 + 青绿渐变圆环仪表盘 + 薄荷绿叶片，呼应"清风"与"清理"。

用法：
    python assets/generate_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent

# 逻辑尺寸，内部按 SS 超采样以获得平滑边缘（Tk/Pillow 的 ICO 不支持抗锯齿）
S = 1024
SS = 2
W = S * SS

# 配色（与 UI 主题一致）
BG_TOP = (18, 49, 58)          # #12313A
BG_BOTTOM = (7, 23, 28)        # #07171C
RING_TRACK = (23, 59, 64)      # #173B40
RING_STOPS = [(45, 212, 167), (39, 168, 224), (15, 108, 189)]  # #2DD4A7 -> #27A8E0 -> #0F6CBD
LEAF_FILL = (233, 255, 246)    # #E9FFF6
LEAF_VEIN = (14, 159, 110)     # #0E9F6E
ACCENT_DOT = (125, 243, 198)   # #7DF3C6


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _ring_color(t: float) -> tuple[int, int, int]:
    stops = RING_STOPS
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[-1]
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    return _lerp(stops[i], stops[i + 1], pos - i)


def _vertical_gradient(size: int, top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (size, size), top)
    draw = ImageDraw.Draw(img)
    for y in range(size):
        draw.line([(0, y), (size, y)], fill=_lerp(top, bottom, y / max(1, size - 1)))
    return img


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=radius, fill=255
    )
    return mask


def _rotate(points: list[tuple[float, float]], angle_deg: float) -> list[tuple[float, float]]:
    ca = math.cos(math.radians(angle_deg))
    sa = math.sin(math.radians(angle_deg))
    return [(x * ca - y * sa, x * sa + y * ca) for x, y in points]


def _translate(points: list[tuple[float, float]], cx: float,
               cy: float) -> list[tuple[float, float]]:
    return [(x + cx, y + cy) for x, y in points]


def _leaf_points(length: float, width: float, n: int = 80) -> list[tuple[float, float]]:
    """对称叶片轮廓，长轴沿 -y 方向。"""
    pts: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        y = -length / 2 + length * t
        bulge = width * (math.sin(math.pi * t) ** 0.72)
        pts.append((bulge, y))
    for i in range(n - 1, 0, -1):
        t = i / n
        y = -length / 2 + length * t
        bulge = width * (math.sin(math.pi * t) ** 0.72)
        pts.append((-bulge, y))
    return pts


def render_master() -> Image.Image:
    """渲染 2048x2048 母图（超采样后缩小到 1024）。"""
    size = W
    # ---- 底：深色渐变 + 右下角青色氛围光 ----
    img = _vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    r = int(size * 0.52)
    gx, gy = int(size * 0.78), int(size * 0.82)
    gd.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(14, 159, 110, 105))
    glow = glow.filter(ImageFilter.GaussianBlur(int(size * 0.10)))
    img = Image.alpha_composite(img, glow)

    # ---- 圆环仪表盘 ----
    cx = cy = size / 2
    radius = size * 0.325
    thickness = int(size * 0.088)
    box = [cx - radius, cy - radius, cx + radius, cy + radius]

    # 轨道（淡青，制造层次）
    track = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(track).ellipse(box, outline=RING_TRACK + (255,), width=thickness)
    track = track.filter(ImageFilter.GaussianBlur(size * 0.006))
    img = Image.alpha_composite(img, track)

    # 渐变圆弧：从右侧偏下顺时针扫 285°，留出缺口
    segments = 288
    start = -58.0
    sweep = 285.0
    d = ImageDraw.Draw(img)
    for i in range(segments):
        t0 = i / segments
        t1 = (i + 1) / segments
        a0 = start + sweep * t0
        a1 = start + sweep * t1 + 0.9 * (sweep / segments)  # 轻微重叠，避免接缝
        color = _ring_color(t0 + (t1 - t0) * 0.5)
        d.arc(box, a0, a1, fill=color + (255,), width=thickness)

    # 圆头端点
    def _endpoint(angle_deg: float, t: float) -> tuple[float, float, tuple[int, int, int]]:
        rad = math.radians(angle_deg)
        return (cx + radius * math.cos(rad), cy + radius * math.sin(rad), _ring_color(t))

    for angle, t in ((start, 0.0), (start + sweep, 1.0)):
        ex, ey, color = _endpoint(angle, t)
        rr = thickness / 2
        d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=color + (255,))

    # 弧末端的青绿光点
    ex, ey, _ = _endpoint(start + sweep, 1.0)
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hr = int(size * 0.035)
    ImageDraw.Draw(halo).ellipse([ex - hr, ey - hr, ex + hr, ey + hr],
                                 fill=ACCENT_DOT + (110,))
    halo = halo.filter(ImageFilter.GaussianBlur(size * 0.012))
    img = Image.alpha_composite(img, halo)
    d = ImageDraw.Draw(img)
    dr = int(size * 0.014)
    d.ellipse([ex - dr, ey - dr, ex + dr, ey + dr], fill=ACCENT_DOT + (255,))

    # ---- 叶片 ----
    leaf_len = size * 0.30
    leaf_w = size * 0.105
    angle = -32.0
    cx_l, cy_l = cx, cy - size * 0.012

    leaf_pts = _translate(_rotate(_leaf_points(leaf_len, leaf_w), angle), cx_l, cy_l)
    # 叶片投影
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(
        _translate(_rotate(_leaf_points(leaf_len, leaf_w), angle), cx_l + size * 0.012,
                   cy_l + size * 0.018),
        fill=(0, 0, 0, 90),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.018))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)

    d.polygon(leaf_pts, fill=LEAF_FILL + (255,))
    d.line(leaf_pts + [leaf_pts[0]], fill=(190, 246, 226, 255), width=max(2, int(size * 0.003)),
           joint="curve")

    # 主叶脉 + 支脉
    vein_w = max(2, int(size * 0.006))
    p_main = _translate(_rotate([(0, -leaf_len * 0.42), (0, leaf_len * 0.48)], angle), cx_l, cy_l)
    d.line(p_main, fill=LEAF_VEIN + (255,), width=vein_w, joint="curve")
    for t in (0.18, 0.34, 0.50, 0.66):
        y = -leaf_len / 2 + leaf_len * t
        bulge = leaf_w * (math.sin(math.pi * t) ** 0.72) * 0.82
        for side in (1, -1):
            br = _translate(_rotate([(0, y), (side * bulge, y + leaf_len * 0.055)], angle),
                            cx_l, cy_l)
            d.line(br, fill=LEAF_VEIN + (200,), width=max(2, vein_w - 1), joint="curve")

    # ---- 圆角裁切 + 极细亮边 ----
    mask = _rounded_mask(size, int(size * 0.225))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    border = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [(2, 2), (size - 3, size - 3)],
        radius=int(size * 0.225),
        outline=(134, 239, 205, 105),
        width=max(2, int(size * 0.004)),
    )
    out = Image.alpha_composite(out, border)

    return out.resize((S, S), Image.LANCZOS)


def main() -> None:
    master = render_master()
    ico_path = ASSETS / "app.ico"
    png_path = ASSETS / "app_256.png"
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    master.save(ico_path, format="ICO", sizes=sizes)
    master.resize((256, 256), Image.LANCZOS).save(png_path, format="PNG")
    print(f"已生成 {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f"已生成 {png_path} ({png_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
