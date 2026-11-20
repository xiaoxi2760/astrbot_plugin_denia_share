# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""Twitter / X 解析器。

优先从 vxtwitter / fxtwitter 获取图文和视频。两个镜像均不可用时，
从 X 官方 oEmbed 的公开引用中提取作者和正文，供文本卡片使用。
"""

import re
from datetime import timezone
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, ParseResult, platform_of

# pbs.twimg.com / video.twimg.com 在国内常不可达，可配置反代根地址绕过
_TWIMG_HOST_PREFIX = {
    "pbs.twimg.com": "pbs",
    "video.twimg.com": "video",
}

# Retain the non-browser user agent used by the vx mirror.
VX_UA = "python-httpx/0.27.0"

# Accept the web share sheet's anonymous /i/web/status route as well as
# user permalinks. Query parameters and /photo/1 are outside the identity.
STATUS_PATTERN = (
    r"(?ai)(?<![a-z0-9./@_-])(?:https?://)?(?:(?:www|mobile|m)\.)?"
    r"(?:x\.com|twitter\.com|vxtwitter\.com|fxtwitter\.com|fixupx\.com)/"
    r"(?:(?P<user>[a-z0-9_]{1,20})/status(?:es)?|i/web/status|status)"
    r"/(?P<tweet_id>[0-9]{1,22})(?![a-z0-9_])"
)


def to_api_url(url: str, host: str) -> str:
    """把 ``x.com`` / ``twitter.com`` 换成镜像 API 域名，保留 path 与 query。

    **不要用 ``url.replace("x.com", host).replace("twitter.com", host)``。**
    第一段产出的 ``api.fxtwitter.com`` 本身就含 ``twitter.com``（``f-x`` + ``twitter.com``），
    第二段会对着它再替换一次，拼出 ``api.fxapi.fxtwitter.com`` 这种不存在的域名。
    两级兜底因此同时失效，而报错是 ``SSLV3_ALERT_HANDSHAKE_FAILURE`` 或空的
    ``ConnectError`` —— 全都指向"网络问题"，排查方向会被彻底带偏。
    这里显式拆 host，替换只发生一次。
    """
    parts = urlsplit(url)
    return urlunsplit(("https", host, parts.path, parts.query, ""))


def proxy_media_url(url: str | None) -> str | None:
    """把 X 官方媒体 CDN 地址改写为自定义反代地址。

    未配置 ``TWITTER_MEDIA_PROXY_BASE`` 时原样返回，无副作用。
    """
    if not url:
        return url
    from ..config import get_config

    config = get_config()
    base = config.TWITTER_MEDIA_PROXY_BASE
    if not base:
        return url
    parts = urlsplit(url)
    prefix = _TWIMG_HOST_PREFIX.get(parts.netloc.lower())
    if prefix is None:
        return url
    query = f"?{parts.query}" if parts.query else ""
    return f"{base}/{prefix}{parts.path}{query}"


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.TWITTER)

    @classmethod
    def dedup_identity(cls, searched: re.Match[str]) -> str:
        return searched.group("tweet_id")

    @handle(".", STATUS_PATTERN)
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        tweet_id = searched.group("tweet_id")
        user = searched.group("user") or "i"
        url = f"https://x.com/{user}/status/{tweet_id}"
        failures: list[str] = []

        # **注意 ``BaseException`` 里的 ``CancelledError`` 不在这里捕获**：
        # 取消是控制流，吞掉它会让上层永远等不到结果。下面两个 except 只吃 Exception。
        try:
            return await self.parse_by_vxapi(url)
        except Exception as e:  # noqa: BLE001 —— 失败即降级，原因记下来
            logger.debug(f"[twitter] vxtwitter 失败: {e}")
            failures.append(f"vxtwitter: {e}")

        try:
            return await self.parse_by_fxapi(url, tweet_id)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[twitter] fxtwitter 失败: {e}")
            failures.append(f"fxtwitter: {e}")

        try:
            return await self.parse_by_oembed(url)
        except Exception as e:
            failures.append(f"X oEmbed: {e}")

        raise ParseException("Twitter 解析失败（" + "；".join(failures) + "）")

    # ------------------------------------------------------------------ #
    # 1) vxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_vxapi(self, url: str) -> ParseResult:
        api_url = to_api_url(url, "api.vxtwitter.com")
        # 独立 UA：不能用全局 COMMON_HEADER，否则被 Cloudflare 挑战（见模块 docstring）
        headers = {**self.headers, "User-Agent": VX_UA}
        async with self.new_client(headers=headers) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict) or data.get("error"):
            raise ParseException("vxtwitter 未返回公开推文")
        author = self.create_author(
            data.get("user_name") or "", proxy_media_url(data.get("user_profile_image_url"))
        )
        article = data.get("article")
        title = article.get("title") if isinstance(article, dict) else article

        result = self.result(
            author=author, title=title, text=data.get("text"),
            timestamp=data.get("date_epoch"), url=url,
        )
        for media in data.get("media_extended") or []:
            if not isinstance(media, dict) or not media.get("url"):
                continue
            mtype = media.get("type")
            if mtype in ("video", "gif"):
                duration = (media.get("duration_millis") or 0) / 1000 or None
                self._add_limit_warning(result, duration)
                result.contents.append(
                    self.create_video(
                        proxy_media_url(media.get("url")), proxy_media_url(media.get("thumbnail_url")),
                        duration=duration, is_gif=mtype == "gif",
                    )
                )
            elif mtype == "image":
                result.contents.append(
                    self.create_image(proxy_media_url(f"{media.get('url')}?name=orig"))
                )
        if not (result.contents or result.text or title):
            raise ParseException("vxtwitter 未返回正文或媒体")
        return result

    # ------------------------------------------------------------------ #
    # 2) fxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_fxapi(self, url: str, tweet_id: str) -> ParseResult:
        api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
        # fxtwitter 要求非空 UA（空 UA → 401 并提示 "You must identify yourself
        # with a User-Agent header"）。全局 COMMON_HEADER 不带这条限制，直接用即可；
        # 这里显式兜住「headers 里没有 User-Agent」的极端情况。
        headers = {**self.headers, "User-Agent": self.headers.get("User-Agent") or VX_UA}
        async with self.new_client(headers=headers) as client:
            response = await client.get(api_url)
            if response.status_code >= 500:
                raise ParseException(f"fxtwitter 服务异常: {response.status_code}")
            response.raise_for_status()
            payload = response.json()

        tweet = (payload or {}).get("tweet") or {}
        if not tweet:
            raise ParseException("fxtwitter 响应缺少 tweet 字段")

        info = tweet.get("author") or {}
        name = info.get("name") or ""
        screen = info.get("screen_name") or ""
        author = self.create_author(
            f"{name}(@{screen})" if name and screen else (name or screen),
            proxy_media_url(info.get("avatar_url") or None),
        )

        timestamp = None
        if created := tweet.get("created_at"):
            timestamp = self._parse_created_at(created)

        result = self.result(
            author=author, title=f"{author.name} 的推文" if author.name else "Twitter 推文",
            text=self._tweet_text(tweet), timestamp=timestamp, url=url,
        )

        media = tweet.get("media") or {}
        for photo in media.get("photos") or []:
            if isinstance(photo, dict) and photo.get("url"):
                result.contents.append(self.create_image(proxy_media_url(photo["url"])))
        for video in media.get("videos") or []:
            if isinstance(video, dict) and video.get("url"):
                duration = float(video.get("duration") or 0) or None
                self._add_limit_warning(result, duration)
                result.contents.append(
                    self.create_video(
                        proxy_media_url(video["url"]),
                        proxy_media_url(video.get("thumbnail_url")),
                        duration=duration,
                    )
                )

        if not result.contents and not result.text:
            raise ParseException("推文中没有可提取的内容")
        return result

    async def parse_by_oembed(self, url: str) -> ParseResult:
        from bs4 import BeautifulSoup

        async with self.new_client(headers={"User-Agent": VX_UA}) as client:
            response = await client.get("https://publish.twitter.com/oembed", params={
                "url": url.replace("https://x.com/", "https://twitter.com/", 1),
                "omit_script": "true",
            })
            response.raise_for_status()
            payload = response.json()
        paragraph = BeautifulSoup(payload.get("html") or "", "html.parser").select_one("blockquote p")
        if paragraph is not None:
            for br in paragraph.find_all("br"):
                br.replace_with("\n")
        if paragraph is None or not (text := paragraph.get_text().strip()):
            raise ParseException("X oEmbed 未返回公开正文")
        author = payload.get("author_name") or "X"
        return self.result(author=self.create_author(author), title=f"{author} 的推文", text=text, url=url)

    @staticmethod
    def _parse_created_at(value: Any) -> int | None:
        """解析 Twitter 的 ``Mon Jan 01 00:00:00 +0000 2024`` 时间串。

        不用 ``datetime.strptime`` 的 ``%a %b``：它们跟随进程 locale，
        宿主程序切到中文 locale 后会解析失败。改用与 locale 无关的
        RFC 2822 解析（两者格式兼容：星期与月份均为英文缩写）。
        """
        if not value:
            return None
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except Exception:
            return None

    @staticmethod
    def _tweet_text(tweet: dict) -> str:
        raw = tweet.get("raw_text")
        if isinstance(raw, dict) and raw.get("text"):
            return str(raw["text"])
        return str(tweet.get("text") or "")
