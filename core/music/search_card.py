# 本文件参考 MusicUID（MIT License）的搜索结果卡片交互思路实现，
# 上游项目：https://github.com/Xbaiyz12/MusicUID
# 本仓库为其改编版本，完整归属与许可证见项目根目录 README「许可与致谢」。

"""音乐搜索结果卡片渲染（沿用「希望解析」分享卡片视觉语言）。

用户在群里搜歌时会收到一张**带编号的候选卡片**，随后用「听2」
这类指令按序号选歌。本模块用 Pillow 在本地画这张卡片。

视觉一致性
----------
本模块**不另起一套设计**，而是逐项对齐 ``core/card_renderer.py``（希望解析器
分享卡片）的既有语言，保证同一插件里两种卡片看起来是同一家：

======================  =====================================================
本模块                  复用自 card_renderer
======================  =====================================================
画布渐变 + 圆角遮罩      ``_Theme.gradient_top/bottom``、``_L.RADIUS=30``
accent 径向光晕         ``_radial_glow`` + ``Theme.glow_alpha``
卡片投影 + 1px 描边      ``_L.SHADOW_INSET/BLUR``、``Theme.shadow_alpha``
顶部 accent 短横条       ``_L.HEAD_BAR_W=64 / HEAD_BAR_H=6``、渐隐曲线
平台药丸（毛玻璃+圆点）   ``_draw_flat_badge``、``_L.HEAD_PILL_H=42``
封面等比裁剪 + 圆角      ``_cover_fit``、``_rounded_image``
页脚分隔线 + 右下角水印   ``Theme.divider``、``WM_DOT``+``WM_DOT_GAP``
字号阶梯                 ``F_TITLE/F_DESC/F_NAME/F_FOOT`` 同源
======================  =====================================================

``card_renderer`` 顶层会 import astrbot，在纯测试环境下可能导入失败；此时
本模块退回一份**等价的内置主题副本**（数值与 ``_THEMES`` 保持一致），
保证单文件可独立运行。公共渲染器本身不做任何修改。

约束
----
- 纯本地绘制，**不联网、不下载远程字体或封面**
- 封面图由调用方（controller）下载后通过 ``options['covers']`` 传入
- 缺图 / 坏图 / 超大图一律用同风格占位，不让整张卡片失败
- 绘图是 CPU 密集同步操作，统一经 ``asyncio.to_thread`` 执行
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from ..platform_icons import load_platform_icon

try:  # Pillow 是唯一硬依赖，缺失时延迟报错以便其它模块照常导入
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFilter = ImageFont = None  # type: ignore[assignment]

_LANCZOS = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', 1)


def _load_card_renderer() -> Any:
    """尽力导入 core.card_renderer 复用其常量/工具；失败返回 None 走内置副本。"""
    try:
        from .. import card_renderer
        return card_renderer
    except ImportError:  # pragma: no cover
        return None


_CR = _load_card_renderer()

PLATFORM_LABELS: dict[str, str] = {
    "netease": "网易云音乐",
    "qqmusic": "QQ音乐",
    "qq": "QQ音乐",
    "163": "网易云音乐",
    "tencent": "QQ音乐",
}
PLATFORM_COLORS: dict[str, str] = {
    "netease": "#FB7299",
    "qqmusic": "#31C27C",
    "default": "#8B7CF6",
}

MAX_CANDIDATES = 10
DEFAULT_CANDIDATES = 5
_FONT_CACHE_LIMIT = 96          # 防止长时间运行后字体缓存无限增长
_MAX_COVER_BYTES = 8 * 1024 * 1024
_MAX_COVER_PIXELS = 40_000_000
_FORMATS = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp"}

_MEASURE_DRAW: Any = None


def _measure() -> Any:
    global _MEASURE_DRAW
    if _MEASURE_DRAW is None and Image is not None:
        _MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return _MEASURE_DRAW


# ---------------------------------------------------------------------------
# 主题：优先用 card_renderer 的 _THEMES，否则用等价的内置副本
# ---------------------------------------------------------------------------

class _Theme:
    """与 card_renderer._Theme 同构的轻量副本（字段名刻意保持一致）。"""

    def __init__(self, **kw: Any) -> None:
        self.gradient_top = kw["gradient_top"]
        self.gradient_bottom = kw["gradient_bottom"]
        self.border = kw["border"]
        self.text_primary = kw["text_primary"]
        self.text_secondary = kw["text_secondary"]
        self.text_tertiary = kw["text_tertiary"]
        self.pill_bg = kw["pill_bg"]
        self.quote_bg = kw["quote_bg"]
        self.divider = kw["divider"]
        self.shadow_alpha = kw["shadow_alpha"]
        self.stat_pill_bg = kw["stat_pill_bg"]
        self.glow_alpha = kw["glow_alpha"]
        self.frost_alpha = kw["frost_alpha"]
        self.frost_border_alpha = kw["frost_border_alpha"]
        self.border_alpha = kw["border_alpha"]
        self.placeholder_top = kw["placeholder_top"]
        self.placeholder_bottom = kw["placeholder_bottom"]


_FALLBACK_THEMES = {
    # 与 card_renderer._THEMES 数值一致
    "dark": _Theme(
        gradient_top=(36, 43, 63), gradient_bottom=(18, 22, 31), border=(255, 255, 255),
        text_primary=(245, 247, 252), text_secondary=(174, 182, 200), text_tertiary=(123, 133, 152),
        pill_bg=(255, 255, 255), quote_bg=(255, 255, 255), divider=(255, 255, 255),
        shadow_alpha=130, stat_pill_bg=(255, 255, 255), glow_alpha=30,
        frost_alpha=14, frost_border_alpha=26, border_alpha=24,
        placeholder_top=(44, 51, 71), placeholder_bottom=(20, 24, 35),
    ),
    "light": _Theme(
        gradient_top=(255, 255, 255), gradient_bottom=(241, 244, 249), border=(27, 34, 51),
        text_primary=(26, 33, 48), text_secondary=(85, 96, 122), text_tertiary=(140, 149, 169),
        pill_bg=(27, 34, 51), quote_bg=(27, 34, 51), divider=(27, 34, 51),
        shadow_alpha=55, stat_pill_bg=(27, 34, 51), glow_alpha=16,
        frost_alpha=10, frost_border_alpha=20, border_alpha=14,
        placeholder_top=(228, 233, 242), placeholder_bottom=(243, 246, 251),
    ),
}


def _hex_to_rgb(value: Any) -> tuple[int, int, int]:
    """与 card_renderer._hex_to_rgb 同语义（接受 RGB 元组或 hex 字符串）。"""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return (int(value[0]), int(value[1]), int(value[2]))
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return (139, 124, 246)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (139, 124, 246)


def get_theme(name: str) -> _Theme:
    """取指定主题对象：能拿到 card_renderer 的就复用它的（字段天然一致）。"""
    key = name if name in _FALLBACK_THEMES else "dark"
    if _CR is not None:
        themes = getattr(_CR, "_THEMES", None)
        theme = themes.get(key) if isinstance(themes, dict) else None
        if theme is not None:
            return theme  # type: ignore[return-value]
    return _FALLBACK_THEMES[key]


# ---------------------------------------------------------------------------
# 文本安全处理
# ---------------------------------------------------------------------------

_EMOJI_RE = getattr(_CR, "_EMOJI_RE", None) or re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿️]"
)
_VARIATION_RE = re.compile(r"[️︎]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")


def safe_text(value: Any, limit: int = 60) -> str:
    """规整成可安全绘制的单行文本：去 emoji/控制字符、NFKC 归一、超长硬截断。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_RE.sub("", text)
    text = _VARIATION_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def platform_label(platform: Any) -> str:
    key = safe_text(platform, limit=12).lower()
    if not key:
        return "音乐"
    return PLATFORM_LABELS.get(key, key.upper()[:10])


