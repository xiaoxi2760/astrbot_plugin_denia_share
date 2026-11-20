"""Resolve YouTube previews and lazily download eligible public videos."""

import asyncio
from hashlib import sha256
from pathlib import Path
from re import Match, fullmatch
from urllib.parse import parse_qs, urlsplit

from httpx import HTTPError, HTTPStatusError

from ..base_parser import BaseParser, handle
from ..config import get_config
from ..data import Author, ParseResult, platform_of
from ..download import StreamDownloader
from ..exception import DownloadException, ParseException
from ..media_utils import ffmpeg_available
from ..task import PathTask
from .youtube_media import extract_formats, select_streams
from .youtube_posts import extract_post


_VIDEO_ID = r"[A-Za-z0-9_-]{11}"
_ID_END = r"(?=$|[?#\s<>\x22\x27\x60\[\](){},.!，。！？、：；）】》」])"
_QUERY = r"[^\s<>\"'`\[\](){},，。！？、：；）】》」#]*"
_VIDEO_LINK = (
    r"(?<![\w./@:%=-])(?i:https?://)?(?:"
    r"(?i:(?:(?:www|m|music)\.)?youtube\.com)/"
    rf"(?:watch\?(?=(?:{_QUERY}&)?v={_VIDEO_ID}(?:&|{_ID_END}))(?P<query>{_QUERY})"
    rf"|(?:shorts|live|embed|v)/(?P<video>{_VIDEO_ID}){_ID_END})"
    rf"|(?i:(?:www\.)?youtu\.be)/(?P<short>{_VIDEO_ID}){_ID_END}"
    rf"|(?i:(?:www\.)?youtube-nocookie\.com)/embed/(?P<embed>{_VIDEO_ID}){_ID_END}"
    r")"
)
_POST_ID = r"Ug[A-Za-z0-9_-]{20,62}"
_POST_LINK = (
    r"(?<![\w./\\@:%=-])(?i:https?://)?(?i:(?:(?:www|m)\.)?youtube\.com)/"
    rf"(?:post/(?P<post>{_POST_ID})/?{_ID_END}"
    r"|(?:@[\w.%-]+|(?:channel|c|user)/[\w.%-]+)/(?:community|posts)/?\?"
    rf"(?=(?:{_QUERY}&)?lb={_POST_ID}(?:&|{_ID_END}))(?P<post_query>{_QUERY}))"
)
YOUTUBE_LINK = rf"(?:{_VIDEO_LINK}|{_POST_LINK})"
_OEMBED_URL = "https://www.youtube.com/oembed"
_THUMBNAIL_HOSTS = {"i.ytimg.com", "img.youtube.com"}


