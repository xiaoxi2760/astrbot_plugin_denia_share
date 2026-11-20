"""Lily's white-veil and black-veil cards.

The four layouts share the plugin's data, image and measurement helpers.
The saved card theme selects the veil, independent of the page appearance.
"""


from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from astrbot.api import logger

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .card_renderer import (
    _LANCZOS,
    _L,
    _Theme,
    _card_text,
    _hex_to_rgb,
    _with_alpha,
    format_timestamp,
    parse_stats_line,
    short_url,
    strip_emoji,
)
from .platform_icons import load_platform_icon
from . import card_denia as family
from . import card_denia_art as paper
from . import card_lily_art as art

__all__ = [
    "render_lily_card",
    "LAYOUT_VERSION",
    "FOOTER_COPY",
    "stat_layout_summary",
]

#: 布局 / 装饰调整后递增，保证旧 PNG 缓存不会混入新设计。
LAYOUT_VERSION = "lily-dream-garden-v9"

BRAND_CN = "莉莉的梦境花园"
BRAND_EN = "LILY'S DREAM GARDEN"

FOOTER_COPY = {
    "light": "去迎接属于你的明天。",
    "dark": "愿您的灵魂不再迷惘。",
}


_LAYOUTS = ("standard", "magazine", "immersive", "feed")

_STAT_FILE = {
    "view": "stat-view.png",
    "like": "stat-like.png",
    "coin": "stat-coin.png",
    "favorite": "stat-favorite.png",
    "reply": "stat-reply.png",
    "share": "stat-share.png",
    "danmaku": "stat-danmaku.png",
}

_STAT_KINDS = {
    "播放": "view",
    "观看": "view",
    "浏览": "view",
    "view": "view",
    "views": "view",
    "like": "like",
    "likes": "like",
    "点赞": "like",
    "喜欢": "like",
    "coin": "coin",
    "coins": "coin",
    "投币": "coin",
    "favorite": "favorite",
    "favorites": "favorite",
    "fav": "favorite",
    "收藏": "favorite",
    "reply": "reply",
    "replies": "reply",
    "comment": "reply",
    "comments": "reply",
    "回复": "reply",
    "评论": "reply",
    "share": "share",
    "shares": "share",
    "repost": "share",
    "转发": "share",
    "分享": "share",
    "弹幕": "danmaku",
    "danmaku": "danmaku",
}

_DROP_STATS = {"时长", "duration", "length"}

_STAT_ORDER = ("view", "like", "coin", "favorite", "reply", "share", "danmaku")


# ============================ 主题 ============================


@dataclass(frozen=True, slots=True)
class _Palette:
    top: str
    bottom: str
    border: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    pill_bg: str
    quote_bg: str
    divider: str
    shadow_alpha: int
    stat_pill_bg: str
    glow_alpha: int
    frost_alpha: int
    frost_border_alpha: int
    border_alpha: int
    placeholder_top: tuple[int, int, int]
    placeholder_bottom: tuple[int, int, int]
    accent: str
    decor_line: str
    page_fill: str
    wash: str
    metal: str
    bloom: str


_LIGHT = _Palette(
    top="#EDE6F2",
    bottom="#D9CCE5",
    border="#B6A5C7",
    text_primary="#30263A",
    text_secondary="#594A68",
    text_tertiary="#645670",
    pill_bg="#F5EEF7",
    quote_bg="#F6EFF7",
    divider="#C6B7D2",
    shadow_alpha=40,
    stat_pill_bg="#E5D9EE",
    glow_alpha=18,
    frost_alpha=45,
    frost_border_alpha=50,
    border_alpha=76,
    placeholder_top=(246, 238, 249),
    placeholder_bottom=(216, 199, 229),
    accent="#64497E",
    decor_line="#A692B9",
    page_fill="#F7F0F8",
    wash="#E6DAEF",
    metal="#988098",
    bloom="#FCF4F7",
)


_DARK = _Palette(
    top="#231A31",
    bottom="#15101F",
    border="#675579",
    text_primary="#F5EFF9",
    text_secondary="#C8B9D4",
    text_tertiary="#BFAECC",
    pill_bg="#30263E",
    quote_bg="#2B2238",
    divider="#5C4B6E",
    shadow_alpha=130,
    stat_pill_bg="#30263E",
    glow_alpha=26,
    frost_alpha=26,
    frost_border_alpha=36,
    border_alpha=70,
    placeholder_top=(57, 43, 75),
    placeholder_bottom=(32, 24, 45),
    accent="#D4B9EC",
    decor_line="#765F89",
    page_fill="#282033",
    wash="#32243F",
    metal="#AF94C0",
    bloom="#84699D",
)


def _make_theme(renderer: Any, palette: _Palette) -> _Theme:
    theme = _Theme(
        gradient_top=palette.top,
        gradient_bottom=palette.bottom,
        border=palette.border,
        text_primary=palette.text_primary,
        text_secondary=palette.text_secondary,
        text_tertiary=palette.text_tertiary,
        pill_bg=palette.pill_bg,
        quote_bg=palette.quote_bg,
        divider=palette.divider,
        shadow_alpha=palette.shadow_alpha,
        stat_pill_bg=palette.stat_pill_bg,
        glow_alpha=palette.glow_alpha,
        frost_alpha=palette.frost_alpha,
        frost_border_alpha=palette.frost_border_alpha,
        border_alpha=palette.border_alpha,
        placeholder_top=palette.placeholder_top,
        placeholder_bottom=palette.placeholder_bottom,
    )
    # 用户自定义背景渐变必须覆盖本皮肤的默认色。
    if renderer.gradient_top:
        theme.gradient_top = _hex_to_rgb(renderer.gradient_top)
    if renderer.gradient_bottom:
        theme.gradient_bottom = _hex_to_rgb(renderer.gradient_bottom)
    return theme


def _accent_for(renderer: Any, palette: _Palette) -> tuple[str, tuple[int, int, int]]:
    if renderer.accent_color:
        return renderer.accent_color, _hex_to_rgb(renderer.accent_color)
    return palette.accent, _hex_to_rgb(palette.accent)


# ============================ 统计图标 ============================


@lru_cache(maxsize=24)
def _load_base(path_text: str, mtime_ns: int, file_size: int) -> Image.Image | None:
    _ = (mtime_ns, file_size)  # 两者是缓存键的一部分，保证同路径换图后重新读取。
    try:
        with Image.open(path_text) as im:
            im.load()
            return im.convert("RGBA")
    except Exception:
        logger.warning("莉莉卡片素材读取失败: %s", path_text, exc_info=True)
        return None


def _asset_source(filename: str, asset_dir: Any) -> tuple[str, int, int] | None:
    """返回（绝对路径, mtime_ns, size）；没有可用素材时返回 None。"""
    if not filename or asset_dir is None:
        return None
    try:
        path = Path(asset_dir).resolve(strict=False) / filename
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat()
    except (OSError, TypeError, ValueError):
        return None
    return str(path), info.st_mtime_ns, info.st_size


