"""Pillow mixed text/emoji fallback, with no downloads at render time.

Basic layout cannot shape ZWJ, flags or skin-tone combinations. Keep each
sequence indivisible for wrapping, but draw its basic symbols in that case.
The bundled OFL Noto Emoji font provides scalable monochrome coverage on
servers that have no system emoji font.
"""
from __future__ import annotations

import os
import struct
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw, ImageFont, features


def _extension(char: str) -> bool:
    cp = ord(char)
    return (unicodedata.category(char).startswith("M") or
            0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F)


def text_units(text: str) -> Iterator[str]:
    """Keep combining marks, emoji modifiers, flags and ZWJ chains together."""
    i = 0
    while i < len(text):
        start = i
        first = ord(text[i])
        i += 1
        if (0x1F1E6 <= first <= 0x1F1FF and i < len(text) and
                0x1F1E6 <= ord(text[i]) <= 0x1F1FF):
            i += 1
        while i < len(text):
            if _extension(text[i]):
                i += 1
            elif text[i] == "\u200d" and i + 1 < len(text):
                i += 2
            else:
                break
        yield text[start:i]


def _emoji(unit: str) -> bool:
    return any(0x1F000 <= ord(c) <= 0x1FAFF or
               0x2600 <= ord(c) <= 0x27BF or
               ord(c) in (0xA9, 0xAE, 0x203C, 0x2049, 0x20E3, 0x2122,
                          0x2139, 0x2194, 0x2195, 0x2196, 0x2197, 0x2198,
                          0x2199, 0x21A9, 0x21AA, 0x231A, 0x231B, 0x2328,
                          0x23CF, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23ED,
                          0x23EE, 0x23EF, 0x23F0, 0x23F1, 0x23F2, 0x23F3,
                          0x23F8, 0x23F9, 0x23FA, 0x24C2, 0x25AA, 0x25AB,
                          0x25B6, 0x25C0, 0x25FB, 0x25FC, 0x25FD, 0x25FE,
                          0x2934, 0x2935, 0x2B05, 0x2B06, 0x2B07, 0x2B1B,
                          0x2B1C, 0x2B50, 0x2B55, 0x3030, 0x303D, 0x3297,
                          0x3299) for c in unit)


@lru_cache(maxsize=16)
def _coverage(path: str) -> frozenset[int]:
    """Read Unicode cmap formats 4/12; avoid an extra fonttools dependency."""
    try:
        data = Path(path).read_bytes()
        u16 = lambda offset: struct.unpack_from(">H", data, offset)[0]
        u32 = lambda offset: struct.unpack_from(">I", data, offset)[0]
        sfnt = u32(12) if data[:4] == b"ttcf" else 0
        cmap = None
        for i in range(u16(sfnt + 4)):
            pos = sfnt + 12 + i * 16
            if data[pos:pos + 4] == b"cmap":
                cmap = u32(pos + 8)
                break
        if cmap is None:
            return frozenset()
        chars: set[int] = set()
        for i in range(u16(cmap + 2)):
            record = cmap + 4 + i * 8
            platform, encoding = u16(record), u16(record + 2)
            if platform != 0 and not (platform == 3 and encoding in (1, 10)):
                continue
            table = cmap + u32(record + 4)
            if u16(table) == 12:
                for j in range(u32(table + 12)):
                    pos = table + 16 + j * 12
                    start, end, glyph = u32(pos), u32(pos + 4), u32(pos + 8)
                    chars.update(range(start + (glyph == 0), min(end, 0x10FFFF) + 1))
            elif u16(table) == 4:
                count = u16(table + 6) // 2
                ends = table + 14
                starts = ends + count * 2 + 2
                deltas = starts + count * 2
                offsets = deltas + count * 2
                for j in range(count):
                    delta, offset = u16(deltas + j * 2), u16(offsets + j * 2)
                    for cp in range(u16(starts + j * 2), u16(ends + j * 2) + 1):
                        glyph = (cp + delta) & 0xFFFF
                        if offset:
                            glyph = u16(offsets + j * 2 + offset +
                                        (cp - u16(starts + j * 2)) * 2)
                            glyph = ((glyph + delta) & 0xFFFF) if glyph else 0
                        if glyph:
                            chars.add(cp)
        return frozenset(chars)
    except (OSError, ValueError, struct.error):
        return frozenset()


