"""B站自建解析器 —— ``bilibili-api-python`` 缺失时的替补。

**这不是主力，是替补。** 主力在 :mod:`core.parsers.bilibili`，有登录态、有
AI 总结、在线人数、番剧、动态、直播、收藏夹、专栏。本模块只在那个依赖
压根装不上时顶班（见 :mod:`core.parsers` 的隔离说明）。

## 刻意不做的功能

只做 **BV / av 视频**，其余一律不做：动态、opus、直播、收藏夹、专栏、AI 总结、
在线人数。理由不是省事，而是**备用路径一旦开始复刻主力，就会跟着主力一起腐化**
—— 它存在的意义是「主力彻底不能用时仍能下载视频」，不是「功能对等」。

**零 ``bilibili_api`` 依赖**：本模块及其 import 链里不允许出现那个名字。
一旦有人在里面偷偷 import 回来，替补在真正的关键时刻会直接 import 失败 ——
那正是它要防的场景。

## 匹配范围必须与主力一致

``@handle`` 的 pattern 与 :mod:`core.parsers.bilibili` 逐条对齐。否则会出现
「依赖正常时能解析、依赖缺失时匹配不上」这种最难查的不一致。
:mod:`tests.test_bili_fallback_parser` 有对照用例钉住这一点。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar, Dict, List, Optional

from ..base_parser import (
    BaseParser,
    ParseException,
    ParseResult,
    PlatformEnum,
    handle,
)
from ..data import Platform, platform_of
from ..exception import MediaProcessException
from .fetch import fetch_streams
from .video import ResolvedPage, VideoInfo, fetch_video_info
from .wbi import WbiKeyProvider

__all__ = ["FallbackBilibiliParser"]

# 与 core/parsers/bilibili.py 的 _codec_order 同源的清晰度档位映射
_QUALITY_BY_NAME: Dict[str, int] = {
    "360P": 16,
    "480P": 32,
    "720P": 64,
    "1080P": 80,
    "1080P+": 112,
    "4K": 120,
    "8K": 127,
}
_DEFAULT_QN = 80

_STAT_ICONS = (
    ("like", "👍"),
    ("coin", "🪙"),
    ("favorite", "⭐"),
    ("share", "↩️"),
    ("reply", "💬"),
    ("view", "👀"),
    ("danmaku", "💭"),
)


def _fmt_count(value: Any) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 10000:
        text = f"{n / 10000:.1f}万"
        return text[:-1] if text.endswith(".0万") else text
    return str(n)


def _fmt_stats(stat: Dict[str, int]) -> str:
    parts = []
    for key, icon in _STAT_ICONS:
        text = _fmt_count(stat.get(key))
        if text != "0":
            parts.append(f"{icon} {text}")
    return " ".join(parts)


class FallbackBilibiliParser(BaseParser):
    """不依赖 ``bilibili-api-python`` 的 B站视频解析器。"""

    platform: ClassVar[Platform] = platform_of(PlatformEnum.BILIBILI)

    #: 替补解析器的对外提示。展示在结果里，别让用户以为功能完整。
    _NOTICE = "ℹ️ B站自建备用解析：动态 / 直播 / 收藏夹 / 专栏暂不可用"

    def __init__(self, downloader, bili_ck: str = "", config_dir=None):
        super().__init__(downloader)
        self._bili_ck = str(bili_ck or "")
        self._config_dir = config_dir
        self._key_provider = WbiKeyProvider(lambda: self.new_client())

    # ---- 登录态 ----
    #
    # 这两个方法是 main.py 用 `hasattr(parser, "update_cookie")` 探测后调用的。
    # 替补**必须**实现它们，否则 `hasattr` 为 False → 扫码登录拿到的 cookie
    # 被**静默丢弃**（不报错、不留痕），而 WebUI 仍显示「已配置」——
    # 用户看到「登录成功了」，实际解析器一直是匿名，清晰度默默降档。
    #
    # 这里刻意不碰 Credential / 扫码刷新：替补只把 cookie 当字符串透传，
    # 用完即弃。少即是多 —— 复刻主力那套登录态机制就等于把备用路径拖进
    # 同样的腐化里。

    def update_cookie(self, cookie_str: str) -> None:
        """接收扫码登录得到的 cookie。"""
        self._bili_ck = str(cookie_str or "").strip()

    def clear_cookie(self) -> None:
        """清除 cookie（``/bili_logout`` 会调）。"""
        self._bili_ck = ""

    @property
    def has_cookie(self) -> bool:
        return bool(self._bili_ck)

    def _headers_with_cookie(self) -> Dict[str, str]:
        """带 Cookie 的请求头。``view`` / ``nav`` / ``playurl`` 三处共用。"""
        headers = dict(self.headers)
        if self._bili_ck:
            headers["Cookie"] = self._bili_ck
        return headers

    # ---- 与主力一致的 @handle pattern ----

    @handle("b23.tv", r"b23\.tv/[0-9a-zA-Z._?%&+-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[0-9a-zA-Z._?%&+-=/#]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        return await self.parse_with_redirect(f"https://{searched.group(0)}")

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/BV", r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_bv(self, searched: re.Match[str]):
        return await self.parse_video(
            bvid=str(searched.group("bvid")),
            page_num=int(searched.group("page_num") or 1),
        )

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/av", r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_av(self, searched: re.Match[str]):
        return await self.parse_video(
            avid=int(searched.group("avid")),
            page_num=int(searched.group("page_num") or 1),
        )

    # ---- 主流程 ----

    async def parse_video(
        self, *, bvid: str = "", avid: int = 0, page_num: int = 1
    ) -> ParseResult:
        from ..config import get_config
        from ..media_utils import fmt_duration

        pconfig = get_config()

        info = await fetch_video_info(
            lambda: self.new_client(),
            bvid=bvid,
            avid=avid,
            # view 也要带 cookie：登录态下它决定标题/统计/分P 这些元数据的
            # 完整度。只在 playurl 上带、这里不带，等于「用户扫码登录了但看到的
            # 还是匿名视角的内容」。
            headers=self._headers_with_cookie(),
        )
        if not info.bvid:
            raise ParseException("无法从接口响应中取得 BV 号")

        page = info.resolve_page(page_num)
        if page is None:
            raise ParseException("接口未返回分P信息")

        url = f"https://bilibili.com/{info.bvid}"
        if page.index > 0:
            url += f"?p={page.index + 1}"

        limit_warnings: List[str] = []
        duration_str = fmt_duration(page.duration)
        if page.duration > pconfig.VIDEO_DURATION_MAXIMUM:
            limit_warnings.append(
                f"⚠️ 视频时长({duration_str})超过限制("
                f"{fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，不会下载视频"
            )
        # 备用模式的告知**要放进 limit_warnings**，不能只放 extra["info"] ——
        # card_renderer 只渲染 stats_line / duration / online / limit_warnings
        # 四个字段，info 在默认的出图模式下**根本不显示**（只有文本回退路径会读）。
        # 放这里用户才能在卡片上看到「现在解析能力是打折的」。
        limit_warnings.insert(0, self._NOTICE)

        author = self.create_author(info.author_name, info.author_face)

        async def download_video():
            return await self._download(
                info, page, pconfig, limit_warnings,
            )

        video_content = self.create_video(
            asyncio.create_task(download_video()),
            page.cover,
            page.duration,
        )

        return self.result(
            url=url,
            title=page.title,
            timestamp=page.timestamp,
            text=info.description,
            author=author,
            contents=[video_content],
            extra={
                "info": self._NOTICE,
                "stats_line": _fmt_stats(info.stat),
                "duration": duration_str,
                "content_type": "视频",
                "limit_warnings": limit_warnings,
            },
        )

    async def _download(
        self,
        info: VideoInfo,
        page: ResolvedPage,
        pconfig,
        limit_warnings: List[str],
    ):
        """取流 + 下载。备用路径拿不到流时给一句人话，而不是裸异常。"""
        from ..base_parser import DownloadException, IgnoreException

        if page.duration > pconfig.VIDEO_DURATION_MAXIMUM:
            raise IgnoreException

        target_qn = _QUALITY_BY_NAME.get(
            str(pconfig.BILI_QUALITY).strip().upper().replace("＋", "+"),
            _DEFAULT_QN,
        )
        try:
            video, audio, _raw = await fetch_streams(
                lambda: self.new_client(),
                info.bvid,
                page.cid,
                headers=self.headers,
                cookie_header=self._bili_ck,
                codec_preference=pconfig.BILI_CODEC,
                max_qn=target_qn,
                key_provider=self._key_provider,
            )
        except Exception as error:
            msg = f"自建取流失败：{error}"
            if msg not in limit_warnings:
                limit_warnings.append(msg)
            raise IgnoreException(msg) from error

        if not video:
            msg = "自建取流没有拿到可下载的视频流"
            if msg not in limit_warnings:
                limit_warnings.append(msg)
            raise IgnoreException(msg)

        output_path = pconfig.cache_dir / f"{info.bvid}-{page.index + 1}.mp4"
        if output_path.exists():
            return output_path

        v_url, v_backups = video
        # 备用 CDN 逐个试：主地址挂了就换下一个
        url_pairs = [(v_url, audio[0] if audio else None)]
        for i, backup in enumerate(v_backups):
            url_pairs.append((backup, audio[1][i] if audio and i < len(audio[1]) else (audio[0] if audio else None)))

        last_error: Optional[Exception] = None
        for idx, (v_try, a_try) in enumerate(url_pairs):
            try:
                if idx > 0:
                    from astrbot.api import logger

                    logger.info(f"[bili-fallback] CDN 重试 ({idx + 1}/{len(url_pairs)})")
                if a_try is not None:
                    return await self.downloader.download_av_and_merge(
                        v_try, a_try, output_path=output_path, ext_headers=self.headers,
                    )
                return await self.downloader._download_file(
                    v_try, file_name=output_path.name, ext_headers=self.headers,
                    slots=self.downloader._media_slots,
                )
            except (IgnoreException, MediaProcessException):
                # 策略跳过 / 本机环境问题：换地址也不会变好，直接打断
                raise
            except Exception as error:
                last_error = error
                continue

        raise DownloadException("视频下载失败，已尝试所有CDN") from last_error