class YouTubeParser(BaseParser):
    platform = platform_of("youtube")

    def __init__(self, downloader: StreamDownloader):
        super().__init__(downloader)
        self._extraction_slots = asyncio.Semaphore(2)

    async def _download_video(self, video_id: str, url: str) -> Path:
        try:
            async with asyncio.timeout(35):
                await self._extraction_slots.acquire()
                worker = asyncio.create_task(asyncio.to_thread(extract_formats, url, self.proxies))

                def release_slot(task: asyncio.Task) -> None:
                    self._extraction_slots.release()
                    if not task.cancelled():
                        task.exception()

                # Timeout/cancellation stops the waiter. The thread retains its slot until done.
                worker.add_done_callback(release_slot)
                info = await asyncio.shield(worker)
        except TimeoutError as exc:
            raise DownloadException("等待 YouTube 视频取流超时，请稍后重试") from exc

        downloader = self.downloader
        video, audio, track_limits = select_streams(
            info,
            duration_limit=get_config().VIDEO_DURATION_MAXIMUM,
            byte_limit=downloader._max_bytes,
            can_merge=ffmpeg_available(),
        )
        # Video IDs are case-sensitive even on case-insensitive cache volumes.
        file_name = f"youtube-{sha256(video_id.encode('ascii')).hexdigest()}.mp4"
        headers = {
            key: value for key, value in (info.get("http_headers") or {}).items()
            if key.lower() in {"user-agent", "referer", "accept", "accept-language"}
        }
        if audio is None:
            return await downloader.download_video(video["url"], video_name=file_name, ext_headers=headers)
        return await downloader.download_av_and_merge(
            video["url"], audio["url"], output_path=downloader.cache_dir / file_name,
            ext_headers=headers, track_limits=track_limits,
        )

    @classmethod
    def dedup_identity(cls, searched: Match[str]) -> str | None:
        if searched.group("post"):
            return searched.group("post")
        post_query = searched.group("post_query")
        if post_query is not None:
            values = parse_qs(post_query.rstrip(".!"), keep_blank_values=True).get("lb", [])
            if len(values) == 1 and fullmatch(_POST_ID, values[0]):
                return values[0]
            return None
        query = searched.group("query")
        if query is not None:
            values = parse_qs(query.rstrip(".!"), keep_blank_values=True).get("v", [])
            if len(values) == 1:
                return values[0] if fullmatch(_VIDEO_ID, values[0]) else None
            return None
        return searched.group("video") or searched.group("short") or searched.group("embed")

    @handle("/", YOUTUBE_LINK)
    async def parse_share(self, searched: Match[str]) -> ParseResult:
        if searched.group("post") or searched.group("post_query") is not None:
            return await self.parse_post(searched)
        return await self.parse_video(searched)

    async def parse_post(self, searched: Match[str]) -> ParseResult:
        post_id = self.dedup_identity(searched)
        if not post_id:
            raise ParseException("YouTube 帖子链接缺少有效的帖子 ID")
        url = f"https://www.youtube.com/post/{post_id}"
        try:
            async with self.new_client(follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
        except HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403, 404, 410} or 300 <= status < 400:
                raise ParseException("YouTube 帖子无法公开预览，可能已删除或限制访问") from exc
            raise ParseException("YouTube 帖子请求失败") from exc
        except HTTPError as exc:
            raise ParseException("YouTube 帖子请求失败") from exc

        name, body, image_urls = extract_post(response.text, post_id)
        return self.result(
            title="社区帖子",
            text=body or None,
            author=Author(name=name) if name else None,
            url=url,
            contents=self.create_images(image_urls),
            extra={"include_post_text": True},
        )

    async def parse_video(self, searched: Match[str]) -> ParseResult:
        video_id = self.dedup_identity(searched)
        if not video_id:
            raise ParseException("YouTube 链接缺少有效的影片 ID")
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            async with self.new_client(follow_redirects=False) as client:
                response = await client.get(_OEMBED_URL, params={"url": url, "format": "json"})
                response.raise_for_status()
                data = response.json()
        except HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 404}:
                raise ParseException("YouTube 影片无法提供公开预览") from exc
            raise ParseException("YouTube 预览服务请求失败") from exc
        except HTTPError as exc:
            raise ParseException("YouTube 预览服务请求失败") from exc
        except ValueError as exc:
            raise ParseException("YouTube 预览服务返回了无效数据") from exc

        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str) or not title.strip():
            raise ParseException("YouTube 预览缺少标题")
        name = data.get("author_name")
        author = Author(name=name.strip()) if isinstance(name, str) and name.strip() else None
        result = self.result(title=title.strip(), author=author, url=url)
        cover_url = None
        thumbnail = data.get("thumbnail_url")
        if isinstance(thumbnail, str):
            try:
                parsed = urlsplit(thumbnail)
            except ValueError:
                pass
            else:
                if parsed.scheme == "https" and parsed.netloc.lower() in _THUMBNAIL_HOSTS:
                    cover_url = thumbnail
        video = self.create_video(
            PathTask.lazy(lambda: self._download_video(video_id, url)), cover_url=cover_url,
        )
        # A missing oEmbed thumbnail must not trigger stream extraction during preview.
        if cover_url is None:
            video.cover = None
        result.contents.append(video)
        return result
