"""生成插件 logo（根目录 logo.png + assets/ 下的多尺寸位图）。

思路：先在 2048px 画布上绘制，再 LANCZOS 降采样，得到干净的抗锯齿边缘。
配色取自方案两端：HuggingFace 黄 #FFD21E 与 Cloudflare 橙 #F6821F，
底板沿用 AstrBot WebUI 深紫，保证插件市场缩略图里辨识度够高。

图形语义：云 + 向上箭头（上传到云端图床），下方三张叠放的照片卡（批量图片）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent  # 插件根目录
S = 2048  # 超采样画布边长

BG_TOP = (58, 35, 82)
BG_BOTTOM = (23, 15, 38)
GLOW = (140, 110, 190)
CLOUD_TOP = (255, 214, 66)
CLOUD_BOTTOM = (240, 122, 24)
CREAM = (250, 240, 214)
INK = (40, 26, 60)
SUN = (246, 130, 31)
EDGE = (255, 226, 150)

# 云整体右移，让视觉中心与箭头中线（x = 0.5）对齐。
CLOUD_DX = 0.033


def linear_gradient(
    size: tuple[int, int],
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    *,
    horizontal: bool = False,
) -> Image.Image:
    """生成两色线性渐变位图。"""
    width, height = size
    span = width if horizontal else height
    strip = Image.new("RGB", (span, 1) if horizontal else (1, span))
    pixels = strip.load()
    for i in range(span):
        t = i / max(1, span - 1)
        color = tuple(round(start[c] + (end[c] - start[c]) * t) for c in range(3))
        pixels[(i, 0) if horizontal else (0, i)] = color
    return strip.resize(size, Image.BILINEAR)


def mask() -> Image.Image:
    return Image.new("L", (S, S), 0)


def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    """把 0–1 的相对坐标换成画布像素坐标。"""
    return (S * x0, S * y0, S * x1, S * y1)


def circle(cx: float, cy: float, r: float) -> tuple[float, float, float, float]:
    return box(cx - r, cy - r, cx + r, cy + r)


def card(
    bounds: tuple[float, float, float, float],
    radius: float,
    alpha: int,
    *,
    angle: float = 0.0,
) -> tuple[Image.Image, Image.Image]:
    """画一张照片卡，返回 (RGBA 图层, L 蒙版)。angle 非 0 时绕卡片中心旋转。"""
    shape = mask()
    ImageDraw.Draw(shape).rounded_rectangle(bounds, radius=round(S * radius), fill=alpha)
    if angle:
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
        shape = shape.rotate(angle, resample=Image.BICUBIC, center=center)
    layer = Image.new("RGBA", (S, S), (*CREAM, 0))
    layer.putalpha(shape)
    return layer, shape


def cloud_mask() -> Image.Image:
    """云的轮廓：三个圆 + 一条底部圆角矩形的并集。"""
    shape = mask()
    draw = ImageDraw.Draw(shape)
    draw.ellipse(circle(0.460 + CLOUD_DX, 0.380, 0.150), fill=255)
    draw.ellipse(circle(0.315 + CLOUD_DX, 0.430, 0.100), fill=255)
    draw.ellipse(circle(0.615 + CLOUD_DX, 0.415, 0.115), fill=255)
    draw.rounded_rectangle(
        box(0.215 + CLOUD_DX, 0.435, 0.720 + CLOUD_DX, 0.530),
        radius=round(S * 0.048),
        fill=255,
    )
    return shape


def arrow_mask() -> Image.Image:
    """向上箭头：从云里挖空，露出底板颜色。"""
    shape = mask()
    draw = ImageDraw.Draw(shape)
    draw.polygon(
        [(S * 0.500, S * 0.272), (S * 0.398, S * 0.378), (S * 0.602, S * 0.378)],
        fill=255,
    )
    draw.rounded_rectangle(box(0.454, 0.358, 0.546, 0.508), radius=round(S * 0.016), fill=255)
    return shape


def build() -> Image.Image:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 底板：圆角方形 + 竖向渐变
    plate = mask()
    ImageDraw.Draw(plate).rounded_rectangle((0, 0, S - 1, S - 1), radius=round(S * 0.235), fill=255)
    canvas.paste(linear_gradient((S, S), BG_TOP, BG_BOTTOM), (0, 0), plate)

    # 左上柔光，避免大块深色发死
    glow = mask()
    ImageDraw.Draw(glow).ellipse(box(-0.25, -0.35, 0.72, 0.55), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.06))
    glow = Image.composite(glow, mask(), plate)
    canvas.paste(Image.new("RGB", (S, S), GLOW), (0, 0), glow)

    # 下方：三张扇形叠放的照片卡，暗示批量
    left, _ = card(box(0.206, 0.672, 0.410, 0.856), 0.038, 170, angle=14.0)
    right, _ = card(box(0.590, 0.672, 0.794, 0.856), 0.038, 170, angle=-14.0)
    canvas = Image.alpha_composite(canvas, left)
    canvas = Image.alpha_composite(canvas, right)

    front, front_mask = card(box(0.372, 0.612, 0.628, 0.866), 0.045, 255)
    canvas = Image.alpha_composite(canvas, front)

    # 前卡内容：山 + 太阳，用卡片蒙版裁剪，防止溢出圆角
    art = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    art_draw = ImageDraw.Draw(art)
    art_draw.ellipse(circle(0.566, 0.679, 0.029), fill=(*SUN, 255))
    art_draw.polygon(
        [(S * 0.556, S * 0.806), (S * 0.614, S * 0.727), (S * 0.664, S * 0.806)],
        fill=(*INK, 220),
    )
    art_draw.polygon(
        [(S * 0.388, S * 0.806), (S * 0.478, S * 0.686), (S * 0.568, S * 0.806)],
        fill=(*INK, 255),
    )
    art.putalpha(Image.composite(art.getchannel("A"), mask(), front_mask))
    canvas = Image.alpha_composite(canvas, art)

    # 上方：云 + 挖空的向上箭头
    shape = cloud_mask()
    hole = Image.composite(arrow_mask(), mask(), shape)
    cloud = Image.composite(mask(), shape, hole)
    canvas.paste(linear_gradient((S, S), CLOUD_TOP, CLOUD_BOTTOM), (0, 0), cloud)

    # 内描边，收一下边缘
    inset = round(S * 0.012)
    edge = mask()
    ImageDraw.Draw(edge).rounded_rectangle(
        (inset, inset, S - 1 - inset, S - 1 - inset),
        radius=round(S * 0.225),
        outline=52,
        width=round(S * 0.010),
    )
    canvas.paste(Image.new("RGB", (S, S), EDGE), (0, 0), edge)
    return canvas


def main() -> None:
    art = build()
    assets = ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for target, size in (
        (ROOT / "logo.png", 512),
        (assets / "logo.png", 512),
        (assets / "logo-256.png", 256),
        (assets / "logo-128.png", 128),
        (assets / "logo-64.png", 64),
    ):
        art.resize((size, size), Image.LANCZOS).save(target, format="PNG", optimize=True)
        print(f"wrote {target.name} ({size}px)")


if __name__ == "__main__":
    main()
