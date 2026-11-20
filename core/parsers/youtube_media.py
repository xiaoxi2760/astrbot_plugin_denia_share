"""Anonymous YouTube extraction and selection of downloadable MP4 formats."""

import math
from urllib.parse import urlsplit

from ..exception import DownloadException, IgnoreException, MediaSizeExceeded
from ..media_utils import fmt_duration


def extract_formats(url: str, proxy: str | None) -> dict:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, ExtractorError
    except ImportError as exc:
        raise DownloadException("YouTube 视频下载依赖 yt-dlp，请安装插件依赖") from exc

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "retries": 0,
        "extractor_retries": 0,
        "format": "all",
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if proxy:
        options["proxy"] = proxy
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False, ie_key="Youtube")
    except (DownloadError, ExtractorError, OSError) as exc:
        raise DownloadException("YouTube 暂无法提供可下载视频，请打开原链接播放") from exc
    if not isinstance(info, dict):
        raise DownloadException("YouTube 未返回视频信息")
    return info


def _cdn_format(stream: dict) -> bool:
    if stream.get("protocol") != "https" or stream.get("has_drm"):
        return False
    url = stream.get("url")
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.port in (None, 443)
            and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
        )
    except ValueError:
        return False


def _size(stream: dict, *, exact: bool = False) -> int:
    size = stream.get("filesize")
    if not exact and not size:
        size = stream.get("filesize_approx")
    return size if isinstance(size, int) and size > 0 else 0


def select_streams(
    info: dict, *, duration_limit: int, byte_limit: int, can_merge: bool,
) -> tuple[dict, dict | None, tuple[int, int] | None]:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise IgnoreException("YouTube 直播或尚未完成处理的直播回放暂不下载")
    if info.get("availability") in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        raise IgnoreException("此 YouTube 视频需要授权才能观看，请打开原链接")
    duration = info.get("duration")
    if not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
        raise IgnoreException("YouTube 未提供有效的视频时长，无法确认是否符合下载限制")
    if duration > duration_limit:
        raise IgnoreException(
            f"YouTube 视频时长({fmt_duration(duration)})超过上限({fmt_duration(duration_limit)})"
        )

    formats = [item for item in info.get("formats") or [] if isinstance(item, dict) and _cdn_format(item)]
    # yt-dlp orders formats from worst to best. Keep browser/client-compatible MP4 video.
    video_formats = [item for item in formats if item.get("ext") == "mp4" and str(item.get("vcodec")).startswith("avc1")]
    audio_formats = [
        item for item in formats
        if item.get("ext") == "m4a" and item.get("vcodec") == "none"
        and str(item.get("acodec")).startswith("mp4a")
    ]
    oversized = []
    for video in reversed(video_formats):
        video_size = _size(video)
        if str(video.get("acodec")).startswith("mp4a"):
            if video_size <= byte_limit:
                return video, None, None
            oversized.append(video_size)
        elif can_merge and video.get("acodec") == "none":
            # At least one track needs an exact size to reserve the remaining budget.
            # Allocating both limits up front lets the downloader enforce them while streaming.
            for audio in reversed(audio_formats):
                audio_size = _size(audio)
                total = video_size + audio_size
                if total > byte_limit:
                    oversized.append(total)
                elif exact_video := _size(video, exact=True):
                    if exact_video < byte_limit:
                        return video, audio, (exact_video, byte_limit - exact_video)
                elif exact_audio := _size(audio, exact=True):
                    if exact_audio < byte_limit:
                        return video, audio, (byte_limit - exact_audio, exact_audio)

    if oversized:
        raise MediaSizeExceeded(received_bytes=min(oversized), limit_bytes=byte_limit)
    if video_formats and audio_formats and not can_merge:
        raise DownloadException("未安装 ffmpeg，且 YouTube 未提供可直接发送的有声 MP4")
    raise DownloadException("YouTube 暂未提供可下载的有声 MP4，请打开原链接播放")
