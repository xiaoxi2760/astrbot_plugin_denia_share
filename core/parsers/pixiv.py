"""Pixiv 解析器

支持两种入口：
- 作品链接解析（pixiv.net/artworks/{id}）— 走 BaseParser 的链接注册
- 关键词搜索（search()） — 由 /pixiv 命令调用，不经过链接匹配

**内容安全（硬性，不可配置关闭）**
Pixiv 的 `xRestrict` 字段：0=全年龄，1=R18，2=R18G。
实测未登录时搜索结果 `xRestrict` 恒为 0，但一旦配置了 Cookie 就可能返回 R18。
在 QQ 群内发送 R18 内容是违规行为，因此本模块**强制过滤** xRestrict != 0 的作品，
不提供任何开关。配置 Cookie 只是为了让搜索能带登录态（更全的收录），
不会因为登录而放宽过滤。
"""

import re
from typing import Any, ClassVar
from urllib.parse import quote

from httpx import AsyncClient
from astrbot.api import logger

from ..base_parser import (
    BaseParser, PlatformEnum, ParseException, IgnoreException, handle,
)
from ..data import Platform

AJAX_BASE = "https://www.pixiv.net/ajax"
PIXIV_REFERER = "https://www.pixiv.net/"

PIXIV_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# 单次最多发送的图片张数，避免刷屏与发送超时
MAX_IMAGES = 9
MAX_SEARCH_IMAGES = 6


class PixivParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.PIXIV, display_name="Pixiv")

    def __init__(self, downloader, cookie: str = ""):
        super().__init__(downloader)
        self.cookie = (cookie or "").strip()
        # pximg 校验 Referer，基类 create_image / create_author 都会带上
        self.headers = {
            "User-Agent": PIXIV_UA,
            "Referer": PIXIV_REFERER,
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }

    def _ajax_headers(self, referer: str = PIXIV_REFERER) -> dict[str, str]:
        headers = dict(self.headers)
        headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _fetch_ajax(self, client: AsyncClient, url: str, referer: str = PIXIV_REFERER, params: dict | None = None):
        """请求 Pixiv Ajax 接口，被 Cloudflare 拦截或返回 HTML 时抛出 ParseException。"""
        resp = await client.get(url, headers=self._ajax_headers(referer), params=params)
        if resp.status_code == 404:
            raise ParseException("作品不存在或已被删除")
        resp.raise_for_status()

        body = resp.text
        if body.lstrip().lower().startswith("<!doctype html") or "text/html" in (
            resp.headers.get("content-type", "").lower()
        ):
            if "just a moment" in body.lower():
                raise ParseException("Pixiv 被 Cloudflare 拦截，Cookie 可能已失效")
            raise ParseException("Pixiv 返回了 HTML 页面而非 JSON")

        try:
            data = resp.json()
        except Exception as e:
            raise ParseException(f"Pixiv JSON 解析失败: {e}") from e
        if not isinstance(data, dict) or data.get("error"):
            raise ParseException(f"Pixiv 接口错误: {data.get('message') if isinstance(data, dict) else '未知'}")
        return data.get("body") or {}

    # ────────────── 作品链接解析 ────────────── #

    @handle("pixiv.net", r"pixiv\.net/(?:artworks/|i/|member_illust\.php\?.*?illust_id=)(?P<illust_id>\d+)")
    async def _parse(self, searched: re.Match[str]):
        illust_id = searched.group("illust_id")
        referer = f"https://www.pixiv.net/artworks/{illust_id}"

        async with self.new_client() as client:
            detail = await self._fetch_ajax(client, f"{AJAX_BASE}/illust/{illust_id}", referer)
            if self._is_unsafe(detail):
                raise IgnoreException("该作品非全年龄内容，已跳过")
            pages = await self._fetch_ajax(client, f"{AJAX_BASE}/illust/{illust_id}/pages", referer)

        urls = self._page_urls(pages, original=True)[:MAX_IMAGES]
        if not urls:
            raise ParseException("未取到图片地址")

        author = self.create_author(
            detail.get("userName") or "",
            detail.get("profileImageUrl") or None,
        )
        tags = [t for t in (detail.get("tags") or {}).get("tags", []) if isinstance(t, dict)]
        tag_names = [str(t.get("tag") or "") for t in tags if t.get("tag")]

        desc = self._strip_tags(detail.get("description") or "")
        lines = []
        if tag_names:
            lines.append("标签: " + "、".join(tag_names[:10]))
        size = detail.get("pageCount") or len(urls)
        if size and size > 1:
            lines.append(f"共 {size} 张（本次发送 {len(urls)} 张）")
        if detail.get("aiType") == 2:
            lines.append("AI 生成")

        result = self.result(
            title=detail.get("title") or f"Pixiv {illust_id}",
            text=desc or None,
            author=author,
            url=f"https://www.pixiv.net/artworks/{illust_id}",
            extra={"content_type": "插画" if size == 1 else "图集"},
        )
        if lines:
            result.extra["info"] = "\n".join(lines)
        result.contents.extend(self.create_images(urls))
        return result

    # ────────────── 关键词搜索 ────────────── #

    async def search(self, keyword: str, limit: int = MAX_SEARCH_IMAGES) -> Any:
        """按关键词搜索作品，返回最多 limit 张全年龄作品。

        与链接解析不同，这里没有一个"作品作者"，因此只填充标题与图片。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise ParseException("请输入搜索关键词")
        limit = max(1, min(int(limit), MAX_SEARCH_IMAGES))

        url = f"{AJAX_BASE}/search/artworks/{quote(keyword, safe='')}"
        params = {
            "word": keyword,
            "order": "date_d",
            "mode": "all",
            "p": "1",
            "s_mode": "s_tag",
            "type": "all",
            "lang": "zh",
        }
        async with self.new_client() as client:
            data = await self._fetch_ajax(client, url, PIXIV_REFERER, params=params)

        items = ((data.get("illustManga") or {}).get("data")) or []
        if not items:
            raise ParseException(f"未搜到「{keyword}」相关作品")

        total = (data.get("illustManga") or {}).get("total") or len(items)
        safe_items = [it for it in items if not self._is_unsafe(it)]
        blocked = len(items) - len(safe_items)
        if blocked:
            logger.info(f"[pixiv] 搜索「{keyword}」过滤掉 {blocked} 个非全年龄作品")
        if not safe_items:
            raise IgnoreException(f"「{keyword}」的搜索结果均为非全年龄内容，已全部跳过")

        picked: list[str] = []
        authors: set[str] = set()
        for item in safe_items:
            if len(picked) >= limit:
                break
            u = (item.get("url") or "").strip()
            if not u:
                continue
            picked.append(self._enlarge_thumb(u))
            if item.get("userName"):
                authors.add(str(item["userName"]))

        if not picked:
            raise ParseException("搜索结果中没有可用图片")

        result = self.result(
            title=f"Pixiv 搜索: {keyword}",
            text=f"命中 {total} 件，展示 {len(picked)} 张"
                 + (f"（已过滤 {blocked} 件非全年龄）" if blocked else ""),
            extra={"content_type": "搜索"},
        )
        if authors:
            result.extra["info"] = "画师: " + "、".join(list(authors)[:6])
        result.contents.extend(self.create_images(picked))
        return result

    # ────────────── 工具 ────────────── #

    @staticmethod
    def _is_unsafe(item: dict[str, Any]) -> bool:
        """xRestrict: 0=全年龄, 1=R18, 2=R18G — 非 0 一律拦截。"""
        return int(item.get("xRestrict") or 0) != 0

    @staticmethod
    def _enlarge_thumb(url: str) -> str:
        """搜索结果给的是 250x250 缩略图，换成 1200px 的 master 图。"""
        return re.sub(
            r"/c/\d+x\d+_\d+_a2/img-master/",
            "/img-master/",
            url,
        )

    @staticmethod
    def _page_urls(pages: Any, original: bool = False) -> list[str]:
        if not isinstance(pages, list):
            return []
        urls: list[str] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            u = (page.get("urls") or {}).get("original" if original else "regular")
            if u:
                urls.append(u)
        return urls

    @staticmethod
    def _strip_tags(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text or "").strip()