class EmojiText:
    def __init__(self) -> None:
        bundled = Path(__file__).resolve().parent.parent / "resources/fonts/NotoEmoji.ttf"
        self.paths = [p for p in (
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/seguiemj.ttf",
            Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf"),
            Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"), bundled,
        ) if p.is_file()]
        self.raqm = features.check_feature("raqm")
        self._cache: dict[int, list[tuple[Any, frozenset[int], bool]]] = {}
        self._lock = threading.Lock()

    def _fonts(self, size: int) -> list[tuple[Any, frozenset[int], bool]]:
        with self._lock:
            if size not in self._cache:
                fonts = []
                for path in self.paths:
                    try:
                        font = ImageFont.truetype(str(path), size)
                        probe = ImageDraw.Draw(Image.new("RGBA", (size * 3, size * 3)))
                        probe.text((0, 0), "😀", font=font, embedded_color=True)
                        # Embedded color is harmless for monochrome OpenType fonts.
                        fonts.append((font, _coverage(str(path)), True))
                    except (OSError, ValueError):
                        # Bitmap-only fonts often cannot be loaded at arbitrary sizes.
                        continue
                self._cache[size] = fonts
            return self._cache[size]

    def runs(self, text: str, base: Any) -> Iterator[tuple[str, Any, bool]]:
        plain = ""
        fonts = self._fonts(getattr(base, "size", 24)) if any(_emoji(u) for u in text_units(text)) else []
        for unit in text_units(text):
            if not _emoji(unit):
                plain += unit
                continue
            if plain:
                yield plain, base, False
                plain = ""
            visible = unit
            if not self.raqm:
                if "\u20e3" in unit:
                    # BASIC does not position the enclosing-keycap glyph reliably.
                    yield "[" + unit[0] + "]", base, False
                    continue
                if all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in unit):
                    yield "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in unit), base, False
                    continue
                visible = "".join(c for c in unit if c not in ("\u200d", "\ufe0e", "\ufe0f") and
                                  not 0x1F3FB <= ord(c) <= 0x1F3FF and
                                  not 0xE0020 <= ord(c) <= 0xE007F)
            # Ignore selectors in coverage checks; they do not need standalone glyphs.
            required = {ord(c) for c in visible if c not in ("\ufe0e", "\ufe0f", "\u200d")
                        and not 0xE0020 <= ord(c) <= 0xE007F}
            chosen = next(((f, color) for f, coverage, color in fonts if required <= coverage), None)
            if chosen:
                yield visible, chosen[0], chosen[1]
            else:
                # New Unicode symbols outside the bundled font cannot become tofu.
                yield "?", base, False
        if plain:
            yield plain, base, False

    def width(self, draw: Any, text: str, base: Any) -> float:
        return sum(draw.textlength(t, font=f) for t, f, _ in self.runs(text, base))

    def draw(self, draw: Any, xy: tuple[int, int], text: str, base: Any,
             fill: Any, stroke_width: int = 0, stroke_fill: Any = None,
             color: bool = True) -> None:
        x, y = xy
        ascent = base.getmetrics()[0]
        for text, font, embedded in self.runs(text, base):
            dy = ascent - font.getmetrics()[0]
            draw.text((x, y + dy), text, font=font, fill=fill,
                      stroke_width=0 if embedded else stroke_width,
                      stroke_fill=stroke_fill, embedded_color=embedded and color)
            x += draw.textlength(text, font=font)