def platform_color(platform: Any) -> tuple[int, int, int]:
    key = safe_text(platform, limit=12).lower()
    return _hex_to_rgb(PLATFORM_COLORS.get(key, PLATFORM_COLORS["default"]))


def normalize_accent(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if _CR is not None:
        fn = getattr(_CR, "normalize_hex_color", None)
        if callable(fn):
            got = fn(text)
            if got:
                return got
    if re.match(r"^#?[0-9a-fA-F]{6}$", text):
        return "#" + text.lstrip("#").upper()
    return None


def format_duration(seconds: Any) -> str:
    """秒 → ``m:ss`` / ``h:mm:ss``；缺失或非法返回空串（不显示时长）。"""
    if seconds is None or isinstance(seconds, bool):
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        return ""
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# 字体（缓存键含 bold，避免粗细串味；带容量上限）
# ---------------------------------------------------------------------------

_FONT_CACHE: dict[tuple[str | None, int, bool], Any] = {}


def _discover_fonts(custom_path: Any = None) -> tuple[str | None, str | None]:
    if _CR is not None:
        fn = getattr(_CR, "_discover_fonts", None)
        if callable(fn):
            try:
                reg, bold = fn(custom_path or None)
                if reg:
                    return reg, bold or reg
            except Exception:  # pragma: no cover
                pass
    if custom_path:
        p = Path(str(custom_path)).expanduser()
        if p.is_file():
            return str(p), str(p)
        if p.is_dir():
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                found = sorted(p.glob(ext))
                if found:
                    return str(found[0]), str(found[-1] if len(found) > 1 else found[0])
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc",
                 "NotoSansCJK-Regular.ttc", "wqy-zenhei.ttc", "PingFang.ttc",
                 "Hiragino Sans GB.ttc", "arialuni.ttf"):
        for d in ("C:/Windows/Fonts", "/System/Library/Fonts",
                  "/usr/share/fonts/opentype/noto", "/usr/share/fonts/truetype/wqy"):
            p = Path(d) / name
            if p.exists():
                return str(p), str(p)
    return None, None


