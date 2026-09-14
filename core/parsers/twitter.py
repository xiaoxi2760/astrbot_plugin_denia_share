"""Twitter / X 解析器。

三级取数，逐级兜底：

1. **vxtwitter**（rika 原实现）—— 第三方镜像，零成本、字段最全。
2. **fxtwitter** —— 同族镜像，vxtwitter 抽风时顶上。
3. **Guest GraphQL**（娅娅版移植）—— 官方接口 + guest token，
   不依赖任何第三方镜像，是前两级都失败时的最后防线。
"""

import json
import re
from datetime import datetime
from typing import Any, ClassVar

import aiohttp
from httpx import AsyncClient
from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, ParseResult

BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOjj6tT7UeCs"
    "TnIU3U%3D0owR4rQG2v0nE"
)
GUEST_TOKEN_URL = "https://api.twitter.com/1.1/guest/activate.json"
GRAPHQL_ENDPOINT = (
    "https://twitter.com/i/api/graphql/0hWvDhmW8YQ-S_ib3azIrw/TweetResultByRestId"
)

GRAPHQL_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.TWITTER, display_name="小蓝鸟")

    @handle("x.com", r"x\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)")
    @handle("twitter.com", r"twitter\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)")
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        url = f"https://{searched.group(0)}"
        tweet_id = searched.group("tweet_id")

        try:
            return await self.parse_by_vxapi(url)
        except Exception as e:
            logger.debug(f"[twitter] vxtwitter 失败: {e}")

        try:
            return await self.parse_by_fxapi(url, tweet_id)
        except Exception as e:
            logger.debug(f"[twitter] fxtwitter 失败: {e}")

        return await self.parse_by_graphql(url, tweet_id)

    # ------------------------------------------------------------------ #
    # 1) vxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_vxapi(self, url: str) -> ParseResult:
        api_url = url.replace("x.com", "api.vxtwitter.com").replace("twitter.com", "api.vxtwitter.com")
        async with AsyncClient(headers=self.headers, timeout=self.timeout) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()

        author = self.create_author(data.get("user_name") or "", data.get("user_profile_image_url"))
        article = data.get("article")
        title = article.get("title") if isinstance(article, dict) else article

        result = self.result(
            author=author, title=title, text=data.get("text"),
            timestamp=data.get("date_epoch"), url=url,
        )
        for media in data.get("media_extended") or []:
            mtype = media.get("type")
            if mtype in ("video", "gif"):
                duration = (media.get("duration_millis") or 0) / 1000 or None
                self._add_limit_warning(result, duration)
                result.contents.append(
                    self.create_video(
                        media.get("url"), media.get("thumbnail_url"),
                        duration=duration, is_gif=mtype == "gif",
                    )
                )
            elif mtype == "image":
                result.contents.append(self.create_image(f"{media.get('url')}?format=jpg&name=orig"))
        return result

    # ------------------------------------------------------------------ #
    # 2) fxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_fxapi(self, url: str, tweet_id: str) -> ParseResult:
        api_url = url.replace("x.com", "api.fxtwitter.com").replace("twitter.com", "api.fxtwitter.com")
        async with AsyncClient(headers=self.headers, timeout=self.timeout) as client:
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
            (info.get("avatar_url") or None),
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
                result.contents.append(self.create_image(photo["url"]))
        for video in media.get("videos") or []:
            if isinstance(video, dict) and video.get("url"):
                duration = float(video.get("duration") or 0) or None
                self._add_limit_warning(result, duration)
                result.contents.append(
                    self.create_video(video["url"], video.get("thumbnail_url"), duration=duration)
                )
        return result

    # ------------------------------------------------------------------ #
    # 3) Guest GraphQL
    # ------------------------------------------------------------------ #

    async def parse_by_graphql(self, url: str, tweet_id: str) -> ParseResult:
        async with aiohttp.ClientSession() as session:
            token = await self._guest_token(session)
            headers = {
                **self.headers,
                "Authorization": f"Bearer {BEARER}",
                "x-guest-token": token,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
                "Referer": "https://twitter.com/",
            }
            params = {
                "variables": json.dumps(
                    {
                        "tweetId": tweet_id,
                        "withCommunity": False,
                        "includePromotedContent": False,
                        "withVoice": False,
                    },
                    separators=(",", ":"),
                ),
                "features": json.dumps(GRAPHQL_FEATURES, separators=(",", ":")),
            }
            async with session.get(
                GRAPHQL_ENDPOINT, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)

        tweet = self._find_tweet(data, tweet_id)
        if tweet is None:
            raise ParseException("Twitter GraphQL 响应中未找到目标推文")

        legacy = tweet.get("legacy") or {}
        user_result = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
        user_legacy = user_result.get("legacy") or {}

        name = user_legacy.get("name") or ""
        screen = user_legacy.get("screen_name") or ""
        author = self.create_author(
            f"{name}(@{screen})" if name and screen else (name or screen),
            user_legacy.get("profile_image_url_https"),
        )

        result = self.result(
            author=author, title=f"{author.name} 的推文" if author.name else "Twitter 推文",
            text=legacy.get("full_text") or "",
            timestamp=self._parse_created_at(legacy.get("created_at")),
            url=url,
        )

        for media in (legacy.get("extended_entities") or {}).get("media") or []:
            if not isinstance(media, dict):
                continue
            mtype = media.get("type")
            if mtype == "photo":
                img = media.get("media_url_https")
                if img:
                    result.contents.append(self.create_image(f"{img}?format=jpg&name=orig"))
            elif mtype in ("video", "animated_gif"):
                video_url = self._best_variant(media)
                if video_url:
                    duration = (media.get("video_info", {}).get("duration_millis") or 0) / 1000 or None
                    self._add_limit_warning(result, duration)
                    result.contents.append(
                        self.create_video(
                            video_url, media.get("media_url_https"),
                            duration=duration, is_gif=mtype == "animated_gif",
                        )
                    )

        if not result.contents and not result.text:
            raise ParseException("推文中没有可提取的内容")
        return result

    async def _guest_token(self, session: aiohttp.ClientSession) -> str:
        headers = {**self.headers, "Authorization": f"Bearer {BEARER}"}
        async with session.post(
            GUEST_TOKEN_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        token = str(data.get("guest_token") or "").strip()
        if not token:
            raise ParseException("Twitter guest token 为空")
        return token

    @staticmethod
    def _walk(obj: Any):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from TwitterParser._walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from TwitterParser._walk(value)

    @classmethod
    def _find_tweet(cls, data: Any, tweet_id: str) -> dict | None:
        for candidate in cls._walk(data):
            legacy = candidate.get("legacy")
            if not isinstance(legacy, dict):
                continue
            if str(candidate.get("rest_id") or legacy.get("id_str") or "") == str(tweet_id):
                return candidate
        return None

    @staticmethod
    def _best_variant(media: dict) -> str | None:
        """在 video_info.variants 中挑选码率最高的 mp4。"""
        variants = (media.get("video_info") or {}).get("variants") or []
        best_url, best_rate = None, -1
        for variant in variants:
            url = variant.get("url") or ""
            if ".mp4" not in url:
                continue
            rate = int(variant.get("bitrate") or 0)
            if rate >= best_rate:
                best_url, best_rate = url, rate
        return best_url

    @staticmethod
    def _parse_created_at(value: Any) -> int | None:
        if not value:
            return None
        try:
            return int(datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y").timestamp())
        except Exception:
            return None

    @staticmethod
    def _tweet_text(tweet: dict) -> str:
        raw = tweet.get("raw_text")
        if isinstance(raw, dict) and raw.get("text"):
            return str(raw["text"])
        return str(tweet.get("text") or "")
