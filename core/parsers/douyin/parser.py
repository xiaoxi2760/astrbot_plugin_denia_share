# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""抖音解析器。

双路径取数，任一成功即可：

1. **轻量 HTML 路径**（rika 原实现）：抓取 iesdouyin / m.douyin 分享页的
   ``window._ROUTER_DATA``。零签名成本，但近年该入口逐渐收紧，常返回空页面。
2. **签名 Web API 路径**（娅娅版移植）：``/aweme/v1/web/aweme/detail/`` 配合
   a_bogus 签名 + ttwid 会话，稳定性更好，是 HTML 路径失败后的兜底。
"""

import re
from typing import Any, ClassVar

import aiohttp
from httpx import AsyncClient
from astrbot.api import logger

from ...base_parser import BaseParser, PlatformEnum, ParseException, handle
from ...data import Platform


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.DOUYIN, display_name="抖音")

    def __init__(self, downloader):
        super().__init__(downloader)
        self._web_client = None

    # ------------------------------------------------------------------ #
    # URL 注册
    # ------------------------------------------------------------------ #

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("douyin", r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("jingxuan.douyin", r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")

        # 1) 先走零成本的 HTML 路径
        if ty == "slides":
            try:
                return await self.parse_slides(vid)
            except Exception as e:
                logger.debug(f"[douyin] slides HTML 路径失败，转签名接口: {e}")
        else:
            for url in (self._build_m_douyin_url(ty, vid), self._build_iesdouyin_url(ty, vid)):
                try:
                    return await self.parse_video(url)
                except ParseException as e:
                    logger.debug(f"[douyin] HTML 路径失败 {url}: {e}")
                    continue

        # 2) 兜底：签名 Web API
        return await self.parse_by_web_api(vid)

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    # ------------------------------------------------------------------ #
    # 路径一：HTML _ROUTER_DATA
    # ------------------------------------------------------------------ #

    async def parse_video(self, url: str):
        from ...models.douyin.video import decoder as video_decoder

        async with AsyncClient(
            **self.client_kwargs(headers=self.ios_headers, follow_redirects=False)
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                raise ParseException(f"status: {response.status_code}")
            text = response.text

        pattern = re.compile(pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", flags=re.DOTALL)
        matched = pattern.search(text)
        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        video_data = video_decoder.decode(matched.group(1).strip()).video_data
        author = self.create_author(video_data.author.nickname, video_data.avatar_url)
        result = self.result(title=video_data.desc, author=author, timestamp=video_data.create_time)

        if image_urls := video_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
        elif video_url := video_data.video_url:
            self._add_limit_warning(result, video_data.duration)
            result.video = self.create_video(video_url, video_data.cover_url, video_data.duration)
        return result

    async def parse_slides(self, video_id: str):
        from ...models.douyin.slides import decoder as slides_decoder

        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}
        async with AsyncClient(
            **self.client_kwargs(headers=self.android_headers)
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        slides_data = slides_decoder.decode(response.content).aweme_details[0]
        author = self.create_author(slides_data.name, slides_data.avatar_url)
        result = self.result(title=slides_data.desc, author=author, timestamp=slides_data.create_time)

        if dynamic_urls := slides_data.dynamic_urls:
            for dynamic_url in dynamic_urls:
                result.contents.append(self.create_gif(dynamic_url))
        elif image_urls := slides_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
        return result

    # ------------------------------------------------------------------ #
    # 路径二：签名 Web API（娅娅版移植）
    # ------------------------------------------------------------------ #

    def _get_web_client(self):
        if self._web_client is None:
            from .web import DouyinWebClient

            self._web_client = DouyinWebClient()
        return self._web_client

    async def parse_by_web_api(self, item_id: str):
        client = self._get_web_client()
        async with aiohttp.ClientSession() as session:
            data = await client.fetch_detail(session, item_id)

        if not data:
            raise ParseException("分享已删除或资源直链提取失败, 请稍后再试")

        item = self._pick_item(data, item_id)
        if item is None:
            raise ParseException("响应中未包含目标作品")

        return self._build_result_from_item(item)

    @staticmethod
    def _pick_item(data: dict, item_id: str) -> dict | None:
        candidates: list[dict] = []
        detail = data.get("aweme_detail")
        if isinstance(detail, dict):
            candidates.append(detail)
        for key in ("aweme_details", "aweme_list", "item_list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(i for i in value if isinstance(i, dict))
        for item in candidates:
            if str(item.get("aweme_id") or item.get("id") or "") == str(item_id):
                return item
        return candidates[0] if candidates else None

    # ---- 媒体 URL 提取 ---- #

    @staticmethod
    def _first_url(value: Any) -> str | None:
        """从 url_list / 字符串 / 嵌套结构中取出第一个 http(s) 链接。"""
        if isinstance(value, str):
            return value if value.startswith(("http://", "https://")) else None
        if isinstance(value, list):
            for item in value:
                if found := DouyinParser._first_url(item):
                    return found
            return None
        if isinstance(value, dict):
            for key in ("url_list", "urlList", "download_url_list", "url"):
                if found := DouyinParser._first_url(value.get(key)):
                    return found
        return None

    @staticmethod
    def _no_watermark(url: str) -> str:
        """抖音直链去水印：playwm → play。"""
        return url.replace("/playwm/", "/play/").replace("playwm?", "play?")

    @classmethod
    def _video_urls(cls, item: dict) -> list[str]:
        """返回按优先级排序的备用视频直链（已去水印）。"""
        urls: list[str] = []
        video = item.get("video") or {}
        for key in ("play_addr", "playAddr", "PlayAddrStruct", "download_addr", "play_addr_h264"):
            if found := cls._first_url(video.get(key)):
                urls.append(cls._no_watermark(found))
        for bitrate in video.get("bit_rate") or []:
            if isinstance(bitrate, dict) and (found := cls._first_url(bitrate.get("play_addr"))):
                urls.append(cls._no_watermark(found))
        # 去重保序
        return list(dict.fromkeys(urls))

    @classmethod
    def _image_urls(cls, item: dict) -> list[str]:
        urls: list[str] = []
        for image in item.get("images") or []:
            if found := cls._first_url(image.get("url_list") or image):
                urls.append(found)
            # 图文里的内嵌视频段（slides）
            if isinstance(image, dict) and (found := cls._first_url((image.get("video") or {}).get("play_addr"))):
                urls.append(cls._no_watermark(found))
        return list(dict.fromkeys(urls))

    @staticmethod
    def _cover_url(item: dict) -> str | None:
        video = item.get("video") or {}
        for key in ("cover", "origin_cover", "dynamic_cover"):
            if found := DouyinParser._first_url(video.get(key)):
                return found
        return None

    @staticmethod
    def _duration(item: dict) -> float:
        video = item.get("video") or {}
        for key in ("duration", "video_duration"):
            raw = video.get(key) or item.get(key)
            if raw:
                value = float(raw)
                # 抖音 Web 接口返回的 duration 单位是毫秒
                return value / 1000 if value > 1000 else value
        return 0.0

    def _build_result_from_item(self, item: dict):
        author_info = item.get("author") or {}
        avatar = self._first_url(author_info.get("avatar_larger") or author_info.get("avatar_thumb"))
        author = self.create_author(author_info.get("nickname") or "抖音用户", avatar)

        result = self.result(
            title=item.get("desc") or item.get("item_title") or "",
            author=author,
            timestamp=int(item.get("create_time") or 0) or None,
            url=f"https://www.douyin.com/video/{item.get('aweme_id')}" if item.get("aweme_id") else None,
        )

        image_urls = self._image_urls(item)
        video_urls = self._video_urls(item)

        if video_urls:
            duration = self._duration(item)
            self._add_limit_warning(result, duration)
            result.video = self.create_video(video_urls[0], self._cover_url(item), duration)
        elif image_urls:
            result.contents.extend(self.create_images(image_urls))
        else:
            raise ParseException("未从作品中提取到媒体内容")

        return result