def _font(size: int, *, bold: bool = False, font_path: Any = None) -> Any:
    if ImageFont is None:
        return None
    key = (str(font_path) if font_path else None, int(size), bool(bold))
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    regular, heavy = _discover_fonts(font_path)
    path = heavy if (bold and heavy) else regular
    obj = None
    if path:
        try:
            obj = ImageFont.truetype(path, int(size))
        except Exception:  # pragma: no cover - 字体文件损坏
            obj = None
    if obj is None:
        try:
            obj = ImageFont.load_default(size=int(size))
        except TypeError:  # pragma: no cover
            obj = ImageFont.load_default()
    if len(_FONT_CACHE) >= _FONT_CACHE_LIMIT:
        _FONT_CACHE.clear()
    _FONT_CACHE[key] = obj
    return obj


def _text_w(text: str, font: Any) -> int:
    if not text:
        return 0
    d = _measure()
    if d is None or font is None:
        return len(text) * 10
    try:
        return int(d.textlength(text, font=font))
    except Exception:  # pragma: no cover
        try:
            l, t, r, b = d.textbbox((0, 0), text, font=font)
            return int(r - l)
        except Exception:
            return len(text) * 10


def _ellipsize(text: str, font: Any, max_w: int) -> str:
    if _text_w(text, font) <= max_w:
        return text
    out = ""
    for ch in text:
        if _text_w(out + ch + "…", font) > max_w:
            break
        out += ch
    return (out + "…") if out else "…"


def wrap_text(text: str, font: Any, max_w: int, max_lines: int = 2) -> list[str]:
    """按像素宽度折行（逐字符，兼容中文与无空格长串），行数最多 ``max_lines``。"""
    text = text or ""
    if max_lines <= 0 or max_w <= 0 or not text:
        return []
    if _text_w(text, font) <= max_w:
        return [text]
    lines: list[str] = []
    cur = ""
    for ch in text:
        if _text_w(cur + ch, font) <= max_w:
            cur += ch
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if len(lines) == max_lines:
            break
        lines.append(ch)
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and sum(len(x) for x in lines) < len(text):
        last = lines[-1]
        while last and _text_w(last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines


# ---------------------------------------------------------------------------
# 绘图原语（与 card_renderer 同款做法）
# ---------------------------------------------------------------------------

def _gradient(size: tuple[int, int], top: tuple[int, int, int],
              bottom: tuple[int, int, int]) -> Any:
    w, h = size
    grad = Image.new("RGB", (1, max(h, 1)))
    for y in range(max(h, 1)):
        ratio = y / max(h - 1, 1)
        grad.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * ratio)
                                   for i in range(3)))
    return grad.resize((w, h))