def _scale_asset(base: Image.Image, size: int) -> Image.Image:
    im = base.copy()
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        return im
    scale = min(size / iw, size / ih)
    return im.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), _LANCZOS)


@lru_cache(maxsize=24)
def _stat_icon_base(
    kind: str,
    size: int,
    rgb: tuple[int, int, int],
    path_text: str = "",
    mtime_ns: int = 0,
    file_size: int = 0,
) -> Image.Image:
    base = _load_base(path_text, mtime_ns, file_size) if path_text else None
    if base is not None:
        im = _scale_asset(base, max(1, size))
        alpha = im.getchannel("A")
        solid = Image.new("RGBA", im.size, (*rgb, 0))
        solid.putalpha(alpha)
        return solid
    return _fallback_stat_icon(kind, size, rgb)


def stat_icon(
    kind: str,
    size: int,
    rgb: tuple[int, int, int],
    asset_dir: Any = None,
) -> Image.Image:
    """返回独立统计图标；默认无素材时使用代码绘制，并始终返回副本。"""
    size = max(1, size)
    source = _asset_source(_STAT_FILE.get(kind, ""), asset_dir)
    if source is None:
        return _stat_icon_base(kind, size, rgb).copy()
    return _stat_icon_base(kind, size, rgb, *source).copy()


def _fallback_stat_icon(kind: str, size: int, rgb: tuple[int, int, int]) -> Image.Image:
    scale = 4
    side = max(1, size) * scale
    im = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    pen = ImageDraw.Draw(im)
    u = side / 24
    color = (*rgb, 255)

    def box(coords: tuple[float, float, float, float], radius: float = 3.0) -> None:
        pen.rounded_rectangle(
            tuple(round(v * u) for v in coords), radius=round(radius * u), fill=color
        )

    def cut(coords: list[tuple[float, float]], width: float = 1.7) -> None:
        pen.line(
            [(round(a * u), round(b * u)) for a, b in coords],
            fill=(0, 0, 0, 0),
            width=max(1, round(width * u)),
        )

    if kind == "view":
        box((2, 5, 22, 19), 5)
        pen.polygon(
            [(round(10 * u), round(9 * u)), (round(16 * u), round(12 * u)), (round(10 * u), round(15 * u))],
            fill=(0, 0, 0, 0),
        )
    elif kind == "like":
        box((3, 10, 7, 21), 1)
        pen.polygon(
            [
                (round(8 * u), round(11 * u)), (round(11 * u), round(7 * u)),
                (round(12 * u), round(3 * u)), (round(14 * u), round(3 * u)),
                (round(15 * u), round(5 * u)), (round(15 * u), round(10 * u)),
                (round(21 * u), round(10 * u)), (round(23 * u), round(12 * u)),
                (round(21 * u), round(20 * u)), (round(19 * u), round(21 * u)),
                (round(8 * u), round(21 * u)),
            ],
            fill=color,
        )
    elif kind == "coin":
        pen.ellipse((round(2 * u), round(2 * u), round(22 * u), round(22 * u)), fill=color)
        pen.polygon(
            [
                (round(12 * u), round(6 * u)), (round(17 * u), round(12 * u)),
                (round(12 * u), round(18 * u)), (round(7 * u), round(12 * u)),
            ],
            fill=(0, 0, 0, 0),
        )
    elif kind == "favorite":
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            radius = 10 if index % 2 == 0 else 4.6
            points.append(
                (round((12 + radius * math.cos(angle)) * u), round((12 + radius * math.sin(angle)) * u))
            )
        pen.polygon(points, fill=color)
    elif kind == "reply":
        box((2, 3, 22, 18), 5)
        pen.polygon(
            [(round(5 * u), round(15 * u)), (round(5 * u), round(22 * u)), (round(12 * u), round(15 * u))],
            fill=color,
        )
        cut([(7, 8), (17, 8)])
        cut([(7, 13), (14, 13)])
    elif kind == "share":
        pen.polygon(
            [
                (round(2 * u), round(21 * u)), (round(4 * u), round(14 * u)),
                (round(8 * u), round(10 * u)), (round(14 * u), round(8 * u)),
                (round(14 * u), round(3 * u)), (round(23 * u), round(12 * u)),
                (round(14 * u), round(20 * u)), (round(14 * u), round(15 * u)),
                (round(9 * u), round(15 * u)), (round(5 * u), round(18 * u)),
            ],
            fill=color,
        )
    else:  # danmaku
        box((2, 4, 22, 20), 3)
        cut([(6, 9), (18, 9)])
        cut([(6, 15), (13, 15)])
    return im.resize((max(1, size), max(1, size)), _LANCZOS)


# ============================ 文案与数据 ============================


def _duration_text(raw: Any) -> str:
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        total = max(0, int(raw))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    text = _card_text(str(raw))
    if not text:
        return ""
    if ":" in text:
        return text[:18]
    try:
        numeric = float(text)
    except ValueError:
        return text[:18]
    total = max(0, int(numeric))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _compact_stat_value(value: str) -> str:
    text = (value or "").strip()
    if text.isdigit():
        number = int(text)
        if number >= 100_000_000:
            return f"{number / 100_000_000:.1f}亿"
        if number >= 10_000:
            return f"{number / 10_000:.1f}万"
    return text


@dataclass(slots=True)
class _Stat:
    kind: str | None
    label: str
    value: str


def _parse_lily_stats(result: Any) -> list[_Stat]:
    stats: list[_Stat] = []
    for label, value in parse_stats_line(result.extra.get("stats_line")):
        clean_label = strip_emoji(label).strip()
        clean_value = strip_emoji(value).strip()
        if not clean_label:
            continue
        if clean_label.lower() in _DROP_STATS:
            # 时长固定在封面角标，不再占用统计位。
            continue
        kind = _STAT_KINDS.get(clean_label.lower()) or _STAT_KINDS.get(clean_label)
        stats.append(_Stat(kind=kind, label=clean_label, value=_compact_stat_value(clean_value)))
    # 已知的 B 站七项按固定语义顺序排列；未知标签保持原顺序。
    known = [s for s in stats if s.kind]
    if known and len(known) == len(stats):
        rank = {kind: index for index, kind in enumerate(_STAT_ORDER)}
        stats.sort(key=lambda s: rank.get(s.kind or "", len(rank)))
    return stats


# ============================ 统计布局 ============================


@dataclass(slots=True)
class _StatItem:
    kind: str | None
    label: str
    value: str
    x: int = 0
    width: int = 0


@dataclass(slots=True)
class _StatRow:
    items: list[_StatItem]
    cell_w: int = 0


@dataclass(slots=True)
class _StatLayout:
    rows: list[_StatRow]
    font_size: int
    label_size: int
    icon_size: int
    row_h: int
    row_gap: int
    single_row: bool
    evenly_spaced: bool
    text_primary: str
    text_secondary: str

    @property
    def height(self) -> int:
        if not self.rows:
            return 0
        return len(self.rows) * self.row_h + (len(self.rows) - 1) * self.row_gap


