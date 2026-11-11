"""Bounded, disposable JPEG previews; original media files are never modified."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

PAGE_ITEMS = 9
MAX_PAGES = 2
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
WIDTH = 1280
HEIGHT = 1600
_BUILD_LOCK = threading.Lock()


def _inspect(path: Path) -> bool:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("image exceeds preview input byte budget")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.width * image.height > MAX_SOURCE_PIXELS:
                raise ValueError("image exceeds preview pixel budget")
            return bool(getattr(image, "is_animated", False))


def build_previews(paths: list[Path], cache_dir: Path) -> tuple[list[Path], int, int]:
    """Return pages, represented static count, and omitted animated count.

    Called in a worker thread. Decode only one input at a time, and retain at
    most one bounded canvas. Animated images are never flattened silently.
    """
    with _BUILD_LOCK:
        return _build_previews(paths, cache_dir)


def _build_previews(paths: list[Path], cache_dir: Path) -> tuple[list[Path], int, int]:
    static = []
    animated = 0
    for path in paths:
        if _inspect(path):
            animated += 1
        else:
            static.append(path)
    selected = static[:PAGE_ITEMS * MAX_PAGES]
    cache_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for offset in range(0, len(selected), PAGE_ITEMS):
        batch = selected[offset:offset + PAGE_ITEMS]
        identity = "v1:1280x1600:" + ":".join(
            f"{p.resolve()}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in batch
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        target = cache_dir / f"qq-preview-{digest}.jpg"
        canvas = Image.new("RGB", (WIDTH, HEIGHT), "#f3f3f3")
        try:
            draw = ImageDraw.Draw(canvas)
            for index, path in enumerate(batch):
                x, y = (index % 3) * 424 + 8, (index // 3) * 528 + 8
                with Image.open(path) as original:
                    # JPEG draft reduces decoder memory before EXIF conversion.
                    original.draft("RGB", (416, 492))
                    oriented = ImageOps.exif_transpose(original)
                    try:
                        oriented.thumbnail((408, 492), Image.Resampling.LANCZOS)
                        with oriented.convert("RGBA") as thumb:
                            canvas.paste(thumb, (x + (408 - thumb.width) // 2,
                                                y + (492 - thumb.height) // 2), thumb)
                    finally:
                        if oriented is not original:
                            oriented.close()
                draw.text((x + 4, y + 496), str(offset + index + 1), fill="#333333")
            encoded = io.BytesIO()
            canvas.save(encoded, "JPEG", quality=82, optimize=True)
            if encoded.tell() > MAX_PREVIEW_BYTES:
                raise ValueError("preview exceeds byte budget")
            fd, temporary = tempfile.mkstemp(prefix=".qq-preview-", suffix=".tmp", dir=cache_dir)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded.getbuffer())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            pages.append(target)
        finally:
            canvas.close()
    return pages, len(selected), animated
