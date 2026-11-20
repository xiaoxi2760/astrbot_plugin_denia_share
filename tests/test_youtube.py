"""Regression checks for the YouTube link and oEmbed adapter."""

import asyncio
from hashlib import sha256
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    sys.modules["astrbot.api"] = api

from astrbot_plugin_denia_share.core.exception import (
    DownloadException, IgnoreException, MediaSizeExceeded, ParseException, SilentException,
)
from astrbot_plugin_denia_share.core.parsers import youtube
from astrbot_plugin_denia_share.core.parsers.youtube import YouTubeParser
from astrbot_plugin_denia_share.core.parsers.youtube_media import select_streams


VIDEO_ID = "M7lc1UVf-VE"
CANONICAL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
THUMBNAIL = f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"


@pytest.mark.parametrize("url", [
    CANONICAL,
    f"https://m.youtube.com/watch?feature=share&v={VIDEO_ID}&list=ignored",
    f"视频：<https://WWW.YOUTUBE.COM/watch?t=7&v={VIDEO_ID}>",
    CANONICAL + ".",
    f"https://youtu.be/{VIDEO_ID}?si=abc",
    f"youtube.com/shorts/{VIDEO_ID}",
    f"https://youtube.com/live/{VIDEO_ID}",
    f"https://youtube.com/embed/{VIDEO_ID}",
    f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
])
def test_link_forms_have_same_identity(url):
    _, matched = YouTubeParser.search_url(url)
    assert YouTubeParser.dedup_identity(matched) == VIDEO_ID


@pytest.mark.parametrize("url", [
    f"https://fake.youtube.com/watch?v={VIDEO_ID}",
    f"https://youtube.com.evil.example/watch?v={VIDEO_ID}",
    f"https://www.youtube.com@evil.example/watch?v={VIDEO_ID}",
    f"https://evil.example/youtube.com/shorts/{VIDEO_ID}",
    f"https://youtu.be/{VIDEO_ID}X",
    "https://youtube.com/watch?list=xyz",
])
def test_unsupported_links_are_ignored(url):
    with pytest.raises(SilentException):
        YouTubeParser.search_url(url)


@pytest.mark.parametrize("status, thumbnail, expected", [
    (200, THUMBNAIL, True),
    (200, "https://i.ytimg.com@evil.example/preview.jpg", False),
    (401, None, None),
    (500, None, None),
])
def test_oembed_preview(status, thumbnail, expected, monkeypatch):
    def respond(request):
        assert request.url.host == "www.youtube.com"
        assert request.url.path == "/oembed"
        assert request.url.params["url"] == CANONICAL
        return httpx.Response(status, json={
            "title": "Example video",
            "author_name": "Example channel",
            "thumbnail_url": thumbnail,
        })

    parser = object.__new__(YouTubeParser)
    def unexpected_extraction(*args):
        pytest.fail("Stream extraction must be lazy")

    monkeypatch.setattr(youtube, "extract_formats", unexpected_extraction)
    parser.headers = {}
    parser.downloader = types.SimpleNamespace(download_img=AsyncMock(return_value=Path("cover.jpg")))
    parser.new_client = lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(respond), **kwargs,
    )
    args = parser.search_url(f"https://youtu.be/{VIDEO_ID}")

    if expected is None:
        with pytest.raises(ParseException):
            asyncio.run(parser.parse(*args))
        return
    result = asyncio.run(parser.parse(*args))
    assert result.title == "Example video"
    assert result.author.name == "Example channel"
    assert result.url == CANONICAL
    assert not result.video.path_task.started
    if expected:
        assert asyncio.run(result.video.cover.get()) == Path("cover.jpg")
        parser.downloader.download_img.assert_awaited_once_with(THUMBNAIL, ext_headers={})
    else:
        assert result.video.cover is None
    assert not result.video.path_task.started


def _stream(name, size, video="avc1.4d401f", audio="mp4a.40.2", ext="mp4"):
    return {
        "url": f"https://edge.googlevideo.com/{name}",
        "protocol": "https",
        "ext": ext,
        "vcodec": video,
        "acodec": audio,
        "filesize": size,
    }


def test_stream_choice_respects_combined_limit_and_ffmpeg():
    progressive = _stream("progressive", 23)
    audio = _stream("audio", 12, video="none", ext="m4a")
    video = _stream("video", 44, audio="none")
    info = {"duration": 10, "formats": [progressive, audio, video]}
    assert select_streams(info, duration_limit=100, byte_limit=100, can_merge=True) == (
        video, audio, (44, 56),
    )
    assert select_streams(info, duration_limit=100, byte_limit=50, can_merge=True) == (
        progressive, None, None,
    )
    assert select_streams(info, duration_limit=100, byte_limit=100, can_merge=False) == (
        progressive, None, None,
    )
    with pytest.raises(MediaSizeExceeded):
        select_streams(info, duration_limit=100, byte_limit=22, can_merge=True)


@pytest.mark.parametrize("info", [
    {"duration": 300},
    {"duration": None},
    {"duration": 5, "live_status": "is_live"},
    {"duration": 5, "availability": "private"},
])
def test_unavailable_or_out_of_policy_video_is_skipped(info):
    with pytest.raises(IgnoreException):
        select_streams(info, duration_limit=100, byte_limit=100, can_merge=True)


def test_misleading_cdn_is_rejected():
    stream = _stream("progressive", 20)
    stream["url"] = "https://googlevideo.com@evil.example/stream"
    with pytest.raises(DownloadException):
        select_streams({"duration": 10, "formats": [stream]}, duration_limit=100, byte_limit=100, can_merge=True)


def test_download_delegates_selected_audio_and_video(monkeypatch):
    parser = object.__new__(YouTubeParser)
    parser._extraction_slots = asyncio.Semaphore(2)
    parser.proxies = "http://proxy.example:8080"
    parser.downloader = types.SimpleNamespace(
        _max_bytes=100, cache_dir=Path("cache"),
        download_av_and_merge=AsyncMock(return_value=Path("result.mp4")),
    )
    audio = _stream("audio", 12, video="none", ext="m4a")
    video = _stream("video", 44, audio="none")

    def extract(url, proxy):
        assert url == CANONICAL
        assert proxy == parser.proxies
        return {
            "duration": 10, "formats": [audio, video],
            "http_headers": {"User-Agent": "test", "Cookie": "hidden"},
        }

    monkeypatch.setattr(youtube, "extract_formats", extract)
    monkeypatch.setattr(youtube, "get_config", lambda: types.SimpleNamespace(VIDEO_DURATION_MAXIMUM=100))
    monkeypatch.setattr(youtube, "ffmpeg_available", lambda: True)
    assert asyncio.run(parser._download_video(VIDEO_ID, CANONICAL)) == Path("result.mp4")
    parser.downloader.download_av_and_merge.assert_awaited_once_with(
        video["url"], audio["url"], output_path=Path(f"cache/youtube-{sha256(VIDEO_ID.encode()).hexdigest()}.mp4"),
        ext_headers={"User-Agent": "test"}, track_limits=(44, 56),
    )
