"""达妮娅「泡泡工坊」卡片皮肤：四种布局，两套材料语言。

2026-11-13 用户认可的浅色 / 暗色视觉在这里正式落地，而不是给旧模板换色：

* 浅色：奶油纸页、带横线的配方纸、透明泡泡、扇贝承托、剪纸窄带，
  以及斜切糖霜橘子蛋糕。
* 暗色：星云、切角玻璃、星轨、虚质方块、粒子与带环星球。

皮肤跟随 ``WEBUI_STYLE=denia``，布局仍然只有 ``standard / magazine /
immersive / feed`` 四种；本模块从 ``renderer.layout_name`` 读取布局，从
``renderer.theme_name`` 读取明暗，不新增第五种布局。

入口 ``render_denia_card(renderer, result, images, out_path)`` 保持同步，
图片收集、异步流水线与缓存仍由 :class:`ShareCardRenderer` 负责。所有装饰
均为 Pillow 代码绘制，不依赖 ``_review_scripts``、Windows 字体或运行时网络。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from astrbot.api import logger

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

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
from . import card_denia_art as art

__all__ = [
    "render_denia_card",
    "LAYOUT_VERSION",
    "ASSET_DIR",
    "FOOTER_COPY",
    "stat_layout_summary",
]

#: 布局 / 装饰调整后递增，保证旧 PNG 缓存不会混入新设计。
LAYOUT_VERSION = "denia-20261113-four-layouts-v4"

BRAND_CN = "达妮娅的泡泡工坊"
BRAND_EN = "DENIA’S BUBBLE WORKSHOP"

FOOTER_COPY = {
    "light": "甜的留一半，喜欢的分给你。",
    "dark": "星海很远，喜欢的就在这里。",
}

#: 统计图标资源目录（资源缺失时全部退回代码绘制）。
ASSET_DIR = Path(__file__).resolve().parent.parent / "resources" / "cards" / "denia"

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
    accent2: str
    bubble: str
    paper: str
    orbit: str
    cube: str
    stat: str
    decor_line: str
    page_fill: str
    page_shadow: str
    page_edge: str
    wash: str
    cake_shadow: str


_LIGHT = _Palette(
    top="#F2DCE6",
    bottom="#F6E3EB",
    border="#DDB6C5",
    text_primary="#65414F",
    text_secondary="#906C7B",
    text_tertiary="#906C7B",
    pill_bg="#FFF9F1",
    quote_bg="#FFF9F1",
    divider="#DDB6C5",
    shadow_alpha=54,
    stat_pill_bg="#FFF9F1",
    glow_alpha=20,
    frost_alpha=52,
    frost_border_alpha=42,
    border_alpha=55,
    placeholder_top=(255, 249, 241),
    placeholder_bottom=(249, 234, 240),
    accent="#B66F8C",
    accent2="#D398A8",
    bubble="#FFF9F1",
    paper="#FFF9F1",
    orbit="#B66F8C",
    cube="#D398A8",
    stat="#B66F8C",
    decor_line="#E5C2CA",
    page_fill="#FFF9F1",
    page_shadow="#DFB7C5",
    page_edge="#E5C2CB",
    wash="#F9EAF0",
    cake_shadow="#CB9DAA",
)

_DARK = _Palette(
    top="#100D20",
    bottom="#18122B",
    border="#61527F",
    text_primary="#F3EFFC",
    text_secondary="#C6BDD7",
    text_tertiary="#C6BDD7",
    pill_bg="#251D3A",
    quote_bg="#251D3A",
    divider="#61527F",
    shadow_alpha=148,
    stat_pill_bg="#251D3A",
    glow_alpha=34,
    frost_alpha=30,
    frost_border_alpha=44,
    border_alpha=60,
    placeholder_top=(53, 40, 74),
    placeholder_bottom=(26, 20, 43),
    accent="#C4ABE8",
    accent2="#7FB4EE",
    bubble="#C4ABE8",
    paper="#251D3A",
    orbit="#8878AE",
    cube="#8968AF",
    stat="#C4ABE8",
    decor_line="#61527F",
    page_fill="#211C34",
    page_shadow="#0B0916",
    page_edge="#88739F",
    wash="#322646",
    cake_shadow="#594A76",
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
def _load_base(path_text: str) -> Image.Image | None:
    try:
        with Image.open(path_text) as im:
            im.load()
            return im.convert("RGBA")
    except Exception:
        logger.warning("达妮娅卡片统计图标读取失败: %s", path_text, exc_info=True)
        return None


def _base_asset(filename: str) -> Image.Image | None:
    path = ASSET_DIR / filename
    if not path.is_file():
        return None
    return _load_base(str(path))


def _scale_asset(base: Image.Image, size: int) -> Image.Image:
    im = base.copy()
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        return im
    scale = min(size / iw, size / ih)
    return im.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), _LANCZOS)


@lru_cache(maxsize=24)
def _stat_icon_base(kind: str, size: int, rgb: tuple[int, int, int]) -> Image.Image:
    base = _base_asset(_STAT_FILE.get(kind, ""))
    if base is not None:
        im = _scale_asset(base, max(1, size))
        alpha = im.getchannel("A")
        solid = Image.new("RGBA", im.size, (*rgb, 0))
        solid.putalpha(alpha)
        return solid
    return _fallback_stat_icon(kind, size, rgb)


def stat_icon(kind: str, size: int, rgb: tuple[int, int, int]) -> Image.Image:
    """返回独立统计图标；缓存只存基准图，调用方可以安全修改副本。"""
    return _stat_icon_base(kind, max(1, size), rgb).copy()


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


def _parse_denia_stats(result: Any) -> list[_Stat]:
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
    *,
    force_single_row: bool = False,
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
    single_row = bool(force_single_row and all_known) or (all_known and len(stats) <= 7)

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
        for cols in range(len(stats), 0, -1):
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
        item = _StatItem(stat.kind, stat.label, stat.value, x=cursor, width=width)
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
    value_font = renderer._font(layout.font_size, bold=True)
    label_font = renderer._font(layout.label_size, bold=True)
    value_lh = renderer._line_height(value_font)
    label_lh = renderer._line_height(label_font)
    for row_index, row in enumerate(layout.rows):
        row_y = y + row_index * (layout.row_h + layout.row_gap)
        for item in row.items:
            x = x0 + item.x
            if item.kind and item.value:
                icon = stat_icon(item.kind, layout.icon_size, accent_rgb)
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
class _Plan:
    layout: str = "standard"
    theme: _Theme | None = None
    palette: _Palette = _LIGHT
    dark: bool = False
    accent: str = "#B66F8C"
    accent_rgb: tuple[int, int, int] = (182, 111, 140)
    width: int = 960
    pad: int = 52
    inner_w: int = 856
    u: float = 1.0
    text_primary: str = "#65414F"
    text_secondary: str = "#906C7B"
    text_tertiary: str = "#906C7B"
    brand_y: int = 27
    brand_cn_size: int = 19
    brand_en_size: int = 17
    brand_line_y: int = 68
    content_top: int = 96
    avatar_path: Path | None = None
    avatar_size: int = 58
    avatar_x: int = 52
    avatar_y: int | None = None
    text_x: int = 52
    name_text: str = ""
    name_lines: list[str] | None = None
    name_size: int = 28
    name_line_h: int = 36
    name_x: int = 52
    name_y: int = 0
    meta_text: str = ""
    meta_lines: list[str] | None = None
    meta_size: int = 19
    meta_line_h: int = 25
    meta_x: int = 52
    meta_y: int = 0
    platform_icon: Image.Image | None = None
    platform_icon_y: int = 0
    title: str = ""
    title_x: int = 52
    title_y: int | None = None
    title_lines: list[str] | None = None
    title_size: int = 35
    title_line_h: int = 49
    title_width: int = 856
    hero: Path | None = None
    hero_x: int = 52
    hero_y: int | None = None
    hero_w: int = 856
    hero_h: int = 0
    hero_placeholder: bool = False
    is_video_hero: bool = False
    duration_text: str = ""
    cover_style: str = "paper"
    desc_x: int = 52
    desc_y: int | None = None
    desc_width: int = 856
    desc_lines: list[str] | None = None
    desc_size: int = 26
    desc_line_h: int = 38
    desc_blank_h: int = 18
    desc_h: int = 0
    ai_y: int | None = None
    ai_h: int = 0
    stats_y: int | None = None
    stats_layout: _StatLayout | None = None
    stats_h: int = 0
    online_y: int | None = None
    online_lines: list[str] | None = None
    online_size: int = 21
    online_line_h: int = 32
    warnings_y: int | None = None
    warnings: list[str] | None = None
    warnings_h: int = 0
    grid_y: int | None = None
    grid: list[Path] | None = None
    grid_h: int = 0
    grid_cols: int = 0
    grid_rows: int = 0
    grid_cell: int = 0
    grid_gap: int = 12
    quote_y: int | None = None
    quote_h: int = 0
    page_box: tuple[int, int, int, int] | None = None
    page_kind: str = "cream"
    page_rule_y0: int | None = None
    page_rule_y1: int | None = None
    ribbon_box: tuple[int, int, int, int] | None = None
    section_y: int = 96
    ending_y: int = 0
    footer_y: int = 0
    card_h: int = 0
    content_end: int = 0


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


def _wrap_title_text(renderer: Any, text: str, font: Any, width: int) -> list[str]:
    """Word/punctuation aware title wrapping.

    ``ShareCardRenderer._wrap`` is intentionally character based (safe for
    CJK), but a narrow magazine column must not split an English word such as
    ``TAKE`` into ``TA`` / ``KE``.  This helper keeps all source characters
    while preferring the last space / punctuation break that fits.
    """
    lines: list[str] = []
    current = ""
    for char in renderer._text_units(text):
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if current and renderer._text_width(candidate, font) > width:
            cut = 0
            for index, value in enumerate(current):
                if value in _TITLE_BREAK_AFTER:
                    cut = index + 1
                elif value in _TITLE_BREAK_BEFORE and index > 0:
                    cut = index
            if cut > 0:
                head = current[:cut].rstrip()
                tail = current[cut:].lstrip()
                if head:
                    lines.append(head)
                current = tail + char
            else:
                lines.append(current)
                current = char
            # The retained tail may itself still be too wide (e.g. a long URL).
            while current and renderer._text_width(current, font) > width and len(current) > 1:
                piece = ""
                for value in renderer._text_units(current):
                    if piece and renderer._text_width(piece + value, font) > width:
                        break
                    piece += value
                if not piece:
                    break
                lines.append(piece)
                current = current[len(piece):]
        else:
            current = candidate
    if current:
        lines.append(current)
    return [line for line in lines if line] or ([text] if text else [])


def _title_lines(renderer: Any, title: str, font: Any, width: int) -> list[str]:
    if not title:
        return []
    if " | " in title:
        head, separator, tail = title.partition(" | ")
        first = head + separator
        if head and tail and renderer._text_width(first, font) <= width and renderer._text_width(tail, font) <= width:
            return [first, tail]
    return _wrap_title_text(renderer, title, font, width)


def _place_author(
    renderer: Any, plan: _Plan, x: int, y: int, width: int, avatar_size: int | None = None
) -> int:
    size = avatar_size or plan.avatar_size
    visible = plan.avatar_path is not None and renderer.show_avatar
    offset = size + _space(15, plan.u, 8) if visible else 0
    text_x = x + offset
    text_w = max(40, width - offset)
    name_font = renderer._font(plan.name_size, bold=True)
    name_lh = max(round(plan.name_size * 1.23), renderer._line_height(name_font))
    name_lines = renderer._wrap(plan.name_text, name_font, text_w) if plan.name_text else []
    if not plan.name_text:
        name_lines = [""]

    meta_font = renderer._font(plan.meta_size)
    meta_lh = max(round(plan.meta_size * 1.25), renderer._line_height(meta_font))
    icon_w = plan.platform_icon.width + _space(5, plan.u, 3) if plan.platform_icon is not None else 0
    meta_lines = (
        renderer._fit_lines(plan.meta_text, meta_font, max(40, text_w - icon_w), 2)
        if plan.meta_text else []
    )
    name_y = y
    meta_y = name_y + len(name_lines) * name_lh + _space(3, plan.u, 2)
    row_h = max(size if visible else 0, meta_y + len(meta_lines) * meta_lh - y)

    plan.avatar_size = size
    plan.avatar_x = x
    plan.avatar_y = y if visible else None
    plan.text_x = text_x
    plan.name_x = text_x
    plan.name_y = name_y
    plan.name_lines = name_lines
    plan.name_line_h = name_lh
    plan.meta_x = text_x + icon_w
    plan.meta_y = meta_y
    plan.meta_lines = meta_lines
    plan.meta_line_h = meta_lh
    plan.platform_icon_y = meta_y + max(0, (meta_lh - (plan.platform_icon.height if plan.platform_icon else 0)) // 2)
    return y + row_h


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
    line_h = max(round(size * 1.40), renderer._line_height(font) + 4)
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
    if include_stats and plan.stats_layout is not None and plan.stats_layout.rows:
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


def _media_height(renderer: Any, plan: _Plan, width: int) -> int:
    default = round(width * 9 / 16)
    return renderer._hero_aspect_height(plan.hero, width, default)


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
    pad = max(24, round(52 * u))
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

    # 品牌行：独立于内容，顶部左中右英。
    plan.brand_y = _space(27, u, 12)
    plan.brand_cn_size = _font_size(renderer, 19, u, 12, 22)
    plan.brand_en_size = _font_size(renderer, 17, u, 10, 20)
    plan.brand_line_y = _space(68, u, 34)
    plan.content_top = _space(96, u, 58)

    # 作者 / 平台 / 时间。
    hero = _as_path(images.get("hero"))
    grid = [path for path in (_as_path(item) for item in (images.get("grid") or [])) if path is not None]
    if hero is None and grid:
        # 没有视频封面时，第一张图集图作为视觉封面，其余仍进入图集。
        hero = grid.pop(0)
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
    plan.avatar_size = _font_size(renderer, 60, u, 36, 64)
    plan.name_size = _font_size(renderer, 28, u, 17, 32)
    plan.meta_size = _font_size(renderer, 19, u, 12, 22)
    plan.title_size = _font_size(renderer, 35, u, 20, 40)
    plan.desc_size = _font_size(renderer, 25, u, 15, 30)
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
    plan.is_video_hero = result.video is not None and images.get("hero") is not None

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
    stats = _parse_denia_stats(result)
    force_single = len(stats) >= 7
    plan.stats_layout = _build_stat_layout(
        renderer, stats, inner_w, force_single_row=force_single
    )
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


def _first_content_y(plan: _Plan) -> int:
    values = [
        value
        for value in (plan.desc_y, plan.stats_y, plan.ai_y, plan.online_y,
                      plan.warnings_y, plan.grid_y, plan.quote_y)
        if value is not None
    ]
    return min(values) if values else plan.section_y


def _plan_standard(renderer: Any, result: Any, plan: _Plan) -> None:
    width, u, pad, inner_w = plan.width, plan.u, plan.pad, plan.inner_w
    cursor = plan.content_top

    # 已认可的标准稿：作者、标题在封面前，统计紧随封面，原文单独承托。
    plan.section_y = cursor
    cursor = _place_author(renderer, plan, pad, cursor, inner_w)
    cursor += _space(21, u, 10)
    cursor = _place_title(renderer, plan, pad, cursor, inner_w)
    if plan.title_lines:
        cursor += _space(28, u, 12)

    if plan.hero is not None:
        plan.hero_x = pad
        plan.hero_y = cursor
        plan.hero_w = inner_w
        plan.hero_h = _media_height(renderer, plan, inner_w)
        plan.cover_style = "bevel" if plan.dark else "round"
        cursor += plan.hero_h + _space(24, u, 12)
    else:
        plan.hero_y = None
        plan.hero_h = 0

    if plan.stats_layout.rows:
        cursor += _space(20, u, 10)
        plan.stats_y = cursor
        plan.stats_h = plan.stats_layout.height
        cursor += plan.stats_h + _space(66, u, 34)
    elif not plan.dark:
        cursor += _space(44, u, 22)

    body_top = cursor
    cursor = _place_description(renderer, result, plan, pad, cursor, inner_w)
    if plan.desc_y is not None:
        cursor += _space(16, u, 8)
    cursor = _place_ai(renderer, result, plan, cursor)
    cursor = _place_tail(renderer, result, plan, cursor, include_stats=False)
    plan.content_end = cursor

    page_top = body_top - _space(24, u, 12)
    plan.page_box = (pad - _space(22, u, 10), page_top, width - pad + _space(18, u, 8), cursor + _space(26, u, 12))
    plan.page_kind = "dark" if plan.dark else "cream"
    plan.page_rule_y0 = plan.desc_y
    plan.page_rule_y1 = None if plan.desc_y is None else plan.desc_y + plan.desc_h
    if not plan.dark:
        plan.ribbon_box = (width - pad - _space(190, u, 100), page_top - _space(40, u, 20),
                           width - pad, page_top - _space(2, u, 1))
    _finish_plan(plan)


def _plan_magazine(renderer: Any, result: Any, plan: _Plan) -> None:
    width, u, pad, inner_w = plan.width, plan.u, plan.pad, plan.inner_w
    cursor = plan.content_top + _space(8, u, 2)
    gap = _space(26, u, 12)
    left_w = max(180, round(inner_w * 0.46))
    right_w = inner_w - left_w - gap
    left_x = pad
    right_x = pad + left_w + gap

    plan.hero_x = left_x
    plan.hero_y = cursor
    plan.hero_w = left_w
    plan.hero_h = _media_height(renderer, plan, left_w)
    if plan.hero is None:
        plan.hero_placeholder = True
    plan.cover_style = "bevel" if plan.dark else "round"
    cover_bottom = cursor + plan.hero_h + _space(32, u, 14)

    # 右栏：标题在上，作者 / 平台信息在下。
    title_size = _font_size(renderer, 31, u, 18, 36)
    right_y = cursor - _space(2, u, 0)
    right_y = _place_title(renderer, plan, right_x, right_y, right_w, title_size)
    if plan.title_lines:
        right_y += _space(20, u, 8)
    right_y = _place_author(
        renderer, plan, right_x, right_y, right_w,
        _font_size(renderer, 50, u, 32, 54),
    )
    section_bottom = max(cover_bottom, right_y) + _space(74 if not plan.dark else 34, u, 32 if not plan.dark else 14)
    plan.section_y = section_bottom

    body_top = section_bottom
    cursor = _place_description(renderer, result, plan, pad, body_top, inner_w)
    if plan.desc_y is not None:
        cursor += _space(16, u, 8)
    cursor = _place_ai(renderer, result, plan, cursor)
    cursor = _place_tail(renderer, result, plan, cursor)
    plan.content_end = cursor

    page_top = body_top - _space(28, u, 12)
    plan.page_box = (pad - _space(22, u, 10), page_top, width - pad + _space(18, u, 8), cursor + _space(26, u, 12))
    plan.page_kind = "dark" if plan.dark else "cream"
    plan.page_rule_y0 = plan.desc_y
    plan.page_rule_y1 = None if plan.desc_y is None else plan.desc_y + plan.desc_h
    if not plan.dark:
        plan.ribbon_box = (right_x + right_w - _space(170, u, 90), page_top - _space(40, u, 20),
                           right_x + right_w, page_top - _space(2, u, 1))
    _finish_plan(plan)


def _plan_immersive(renderer: Any, result: Any, plan: _Plan) -> None:
    width, u, pad, inner_w = plan.width, plan.u, plan.pad, plan.inner_w
    image_top = _space(82, u, 48)
    if plan.hero is not None:
        image_h = renderer._hero_aspect_height(plan.hero, width, round(width * 9 / 16))
        plan.hero_x = 0
        plan.hero_w = width
        plan.cover_style = "bleed"
    else:
        # 无图回退仍使用同一皮肤：给空的媒体位一个奶油 / 玻璃承托，
        # 而不是把整段标题直接放在裸背景上。
        plan.hero_placeholder = True
        image_h = _space(260, u, 150)
        plan.hero_x = pad
        plan.hero_w = inner_w
        plan.cover_style = "bevel" if plan.dark else "round"
    plan.hero_y = image_top
    plan.hero_h = image_h

    # 标题 / 身份落在图像下缘的柔化延展带里，而不是压在人物主体上。
    ext_top = image_top + image_h
    cursor = ext_top + _space(34, u, 18)
    title_size = _font_size(renderer, 35, u, 20, 40)
    cursor = _place_title(renderer, plan, pad, cursor, inner_w, title_size)
    if plan.title_lines:
        cursor += _space(22, u, 10)
    cursor = _place_author(renderer, plan, pad, cursor, inner_w)
    plan.section_y = cursor
    # 图像本身保持完整比例；标题 / 身份落在下缘的柔化延展带里。
    plan.hero_h = image_h

    body_top = cursor + _space(32, u, 14)
    cursor = _place_description(renderer, result, plan, pad, body_top, inner_w)
    if plan.desc_y is not None:
        cursor += _space(16, u, 8)
    # 沉浸布局无论如何都不显示 AI 摘要。
    plan.ai_y = None
    plan.ai_h = 0
    cursor = _place_tail(renderer, result, plan, cursor)
    plan.content_end = cursor

    page_top = max(ext_top + _space(90, u, 40), body_top - _space(24, u, 10))
    plan.page_box = (pad - _space(22, u, 10), page_top, width - pad + _space(18, u, 8), cursor + _space(26, u, 12))
    plan.page_kind = "dark" if plan.dark else "cream"
    plan.page_rule_y0 = plan.desc_y
    plan.page_rule_y1 = None if plan.desc_y is None else plan.desc_y + plan.desc_h
    _finish_plan(plan)


def _plan_feed(renderer: Any, result: Any, plan: _Plan) -> None:
    width, u, pad, inner_w = plan.width, plan.u, plan.pad, plan.inner_w
    cursor = plan.content_top + _space(30, u, 14)
    plan.section_y = cursor
    cursor = _place_author(renderer, plan, pad, cursor, inner_w)
    cursor += _space(20, u, 8)
    cursor = _place_title(renderer, plan, pad, cursor, inner_w, _font_size(renderer, 33, u, 19, 38))
    if plan.title_lines:
        cursor += _space(18, u, 8)
    cursor = _place_description(renderer, result, plan, pad, cursor, inner_w)
    if plan.desc_y is not None:
        cursor += _space(16, u, 8)
    cursor = _place_ai(renderer, result, plan, cursor)

    # 作者 → 标题 → 正文之后才是媒体。
    if plan.hero is not None:
        cursor += _space(46, u, 20)
        plan.hero_x = pad
        plan.hero_y = cursor
        plan.hero_w = inner_w
        plan.hero_h = _media_height(renderer, plan, inner_w)
        plan.cover_style = "bevel" if plan.dark else "round"
        if not plan.dark:
            plan.ribbon_box = (width - pad - _space(190, u, 100), plan.hero_y - _space(42, u, 18),
                               width - pad, plan.hero_y - _space(2, u, 0))
        cursor += plan.hero_h + _space(30, u, 12)
    else:
        plan.hero_y = None
        plan.hero_h = 0

    cursor = _place_tail(renderer, result, plan, cursor)
    plan.content_end = cursor

    page_top = plan.content_top - _space(12, u, 6)
    plan.page_box = (pad - _space(22, u, 10), page_top, width - pad + _space(18, u, 8), cursor + _space(26, u, 12))
    plan.page_kind = "dark" if plan.dark else "cream"
    plan.page_rule_y0 = plan.desc_y
    plan.page_rule_y1 = None if plan.desc_y is None else plan.desc_y + plan.desc_h
    if not plan.dark and plan.ribbon_box is None:
        plan.ribbon_box = (width - pad - _space(190, u, 100), page_top - _space(20, u, 8),
                           width - pad, page_top + _space(20, u, 8))
    _finish_plan(plan)


def _finish_plan(plan: _Plan) -> None:
    u = plan.u
    plan.ending_y = plan.content_end + _space(44, u, 24)
    footer_gap = _space(108, u, 70) if not plan.dark else _space(92, u, 62)
    plan.footer_y = plan.ending_y + footer_gap
    plan.card_h = plan.footer_y + _space(78, u, 54)


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


def _draw_background(canvas: Image.Image, plan: _Plan) -> None:
    width, height, u = plan.width, plan.card_h, plan.u
    if plan.dark:
        _soft_glow(canvas, (width * 0.56, -height * 0.11, width * 1.16, height * 0.42), "#6A4A97", 158, 110 * u)
        _soft_glow(canvas, (-width * 0.18, height * 0.42, width * 0.68, height * 0.86), "#304D77", 95, 135 * u)
        _soft_glow(canvas, (width * 0.66, height * 0.76, width * 1.18, height * 1.02), "#70538D", 92, 110 * u)
        rng = random.Random(width * 97 + height)
        d = ImageDraw.Draw(canvas)
        for _ in range(34):
            left = rng.random() < 0.5
            if left:
                x = rng.randrange(max(1, round(10 * u)), max(2, round(38 * u)))
            else:
                x = rng.randrange(max(1, width - round(38 * u)), max(2, width - round(8 * u)))
            y = rng.randrange(round(80 * u), max(round(81 * u), height - round(20 * u)))
            radius = rng.choice((0.8, 1.0, 1.0, 1.6, 2.1)) * u
            alpha = rng.randrange(55, 190)
            d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(205, 194, 248, alpha))
        for x, y, r in (
            (width - 58 * u, 108 * u, 3.4 * u),
            (width - 136 * u, 208 * u, 2.2 * u),
            (54 * u, plan.content_top + 170 * u, 2.0 * u),
        ):
            art.star(canvas, x, y, max(1.2, r), _hex_to_rgb(plan.palette.accent))
    else:
        _soft_glow(canvas, (-width * 0.26, -height * 0.10, width * 0.58, height * 0.34), "#FFF6E8", 205, 138 * u)
        _soft_glow(canvas, (width * 0.58, height * 0.60, width * 1.14, height * 1.02), "#F9CBB7", 128, 125 * u)
        # 透明泡泡：贴近页边和封面，不压住正文。
        bubbles = [
            (width - 96 * u, 156 * u, 132 * u, 0.92),
            (width - 226 * u, 68 * u, 58 * u, 0.88),
            (width - 22 * u, 342 * u, 57 * u, 0.78),
            (20 * u, plan.content_top + 210 * u, 42 * u, 0.60),
            (width - 56 * u, plan.ending_y - 18 * u, 32 * u, 0.88),
        ]
        for cx, cy, radius, opacity in bubbles:
            art.bubble(canvas, cx, cy, radius, opacity=opacity)


def _draw_immersive_extension(
    canvas: Image.Image, renderer: Any, plan: _Plan
) -> None:
    """柔和反射延展：把图像下缘延伸成标题 / 身份所在的渐变带。"""
    if plan.layout != "immersive" or plan.hero_y is None or plan.hero_h <= 0:
        return
    image_bottom = plan.hero_y + plan.hero_h
    page_top = plan.page_box[1] if plan.page_box is not None else plan.section_y
    ext_h = max(40, page_top + _space(22, plan.u, 10) - image_bottom)
    if plan.hero is not None:
        try:
            fitted = renderer._cover_fit(
                renderer._open_image(plan.hero), plan.width, plan.hero_h
            )
            strip = fitted.crop(
                (0, max(0, round(plan.hero_h * 0.62)), plan.width, plan.hero_h)
            )
            strip = ImageOps.flip(strip).resize(
                (plan.width, ext_h), Image.Resampling.LANCZOS
            )
            strip = strip.filter(ImageFilter.GaussianBlur(max(2, round(7 * plan.u))))
            canvas.alpha_composite(strip.convert("RGBA"), (0, image_bottom))
        except Exception:
            logger.warning("达妮娅沉浸式延展绘制失败，使用主题渐变", exc_info=True)
    # 左侧阅读遮罩更实，右侧保留图像纹理；底端完全融入纸页 / 玻璃。
    tone = _hex_to_rgb(plan.palette.page_fill if plan.dark else plan.palette.paper)
    overlay = Image.new("RGBA", (plan.width, ext_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    bottom_alpha = 238 if plan.dark else 232
    for yy in range(ext_h):
        t = yy / max(1, ext_h - 1)
        base = 112 if plan.dark else 88
        alpha = round(base + (bottom_alpha - base) * (t ** 1.12))
        od.line((0, yy, plan.width, yy), fill=(*tone, alpha))
    # 左侧进一步压暗 / 提亮，保证标题与作者文字有稳定对比。
    left = Image.new("RGBA", (plan.width, ext_h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(left)
    for yy in range(ext_h):
        t = yy / max(1, ext_h - 1)
        alpha = round((34 if plan.dark else 52) * (1.0 - t * 0.55))
        ld.line((0, yy, round(plan.width * 0.56), yy), fill=(*tone, alpha))
    overlay.alpha_composite(left)
    canvas.alpha_composite(overlay, (0, image_bottom))
    d = ImageDraw.Draw(canvas)
    if plan.dark:
        d.line(
            (0, image_bottom, plan.width - _space(62, plan.u, 30), image_bottom),
            fill=(*_hex_to_rgb("#A98AC9"), 230), width=max(1, round(2 * plan.u)),
        )
    else:
        d.line(
            (0, image_bottom, plan.width - _space(120, plan.u, 50), image_bottom),
            fill=(*_hex_to_rgb("#F7E4DB"), 235), width=1,
        )


def _draw_page(canvas: Image.Image, plan: _Plan) -> None:
    if plan.page_box is None:
        return
    x0, y0, x1, y1 = plan.page_box
    x1 = min(plan.width, x1)
    y1 = min(plan.card_h, y1)
    if x1 <= x0 or y1 <= y0:
        return
    if plan.dark:
        art.dark_panel(
            canvas, (x0, y0, x1, y1), fill=_hex_to_rgb(plan.palette.page_fill),
            light=_hex_to_rgb("#79658F"), dark=_hex_to_rgb("#67577E"),
            cut=_space(22, plan.u, 10),
        )
        d = ImageDraw.Draw(canvas)
        rail_x = x0 + _space(22, plan.u, 10)
        if y1 - _space(44, plan.u, 18) > y0 + _space(44, plan.u, 18):
            d.line(
                (rail_x, y0 + _space(44, plan.u, 18), rail_x, y1 - _space(44, plan.u, 18)),
                fill=(*_hex_to_rgb("#53436E"), 210), width=max(1, round(1.6 * plan.u)),
            )
            for yy in (y0 + 44, y0 + 100, y1 - 100, y1 - 44):
                if y1 - 44 > y0 + 44:
                    d.ellipse(
                        (rail_x - 2.4 * plan.u, yy - 2.4 * plan.u, rail_x + 2.4 * plan.u, yy + 2.4 * plan.u),
                        fill=_hex_to_rgb(plan.palette.accent),
                    )
        # 玻璃边缘的星光只落在面板边线，不进入正文。
        for xx in (x0 + 24 * plan.u, x1 - 30 * plan.u):
            art.star(canvas, xx, y0 + 15 * plan.u, max(1.2, 2.0 * plan.u), _hex_to_rgb(plan.palette.accent))
            art.star(canvas, xx + 8 * plan.u, y1 - 11 * plan.u, max(1.0, 1.6 * plan.u), _hex_to_rgb(plan.palette.accent))
    else:
        art.cream_panel(
            canvas, (x0, y0, x1, y1), paper=_hex_to_rgb(plan.palette.page_fill),
            shadow=_hex_to_rgb(plan.palette.page_shadow), outline=_hex_to_rgb(plan.palette.page_edge),
            radius=_space(24, plan.u, 11), scallop=False,
        )
        d = ImageDraw.Draw(canvas)
        # 配方纸的细横线与左缘竖线，只铺在简介行距上。
        if plan.page_rule_y0 is not None and plan.page_rule_y1 is not None:
            left = x0 + _space(22, plan.u, 10)
            right = x1 - _space(22, plan.u, 10)
            y = plan.page_rule_y0 + plan.desc_line_h * 0.72
            while y < plan.page_rule_y1:
                x = left + _space(8, plan.u, 4)
                while x < right:
                    d.line((x, y, min(right, x + _space(7, plan.u, 3)), y), fill="#F0DFD8", width=1)
                    x += _space(18, plan.u, 8)
                y += plan.desc_line_h
            d.line(
                (left, plan.page_rule_y0, left, plan.page_rule_y1),
                fill=(*_hex_to_rgb("#E7C3CC"), 190), width=max(1, round(1.3 * plan.u)),
            )


def _draw_cover_mount(canvas: Image.Image, plan: _Plan) -> None:
    if plan.hero_y is None or plan.hero_h <= 0 or plan.cover_style == "bleed":
        return
    u = plan.u
    margin_x = _space(16, u, 7)
    margin_y = _space(18, u, 8)
    box = (
        plan.hero_x - margin_x, plan.hero_y - margin_y,
        plan.hero_x + plan.hero_w + margin_x, plan.hero_y + plan.hero_h + margin_y,
    )
    if plan.dark:
        art.dark_panel(
            canvas, box, fill=_hex_to_rgb("#251F39"), light=_hex_to_rgb("#9D84B4"),
            dark=_hex_to_rgb("#5E4975"), cut=_space(24, u, 12),
        )
        # 星轨是媒体框右上角的结构边，而不是孤立徽章。
        cx = plan.hero_x + plan.hero_w - _space(14, u, 6)
        cy = plan.hero_y + _space(8, u, 4)
        rx = min(plan.hero_w * 0.27, _space(195, u, 90))
        art.orbit(canvas, cx, cy, rx, rx * 0.38, -38, (*_hex_to_rgb("#5D507D"), 210), max(1.0, 1.2 * u))
        art.orbit(canvas, cx + 2 * u, cy, rx * 0.93, rx * 0.31, -38, (*_hex_to_rgb("#8878AE"), 220), max(1.0, 1.3 * u))
    else:
        art.cream_panel(
            canvas, box, paper=_hex_to_rgb(plan.palette.page_fill),
            shadow=_hex_to_rgb(plan.palette.page_shadow), outline=_hex_to_rgb(plan.palette.page_edge),
            radius=_space(31, u, 14), scallop=True,
        )
        d = ImageDraw.Draw(canvas)
        d.line(
            (box[0] + _space(16, u, 8), box[1] + _space(20, u, 9), box[0] + _space(16, u, 8), box[3] - _space(38, u, 16)),
            fill=(*_hex_to_rgb("#E9B2C5"), 235), width=max(1, round(2 * u)),
        )
        d.line(
            (box[2] - _space(16, u, 8), box[1] + _space(20, u, 9), box[2] - _space(16, u, 8), box[3] - _space(38, u, 16)),
            fill=(*_hex_to_rgb("#E9B2C5"), 235), width=max(1, round(2 * u)),
        )


def _draw_cover(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.hero_y is None or plan.hero_h <= 0:
        return
    x, y, w, h = plan.hero_x, plan.hero_y, plan.hero_w, plan.hero_h
    cover = None
    if plan.hero is not None:
        try:
            cover = renderer._cover_fit(renderer._open_image(plan.hero), w, h)
        except Exception:
            cover = None
            logger.warning("达妮娅卡片封面读取失败，使用主题占位", exc_info=True)
    placeholder = cover is None
    if cover is None:
        cover = renderer._gradient(
            (w, h), plan.theme.placeholder_top, plan.theme.placeholder_bottom
        ).convert("RGBA")
        cover.alpha_composite(Image.new("RGBA", (w, h), (*plan.accent_rgb, 34)))

    cover = cover.convert("RGBA")
    if plan.cover_style == "round":
        cover = renderer._rounded_image(cover, _space(16, plan.u, 8))
    elif plan.cover_style == "bevel":
        mask = art.bevel_mask((w, h), _space(24, plan.u, 10))
        cover = cover.convert("RGBA")
        cover.putalpha(ImageChops.multiply(cover.getchannel("A"), mask))
    # bleed: full-width photograph, no corner mask.
    canvas.alpha_composite(cover, (x, y))
    if placeholder:
        if plan.dark:
            art.cube(canvas, x + w * 0.58, y + h * 0.52, max(18, h * 0.22))
        else:
            art.bubble(canvas, x + w * 0.46, y + h * 0.58, max(24, h * 0.22), opacity=0.95)
            art.bubble(canvas, x + w * 0.72, y + h * 0.34, max(12, h * 0.10), opacity=0.9)
    if plan.dark and plan.cover_style == "bleed":
        # 沉浸式暗色：星轨压在图像右上边缘，作为延展带的结构入口。
        cx = x + w - _space(30, plan.u, 14)
        cy = y + _space(12, plan.u, 6)
        rx = min(w * 0.22, _space(190, plan.u, 90))
        art.orbit(canvas, cx, cy, rx, rx * 0.34, -32, (*_hex_to_rgb("#8878AE"), 220), max(1.0, 1.3 * plan.u))
        art.orbit(canvas, cx - 3 * plan.u, cy, rx * 0.90, rx * 0.28, -32, (*_hex_to_rgb("#5D507D"), 200), max(1.0, 1.1 * plan.u))

    d = ImageDraw.Draw(canvas)
    if plan.cover_style == "round":
        d.rounded_rectangle(
            (x, y, x + w - 1, y + h - 1), radius=_space(16, plan.u, 8),
            outline=(255, 255, 255, 116), width=1,
        )
    elif plan.cover_style == "bevel":
        d.line(
            [(x, y), (x + w - _space(24, plan.u, 10), y), (x + w, y + _space(24, plan.u, 10))],
            fill=(*_hex_to_rgb("#D2B9EA"), 210), width=max(1, round(1.4 * plan.u)),
        )

    # 播放按钮严格跟随 RENDER_SHOW_PLAY_BUTTON，且只在视频封面出现。
    if plan.is_video_hero and renderer.show_play_button:
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

    if plan.duration_text and plan.hero is not None:
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


def _draw_brand(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    d = ImageDraw.Draw(canvas)
    cn_size = plan.brand_cn_size
    en_size = plan.brand_en_size
    # 英文列使用同一字体族，测量后再决定是否缩小，避免越过右边界。
    en_font = renderer._font(en_size)
    max_en = plan.inner_w * 0.48
    while en_size > 9 and renderer._text_width(BRAND_EN, en_font) > max_en:
        en_size -= 1
        en_font = renderer._font(en_size)
    # CJK fonts give the curly apostrophe a full-width advance and can also add
    # a visible gap after it.  Draw the three Latin fragments with hand kerning
    # so the approved ``DENIA’S`` wordmark stays tight without loading Segoe UI.
    left, mark, right = BRAND_EN.partition("’")
    left_w = renderer._text_width(left, en_font)
    mark_w = renderer._text_width(mark, en_font) if mark else 0
    right_w = renderer._text_width(right, en_font)
    tracking = max(0, mark_w - _space(8, plan.u, 4)) if mark else 0
    en_w = left_w + mark_w + right_w - tracking
    x = plan.width - plan.pad - en_w
    y = plan.brand_y + _space(2, plan.u, 0)
    renderer._draw_text(d, (plan.pad, plan.brand_y), BRAND_CN, cn_size, plan.text_secondary)
    renderer._draw_text(d, (x, y), left, en_size, plan.text_secondary)
    x += left_w
    if mark:
        renderer._draw_text(d, (x, y), mark, en_size, plan.text_secondary)
        x += mark_w - tracking
    renderer._draw_text(d, (x, y), right, en_size, plan.text_secondary)
    d.line(
        (plan.pad, plan.brand_line_y, plan.width - plan.pad, plan.brand_line_y),
        fill=(*_hex_to_rgb(plan.palette.border), 235), width=1,
    )


def _draw_author(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    d = ImageDraw.Draw(canvas)
    if plan.avatar_y is not None and plan.avatar_path is not None:
        try:
            avatar = renderer._circle_avatar(renderer._open_image(plan.avatar_path), plan.avatar_size)
            canvas.alpha_composite(avatar, (plan.avatar_x, plan.avatar_y))
            ring = Image.new("RGBA", (plan.avatar_size, plan.avatar_size), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse(
                (1, 1, plan.avatar_size - 2, plan.avatar_size - 2),
                outline=_with_alpha(plan.accent_rgb, 190), width=max(1, round(2 * plan.u)),
            )
            canvas.alpha_composite(ring, (plan.avatar_x, plan.avatar_y))
        except Exception:
            logger.warning("达妮娅卡片作者头像绘制失败，保留文字信息", exc_info=True)
    if plan.name_lines:
        y = plan.name_y
        for line in plan.name_lines:
            if line:
                renderer._draw_text(d, (plan.name_x, y), line, plan.name_size, plan.text_primary, bold=True)
            y += plan.name_line_h
    if plan.platform_icon is not None:
        canvas.alpha_composite(
            plan.platform_icon,
            (plan.meta_x - plan.platform_icon.width - _space(5, plan.u, 3), plan.platform_icon_y),
        )
    if plan.meta_lines:
        y = plan.meta_y
        for line in plan.meta_lines:
            if line:
                renderer._draw_text(d, (plan.meta_x, y), line, plan.meta_size, plan.text_tertiary)
            y += plan.meta_line_h


def _draw_title(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    if plan.title_y is None:
        return
    d = ImageDraw.Draw(canvas)
    y = plan.title_y
    for line in plan.title_lines:
        if line:
            renderer._draw_text(d, (plan.title_x, y), line, plan.title_size, plan.text_primary, bold=True)
        y += plan.title_line_h


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
    if plan.dark:
        d.line(
            (plan.pad, y - _space(9, plan.u, 4), plan.width - plan.pad, y - _space(9, plan.u, 4)),
            fill=(*_hex_to_rgb("#6B5686"), 220), width=1,
        )
        d.line(
            (plan.pad, y + plan.stats_h + _space(9, plan.u, 4), plan.width - plan.pad,
             y + plan.stats_h + _space(9, plan.u, 4)),
            fill=(*_hex_to_rgb("#493858"), 210), width=1,
        )
    else:
        d.rounded_rectangle(
            (plan.pad - _space(10, plan.u, 4), y - _space(8, plan.u, 4),
             plan.width - plan.pad + _space(10, plan.u, 4), y + plan.stats_h + _space(8, plan.u, 4)),
            radius=_space(15, plan.u, 7), fill=_hex_to_rgb(plan.palette.wash),
        )
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
    """Run a shared renderer block on a translated layer (44px -> Denia pad)."""
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


def _draw_ribbon(canvas: Image.Image, plan: _Plan) -> None:
    if plan.dark or plan.ribbon_box is None:
        return
    x0, y0, x1, y1 = plan.ribbon_box
    if x1 <= x0 or y1 <= y0:
        return
    d = ImageDraw.Draw(canvas)
    mid = (y0 + y1) // 2
    d.polygon(
        [(x0 + 12, y0), (x1, y0), (x1 - 12, mid), (x1, y1), (x0 + 12, y1), (x0, mid)],
        fill=_hex_to_rgb("#EECBD7"),
    )
    size = max(18, round((x1 - x0) * 0.54))
    doll_x = x0 + (x1 - x0 - size) / 2
    art.paper_dolls(canvas, doll_x, y0 + (y1 - y0) * 0.10, size, _hex_to_rgb("#B97994"))


def _draw_ending(canvas: Image.Image, renderer: Any, plan: _Plan) -> None:
    u, pad, width = plan.u, plan.pad, plan.width
    y = plan.ending_y
    d = ImageDraw.Draw(canvas)
    phrase = FOOTER_COPY["dark" if plan.dark else "light"]
    phrase_size = _font_size(renderer, 24, u, 12, 27)
    phrase_font = renderer._font(phrase_size)
    art_w = _space(176, u, 94)
    available = max(80, width - pad * 2 - art_w)
    while phrase_size > 10 and renderer._text_width(phrase, phrase_font) > available:
        phrase_size -= 1
        phrase_font = renderer._font(phrase_size)

    if plan.dark:
        # 底部星座线与右侧虚质方块 / 带环星球属于同一套结构。
        points = [
            (pad, y + _space(10, u, 4)),
            (pad + _space(190, u, 90), y + _space(10, u, 4)),
            (pad + _space(214, u, 102), y - _space(7, u, 3)),
            (pad + _space(360, u, 160), y - _space(7, u, 3)),
            (pad + _space(384, u, 170), y + _space(10, u, 4)),
        ]
        d.line(points, fill=(*_hex_to_rgb(plan.palette.border), 190), width=max(1, round(1.2 * u)), joint="curve")
        for px, py in points:
            d.ellipse(
                (px - 2.2 * u, py - 2.2 * u, px + 2.2 * u, py + 2.2 * u),
                fill=_hex_to_rgb(plan.palette.accent),
            )
        cx = width - pad - _space(100, u, 50)
        cy = y + _space(34, u, 16)
        art.orbit(canvas, cx, cy, _space(100, u, 48), _space(25, u, 12), -18,
                  (*_hex_to_rgb("#85709F"), 220), max(1.0, 1.4 * u))
        art.cube(canvas, cx, y + _space(18, u, 8), _space(50, u, 24))
        renderer._draw_text(d, (pad, y), phrase, phrase_size, plan.accent)
    else:
        d.line(
            (pad, y - _space(12, u, 5), pad + _space(600, u, 260), y - _space(12, u, 5)),
            fill=(*_hex_to_rgb("#E5C2CA"), 230), width=1,
        )
        renderer._draw_text(d, (pad, y), phrase, phrase_size, plan.accent)
        cake_z = max(0.46, 1.16 * u)
        cake_x = width - pad - 145 * cake_z - _space(8, u, 4)
        art.cake(canvas, cake_x, y - _space(60, u, 28), cake_z)
        art.bubble(canvas, width - pad - _space(26, u, 12), y - _space(12, u, 6), _space(31, u, 15), opacity=0.9)
        d.line(
            (cake_x - _space(12, u, 5), y + _space(78, u, 36), width - pad, y + _space(78, u, 36)),
            fill=(*_hex_to_rgb(plan.palette.cake_shadow), 235), width=max(1, round(1.4 * u)),
        )


def _draw_footer(canvas: Image.Image, renderer: Any, result: Any, plan: _Plan) -> None:
    d = ImageDraw.Draw(canvas)
    y = plan.footer_y
    d.line(
        (plan.pad, y, plan.width - plan.pad, y),
        fill=(*_hex_to_rgb(plan.palette.decor_line), 235), width=1,
    )
    size = _font_size(renderer, 18, plan.u, 11, 20)
    font = renderer._font(size)
    wm_size = _font_size(renderer, 19, plan.u, 11, 21)
    wm_font = renderer._font(wm_size, bold=True)
    wm_text = (renderer.watermark or "").strip()
    wm_w = renderer._text_width(wm_text, wm_font) if wm_text else 0
    wm_group = wm_w + _space(14, plan.u, 6) if wm_text else 0
    url = short_url(result.url, 70)
    avail = plan.inner_w - wm_group - _space(20, plan.u, 10)
    if url:
        url = renderer._fit_lines(url, font, max(60, avail), 1)[0]
        renderer._draw_text(d, (plan.pad, y + _space(27, plan.u, 12)), url, size, plan.text_tertiary)
    if wm_text:
        wm_x = plan.width - plan.pad - wm_w
        dot_w = _space(10, plan.u, 5)
        dot_cy = y + _space(27, plan.u, 12) + renderer._line_height(wm_font) // 2
        d.ellipse(
            (wm_x - dot_w - _space(8, plan.u, 4), dot_cy - dot_w / 2,
             wm_x - _space(8, plan.u, 4), dot_cy + dot_w / 2),
            fill=plan.accent,
        )
        renderer._draw_text(
            d, (wm_x, y + _space(27, plan.u, 12)), wm_text, wm_size, plan.accent, bold=True
        )


# ============================ 绘制入口 ============================


def _render_denia_sync(
    renderer: Any, result: Any, images: dict[str, Any], out_path: Path
) -> Path:
    plan = _build_plan(renderer, result, images)
    canvas = renderer._gradient(
        (plan.width, plan.card_h), plan.theme.gradient_top, plan.theme.gradient_bottom
    ).convert("RGBA")

    _draw_background(canvas, plan)
    _draw_page(canvas, plan)
    _draw_cover_mount(canvas, plan)
    _draw_cover(canvas, renderer, plan)
    _draw_immersive_extension(canvas, renderer, plan)
    _draw_brand(canvas, renderer, plan)
    _draw_author(canvas, renderer, plan)
    _draw_title(canvas, renderer, plan)
    _draw_description(canvas, renderer, plan)
    _draw_ai(canvas, renderer, result, plan)
    _draw_stats(canvas, renderer, plan)
    _draw_online(canvas, renderer, plan)
    _draw_tail(canvas, renderer, result, plan)
    _draw_ribbon(canvas, plan)
    _draw_ending(canvas, renderer, plan)
    _draw_footer(canvas, renderer, result, plan)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path


def render_denia_card(
    renderer: Any, result: Any, images: dict[str, Any], out_path: Path
) -> Path:
    """``style='denia'`` 的正式入口；由 ShareCardRenderer 在同步线程调用。"""
    return _render_denia_sync(renderer, result, images, out_path)

