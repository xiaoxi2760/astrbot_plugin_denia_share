# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""快手解析器。

合并两个来源的处理能力：

- **rika 原实现**：短链重定向 + ``window.INIT_STATE`` + msgspec 结构化解码，
  并做了 ``/fw/long-video/`` → ``/fw/photo/`` 的长视频改写。
- **娅娅版增强**：新版分享页把 ``photo`` 序列化成了 **JSON 字符串**（rika 的
  严格结构体解码在这里会直接失败），图集还额外依赖 ``single.cdnList``。
  这里改为「dict / JSON 字符串都兼容」后再提取，图集与视频都能覆盖。
"""

import json
import re
from random import choice
from typing import Any, ClassVar

from httpx import AsyncClient
from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, platform_of


class KuaiShouParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.KUAISHOU)

    def __init__(self, downloader):
        super().__init__(downloader)
        self.ios_headers["Referer"] = "https://v.kuaishou.com/"

    @handle("v.kuaishou", r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    @handle("kuaishou", r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    @handle("chenzhongtech", r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+")
    async def _parse_v_kuaishou(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        real_url = await self.get_redirect_url(url, headers=self.ios_headers)
        if len(real_url) <= 0:
            raise ParseException("failed to get location url from url")

        # 长视频入口改写为标准作品入口（rika 的处理）
        real_url = real_url.replace("/fw/long-video/", "/fw/photo/")

        html = await self._fetch_html(real_url)
        photo = self._extract_photo(html)
        if photo is None:
            raise ParseException("window.INIT_STATE 中未包含作品数据")
        return self._build_result(photo, real_url)

    # ------------------------------------------------------------------ #

    async def _fetch_html(self, url: str) -> str:
        async with AsyncClient(
            **self.client_kwargs(headers=self.ios_headers, follow_redirects=True)
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    @staticmethod
    def _init_state(html: str) -> dict[str, Any] | None:
        matched = re.search(r"window\.INIT_STATE\s*=\s*(.*?)</script>", html, flags=re.DOTALL)
        if not matched:
            return None
        try:
            data = json.loads(matched.group(1).strip())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _loads_if_str(value: Any) -> Any:
        """photo / single / ext_params 在新版页面里可能是 JSON 字符串。"""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return None
        return value

    def _extract_photo(self, html: str) -> dict[str, Any] | None:
        """返回 (photo dict, single dict)。"""
        init_state = self._init_state(html)
        if not init_state:
            return None

        for value in init_state.values():
            if not isinstance(value, dict) or "photo" not in value:
                continue
            photo = self._loads_if_str(value.get("photo"))
            if not isinstance(photo, dict):
                continue
            single = self._loads_if_str(value.get("single"))
            photo["_single"] = single if isinstance(single, dict) else {}
            return photo

        logger.debug("[kuaishou] INIT_STATE 中未找到 photo 字段")
        return None

    # ------------------------------------------------------------------ #

    @staticmethod
    def _cdn_urls(items: Any) -> list[str]:
        if not isinstance(items, list):
            return []
        return [i["url"] for i in items if isinstance(i, dict) and i.get("url")]

    @staticmethod
    def _cdn_list(items: Any) -> list[str]:
        if not isinstance(items, list):
            return []
        return [i["cdn"] for i in items if isinstance(i, dict) and i.get("cdn")]

    def _album_urls(self, photo: dict[str, Any]) -> list[str]:
        """图集图片 URL。新版走 single.cdnList，旧版走 ext_params.atlas。"""
        ext_params = self._loads_if_str(photo.get("ext_params")) or {}
        atlas = ext_params.get("atlas")
        atlas = self._loads_if_str(atlas) if atlas is not None else None

        cdns: list[str] = []
        routes: list[str] = []

        if isinstance(atlas, dict):
            cdns = self._cdn_list(atlas.get("cdnList"))
            routes = [r for r in (atlas.get("list") or []) if isinstance(r, str)]
            if not cdns:
                raw = atlas.get("cdn")
                cdns = [raw] if isinstance(raw, str) else [c for c in (raw or []) if isinstance(c, str)]

        single = photo.get("_single") or {}
        if not cdns:
            cdns = self._cdn_list(single.get("cdnList"))
        if not routes:
            routes = [r for r in (single.get("imgRouteList") or []) if isinstance(r, str)]

        if not cdns or not routes:
            return []
        cdn = choice(cdns)
        return [f"https://{cdn}/{route}" for route in routes]

    def _build_result(self, photo: dict[str, Any], url: str):
        name = str(photo.get("userName") or photo.get("user_name") or "").replace("\u3164", "").strip()
        author = self.create_author(name or "快手用户", photo.get("headUrl") or photo.get("head_url"))

        duration_ms = int(photo.get("duration") or 0)
        duration = duration_ms // 1000

        result = self.result(
            title=photo.get("caption") or "",
            author=author,
            timestamp=int(photo.get("timestamp") or 0) // 1000 or None,
            url=url,
        )

        video_urls = [u for u in self._cdn_urls(photo.get("mainMvUrls")) if ".mp4" in u]
        cover_urls = self._cdn_urls(photo.get("coverUrls"))
        album_urls = self._album_urls(photo)

        if video_urls:
            self._add_limit_warning(result, duration)
            result.video = self.create_video(
                video_urls[0], choice(cover_urls) if cover_urls else None, duration,
            )
        elif album_urls:
            result.contents.extend(self.create_images(album_urls))
        else:
            raise ParseException("作品中没有可提取的视频或图集")

        return result