def stat_layout_summary(layout: Any) -> dict[str, Any]:
    """Small introspection helper kept for layout tests and maintenance scripts."""
    value = getattr(layout, "stats_layout", layout)
    rows = getattr(value, "rows", []) or []
    return {
        "rows": len(rows),
        "single_row": bool(getattr(value, "single_row", len(rows) == 1)),
        "items": sum(len(row.items) for row in rows),
        "font_size": int(getattr(value, "font_size", 0)),
        "icon_size": int(getattr(value, "icon_size", 0)),
        "row_h": int(getattr(value, "row_h", 0)),
        "max_right": max(
            (item.x + item.width for row in rows for item in row.items), default=0
        ),
    }


def _stat_text_width(
    renderer: Any, stat: _Stat, font_size: int, label_size: int, icon_size: int
) -> int:
    value_font = renderer._font(font_size, bold=True)
    label_font = renderer._font(label_size, bold=True)
    if stat.kind and stat.value:
        return icon_size + 5 + renderer._text_width(stat.value, value_font)
    if stat.kind:
        return renderer._text_width(stat.label, label_font)
    width = renderer._text_width(stat.label, label_font)
    if stat.value:
        width += 5 + renderer._text_width(stat.value, value_font)
    return width


def _build_stat_layout(
    renderer: Any,
    stats: list[_Stat],
    inner_w: int,
) -> _StatLayout:
    """Measure statistics once; drawing uses only the saved layout.

    The seven Bilibili counters are forced onto one row.  Font and icon sizes
    shrink until the widest cell fits, never dropping a counter or overflowing
    the content column for realistic values.
    """
    if not stats:
        return _StatLayout([], 20, 19, 20, 32, 10, False, False, "#000000", "#000000")

    u = max(0.4, float(getattr(renderer, "width", 960)) / 960.0)
    base_font = max(10, min(28, round(23 * u)))
    base_icon = max(10, min(28, round(24 * u)))
    gap = max(3, min(12, round(10 * u)))
    all_known = all(stat.kind and stat.value for stat in stats)
    single_row = all_known and len(stats) <= 7

    if all_known:
        if single_row:
            count = len(stats)
            for font_size in range(base_font, 8, -1):
                icon_size = max(8, min(base_icon, round(font_size * 1.08)))
                cell_w = (inner_w - gap * (count - 1)) // count
                if cell_w <= 8:
                    continue
                widest = max(
                    _stat_text_width(renderer, stat, font_size, max(8, font_size - 1), icon_size)
                    for stat in stats
                )
                if widest <= cell_w - 2:
                    items = []
                    for index, stat in enumerate(stats):
                        item = _StatItem(stat.kind, stat.label, stat.value)
                        item.width = _stat_text_width(
                            renderer, item, font_size, max(8, font_size - 1), icon_size
                        )
                        item.x = index * (cell_w + gap) + max(0, (cell_w - item.width) // 2)
                        items.append(item)
                    row_h = max(24, icon_size, renderer._line_height(renderer._font(font_size, bold=True))) + 8
                    return _StatLayout(
                        [_StatRow(items, cell_w)], font_size, max(8, font_size - 1), icon_size,
                        row_h, gap, True, True, "#000000", "#000000",
                    )
        # If a single row is impossible for pathological values, fall back to
        # equal columns and wrap only at the row boundary (no item is dropped).
        for cols in range(min(len(stats), 7), 0, -1):
            cell_w = (inner_w - gap * (cols - 1)) // cols
            if cell_w <= 8:
                continue
            for font_size in range(base_font, 8, -1):
                icon_size = max(8, min(base_icon, round(font_size * 1.08)))
                widest = max(
                    _stat_text_width(renderer, stat, font_size, max(8, font_size - 1), icon_size)
                    for stat in stats
                )
                if widest <= cell_w - 2:
                    rows: list[_StatRow] = []
                    for start in range(0, len(stats), cols):
                        chunk = stats[start : start + cols]
                        items = []
                        for index, stat in enumerate(chunk):
                            item = _StatItem(stat.kind, stat.label, stat.value)
                            item.width = _stat_text_width(
                                renderer, item, font_size, max(8, font_size - 1), icon_size
                            )
                            item.x = index * (cell_w + gap) + max(0, (cell_w - item.width) // 2)
                            items.append(item)
                        rows.append(_StatRow(items, cell_w))
                    row_h = max(24, icon_size, renderer._line_height(renderer._font(font_size, bold=True))) + 8
                    return _StatLayout(
                        rows, font_size, max(8, font_size - 1), icon_size, row_h, gap,
                        len(rows) == 1, True, "#000000", "#000000",
                    )

    # Unknown labels: keep the text label and greedily wrap by measured width.
    font_size = max(10, round(19 * u))
    label_size = max(10, font_size - 1)
    icon_size = max(10, round(20 * u))
    while font_size > 9:
        widths = [
            _stat_text_width(renderer, stat, font_size, label_size, icon_size) for stat in stats
        ]
        if max(widths) <= inner_w - 6:
            break
        font_size -= 1
        label_size = max(8, label_size - 1)
        icon_size = max(8, icon_size - 1)

    widths = [
        _stat_text_width(renderer, stat, font_size, label_size, icon_size) for stat in stats
    ]
    rows: list[_StatRow] = []
    current: list[_StatItem] = []
    cursor = 0
    for stat, width in zip(stats, widths):
        if current and cursor + width > inner_w:
            rows.append(_StatRow(current))
            current = []
            cursor = 0
        label, value = stat.label, stat.value
        if width > inner_w:
            # An unbroken provider label can be wider than an entire narrow card.
            if value:
                visible_icon = icon_size if stat.kind else 0
                label_room = max(1, inner_w // 2) if not stat.kind else 0
                if label_room:
                    label = renderer._fit_lines(label, renderer._font(label_size, bold=True), label_room, 1)[0]
                prefix = visible_icon or renderer._text_width(label, renderer._font(label_size, bold=True))
                value = renderer._fit_lines(value, renderer._font(font_size, bold=True), inner_w - prefix - 5, 1)[0]
            else:
                label = renderer._fit_lines(label, renderer._font(label_size, bold=True), inner_w, 1)[0]
            width = _stat_text_width(renderer, _Stat(stat.kind, label, value), font_size, label_size, icon_size)
        item = _StatItem(stat.kind, label, value, x=cursor, width=width)
        current.append(item)
        cursor += width + gap
    if current:
        rows.append(_StatRow(current))
    row_h = max(icon_size, renderer._line_height(renderer._font(font_size, bold=True))) + 10
    return _StatLayout(
        rows, font_size, label_size, icon_size, row_h, gap,
        len(rows) == 1, False, "#000000", "#000000",
    )


def _draw_stat_layout(
    canvas: Image.Image,
    renderer: Any,
    layout: _StatLayout,
    x0: int,
    y: int,
    inner_w: int,
    accent_rgb: tuple[int, int, int],
) -> None:
    _ = inner_w
    asset_dir = getattr(renderer, "card_assets_dir", None)
    value_font = renderer._font(layout.font_size, bold=True)
    label_font = renderer._font(layout.label_size, bold=True)
    value_lh = renderer._line_height(value_font)
    label_lh = renderer._line_height(label_font)
    for row_index, row in enumerate(layout.rows):
        row_y = y + row_index * (layout.row_h + layout.row_gap)
        for item in row.items:
            x = x0 + item.x
            if item.kind and item.value:
                icon = stat_icon(item.kind, layout.icon_size, accent_rgb, asset_dir)
                canvas.alpha_composite(icon, (x, row_y + (layout.row_h - icon.height) // 2))
                renderer._draw_text(
                    ImageDraw.Draw(canvas),
                    (x + layout.icon_size + 5, row_y + (layout.row_h - value_lh) // 2),
                    item.value, layout.font_size, layout.text_primary, bold=True,
                )
            else:
                renderer._draw_text(
                    ImageDraw.Draw(canvas),
                    (x, row_y + (layout.row_h - label_lh) // 2),
                    item.label, layout.label_size, layout.text_secondary, bold=True,
                )
                if item.value:
                    renderer._draw_text(
                        ImageDraw.Draw(canvas),
                        (x + renderer._text_width(item.label, label_font) + 5,
                         row_y + (layout.row_h - value_lh) // 2),
                        item.value, layout.font_size, layout.text_primary, bold=True,
                    )


# ============================ 布局计划 ============================


@dataclass(slots=True)
class _Plan(family._Plan):
    """The two skins share the card grid, byline and footer measurements."""
    palette: _Palette = _LIGHT
    cover_image: Image.Image | None = None


def _clean(value: Any) -> str:
    text = _card_text(value)
    return text if text else ""


def _as_path(value: Any) -> Path | None:
    if value is None:
        return None
    try:
        return Path(value)
    except TypeError:
        return None


def _font_size(renderer: Any, base: float, u: float, minimum: int, maximum: int) -> int:
    _ = renderer
    return max(minimum, min(maximum, round(base * u)))


def _space(base: float, u: float, minimum: int = 0) -> int:
    return max(minimum, round(base * u))


_TITLE_BREAK_AFTER = set(" |/、，。！？；：）」】》·")
_TITLE_BREAK_BEFORE = set("【「（《")


def _title_lines(renderer: Any, title: str, font: Any, width: int) -> list[str]:
    """Keep word boundaries near the end of each line and wrap CJK freely."""
    lines = []
    for paragraph in title.splitlines():
        units = list(renderer._text_units(paragraph.strip()))
        start = 0
        while start < len(units):
            end = start + 1
            while end < len(units) and renderer._text_width("".join(units[start:end + 1]), font) <= width:
                end += 1
            if end < len(units):
                # Do not allow a short opening tag to strand the rest of a CJK line.
                minimum = start + max(1, round((end - start) * 0.8))
                for pos in range(end, minimum - 1, -1):
                    if units[pos - 1] in _TITLE_BREAK_AFTER or units[pos] in _TITLE_BREAK_BEFORE:
                        end = pos
                        break
                else:
                    # Preserve an ordinary Latin word, even after a short opening word.
                    if units[end].isascii() and units[end].isalnum():
                        pos = end
                        while pos > start and units[pos - 1].isascii() and units[pos - 1].isalnum():
                            pos -= 1
                        if pos > start:
                            end = pos
            line = "".join(units[start:end]).strip()
            if line:
                lines.append(line)
            start = end
    return lines





def _place_title(
    renderer: Any, plan: _Plan, x: int, y: int, width: int, size: int | None = None
) -> int:
    if not plan.title:
        plan.title_y = None
        plan.title_lines = []
        return y
    size = size or plan.title_size
    font = renderer._font(size, bold=True)
    lines = _title_lines(renderer, plan.title, font, width)
    if 2 <= len(lines) <= 3 and len(plan.title) <= 90 and "\n" not in plan.title:
        widths = [renderer._text_width(line, font) for line in lines]
        if widths[-1] < max(widths) * 0.93:
            # Try a more even rag without adding lines or changing the source text.
            best = min(widths) / max(widths)
            for fraction in (0.90, 0.80, 0.70):
                candidate = _title_lines(renderer, plan.title, font, round(width * fraction))
                if len(candidate) == len(lines):
                    measured = [renderer._text_width(line, font) for line in candidate]
                    ratio = min(measured) / max(measured)
                    if ratio > best:
                        lines, best = candidate, ratio
    line_h = max(round(size * 1.23), renderer._line_height(font))
    plan.title_x = x
    plan.title_y = y
    plan.title_lines = lines
    plan.title_size = size
    plan.title_line_h = line_h
    plan.title_width = width
    return y + len(lines) * line_h


def _place_description(
    renderer: Any, result: Any, plan: _Plan, x: int, y: int, width: int
) -> int:
    body = _clean(result.text)
    if not body:
        plan.desc_y = None
        plan.desc_lines = []
        plan.desc_h = 0
        return y
    font = renderer._font(plan.desc_size)
    if renderer.desc_max_lines > 0:
        lines = renderer._fit_lines(body, font, width, renderer.desc_max_lines)
    else:
        # 0 表示不限行数；完整保留原文与段落空行。
        lines = renderer._wrap(body, font, width)
    line_h = max(round(plan.desc_size * 1.46), renderer._line_height(font) + 5)
    blank_h = max(8, round(plan.desc_size * 0.72))
    plan.desc_x = x
    plan.desc_y = y
    plan.desc_width = width
    plan.desc_lines = lines
    plan.desc_line_h = line_h
    plan.desc_blank_h = blank_h
    plan.desc_h = sum(line_h if line else blank_h for line in lines)
    return y + plan.desc_h


def _place_ai(renderer: Any, result: Any, plan: _Plan, y: int) -> int:
    if plan.layout == "immersive":
        # 沉浸布局任何路径都不加载 / 不展示 AI 摘要。
        plan.ai_y = None
        plan.ai_h = 0
        return y
    height = renderer._ai_summary_height(result, plan.inner_w)
    if not height:
        plan.ai_y = None
        plan.ai_h = 0
        return y
    y += _space(14, plan.u, 8)
    plan.ai_y = y
    plan.ai_h = height
    return y + height


def _place_tail(renderer: Any, result: Any, plan: _Plan, y: int, *, include_stats: bool = True) -> int:
    # 统计：七项 B 站统计强制单行实心图标，时长已单独放在封面。
    if include_stats and plan.stats_layout.rows:
        y += _space(24, plan.u, 12)
        plan.stats_y = y
        plan.stats_h = plan.stats_layout.height
        y += plan.stats_h

    online_text = _clean(result.extra.get("online") or "")
    if online_text:
        font = renderer._font(plan.online_size)
        lines = renderer._wrap(online_text, font, plan.inner_w - _space(24, plan.u, 12))
        if lines:
            y += _space(15, plan.u, 8)
            plan.online_y = y
            plan.online_lines = lines
            plan.online_line_h = max(round(plan.online_size * 1.5), renderer._line_height(font) + 4)
            y += len(lines) * plan.online_line_h

    warnings = [str(item) for item in (result.extra.get("limit_warnings") or []) if str(item).strip()]
    if warnings:
        height = renderer._warning_block_height(warnings, plan.inner_w)
        if height:
            y += _space(18, plan.u, 10)
            plan.warnings_y = y
            plan.warnings = warnings
            plan.warnings_h = height
            # helper 的绘制结束位置是 warnings_h + 20。
            y += height + 20

    grid_gap = max(6, round(_L.GRID_GAP * plan.u))
    grid_h, cols, rows, cell = renderer._grid_metrics(len(plan.grid), plan.inner_w, grid_gap)
    plan.grid_gap = grid_gap
    if grid_h:
        y += _space(20, plan.u, 10)
        plan.grid_y = y
        plan.grid_h = grid_h
        plan.grid_cols = cols
        plan.grid_rows = rows
        plan.grid_cell = cell
        y += grid_h + _space(20, plan.u, 8)

    quote_h = renderer._measure_quote(result.repost, plan.inner_w) if result.repost else 0
    if quote_h:
        y += _space(20, plan.u, 10)
        plan.quote_y = y
        plan.quote_h = quote_h
        y += quote_h + _space(20, plan.u, 8)
    return y


def _open_cover(renderer: Any, path: Path | None) -> Image.Image | None:
    if path is None:
        return None
    try:
        return renderer._open_image(path)
    except (OSError, ValueError, Image.DecompressionBombError):
        logger.warning("莉莉卡片封面读取失败，使用剩余内容排版: %s", path)
        return None


def _build_plan(renderer: Any, result: Any, images: dict[str, Any]) -> _Plan:
    layout = getattr(renderer, "layout_name", "standard")
    if layout not in _LAYOUTS:
        layout = "standard"
    dark = getattr(renderer, "theme_name", "dark") == "dark"
    palette = _DARK if dark else _LIGHT
    theme = _make_theme(renderer, palette)
    accent, accent_rgb = _accent_for(renderer, palette)
    width = max(320, int(getattr(renderer, "width", 960)))
    u = width / 960.0
    pad = _space(54, u, 29)
    inner_w = width - pad * 2

    plan = _Plan(
        layout=layout,
        theme=theme,
        palette=palette,
        dark=dark,
        accent=accent,
        accent_rgb=accent_rgb,
        width=width,
        pad=pad,
        inner_w=inner_w,
        u=u,
        text_primary=palette.text_primary,
        text_secondary=palette.text_secondary,
        text_tertiary=palette.text_tertiary,
    )
    plan.name_lines = []
    plan.meta_lines = []
    plan.title_lines = []
    plan.desc_lines = []
    plan.online_lines = []
    plan.warnings = []
    plan.grid = []
    plan.stats_layout = _build_stat_layout(renderer, [], inner_w)

    plan.brand_y = _space(26, u, 14)
    plan.brand_cn_size = _font_size(renderer, 19, u, 12, 22)
    plan.brand_en_size = _font_size(renderer, 16, u, 10, 18)
    plan.brand_line_y = _space(67, u, 37)
    plan.content_top = _space(95, u, 54)

    hero = _as_path(images.get("hero"))
    plan.cover_image = _open_cover(renderer, hero)
    video_cover = plan.cover_image is not None
    grid = [path for path in (_as_path(item) for item in (images.get("grid") or [])) if path is not None]
    while plan.cover_image is None and grid:
        hero = grid.pop(0)
        plan.cover_image = _open_cover(renderer, hero)
    if plan.cover_image is None:
        hero = None

    avatar = _as_path(images.get("avatar"))
    avatar_visible = bool(
        getattr(renderer, "show_avatar", True)
        and result.author is not None
        and avatar is not None
        and avatar.is_file()
    )
    plan.hero = hero
    plan.grid = grid
    plan.avatar_path = avatar if avatar_visible else None
    plan.avatar_size = _font_size(renderer, 60, u, 35, 68)
    plan.name_size = _font_size(renderer, 27, u, 17, 31)
    plan.meta_size = _font_size(renderer, 21, u, 13, 24)
    plan.title_size = _font_size(renderer, 35, u, 20, 40)
    plan.desc_size = _font_size(renderer, 25, u, 15, 29)
    plan.online_size = _font_size(renderer, 21, u, 13, 24)
    duration_raw = result.extra.get("duration")
    if not duration_raw:
        video = result.video
        if video is not None and video.duration:
            duration_raw = video.duration
        else:
            for content in result.audio_contents:
                if content.duration:
                    duration_raw = content.duration
                    break
    plan.duration_text = _duration_text(duration_raw)
    plan.is_video_hero = result.video is not None and video_cover

    author = result.author
    author_name = _clean(author.name) if author and author.name else ""
    platform_text = _clean(result.platform.display_name) or "网页"
    content_type = _clean(result.content_type) or "动态"
    ts = format_timestamp(result.timestamp) or ""
    platform_icon = load_platform_icon(
        getattr(result.platform, "name", ""), _font_size(renderer, 20, u, 12, 22)
    )
    plan.platform_icon = platform_icon

    meta_parts: list[str] = []
    if author is not None:
        plan.name_text = author_name or "未知作者"
    else:
        plan.name_text = platform_text
    if platform_icon is None:
        meta_parts.append(platform_text)
    meta_parts.append(content_type)
    if ts:
        meta_parts.append(ts)
    if plan.duration_text and hero is None:
        meta_parts.append(f"时长 {plan.duration_text}")
    plan.meta_text = " · ".join(part for part in meta_parts if part)

    plan.title = _clean(result.title)
    stats = _parse_lily_stats(result)
    plan.stats_layout = _build_stat_layout(renderer, stats, inner_w)
    plan.stats_layout.text_primary = palette.text_primary
    plan.stats_layout.text_secondary = palette.text_secondary

    if layout == "magazine":
        _plan_magazine(renderer, result, plan)
    elif layout == "immersive":
        _plan_immersive(renderer, result, plan)
    elif layout == "feed":
        _plan_feed(renderer, result, plan)
    else:
        _plan_standard(renderer, result, plan)
    return plan


def _place_media(renderer: Any, plan: _Plan, x: int, y: int, width: int) -> int:
    plan.hero_x, plan.hero_y, plan.hero_w = x, y, width
    image = plan.cover_image
    height = round(width * 9 / 16)
    if renderer.cover_full_size and image is not None:
        height = max(_space(100, plan.u, 56), min(round(width * image.height / image.width), round(width * _L.HERO_MAX_ASPECT)))
    plan.hero_h = height
    return y + height


def _place_body(renderer: Any, result: Any, plan: _Plan, cursor: int, *, include_stats: bool = True) -> None:
    top = cursor
    cursor = _place_description(renderer, result, plan, plan.pad, cursor, plan.inner_w)
    cursor = _place_ai(renderer, result, plan, cursor)
    cursor = _place_tail(renderer, result, plan, cursor, include_stats=include_stats)
    if cursor > top:
        _page_bounds(plan, top, cursor)
    _finish_plan(plan, cursor)


def _page_bounds(plan: _Plan, top: int, bottom: int) -> None:
    inset = plan.pad - _space(20, plan.u, 10)
    plan.page_box = (inset, top - _space(22, plan.u, 12),
                     plan.width - inset, bottom + _space(20, plan.u, 11))


def _plan_standard(renderer: Any, result: Any, plan: _Plan) -> None:
    u, pad, width = plan.u, plan.pad, plan.inner_w
    cursor = family._place_author(renderer, plan, pad, plan.content_top, width)
    if plan.title:
        cursor = _place_title(renderer, plan, pad, cursor + _space(20, u, 11), width)
    if plan.hero is not None:
        cursor = _place_media(renderer, plan, pad, cursor + _space(32, u, 18), width)
        cursor += _space(21, u, 12)
    if plan.stats_layout.rows:
        plan.stats_y = cursor + _space(18, u, 10)
        plan.stats_h = plan.stats_layout.height
        cursor = plan.stats_y + plan.stats_h
    _place_body(renderer, result, plan, cursor + _space(54, u, 29), include_stats=False)


def _plan_magazine(renderer: Any, result: Any, plan: _Plan) -> None:
    u, pad, width = plan.u, plan.pad, plan.inner_w
    top = plan.content_top + _space(7, u, 4)
    gap = _space(33, u, 18)
    cover_w = round(width * 0.46)
    col_w = width - cover_w - gap
    size = _font_size(renderer, 33, u, 20, 38)
    if plan.hero is not None and len(_title_lines(renderer, plan.title, renderer._font(size, bold=True), col_w)) <= 5:
        bottom = _place_media(renderer, plan, pad, top, cover_w)
        x = pad + cover_w + gap
        cursor = _place_title(renderer, plan, x, top, col_w, size)
        cursor = family._place_author(renderer, plan, x, cursor + _space(18, u, 10), col_w,
                                      _space(50, u, 30))
        cursor = max(bottom, cursor)
    else:
        cursor = _place_title(renderer, plan, pad, top, width, size)
        cursor = family._place_author(renderer, plan, pad, cursor + _space(15, u, 8), width)
        if plan.hero is not None:
            cursor = _place_media(renderer, plan, pad, cursor + _space(25, u, 14), width)
    _place_body(renderer, result, plan, cursor + _space(60, u, 33))


def _plan_immersive(renderer: Any, result: Any, plan: _Plan) -> None:
    u, pad, width = plan.u, plan.pad, plan.inner_w
    cursor = plan.content_top
    if plan.hero is not None:
        cursor = _place_media(renderer, plan, 0, _space(81, u, 45), plan.width)
        cursor += _space(29, u, 16)
    cursor = _place_title(renderer, plan, pad, cursor, width)
    if plan.title_lines:
        cursor += _space(16, u, 9)
    cursor = family._place_author(renderer, plan, pad, cursor, width)
    _place_body(renderer, result, plan, cursor + _space(45, u, 24))


def _plan_feed(renderer: Any, result: Any, plan: _Plan) -> None:
    u, pad, width = plan.u, plan.pad, plan.inner_w
    top = plan.content_top + _space(25, u, 14)
    cursor = family._place_author(renderer, plan, pad, top, width)
    cursor = _place_title(renderer, plan, pad, cursor + _space(18, u, 10), width)
    cursor = _place_description(renderer, result, plan, pad, cursor + _space(15, u, 8), width)
    cursor = _place_ai(renderer, result, plan, cursor)
    if plan.hero is not None:
        cursor = _place_media(renderer, plan, pad, cursor + _space(33, u, 18), width)
        cursor += _space(18, u, 10)
    cursor = _place_tail(renderer, result, plan, cursor)
    _page_bounds(plan, top, cursor)
    _finish_plan(plan, cursor)


def _finish_plan(plan: _Plan, content_end: int) -> None:
    plan.content_end = content_end
    bottom = plan.page_box[3] if plan.page_box else content_end
    plan.ending_y = bottom + _space(37, plan.u, 20)
    plan.footer_y = plan.ending_y + _space(137, plan.u, 74)
    plan.card_h = plan.footer_y + _space(71, plan.u, 41)


# ============================ 装饰绘制 ============================


def _soft_glow(
    canvas: Image.Image,
    box: tuple[float, float, float, float],
    color: str,
    alpha: int,
    blur: float,
) -> None:
    """Large soft light field at half resolution, composited at card size."""
    width, height = canvas.size
    scale = 0.5
    mask = Image.new("L", (max(1, round(width * scale)), max(1, round(height * scale))), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse(
        tuple(round(value * scale) for value in box),
        fill=max(0, min(255, alpha)),
    )
    # Blur coverage only: blurring straight RGBA also blends the transparent
    # black RGB channels into the light, producing grey metallic-looking bands.
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, blur * scale)))
    mask = mask.resize((width, height), _LANCZOS)
    light = Image.new("RGBA", (width, height), (*_hex_to_rgb(color), 255))
    light.putalpha(mask)
    canvas.alpha_composite(light)


def _draw_background(canvas: Image.Image, plan: _Plan, renderer: Any) -> Image.Image | None:
    filename = "black-veil-garden.png" if plan.dark else "white-veil-garden.png"
    source = _asset_source(filename, getattr(renderer, "card_assets_dir", None))
    artwork = _load_base(*source) if source else None
    width, height, u = plan.width, plan.card_h, plan.u
    if artwork is None:
        _soft_glow(
            canvas, (width * 0.40, -height * 0.13, width * 1.12, height * 0.53),
            plan.palette.bloom, 90 if plan.dark else 185, 116 * u,
        )
        return
    flower_y = 0
    if plan.layout == "immersive" and plan.hero_y is not None:
        flower_y = plan.hero_y + plan.hero_h
    art.draw_garden(canvas, artwork, plan.footer_y, flower_y, quiet_flower=plan.layout in ("magazine", "immersive"),
                    custom_gradient=bool(renderer.gradient_top or renderer.gradient_bottom))
    return artwork


def _draw_page(canvas: Image.Image, plan: _Plan) -> None:
    if plan.page_box is None:
        return
    u = plan.u
    box = plan.page_box
    if plan.dark:
        art.silver_panel(canvas, box, plan.palette.page_fill, plan.palette.decor_line, _space(15, u, 8))
    else:
        paper.cream_panel(canvas, box, paper=_hex_to_rgb(plan.palette.page_fill),
                          shadow=(203, 186, 215), outline=(207, 190, 215),
                          radius=_space(23, u, 13), shadow_offset=_space(5, u, 3))


def _draw_cover_mount(canvas: Image.Image, plan: _Plan) -> None:
    if plan.hero_y is None or plan.layout == "immersive":
        return
    u = plan.u
    x, y, w, h = plan.hero_x, plan.hero_y, plan.hero_w, plan.hero_h
    margin = _space(14, u, 8)
    box = (x - margin, y - margin, x + w + margin, y + h + margin)
    if plan.dark:
        art.silver_panel(canvas, box, "#30223D", "#A18BB5", _space(23, u, 12))
    else:
        paper.cream_panel(canvas, box, paper=(249, 245, 252), shadow=(209, 193, 218),
                          outline=(209, 192, 217), radius=_space(25, u, 13), scallop=True,
                          shadow_offset=_space(3, u, 2))


def _draw_cover(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.hero_y is None or plan.hero_h <= 0:
        return
    x, y, w, h = plan.hero_x, plan.hero_y, plan.hero_w, plan.hero_h
    image = plan.cover_image
    if image is None:
        return
    if renderer.cover_full_size:
        # The height limit also applies to tall remote covers. Letterbox at the limit so
        # the configured full-cover mode can still show both ends of the source.
        cover = Image.new("RGBA", (w, h), plan.palette.page_fill)
        scale = min(w / image.width, h / image.height)
        fitted_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        fitted = image.resize(fitted_size, _LANCZOS).convert("RGBA")
        cover.alpha_composite(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    else:
        cover = renderer._cover_fit(image, w, h).convert("RGBA")
    if plan.layout != "immersive" and not renderer.cover_full_size:
        if plan.dark:
            cover.putalpha(paper.bevel_mask(cover.size, _space(17, plan.u, 9)))
        else:
            cover = renderer._rounded_image(cover, _space(16, plan.u, 9))
    canvas.alpha_composite(cover, (x, y))
    d = ImageDraw.Draw(canvas)

    # 播放按钮严格跟随 RENDER_SHOW_PLAY_BUTTON，且只在视频封面出现。
    if plan.is_video_hero and renderer.show_play_button and min(w, h) >= _space(75, plan.u, 42):
        radius = max(18, min(42, round(38 * plan.u)))
        cx, cy = x + w // 2, y + h // 2
        renderer._glass(
            canvas, (cx - radius, cy - radius, cx + radius, cy + radius), radius,
            tint_rgb=(255, 255, 255), tint_alpha=38,
            border_rgb=(255, 255, 255), border_alpha=126, blur=max(4, round(8 * plan.u)),
        )
        d.polygon(
            [(cx - radius // 3, cy - radius // 2), (cx - radius // 3, cy + radius // 2), (cx + radius // 2, cy)],
            fill=(255, 255, 255, 242),
        )

    if plan.duration_text and h >= _space(58, plan.u, 32):
        size = _font_size(renderer, 20, plan.u, 12, 22)
        font = renderer._font(size, bold=True)
        text_w = renderer._text_width(plan.duration_text, font)
        badge_w = min(w - 24, text_w + _space(22, plan.u, 12))
        badge_h = max(22, _space(33, plan.u, 18))
        bx = x + w - badge_w - _space(14, plan.u, 7)
        by = y + h - badge_h - _space(14, plan.u, 7)
        d.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=_space(8, plan.u, 4), fill=(48, 36, 57, 224))
        renderer._draw_text(
            ImageDraw.Draw(canvas),
            (bx + (badge_w - text_w) // 2, by + (badge_h - renderer._line_height(font)) // 2),
            plan.duration_text, size, "#FFFFFF", bold=True,
        )


def _draw_immersive_extension(canvas: Image.Image, plan: _Plan) -> None:
    if plan.layout != "immersive" or plan.hero_y is None:
        return
    y = plan.hero_y + plan.hero_h
    end = plan.page_box[1] if plan.page_box else plan.content_end
    height = min(max(0, end - y), _space(220, plan.u, 120))
    if height < 2:
        return
    source_h = min(plan.hero_h, height)
    strip = canvas.crop((0, y - source_h, plan.width, y))
    strip = ImageOps.flip(strip).resize((plan.width, height), _LANCZOS)
    strip = strip.filter(ImageFilter.GaussianBlur(max(2, round(12 * plan.u))))
    mask = Image.new("L", (1, height))
    mask.putdata([round(76 * (1 - i / (height - 1)) ** 1.5) for i in range(height)])
    strip.putalpha(mask.resize(strip.size))
    canvas.alpha_composite(strip, (0, y))


def _draw_brand(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    d = ImageDraw.Draw(canvas)
    y, right = plan.brand_y, plan.width - plan.pad
    en_width = renderer._text_width(BRAND_EN, renderer._font(plan.brand_en_size))
    renderer._draw_text(d, (plan.pad, y), BRAND_CN, plan.brand_cn_size, plan.text_secondary)
    renderer._draw_text(d, (right - en_width, y + _space(2, plan.u, 1)), BRAND_EN,
                        plan.brand_en_size, plan.text_secondary)
    d.line((plan.pad, plan.brand_line_y, right, plan.brand_line_y), fill=plan.palette.border)


def _draw_description(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.desc_y is None:
        return
    d = ImageDraw.Draw(canvas)
    y = plan.desc_y
    for line in plan.desc_lines:
        if line:
            color = plan.text_secondary if line.startswith("*") else plan.text_primary
            renderer._draw_text(d, (plan.desc_x, y), line, plan.desc_size, color)
        y += plan.desc_line_h if line else plan.desc_blank_h


def _draw_stats(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.stats_y is None or not plan.stats_layout.rows:
        return
    y = plan.stats_y
    d = ImageDraw.Draw(canvas)
    box = (plan.pad - _space(8, plan.u, 4), y - _space(7, plan.u, 4),
           plan.width - plan.pad + _space(8, plan.u, 4), y + plan.stats_h + _space(7, plan.u, 4))
    if plan.dark:
        d.line((box[0], box[1], box[2], box[1]), fill=plan.palette.divider)
        d.line((box[0], box[3], box[2], box[3]), fill=plan.palette.divider)
    else:
        d.rounded_rectangle(box, radius=_space(16, plan.u, 9), fill=plan.palette.stat_pill_bg)
    _draw_stat_layout(
        canvas, renderer, plan.stats_layout, plan.pad, y, plan.inner_w, plan.accent_rgb
    )


def _draw_online(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.online_y is None:
        return
    d = ImageDraw.Draw(canvas)
    first_lh = renderer._line_height(renderer._font(plan.online_size))
    dot_y = plan.online_y + first_lh // 2
    d.ellipse(
        (plan.pad, dot_y - _space(4, plan.u, 2), plan.pad + _space(8, plan.u, 4), dot_y + _space(4, plan.u, 2)),
        fill=plan.accent,
    )
    y = plan.online_y
    for line in plan.online_lines:
        renderer._draw_text(d, (plan.pad + _space(20, plan.u, 9), y), line, plan.online_size, plan.accent)
        y += plan.online_line_h


def _shared_block(canvas: Image.Image, plan: _Plan, callback: Any) -> None:
    """Translate shared blocks from the renderer margin to the Lily text column."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    callback(layer, ImageDraw.Draw(layer))
    canvas.alpha_composite(layer, (plan.pad - _L.PAD, 0))


def _draw_ai(canvas: Image.Image, renderer: Any, result: Any, plan: _Plan) -> None:
    if plan.layout == "immersive" or plan.ai_y is None or not plan.ai_h:
        return
    _shared_block(
        canvas, plan,
        lambda layer, _draw: renderer._ai_summary_block(
            layer, plan.theme, result, plan.ai_y, plan.inner_w
        ),
    )


def _draw_tail(canvas: Image.Image, renderer: Any, result: Any, plan: _Plan) -> None:
    if plan.warnings_y is not None and plan.warnings:
        _shared_block(
            canvas, plan,
            lambda layer, draw: renderer._draw_warning_block(
                layer, draw, plan.theme, plan.warnings, plan.warnings_y, plan.inner_w
            ),
        )
    if plan.grid_y is not None and plan.grid:
        _shared_block(
            canvas, plan,
            lambda layer, draw: renderer._draw_grid_block(
                layer, draw, plan.theme, plan.grid, plan.grid_y, plan.inner_w, plan.grid_gap
            ),
        )
    if plan.quote_y is not None and result.repost is not None:
        _shared_block(
            canvas, plan,
            lambda layer, draw: renderer._draw_quote_block(
                layer, draw, plan.theme, plan.accent_rgb, result, plan.quote_y, plan.inner_w
            ),
        )


@lru_cache(maxsize=24)
def _ending_font(size: int) -> ImageFont.FreeTypeFont | None:
    candidates = (
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simkai.ttf",
        Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/System/Library/Fonts/Supplemental/Kaiti.ttc"),
    )
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


def _draw_ending(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    phrase = FOOTER_COPY["dark" if plan.dark else "light"]
    size = _font_size(renderer, 42, plan.u, 22, 46)
    font = _ending_font(size) or renderer._font(size)
    draw = ImageDraw.Draw(canvas)
    u = plan.u
    ink = plan.accent
    trace = plan.palette.metal

    # Draw the sprig at illustration scale to keep its fine contours smooth at chat size.
    sprig = Image.new("RGBA", (96, 124))
    pen = ImageDraw.Draw(sprig)
    pen.line([(12, 117), (21, 104), (32, 86), (42, 64), (53, 48)], fill=ink, width=4)
    pen.polygon([(31, 88), (21, 78), (16, 66), (14, 49), (21, 56),
                 (29, 66), (33, 78)], fill=trace)
    pen.polygon([(32, 87), (42, 74), (57, 68), (70, 66), (60, 77),
                 (47, 84)], fill=ink)
    # Three tapered tepals meet at the calyx of the inclined lily bud.
    pen.polygon([(48, 54), (46, 43), (49, 31), (57, 19), (64, 10),
                 (63, 24), (60, 41)], fill=trace)
    pen.polygon([(48, 54), (52, 39), (62, 26), (74, 15), (86, 6),
                 (82, 19), (73, 34), (61, 47)], fill=ink)
    pen.polygon([(49, 54), (60, 45), (74, 35), (88, 22), (82, 36),
                 (71, 48), (58, 55)], fill=trace)
    sprig = sprig.resize((_space(34, u, 19), _space(44, u, 24)), _LANCZOS)
    canvas.alpha_composite(sprig, (plan.pad + _space(4, u, 2), plan.ending_y))

    text_x = plan.pad + _space(47, u, 26)
    top = plan.ending_y + _space(7, u, 4)
    filename = "lettering-black.png" if plan.dark else "lettering-white.png"
    source = _asset_source(filename, getattr(renderer, "card_assets_dir", None))
    lettering = _load_base(*source) if source else None
    if lettering is not None and lettering.getbbox():
        lettering = lettering.crop(lettering.getbbox())
        width = min(round(size * 9.4), round(canvas.width * .65))
        height = max(1, round(width * lettering.height / lettering.width))
        mask = lettering.getchannel("A").resize((width, height), _LANCZOS)
        canvas.paste(ink, (text_x, top, text_x + width, top + height), mask)
    else:
        bbox = draw.textbbox((0, 0), phrase, font=font)
        draw.text((text_x, top - bbox[1]), phrase, font=font, fill=ink)
        width, height = bbox[2], bbox[3] - bbox[1]
    rule_y = min(plan.footer_y - _space(15, u, 8), top + height + _space(12, u, 7))
    rule_end = text_x + width + _space(30, u, 16)
    draw.line((text_x, rule_y, rule_end, rule_y), fill=trace, width=1)
    tip = _space(3, u, 2)
    draw.polygon(((rule_end - tip, rule_y), (rule_end, rule_y - tip),
                  (rule_end + tip, rule_y), (rule_end, rule_y + tip)), fill=ink)


# ============================ 绘制入口 ============================


def render_lily_card(
    renderer: Any, result: Any, images: dict[str, Any], out_path: Path
) -> Path:
    """Render Lily through the shared card worker and return the PNG path."""
    plan = _build_plan(renderer, result, images)
    canvas = renderer._gradient(
        (plan.width, plan.card_h), plan.theme.gradient_top, plan.theme.gradient_bottom
    ).convert("RGBA")

    artwork = _draw_background(canvas, plan, renderer)
    _draw_page(canvas, plan)
    if plan.layout == "feed" and artwork is not None and plan.page_box:
        art.page_flower(canvas, artwork, plan.page_box)
    _draw_cover_mount(canvas, plan)
    _draw_cover(canvas, renderer, plan)
    _draw_immersive_extension(canvas, plan)
    _draw_brand(canvas, renderer, plan)
    family._draw_author(canvas, renderer, plan)
    family._draw_title(canvas, renderer, plan)
    _draw_description(canvas, renderer, plan)
    _draw_ai(canvas, renderer, result, plan)
    _draw_stats(canvas, renderer, plan)
    _draw_online(canvas, renderer, plan)
    _draw_tail(canvas, renderer, result, plan)
    _draw_ending(canvas, renderer, plan)
    family._draw_footer(canvas, renderer, result, plan)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".lily-card-", suffix=".tmp", dir=out_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            # Preserve opaque cover pixels and give the card a clean silhouette in chat.
            mask = Image.new("L", canvas.size)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, plan.width - 1, plan.card_h - 1),
                                                   radius=_space(26, plan.u, 14), fill=255)
            canvas.putalpha(mask)
            canvas.save(file, "PNG", optimize=True)
        try:
            os.replace(temporary, out_path)
        except PermissionError:
            # Windows may lock the destination after a concurrent render publishes it.
            if not out_path.is_file():
                raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return out_path
