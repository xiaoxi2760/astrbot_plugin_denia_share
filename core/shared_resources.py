"""Read the fixed, public resource set used by the WebUI.

The WebUI serves these small assets as JSON data URLs because its pages can
only load static files from ``pages/``.  The allowlist and MIME types are
defined here; no request value is used to construct a filesystem path.
"""

from __future__ import annotations

import base64
from pathlib import Path


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_RESOURCE_ROOT = _PLUGIN_ROOT / "resources"

MAX_ASSET_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024

# (public key, MIME type). Keep this list explicit: adding a file to resources
# must not make it available to the WebUI automatically.
_ASSETS: tuple[tuple[str, str], ...] = (
    ("brand/logo.png", "image/png"),
    ("platforms/bilibili.svg", "image/svg+xml"),
    ("platforms/douyin.svg", "image/svg+xml"),
    ("platforms/kuaishou.svg", "image/svg+xml"),
    ("platforms/weibo.svg", "image/svg+xml"),
    ("platforms/xiaohongshu.svg", "image/svg+xml"),
    ("platforms/twitter.svg", "image/svg+xml"),
    ("platforms/youtube.png", "image/png"),
    ("platforms/github.svg", "image/svg+xml"),
    ("platforms/pixiv.svg", "image/svg+xml"),
    ("platforms/steam.svg", "image/svg+xml"),
    ("platforms/acfun.png", "image/png"),
    ("platforms/nga.png", "image/png"),
    ("platforms/xiaoheihe.png", "image/png"),
    ("platforms/netease.png", "image/png"),
    ("platforms/qqmusic.png", "image/png"),
    ("platforms/kugou.png", "image/png"),
)
_ALLOWED_ASSET_PATHS = frozenset(path for path, _ in _ASSETS)


def _asset_path(relative_path: str) -> Path | None:
    """Resolve one allowlisted path without following it outside resources."""
    if relative_path not in _ALLOWED_ASSET_PATHS:
        return None
    try:
        plugin_root = _PLUGIN_ROOT.resolve(strict=True)
        resource_root = _RESOURCE_ROOT.resolve(strict=True)
        # A resources directory that is itself redirected outside the plugin
        # root is not a valid source for public WebUI assets.
        resource_root.relative_to(plugin_root)
        path = (resource_root / relative_path).resolve(strict=True)
        path.relative_to(resource_root)
        if not path.is_file():
            return None
        return path
    except (OSError, ValueError):
        return None


def load_shared_resources() -> dict[str, dict[str, str]]:
    """Return available allowlisted assets as bounded base64 data URLs.

    Missing, unreadable, oversized, or out-of-tree files are omitted. The
    response remains a valid ``{"assets": {...}}`` object in every case.
    """
    assets: dict[str, str] = {}
    total_bytes = 0

    for relative_path, mime_type in _ASSETS:
        path = _asset_path(relative_path)
        if path is None:
            continue

        try:
            size = path.stat().st_size
            if size < 0 or size > MAX_ASSET_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
                continue
            with path.open("rb") as source:
                data = source.read(MAX_ASSET_BYTES + 1)
        except OSError:
            continue

        # Recheck after reading in case the file changed between stat and read.
        if len(data) > MAX_ASSET_BYTES or total_bytes + len(data) > MAX_TOTAL_BYTES:
            continue

        encoded = base64.b64encode(data).decode("ascii")
        assets[relative_path] = f"data:{mime_type};base64,{encoded}"
        total_bytes += len(data)

    return {"assets": assets}