def _radial_glow(w: int, h: int, rgb: tuple[int, int, int], alpha: int) -> Any:
    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(base).ellipse((-w // 3, -h // 2, w // 2, h // 2), fill=(*rgb, alpha))
    return base.filter(ImageFilter.GaussianBlur(max(w, h) // 5))


def _rounded(image: Any, radius: int) -> Any:
    img = image.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _cover_fit(image: Any, box_w: int, box_h: int) -> Any:
    """等比缩放 + 居中裁剪填满目标区域（同 ``_cover_fit``）。"""
    img = image.convert("RGB")
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        raise ValueError("invalid image size")
    scale = max(box_w / iw, box_h / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), _LANCZOS)
    left, top = (nw - box_w) // 2, (nh - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def _open_cover(path: Any) -> Any:
    """安全打开本地封面：关闭句柄、限制体积与像素数。失败抛异常由调用方降级。"""
    p = Path(str(path))
    if not p.is_file():
        raise FileNotFoundError(str(p))
    if p.stat().st_size > _MAX_COVER_BYTES:
        raise ValueError("cover too large")
    with Image.open(p) as im:
        w, h = im.size
        if w <= 0 or h <= 0 or w * h > _MAX_COVER_PIXELS:
            raise ValueError("cover dimensions too large")
        im.load()
        out = im.convert("RGBA")
    return out


def _with_alpha(rgb: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (rgb[0], rgb[1], rgb[2], alpha)


# ---------------------------------------------------------------------------
# 布局常量（对齐 card_renderer._L）
# ---------------------------------------------------------------------------

class _L:
    PAD = 44
    RADIUS = 30
    SHADOW_INSET = 10
    SHADOW_BLUR = 16

    HEAD_BAR_W = 64
    HEAD_BAR_H = 6
    HEAD_BAR_TOP = 24
    HEAD_PILL_H = 42
    HEAD_TITLE_TOP_GAP = 18

    COVER = 104          # 列表封面边长
    COVER_RADIUS = 18
    ROW_GAP = 16
    ROW_PAD_Y = 16
    NUM_R = 19           # 序号徽章半径

    FOOTER_H = 108
    WM_DOT = 10
    WM_DOT_GAP = 8

    F_TITLE = 36
    F_SUB = 22
    F_ROW = 27
    F_ROW_META = 21
    F_DUR = 21
    F_NUM = 24
    F_PLATFORM = 22
    F_FOOT = 20


# ---------------------------------------------------------------------------
# Song 访问与封面索引
# ---------------------------------------------------------------------------

def _song_field(song: Any, name: str, default: Any = "") -> Any:
    """兼容 dataclass / dict / 任意对象三种 Song 形态。"""
    if song is None:
        return default
    if isinstance(song, Mapping):
        return song.get(name, default)
    return getattr(song, name, default)


def cover_key(platform: Any, song_id: Any) -> str:
    """统一字符串键 ``platform:id``（推荐 controller 也用这个形式）。"""
    return f"{safe_text(platform, 16).lower()}:{safe_text(song_id, 48)}"


def _normalize_covers(raw: Any) -> dict[str, Path]:
    """把 controller 传入的封面映射规整成 ``{'platform:id': Path}``。

    兼容三种键写法：``('netease','123')`` 元组、``'netease:123'`` 字符串，
    以及平台为键的嵌套字典。非法项静默忽略。
    """
    out: dict[str, Path] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(key, tuple) and len(key) >= 2:
            norm = cover_key(key[0], key[1])
        elif isinstance(key, str) and ":" in key:
            norm = cover_key(key.split(":", 1)[0], key.split(":", 1)[1])
        else:
            continue
        try:
            path = Path(str(value))
        except (TypeError, ValueError):
            continue
        out[norm] = path
    return out


def _identity(songs: Sequence[Any]) -> str:
    parts: list[str] = []
    for s in songs:
        parts.append("|".join((
            safe_text(_song_field(s, "platform"), 16),
            safe_text(_song_field(s, "id"), 48),
            safe_text(_song_field(s, "title"), 60),
            safe_text(_song_field(s, "artist"), 60),
        )))
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:10]


def _plan(songs: Sequence[Any], keyword: str, limit: int) -> tuple[list[Any], str, int]:
    """过滤无歌名条目并截断——**保持原顺序**，编号与输入顺序严格一致。"""
    clean = [s for s in songs if s is not None and safe_text(_song_field(s, "title"), 1)]
    return clean[:limit], safe_text(keyword, limit=40), len(clean)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _render_sync(songs: Sequence[Any], keyword: str, output_dir: Path,
                 options: dict[str, Any]) -> Path:
    if Image is None or ImageDraw is None:  # pragma: no cover
        raise RuntimeError("渲染失败：缺少 Pillow 依赖")

    fmt = str(options.get("format") or "png").strip().lower()
    ext = _FORMATS.get(fmt, "png")

    dark = options.get("dark")
    if dark is None:
        dark = str(options.get("theme") or "dark").strip().lower() != "light"
    theme_name = "dark" if bool(dark) else "light"
    theme = get_theme(theme_name)

    accent_hex = normalize_accent(options.get("accent_color")) or "#8B7CF6"
    accent_rgb = _hex_to_rgb(accent_hex)
    font_path = options.get("font_path") or None
    watermark = options.get("watermark")
    watermark = getattr(_CR, "WATERMARK_TAG", "希望解析") if watermark is None \
        else str(watermark).strip()[:12]
    covers = _normalize_covers(options.get("covers"))

    limit = options.get("limit", options.get("count"))
    try:
        limit = int(limit) if limit is not None else DEFAULT_CANDIDATES
    except (TypeError, ValueError):
        limit = DEFAULT_CANDIDATES
    limit = max(1, min(MAX_CANDIDATES, limit))

    rows, kw, total = _plan(list(songs), keyword, limit)
    if not rows:
        raise ValueError("搜索结果为空，无法生成卡片")

    width = int(options.get("width") or 880)
    width = max(520, min(1080, width))
    pad = _L.PAD
    inner_w = width - pad * 2

    f_title = _font(_L.F_TITLE, bold=True, font_path=font_path)
    f_sub = _font(_L.F_SUB, font_path=font_path)
    f_row = _font(_L.F_ROW, bold=True, font_path=font_path)
    f_meta = _font(_L.F_ROW_META, font_path=font_path)
    f_dur = _font(_L.F_DUR, font_path=font_path)
    f_num = _font(_L.F_NUM, bold=True, font_path=font_path)
    f_pill = _font(_L.F_PLATFORM, bold=True, font_path=font_path)
    f_foot = _font(_L.F_FOOT, bold=True, font_path=font_path)

    # ---------- 高度预算：先算清，再开画布，杜绝溢出 ----------
    pill_w = int(_text_w("音乐", f_pill)) + 46
    head_h = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + _L.HEAD_TITLE_TOP_GAP + _L.F_TITLE + 14
    if kw:
        head_h += _L.F_SUB + 12
    head_h += _L.HEAD_PILL_H + 22

    title_lines = 2
    row_text_h = title_lines * (_L.F_ROW + 6) + (_L.F_ROW_META + 10)
    row_h = max(_L.COVER, row_text_h) + _L.ROW_PAD_Y * 2
    card_h = head_h + row_h * len(rows) + _L.ROW_GAP * (len(rows) - 1) + _L.FOOTER_H

    # ---------- 画布：投影 + 渐变 + 光晕 + 圆角遮罩 + 描边 ----------
    canvas = Image.new("RGBA", (width, card_h + _L.SHADOW_INSET * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, card_h + _L.SHADOW_INSET * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (_L.SHADOW_INSET, _L.SHADOW_INSET, width - _L.SHADOW_INSET,
         _L.SHADOW_INSET + card_h - 2),
        radius=_L.RADIUS + 2, fill=(0, 0, 0, theme.shadow_alpha),
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(_L.SHADOW_BLUR)))

    top_color = normalize_accent(options.get('gradient_top')) or theme.gradient_top
    bottom_color = normalize_accent(options.get('gradient_bottom')) or theme.gradient_bottom
    grad = _gradient((width, card_h), _hex_to_rgb(top_color),
                     _hex_to_rgb(bottom_color)).convert("RGBA")
    grad.alpha_composite(_radial_glow(width, round(width * 0.9), accent_rgb,
                                      theme.glow_alpha), (0, 0))
    mask = Image.new("L", (width, card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, card_h - 1),
                                           radius=_L.RADIUS, fill=255)
    grad.putalpha(mask)
    canvas.alpha_composite(grad, (0, 0))

    border_layer = Image.new("RGBA", (width, card_h), (0, 0, 0, 0))
    ImageDraw.Draw(border_layer).rounded_rectangle(
        (0, 0, width - 1, card_h - 1), radius=_L.RADIUS,
        outline=_with_alpha(_hex_to_rgb(theme.border), theme.border_alpha), width=1,
    )
    canvas.alpha_composite(border_layer, (0, 0))
    draw = ImageDraw.Draw(canvas)

    # ---------- 顶部：accent 短横条（渐隐） ----------
    bar = Image.new("RGBA", (_L.HEAD_BAR_W, _L.HEAD_BAR_H), (0, 0, 0, 0))
    for xx in range(_L.HEAD_BAR_W):
        a = int(230 * (1 - xx / max(_L.HEAD_BAR_W - 1, 1)) ** 1.3)
        ImageDraw.Draw(bar).line([(xx, 0), (xx, _L.HEAD_BAR_H)],
                                 fill=(*accent_rgb, a))
    canvas.alpha_composite(_rounded(bar, _L.HEAD_BAR_H // 2),
                           (pad, _L.HEAD_BAR_TOP))

    y = _L.HEAD_BAR_TOP + _L.HEAD_BAR_H + _L.HEAD_TITLE_TOP_GAP
    draw.text((pad, y), "音乐搜索结果", font=f_title, fill=_hex_to_rgb(theme.text_primary))
    y += _L.F_TITLE + 14

    if kw:
        more = f" · 找到 {total} 首，展示前 {len(rows)} 首" if total > len(rows) \
            else f" · 找到 {total} 首"
        draw.text((pad, y), _ellipsize(kw + more, f_sub, inner_w), font=f_sub,
                  fill=_hex_to_rgb(theme.text_secondary))
        y += _L.F_SUB + 12

    # 平台药丸（毛玻璃底 + accent 圆点 + 文字），同 _draw_flat_badge 视觉
    pill_h = _L.HEAD_PILL_H
    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle(
        (0, 0, pill_w - 1, pill_h - 1), radius=pill_h // 2,
        fill=_with_alpha(_hex_to_rgb(theme.pill_bg), theme.frost_alpha),
        outline=_with_alpha(_hex_to_rgb(theme.pill_bg), theme.frost_border_alpha), width=1,
    )
    canvas.alpha_composite(pill, (pad, y))
    ImageDraw.Draw(canvas).ellipse(
        (pad + 16, y + pill_h // 2 - 5, pad + 26, y + pill_h // 2 + 5),
        fill=(*accent_rgb, 255))
    draw = ImageDraw.Draw(canvas)
    draw.text((pad + 34, y + (pill_h - _L.F_PLATFORM * 1.28) // 2), "音乐",
              font=f_pill, fill=_hex_to_rgb(theme.text_primary))
    draw.text((pad + pill_w + 16, y + (pill_h - _L.F_SUB * 1.28) // 2),
              "搜索结果", font=f_sub, fill=_hex_to_rgb(theme.text_tertiary))
    y += pill_h + 22

    # ---------- 歌曲列表 ----------
    for idx, song in enumerate(rows, start=1):
        top = y
        bottom = y + row_h
        # 毛玻璃容器
        row_layer = Image.new("RGBA", (inner_w, row_h), (0, 0, 0, 0))
        region = canvas.crop((pad, top, pad + inner_w, bottom)).filter(
            ImageFilter.GaussianBlur(6))
        region.alpha_composite(Image.new(
            "RGBA", region.size, (*_hex_to_rgb(theme.quote_bg), theme.frost_alpha)))
        rmask = Image.new("L", region.size, 0)
        ImageDraw.Draw(rmask).rounded_rectangle(
            (0, 0, region.size[0] - 1, region.size[1] - 1), radius=_L.COVER_RADIUS, fill=255)
        canvas.paste(region, (pad, top), rmask)
        border_row = Image.new("RGBA", (inner_w, row_h), (0, 0, 0, 0))
        ImageDraw.Draw(border_row).rounded_rectangle(
            (0, 0, inner_w - 1, row_h - 1), radius=_L.COVER_RADIUS,
            outline=_with_alpha(_hex_to_rgb(theme.quote_bg), theme.frost_border_alpha), width=1)
        canvas.alpha_composite(border_row, (pad, top))
        draw = ImageDraw.Draw(canvas)

        # 专辑封面（缺图/坏图 → 同风格占位）
        cx, cy = pad + _L.ROW_PAD_Y, top + _L.ROW_PAD_Y
        cover = None
        key = cover_key(_song_field(song, "platform"), _song_field(song, "id"))
        raw_path = covers.get(key)
        if raw_path is not None:
            try:
                cover = _cover_fit(_open_cover(raw_path), _L.COVER, _L.COVER)
                cover = _rounded(cover, _L.COVER_RADIUS)
            except Exception:
                cover = None  # 坏图/缺文件/超大图 → 占位，绝不让整卡失败
        if cover is not None:
            canvas.alpha_composite(cover, (cx, cy))
        else:
            ph = _gradient((_L.COVER, _L.COVER),
                           _hex_to_rgb(theme.placeholder_top),
                           _hex_to_rgb(theme.placeholder_bottom)).convert("RGBA")
            ph.alpha_composite(Image.new("RGBA", (_L.COVER, _L.COVER), (*accent_rgb, 34)))
            ph = _rounded(ph, _L.COVER_RADIUS)
            canvas.alpha_composite(ph, (cx, cy))
            pd = ImageDraw.Draw(canvas)
            pcx, pcy = cx + _L.COVER // 2, cy + _L.COVER // 2
            pd.ellipse((pcx - 15, pcy - 15, pcx + 15, pcy + 15),
                       outline=(*accent_rgb, 150), width=3)
            pd.ellipse((pcx - 4, pcy - 4, pcx + 4, pcy + 4), fill=(*accent_rgb, 150))
        draw = ImageDraw.Draw(canvas)

        # 序号徽章（压在封面左下角，沿用 accent 圆点语言）
        bx, by = cx + 8, cy + _L.COVER - 8
        draw.ellipse((bx - _L.NUM_R, by - _L.NUM_R, bx + _L.NUM_R, by + _L.NUM_R),
                     fill=(*accent_rgb, 255))
        num = str(idx)
        nw = _text_w(num, f_num)
        draw.text((bx - nw / 2, by - _L.F_NUM * 0.63), num, font=f_num,
                  fill=(255, 255, 255))

        # 文本区：歌名（最多 2 行）+ 歌手 / 专辑
        tx = cx + _L.COVER + 20
        text_w = width - pad - _L.ROW_PAD_Y - tx
        title = safe_text(_song_field(song, "title"), 120) or "未知歌曲"
        lines = wrap_text(title, f_row, text_w, title_lines)
        block_h = len(lines) * (_L.F_ROW + 6) + (_L.F_ROW_META + 10)
        ty = top + (row_h - block_h) // 2
        for ln in lines:
            draw.text((tx, ty), ln, font=f_row, fill=_hex_to_rgb(theme.text_primary))
            ty += _L.F_ROW + 6

        artist = safe_text(_song_field(song, "artist"), 60)
        album = safe_text(_song_field(song, "album"), 60)
        dur = format_duration(_song_field(song, "duration", 0.0))
        label = platform_label(_song_field(song, "platform"))
        right_width = _text_w(label, f_meta) + 30
        if dur:
            right_width += _text_w(dur, f_dur) + 14
        meta_width = max(1, text_w - right_width - 12)
        meta_parts = [p for p in (artist, album) if p]
        meta = " · ".join(meta_parts)
        if meta:
            draw.text((tx, ty), _ellipsize(meta, f_meta, meta_width), font=f_meta,
                      fill=_hex_to_rgb(theme.text_secondary))
        # 平台色点 + 平台名 + 时长（右列）
        rx = pad + inner_w - _L.ROW_PAD_Y
        if dur:
            dw = _text_w(dur, f_dur)
            draw.text((rx - dw, ty), dur,
                      font=f_dur, fill=_hex_to_rgb(theme.text_tertiary))
            rx -= dw + 14
        lw = _text_w(label, f_meta)
        draw.text((rx - lw, ty), label,
                  font=f_meta, fill=_hex_to_rgb(theme.text_tertiary))
        dot_y = ty + _L.F_ROW_META * 0.65
        icon = load_platform_icon(_song_field(song, 'platform'), 22)
        if icon is not None:
            canvas.alpha_composite(icon, (int(rx - lw - 30), int(dot_y - 11)))
        else:
            draw.ellipse((rx - lw - 16, dot_y - 4, rx - lw - 8, dot_y + 4),
                         fill=(*platform_color(_song_field(song, "platform")), 255))

        y = bottom + _L.ROW_GAP

    # ---------- 页脚：分隔线 + 左侧用法 + 右下角品牌水印 ----------
    foot_y = card_h - _L.FOOTER_H
    divider = Image.new("RGBA", (inner_w, 1), (0, 0, 0, 0))
    ImageDraw.Draw(divider).line([(0, 0), inner_w - 1, 0],
                                 fill=_with_alpha(_hex_to_rgb(theme.divider),
                                                  14 if theme_name == "dark" else 12), width=1)
    canvas.alpha_composite(divider, (pad, foot_y + 12))

    wm_text = watermark
    f_wm = f_foot
    wm_x = width - pad
    wm_group_w = 0
    if wm_text:
        wm_w = _text_w(wm_text, f_wm)
        wm_group_w = _L.WM_DOT + _L.WM_DOT_GAP + wm_w
        wm_x = width - pad - wm_group_w
        draw.ellipse((wm_x, foot_y + 28 + 12, wm_x + _L.WM_DOT, foot_y + 28 + 12 + _L.WM_DOT),
                     fill=(*accent_rgb, 255))
        draw.text((wm_x + _L.WM_DOT + _L.WM_DOT_GAP, foot_y + 28), wm_text,
                  font=f_wm, fill=accent_rgb)

    command = safe_text(options.get("play_command") or "听", 20) or "听"
    example = min(2, len(rows))
    hint = f"发送「{command}{example}」播放第 {example} 首"
    draw.text((pad, foot_y + 28), _ellipsize(hint, f_foot, max(40, wm_x - pad - 20)),
              font=f_foot, fill=_hex_to_rgb(theme.text_tertiary))

    # ---------- 输出（文件名不含关键词 / 斜杠 / 平台非法字符） ----------
    stamp = time.strftime("%Y%m%d%H%M%S")
    token = f"{_identity(rows)}{uuid.uuid4().hex[:8]}"
    path = Path(output_dir) / f"music_search_{stamp}_{token}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)

    final = canvas.convert("RGB") if ext in ("jpg", "jpeg") else canvas
    kwargs: dict[str, Any] = {}
    if ext in ("jpg", "jpeg"):
        kwargs["quality"] = 92
    elif ext == "webp":
        kwargs["quality"] = 90
    final.save(path, format=("JPEG" if ext in ("jpg", "jpeg") else ext.upper()), **kwargs)
    return path


async def render_search_card(
    songs: Iterable[Any],
    keyword: str,
    output_dir: Path,
    **options: Any,
) -> Path:
    """把搜索结果渲染成一张与分享卡片同风格的本地图片，返回文件路径。

    :param songs: 候选歌曲（``Song`` dataclass / dict 均可，最多取 10 条，
                  **顺序即卡片编号顺序**，渲染不会重排）
    :param keyword: 搜索关键词，显示在卡片标题下方
    :param output_dir: 输出目录，不存在会自动创建
    :param options:
        ``theme``('dark'/'light') 或 ``dark``(bool)、``accent_color``(hex)、
        ``watermark``(None=沿用全局默认，''=不显示)、``format``(png/jpeg/webp)、
        ``width``、``font_path``、``limit``(1..10)、
        ``covers``(本地封面映射，键为 ``'platform:id'`` 字符串或 ``(platform, id)``
        元组，值为本地 ``Path``；缺图/坏图自动降级为同风格占位)、
        ``play_command``(页脚提示用的点歌指令，默认「点歌」)

    绘图经 ``asyncio.to_thread`` 执行，不阻塞事件循环。全程不联网。
    歌曲列表为空时抛出 ``ValueError``（消息信息安全，不含内部路径）。
    """
    items = list(songs or [])
    if not items:
        raise ValueError("搜索结果为空，无法生成卡片")
    return await asyncio.to_thread(_render_sync, items, keyword, Path(output_dir), dict(options))
