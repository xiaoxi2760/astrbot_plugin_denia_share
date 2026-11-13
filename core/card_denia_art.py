"""Code-drawn material ornaments for the Denia card skin.

The approved 2026-11-13 design deliberately uses two different material
languages.  Light cards are built from cream paper, scalloped mounts,
transparent bubbles, a narrow paper ribbon and an orange-frosted cake.  Dark
cards are built from nebula glass, bevels, star orbits, a translucent cube and
a ringed planet.  Nothing here loads a bitmap, reads a review-script path or
uses a Windows-only font path; every ornament is painted with Pillow so the
production renderer remains self-contained.

All public helpers paint directly onto an RGBA canvas.  Small ornaments are
supersampled locally and downsampled before compositing, which keeps their
curves clean without allocating a full 2x card canvas.
"""

from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter

__all__ = [
    "bubble",
    "cake",
    "cube",
    "orbit",
    "star",
    "paper_dolls",
    "cream_panel",
    "dark_panel",
    "bevel_mask",
]

Color = tuple[int, int, int]
_RGBA = tuple[int, int, int, int]


def _a(color: Color, alpha: int) -> _RGBA:
    return (color[0], color[1], color[2], max(0, min(255, int(alpha))))


def _patch(
    canvas: Image.Image,
    box: tuple[float, float, float, float],
    painter: Callable[[Image.Image, ImageDraw.ImageDraw, Callable[[float, float], tuple[int, int]], float], None],
    *,
    supersample: float = 2.0,
) -> None:
    """Paint ``painter`` into a clipped local patch, then downsample it."""
    left = max(0, math.floor(box[0]))
    top = max(0, math.floor(box[1]))
    right = min(canvas.width, math.ceil(box[2]))
    bottom = min(canvas.height, math.ceil(box[3]))
    if right <= left or bottom <= top:
        return
    scale = max(1.0, supersample)
    layer = Image.new("RGBA", (round((right - left) * scale), round((bottom - top) * scale)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def point(x: float, y: float) -> tuple[int, int]:
        return (round((x - left) * scale), round((y - top) * scale))

    painter(layer, draw, point, scale)
    if scale != 1:
        layer = layer.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
    canvas.alpha_composite(layer, (left, top))


def _local_orbit(
    draw: ImageDraw.ImageDraw,
    point: Callable[[float, float], tuple[int, int]],
    scale: float,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    angle: float,
    color: _RGBA,
    width: float,
    start: float = 0.0,
    end: float = 360.0,
) -> None:
    q = math.radians(angle)
    points: list[tuple[int, int]] = []
    step = 3
    value = start
    while value <= end:
        a = math.radians(value)
        u = rx * math.cos(a)
        v = ry * math.sin(a)
        points.append(
            point(
                cx + u * math.cos(q) - v * math.sin(q),
                cy + u * math.sin(q) + v * math.cos(q),
            )
        )
        value += step
    if len(points) >= 2:
        draw.line(points, fill=color, width=max(1, round(width * scale)), joint="curve")


def bubble(
    canvas: Image.Image,
    cx: float,
    cy: float,
    radius: float,
    *,
    opacity: float = 1.0,
) -> None:
    """A translucent bubble with a warm rim and two glass highlights."""
    radius = max(1.0, float(radius))
    opacity = max(0.0, min(1.0, float(opacity)))

    def paint(layer: Image.Image, draw: ImageDraw.ImageDraw, p, scale: float) -> None:
        r = radius
        draw.ellipse(
            (*p(cx - r, cy - r), *p(cx + r, cy + r)),
            fill=(255, 251, 246, round(70 * opacity)),
            outline=(255, 255, 255, round(200 * opacity)),
            width=max(1, round(3 * scale)),
        )
        draw.arc(
            (*p(cx - r + 5, cy - r + 5), *p(cx + r - 5, cy + r - 5)),
            30,
            145,
            fill=(203, 139, 172, round(125 * opacity)),
            width=max(1, round(3 * scale)),
        )
        draw.arc(
            (*p(cx - r + 10, cy - r + 10), *p(cx + r - 10, cy + r - 10)),
            208,
            272,
            fill=(255, 255, 255, round(240 * opacity)),
            width=max(1, round(6 * scale)),
        )

    _patch(canvas, (cx - radius - 4, cy - radius - 4, cx + radius + 4, cy + radius + 4), paint)


def cake(canvas: Image.Image, x: float, y: float, z: float = 1.0) -> None:
    """The approved orange marmalade / cream layer cake, cut on a diagonal."""
    z = max(0.2, float(z))

    def paint(layer: Image.Image, draw: ImageDraw.ImageDraw, p, scale: float) -> None:
        def pt(a: float, b: float) -> tuple[int, int]:
            return p(x + a * z, y + b * z)

        def poly(points: list[tuple[float, float]], color: _RGBA) -> None:
            draw.polygon([pt(a, b) for a, b in points], fill=color)

        def line(points: list[tuple[float, float]], color: _RGBA, width: float = 1.0) -> None:
            draw.line(
                [pt(a, b) for a, b in points],
                fill=color,
                width=max(1, round(width * z * scale)),
                joint="curve",
            )

        def ellipse(
            a: float, b: float, c: float, e: float, fill: _RGBA,
            outline: _RGBA | None = None, width: float = 1.0,
        ) -> None:
            draw.ellipse(
                (*pt(a, b), *pt(c, e)),
                fill=fill,
                outline=outline,
                width=max(1, round(width * z * scale)),
            )

        # Golden sponge with marmalade and two cream bands.
        poly([(0, 43), (143, 63), (143, 108), (0, 88)], (232, 173, 99, 255))
        poly([(0, 80), (143, 100), (143, 108), (0, 88)], (216, 145, 79, 255))
        poly([(0, 60), (143, 81), (143, 94), (0, 73)], (255, 242, 219, 255))
        poly([(0, 65), (143, 86), (143, 90), (0, 69)], (238, 161, 105, 255))
        poly([(0, 58), (143, 79), (143, 83), (0, 62)], (233, 181, 163, 255))
        for a, b, rad in (
            (10, 78, 1.2), (20, 82, 1.0), (33, 80, 1.5), (48, 84, 0.9),
            (67, 88, 1.3), (83, 91, 0.8), (102, 94, 1.4), (120, 98, 1.0),
            (135, 101, 0.9), (22, 55, 0.9), (56, 61, 1.0), (88, 66, 0.9),
            (120, 70, 1.0),
        ):
            ellipse(a - rad, b - rad, a + rad, b + rad, (249, 210, 147, 255))

        # Diagonal cut face and bright frosting top.
        poly([(0, 43), (99, 8), (143, 63)], (255, 244, 220, 255))
        poly([(7, 43), (98, 12), (133, 56)], (255, 251, 239, 255))
        line([(0, 43), (99, 8), (143, 63), (143, 108), (0, 88), (0, 43)], (185, 127, 104, 255), 1.15)
        poly([(0, 42), (143, 62), (143, 68), (0, 48)], (255, 248, 233, 255))
        for a, b, w, h in (
            (16, 46, 8, 12), (45, 50, 10, 9), (76, 55, 8, 15),
            (110, 59, 11, 10), (133, 63, 5, 7),
        ):
            ellipse(a - w, b - 3, a + w, b + h, (255, 248, 233, 255))
            line([(a - w + 2, b + h * 0.45), (a - w + 3, b + h * 0.68)], (243, 222, 189, 255), 0.7)
        line([(2, 43), (141, 63)], (255, 253, 244, 255), 1.6)

        # Orange wheel: pith, eight juicy segments, highlights.
        ellipse(76, 14, 120, 58, (239, 155, 53, 255), (200, 125, 60, 255), 1.15)
        ellipse(79, 17, 117, 55, (255, 242, 200, 255))
        colors = ((255, 179, 63, 255), (247, 162, 50, 255), (255, 193, 88, 255), (246, 166, 58, 255))
        for i in range(8):
            a0 = 2 * math.pi * i / 8 + 0.06
            a1 = 2 * math.pi * (i + 1) / 8 - 0.06
            points = [(98, 36)]
            for j in range(11):
                angle = a0 + (a1 - a0) * j / 10
                points.append((98 + 16.5 * math.cos(angle), 36 + 16.5 * math.sin(angle)))
            poly(points, colors[i % 4])
            mid = (a0 + a1) / 2
            line(
                [(98 + 8 * math.cos(mid), 36 + 8 * math.sin(mid)),
                 (98 + 13 * math.cos(mid), 36 + 13 * math.sin(mid))],
                (255, 230, 165, 255), 1.0,
            )
        ellipse(96.5, 34.5, 99.5, 37.5, (255, 247, 217, 255))
        for a, b in ((23, 37), (40, 31), (57, 39), (67, 27), (76, 44), (52, 45)):
            ellipse(a, b, a + 2, b + 2, (255, 255, 255, 255))
        for a, b in ((33, 39), (61, 31), (68, 43)):
            line([(a, b), (a + 4, b - 2), (a + 6, b - 1)], (238, 171, 75, 255), 1.0)

    _patch(canvas, (x - 4, y + 3 * z - 4, x + 149 * z + 4, y + 113 * z + 4), paint)


def cube(canvas: Image.Image, cx: float, cy: float, z: float = 1.0) -> None:
    """Translucent faceted cube, eleven particles and a small ringed planet."""
    z = max(0.2, float(z))
    stars = (
        (-1.08, -0.34, 0.019), (-0.98, -0.78, 0.028), (-0.72, -1.13, 0.015),
        (-0.36, -1.24, 0.027), (0.12, -1.18, 0.016), (0.43, -0.93, 0.014),
        (1.21, -0.13, 0.023), (1.29, 0.25, 0.016), (0.99, 0.57, 0.025),
        (-1.02, 0.39, 0.014), (-0.92, 0.66, 0.022),
    )

    def paint(layer: Image.Image, draw: ImageDraw.ImageDraw, p, scale: float) -> None:
        def pt(a: float, b: float) -> tuple[int, int]:
            return p(cx + a * z, cy + b * z)

        def poly(points: list[tuple[float, float]], color: _RGBA) -> None:
            draw.polygon([pt(a, b) for a, b in points], fill=color)

        def line(points: list[tuple[float, float]], color: _RGBA, width: float = 1.0) -> None:
            # Geometry scales with z; stroke width stays a normal 1-2px line.
            draw.line(
                [pt(a, b) for a, b in points],
                fill=color,
                width=max(1, round(width * scale)),
                joint="curve",
            )

        def ellipse(a: float, b: float, c: float, e: float, fill: _RGBA,
                    outline: _RGBA | None = None, width: float = 1.0) -> None:
            draw.ellipse(
                (*pt(a, b), *pt(c, e)),
                fill=fill,
                outline=outline,
                width=max(1, round(width * scale)),
            )

        # Bound star-dust glow, blurred only inside this local patch.
        glow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for a, b, rad in stars:
            xx, yy = pt(a, b)
            rr = max(1.2, rad * z) * scale
            gd.ellipse((xx - rr * 2, yy - rr * 2, xx + rr * 2, yy + rr * 2), fill=(155, 173, 255, 115))
        glow = glow.filter(ImageFilter.GaussianBlur(max(0.8, 1.8 * scale)))
        layer.alpha_composite(glow)

        _local_orbit(draw, p, scale, cx + 1.04 * z, cy - 0.72 * z, 0.35 * z, 0.10 * z, -22, (160, 145, 222, 160), 0.85)
        ellipse(0.88, -0.88, 1.20, -0.56, (138, 131, 198, 255))
        ellipse(0.91, -0.86, 1.09, -0.65, (178, 163, 221, 255))
        _local_orbit(draw, p, scale, cx + 1.04 * z, cy - 0.72 * z, 0.35 * z, 0.10 * z, -22, (217, 201, 249, 220), 1.05, 0, 180)
        _local_orbit(draw, p, scale, cx, cy, 1.14 * z, 0.34 * z, -20, (154, 135, 207, 95), 0.7, 185, 325)

        a = (0, -1)
        b = (0.87, -0.5)
        c = (0, 0)
        e = (-0.87, -0.5)
        poly([a, b, c, e], (81, 71, 117, 255))
        poly([e, c, (0, 1), (-0.87, 0.5)], (53, 43, 80, 255))
        poly([c, b, (0.87, 0.5), (0, 1)], (115, 96, 149, 255))
        poly([(0, -0.94), (0.78, -0.5), (0, -0.07), (-0.78, -0.5)], (93, 83, 126, 255))
        poly([(0.07, 0.04), (0.80, -0.39), (0.80, 0.44), (0.07, 0.88)], (128, 106, 159, 255))
        line([a, b, (0.87, 0.5), (0, 1), (-0.87, 0.5), e, a], (214, 195, 236, 255), 1.3)
        line([e, c, b], (203, 185, 231, 255), 1.1)
        line([c, (0, 1)], (218, 202, 240, 255), 1.1)
        line([(-0.57, -0.49), (0, -0.80), (0.48, -0.54)], (168, 157, 206, 255), 0.8)
        for a, b, rad in stars:
            rr = max(0.012, rad)
            ellipse(a - rr, b - rr, a + rr, b + rr, (220, 214, 255, 255))
        for a, b in ((-0.98, -0.78), (0.99, 0.57)):
            line([(a - 0.055, b), (a + 0.055, b)], (228, 220, 255, 255), 0.85)
            line([(a, b - 0.055), (a, b + 0.055)], (228, 220, 255, 255), 0.85)

    _patch(canvas, (cx - 1.30 * z - 5, cy - 1.45 * z - 5, cx + 1.55 * z + 5, cy + 1.05 * z + 5), paint)


def orbit(
    canvas: Image.Image,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    angle: float,
    color: _RGBA,
    width: float = 1.0,
    *,
    start: float = 0.0,
    end: float = 360.0,
) -> None:
    pad = max(rx, ry) * 0.25 + width + 6
    _patch(
        canvas,
        (cx - rx - pad, cy - ry - pad, cx + rx + pad, cy + ry + pad),
        lambda layer, draw, p, scale: _local_orbit(draw, p, scale, cx, cy, rx, ry, angle, color, width, start, end),
        supersample=2.0,
    )


def star(canvas: Image.Image, x: float, y: float, radius: float, color: Color) -> None:
    radius = max(0.5, float(radius))

    def paint(layer: Image.Image, draw: ImageDraw.ImageDraw, p, scale: float) -> None:
        pts = []
        for index in range(8):
            angle = -math.pi / 2 + index * math.pi / 4
            r = radius if index % 2 == 0 else radius * 0.26
            pts.append(p(x + r * math.cos(angle), y + r * math.sin(angle)))
        draw.polygon(pts, fill=_a(color, 255))

    _patch(canvas, (x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2), paint)


def paper_dolls(canvas: Image.Image, x: float, y: float, size: float, color: Color) -> None:
    """Three joined paper-cut figures on the narrow pink ribbon."""
    size = max(10.0, float(size))
    unit = size / 120.0

    def paint(layer: Image.Image, draw: ImageDraw.ImageDraw, p, scale: float) -> None:
        def pt(a: float, b: float) -> tuple[int, int]:
            return p(x + a * unit, y + b * unit)

        color_rgba = _a(color, 255)
        draw.line(
            [pt(4, 30), pt(60, 25), pt(116, 30)],
            fill=color_rgba,
            width=max(2, round(11 * unit * scale)),
            joint="curve",
        )
        for cx in (22, 60, 98):
            draw.ellipse(
                (*pt(cx - 9, 4), *pt(cx + 9, 25)),
                fill=color_rgba,
            )
            draw.polygon(
                [
                    pt(cx - 9, 22), pt(cx + 9, 22), pt(cx + 15, 41),
                    pt(cx + 6, 45), pt(cx, 36), pt(cx - 6, 45),
                    pt(cx - 15, 41),
                ],
                fill=color_rgba,
            )

    _patch(canvas, (x - 8, y - 4, x + size + 8, y + size * 0.48 + 8), paint)


def cream_panel(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    paper: Color,
    shadow: Color,
    outline: Color,
    radius: int = 24,
    scallop: bool = False,
    shadow_offset: int = 7,
) -> None:
    """Cream paper with a soft offset under-sheet and optional scalloped hem."""
    x0, y0, x1, y1 = (int(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    d = ImageDraw.Draw(canvas)
    if shadow_offset:
        d.rounded_rectangle(
            (x0, y0 + shadow_offset, x1, y1 + shadow_offset),
            radius=max(0, radius), fill=_a(shadow, 255),
        )
    d.rounded_rectangle(
        (x0, y0, x1, y1), radius=max(0, radius),
        fill=_a(paper, 255), outline=_a(outline, 255), width=1,
    )
    if scallop:
        step = max(14, int((x1 - x0) / 34))
        r = step / 2
        for x in range(x0 + step // 2, x1 - step // 3, step):
            d.ellipse((x - r, y1 - r, x + r, y1 + r + 2), fill=_a(paper, 255))


def dark_panel(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    fill: Color,
    light: Color,
    dark: Color,
    cut: int = 22,
) -> None:
    """Faceted glass field with cut corners and two structural edge lines."""
    x0, y0, x1, y1 = (int(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return
    cut = max(6, int(cut))
    d = ImageDraw.Draw(canvas)
    d.polygon(
        [(x0, y0 + cut), (x0 + cut, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1)],
        fill=_a(fill, 255),
    )
    d.line([(x0, y0 + cut), (x0 + cut, y0), (x1, y0)], fill=_a(light, 255), width=2)
    d.line([(x1, y0 + cut), (x1, y1 - cut), (x1 - cut, y1), (x0 + 22, y1)], fill=_a(dark, 230), width=2)
    d.line([(x0 + cut, y0 + 5), (x1 - 6, y0 + 5)], fill=(255, 255, 255, 22), width=1)
    d.line([(x0 + 18, y1 - 1), (x1 - cut, y1 - 1)], fill=(255, 255, 255, 16), width=1)


def bevel_mask(size: tuple[int, int], cut: int, *, corner: str = "top-right") -> Image.Image:
    """Alpha mask for a photograph with one bevelled corner (dark skin)."""
    width, height = size
    mask = Image.new("L", (width, height), 0)
    cut = max(0, int(cut))
    if corner == "top-right":
        points = [(0, 0), (width - cut, 0), (width, cut), (width, height), (0, height)]
    elif corner == "bottom-left":
        points = [(0, 0), (width, 0), (width, height), (cut, height), (0, height - cut)]
    else:
        points = [(0, 0), (width, 0), (width, height), (0, height)]
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask
