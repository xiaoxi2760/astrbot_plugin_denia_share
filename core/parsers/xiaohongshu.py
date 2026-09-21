# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""小红书解析器"""
import re
from typing import ClassVar
from httpx import Cookies, AsyncClient
from astrbot.api import logger
from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, platform_of


class XiaoHongShuParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.XIAOHONGSHU)

    def __init__(self, downloader, xhs_ck: str | None = None):
        super().__init__(downloader)
        explore_headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            )
        }
        self.headers.update(explore_headers)

        discovery_headers = {
            "origin": "https://www.xiaohongshu.com",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        self.ios_headers.update(discovery_headers)

        if xhs_ck:
            self.headers["cookie"] = xhs_ck
            self.ios_headers["cookie"] = xhs_ck

    @handle("xhslink.com", r"xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    @handle("xhslink.cn", r"xhslink\.cn/[A-Za-z0-9._?%&+=/#@-]+")
    async def _parse_short_link_cn(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url, self.ios_headers)

    @handle("xiaohongshu.com", r"(explore|discovery/item)/(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)(?:\?[A-Za-z0-9._%&+=/#@-]*)?)")
    async def _parse_common(self, searched: re.Match[str]):
        xhs_domain = "https://www.xiaohongshu.com"
        query, xhs_id = searched.group("query", "xhs_id")

        try:
            return await self.parse_explore(f"{xhs_domain}/explore/{query}", xhs_id)
        except Exception as e:
            logger.warning(f"parse_explore failed, error: {e}, fallback to parse_discovery")
            return await self.parse_discovery(f"{xhs_domain}/discovery/item/{query}")

    # originalUrl 参数的域名白名单（含子域）。见 _is_trusted_note_url 的说明。
    _TRUSTED_HOSTS: ClassVar[tuple[str, ...]] = (
        "xiaohongshu.com", "xhslink.com", "xhslink.cn",
    )

    @handle("xiaohongshu.com", r"[?&]originalUrl=(?P<encoded>[^&\s]+)")
    async def _parse_safe_landing(self, searched: re.Match[str]):
        """安全落地页：真正的作品 id 藏在 ``originalUrl`` 参数里，而且是 URL-encoded 的。

        这类链接的 path 只有 ``/explore``（没有 id），上面那条
        ``(explore|discovery/item)/<id>`` **完全匹配不上** → 落到
        「没有平台规则能匹配」→ 走截图兜底（默认关）→ 用户看不到任何反应。

        恢复出真实 URL 后交给 ``_parse_common`` 的同一条链路（含它的
        explore → discovery 兜底）。注意这条 pattern 注册在 ``_parse_common`` **之后**：
        既带 id 又带 originalUrl 的链接应该按 path 里的 id 走，不该被这里改写。
        """
        from urllib.parse import unquote

        recovered = unquote(searched.group("encoded"))
        if not self._is_trusted_note_url(recovered):
            # 不校验域名的话，一个构造出来的 originalUrl=https://evil.example/
            # 就能让解析器去请求任意外站 —— 这是 SSRF 的最小版本。
            raise ParseException("安全落地页里的 originalUrl 不是小红书域名，已拒绝")
        _, matched = self.search_url(recovered)
        return await self._parse_common(matched)

    @classmethod
    def _is_trusted_note_url(cls, url: str) -> bool:
        """``originalUrl`` 的域名白名单校验（含子域，拒绝非 http(s) 协议）。"""
        from urllib.parse import urlparse

        try:
            parsed = urlparse(str(url or ""))
        except (TypeError, ValueError):
            return False
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        return any(
            host == domain or host.endswith(f".{domain}") for domain in cls._TRUSTED_HOSTS
        )

    async def parse_explore(self, url: str, xhs_id: str):
        from ..models.xiaohongshu.explore import decoder as explore_decoder

        async with AsyncClient(**self.client_kwargs(headers=self.headers)) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                response.raise_for_status()

        html = response.text
        raw = self._extract_initial_state_raw(html)
        init_state = explore_decoder.decode(raw)
        note_detail_wrapper = init_state.note.noteDetailMap.get(xhs_id)
        if not note_detail_wrapper:
            raise ParseException(f"can't find note detail for xhs_id: {xhs_id}")

        note_detail = note_detail_wrapper.note
        author = self.create_author(note_detail.nickname, note_detail.avatar_url)
        result = self.result(
            author=author, title=note_detail.title, text=note_detail.desc,
            extra={"is_video": note_detail.is_video},
        )
        if note_detail.is_video:
            video_url, cover_url, duration = note_detail.video_cover_duration
            self._add_limit_warning(result, duration)
            result.video = self.create_video(video_url, self._no_watermark(cover_url), duration)
        elif image_urls := note_detail.image_urls:
            result.contents.extend(self.create_images([self._no_watermark(u) for u in image_urls]))
        else:
            # 既不是视频也没有图片：抛出去让三级兜底继续。返回零媒体的空结果
            # 会让用户拿到只有标题的卡片且零警告（与抖音那类问题同一个形状）。
            raise ParseException("图文详情里既没有视频也没有图片")
        return result

    async def parse_discovery(self, url: str):
        from ..models.xiaohongshu.discovery import decoder as discovery_decoder

        async with AsyncClient(
            **self.client_kwargs(
                headers=self.ios_headers,
                follow_redirects=True,
                cookies=Cookies(),
            )
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        raw = self._extract_initial_state_raw(html)
        init_state = discovery_decoder.decode(raw)
        note_data = init_state.noteData.data.noteData
        preload_data = init_state.noteData.normalNotePreloadData

        author = self.create_author(note_data.user.nickName, note_data.user.avatar)
        result = self.result(
            author=author, title=note_data.title, text=note_data.desc,
            timestamp=note_data.time // 1000,
            extra={"is_video": note_data.is_video},
        )
        if note_data.is_video:
            video_url, duration = note_data.url_and_duration
            # 封面可能缺失（视频笔记没带封面图，或 preload 里没有）：直接 [0] 会
            # IndexError，而它是 Exception 子类、会被 _process_url 的最后一级接住 ——
            # 日志只剩「解析异常」+ 堆栈，看不出是封面缺失。降级为 None，
            # create_video 在没有封面时会自动抽首帧。
            cover_url = None
            if preload_data and preload_data.image_urls:
                cover_url = preload_data.image_urls[0]
            elif note_data.image_urls:
                cover_url = note_data.image_urls[0]
            self._add_limit_warning(result, duration)
            result.video = self.create_video(video_url, self._no_watermark(cover_url), duration)
        elif img_urls := note_data.image_urls:
            result.contents.extend(self.create_images([self._no_watermark(u) for u in img_urls]))
        else:
            # 既不是视频也没有图片：抛出去让三级兜底继续。返回零媒体的空结果会让
            # 用户拿到只有标题的卡片且零警告 —— parse_explore 早就有同样的守卫，
            # 同一个文件里两条路径不一致（这条是补上的）。
            raise ParseException("图文详情里既没有视频也没有图片")
        return result

    @staticmethod
    def _no_watermark(url: str | None) -> str | None:
        """把带鉴权的 webpic 链接改写成公开的无水印原图链接。

        - ``sns-webpic-*`` 是鉴权 CDN，部分场景会叠加平台水印（画面中央「小红书」字样）
        - ``sns-img-hw`` 是公开原图 CDN，无水印

        来源：娅娅版 astrbot_plugin_media_parser，实测（2026-08-23）fileId 需保留
        命名空间前缀（``notes_pre_post/``、``spectrum/``），图集常见的裸 ``1040g...``
        同样可用。匹配不上时原样返回，不会把链接弄坏。
        """
        if not url or ("sns-webpic" not in url and "sns-img" not in url):
            return url
        matched = re.search(r"((?:(?:notes_pre_post|spectrum)/)?1040g[^!?]+)", url)
        if not matched:
            return url
        return f"https://sns-img-hw.xhscdn.com/{matched.group(1)}?imageView2/2/w/1080/format/jpg"

    def _extract_initial_state_raw(self, html: str) -> str:
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        matched = re.search(pattern, html)
        if not matched:
            raise ParseException("小红书分享链接失效或内容已删除")
        return matched.group(1).replace("undefined", "null")
