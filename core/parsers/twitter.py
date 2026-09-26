# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""Twitter / X 解析器。

两级取数，逐级兜底：

1. **vxtwitter**（rika 原实现）—— 第三方镜像，零成本、字段最全。
2. **fxtwitter** —— 同族镜像，vxtwitter 抽风时顶上。

两级都失败时抛 ``ParseException``（可读原因），不再有第三级。

关于第三级 Guest GraphQL 的删除（2026-09-26）
-------------------------------------------
本文件曾移植娅娅版的「官方接口 + guest token」作为最后防线。实测该级**恒定失败**，
已于 2026-09-26 整体移除，原因有三：

1. 它依赖的公开 ``BEARER`` 已被 Twitter 吊销 —— ``guest/activate.json`` 恒返回
   ``401 {"errors":[{"message":"Invalid or expired token","code":89}]}``。
   也就是说这一级**不是兜底，而是一个必然失败的请求**。
2. ``aiohttp.ClientResponseError`` 不是 ``ParseException`` 子类。作为最后一环，
   它的异常会穿透 ``_parse`` 的两个 ``except Exception``（那两处只兜前两级），
   一路到 ``webui.parse_url`` 的 ``except Exception`` —— 用户看到的是 **HTTP 500**，
   而不是「解析失败」的 422 与可读原因。
3. 上游莉卡 v3.0.1 也只保留 vxtwitter 一条路。少一级「必然失败」反而更可靠。

关于 vxtwitter 必须用独立 UA（2026-09-26）
------------------------------------------
``api.vxtwitter.com`` 挂在 Cloudflare 后面，其规则是：**伪装成真实浏览器的 UA 会被
下发 JS 挑战页（403 "Just a moment..."），而老实声明自己是程序的 UA 直接放行。**

2026-09-26 在服务器容器内实测（直连与走代理结果一致）：

===========================  ==========
UA                           状态码
===========================  ==========
（不带头）                    200
``python-httpx/0.27.0``      200
``curl/8.0``                 200
``Wget/1.21.4``              200
仓库全局 ``COMMON_HEADER``    403  ← 本插件原先踩的就是这个
真实 Chrome 120 / Edge 120    403
Safari 17 (macOS)            403
空字符串                      403
===========================  ==========

所以这里给 vxtwitter **单独**一个非浏览器 UA，而**不改全局 ``COMMON_HEADER``** ——
后者是抖音 / 小红书 / 微博等全平台共用的，改它会外溢到无关平台。

对照：``api.fxtwitter.com`` 没有这条规则，任何非空 UA 都能过（只有**空** UA 会返回
``401`` 并明确提示 "You must identify yourself with a User-Agent header"）。
"""

import re
from datetime import timezone
from typing import Any, ClassVar
from urllib.parse import urlsplit

from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, ParseResult, platform_of

# pbs.twimg.com / video.twimg.com 在国内常不可达，可配置反代根地址绕过
_TWIMG_HOST_PREFIX = {
    "pbs.twimg.com": "pbs",
    "video.twimg.com": "video",
}

# vxtwitter 专用 UA。刻意**带 httpx 自报名、不带浏览器签名** —— 见模块 docstring：
# api.vxtwitter.com 的 Cloudflare 只挑战「像浏览器」的 UA，对程序化 UA 直接放行。
VX_UA = "python-httpx/0.27.0"


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

    @handle("x.com", r"x\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)")
    @handle("twitter.com", r"twitter\.com/[0-9a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)")
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        url = f"https://{searched.group(0)}"
        tweet_id = searched.group("tweet_id")
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

        # 这里是最后一级，异常必须以 ParseException 形式抛出：再往上就只有
        # webui.parse_url 的裸 except Exception，那会变成 HTTP 500。
        raise ParseException("Twitter 解析失败（" + "；".join(failures) + "）")

    # ------------------------------------------------------------------ #
    # 1) vxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_vxapi(self, url: str) -> ParseResult:
        api_url = url.replace("x.com", "api.vxtwitter.com").replace("twitter.com", "api.vxtwitter.com")
        # 独立 UA：不能用全局 COMMON_HEADER，否则被 Cloudflare 挑战（见模块 docstring）
        headers = {**self.headers, "User-Agent": VX_UA}
        async with self.new_client(headers=headers) as client:
            response = await client.get(api_url)
            response.raise_for_status()
            data = response.json()

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
        return result

    # ------------------------------------------------------------------ #
    # 2) fxtwitter
    # ------------------------------------------------------------------ #

    async def parse_by_fxapi(self, url: str, tweet_id: str) -> ParseResult:
        api_url = url.replace("x.com", "api.fxtwitter.com").replace("twitter.com", "api.fxtwitter.com")
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
