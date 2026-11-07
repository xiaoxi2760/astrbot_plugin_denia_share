"""Download only media actually referenced by a history record, without fetching.

ZIPs are built on disk in a temporary directory, using stored entries for media
that are already compressed. The response owns the temporary directory lifetime.
"""
from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SourceFilesUnavailable(Exception):
    pass


def safe_filename(value: str, fallback: str = "源文件", limit: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value))
    name = name.strip(" .")[:limit].rstrip(" .")
    return name or fallback


def source_files(cache_dir: Path, record: dict[str, Any]) -> tuple[list[Path], int]:
    """Select unique regular media files inside cache, never the rendered card."""
    base = cache_dir.resolve()
    paths: list[Path] = []
    seen: set[Path] = set()
    missing = 0
    raw_names = record.get("media_files") or []
    if not isinstance(raw_names, list):
        return [], 0
    card = record.get("card_file")
    try:
        card_path = (base / str(card)).resolve() if card else None
    except (OSError, ValueError, RuntimeError):
        card_path = None
    for raw in raw_names:
        if not isinstance(raw, str) or not raw:
            missing += 1
            continue
        try:
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                missing += 1
                continue
            path = (base / relative).resolve()
            path.relative_to(base)
            if path == card_path or not path.is_file() or path.stat().st_size == 0:
                missing += 1
                continue
        except (OSError, ValueError, RuntimeError):
            missing += 1
            continue
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths, missing


def _download_info(paths: list[Path], missing: int, record: dict[str, Any], force_archive: bool) -> dict[str, Any]:
    if not paths:
        raise SourceFilesUnavailable("源文件未下载或已被缓存清理，无法下载")
    archive = force_archive or len(paths) > 1
    title = safe_filename(record.get("title") or record.get("platform_display") or "源文件")
    extension = ".zip" if archive else paths[0].suffix
    filename = f"{title}_源文件{extension}" if archive else f"{title}{extension}"
    return {"filename": filename, "file_count": len(paths), "missing_count": missing, "archive": archive}


def download_info(cache_dir: Path, record: dict[str, Any], *, force_archive: bool = False) -> dict[str, Any]:
    paths, missing = source_files(cache_dir, record)
    return _download_info(paths, missing, record, force_archive)


@dataclass
class MediaExport:
    path: Path
    filename: str
    archive: bool
    temporary: Any = None

    def close(self) -> None:
        if self.temporary is not None:
            temporary, self.temporary = self.temporary, None
            temporary.cleanup()


def build_media_export(cache_dir: Path, record: dict[str, Any], *, force_archive: bool = False) -> MediaExport:
    paths, missing = source_files(cache_dir, record)
    info = _download_info(paths, missing, record, force_archive)
    if not info["archive"]:
        return MediaExport(paths[0], info["filename"], False)
    temporary = tempfile.TemporaryDirectory(prefix="denia-media-")
    target = Path(temporary.name) / "sources.zip"
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for index, path in enumerate(paths, start=1):
                # A prefix keeps names unique even for files from different folders.
                name = f"{index:03d}_{safe_filename(path.name, 'media', 160)}"
                archive.write(path, arcname=name)
        return MediaExport(target, info["filename"], True, temporary)
    except BaseException:
        temporary.cleanup()
        raise
