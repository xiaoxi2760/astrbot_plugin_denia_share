# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""微博解析器"""
import asyncio
import re
from time import time
from uuid import uuid4
from typing import ClassVar
from bs4 import Tag, BeautifulSoup
from astrbot.api import logger
from httpx import AsyncClient
from ..base_parser import (
    MAX_IMAGES_PER_RESULT,
    BaseParser,
    PlatformEnum,
    ParseException,
    handle,
)
from ..data import Platform, ImageContent, platform_of


# ============================ 微博访客身份 ============================
#
# 微博对**完全没有 Cookie** 的请求会返回 403/418（风控），带一份「访客身份」
# 就能正常拿到数据。本仓原先在 parse_weibo_id 里显式传 cookies=Cookies()
# （主动不带），匿名时很容易被拦 —— 前身插件 yaya 的做法是先去 genvisitor2
# 取一份访客身份再请求。
#
# 访客身份由 visitor.passport.weibo.cn 下发，取一次就够（但会过期），所以：
# 拿到就缓存 → 请求被风控时清掉 → 下一次自动重取。

_visitor_cookies: dict[str, str] | None = None
_visitor_lock: asyncio.Lock | None = None


def _get_visitor_lock() -> asyncio.Lock:
    """懒建锁。

    **不在模块级直接 ``asyncio.Lock()``**：那会在「还没有事件循环」时构造。
    懒建就没有这个问题。
    """
    global _visitor_lock
    if _visitor_lock is None:
        _visitor_lock = asyncio.Lock()
    return _visitor_lock


def clear_visitor_cookies() -> None:
    """丢弃缓存的访客身份（下一次请求会重新获取）。"""
    global _visitor_cookies
    _visitor_cookies = None


async def get_visitor_cookies(parser) -> dict[str, str] | None:
    """获取（并缓存）微博访客 Cookie；取不到返回 ``None``。

    **取不到不抛异常**：退回「不带 Cookie 请求」正是本仓原来的行为，比让整条
    解析失败好。并发时只发一次请求（双检锁）。
    """
    global _visitor_cookies
    if _visitor_cookies:
        return _visitor_cookies
    async with _get_visitor_lock():
        if _visitor_cookies:  # 等锁期间别人可能已经取到了
            return _visitor_cookies
        try:
            async with AsyncClient(**parser.client_kwargs(
                headers={
                    **parser.headers,
                    "referer": "https://visitor.passport.weibo.cn/",
                },
            )) as client:
                response = await client.post(
                    "https://visitor.passport.weibo.cn/visitor/genvisitor2",
                    content="cb=visitor_gray_callback",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
        except Exception as error:
            logger.warning(f"微博访客身份获取失败，改用匿名请求：{error}")
            return None
        if response.status_code != 200:
            logger.warning(
                f"微博访客身份获取失败（HTTP {response.status_code}），改用匿名请求"
            )
            return None
        cookies = {key: value for key, value in response.cookies.items()}
        if not cookies:
            logger.warning("微博访客身份响应里没有 Cookie，改用匿名请求")
            return None
        _visitor_cookies = cookies
        logger.debug(f"微博访客身份已获取（{len(cookies)} 个 Cookie）")
        return _visitor_cookies


class WeiBoParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.WEIBO)

    def __init__(self, downloader):
        super().__init__(downloader)
        extra_headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
            "referer": "https://weibo.com/",
        }
        self.headers.update(extra_headers)

    @handle("weibo.com/tv", r"weibo\.com/tv/show/\d{4}:\d+\?mid=(?P<mid>\d+)")
    async def _parse_weibo_tv(self, searched: re.Match[str]):
        mid = str(searched.group("mid"))
        weibo_id = self._mid2id(mid)
        return await self.parse_weibo_id(weibo_id)

    @handle("video.weibo", r"video\.weibo\.com/show\?fid=(?P<fid>\d+:\d+)")
    async def _parse_video_weibo(self, searched: re.Match[str]):
        fid = str(searched.group("fid"))
        return await self.parse_fid(fid)

    @handle("m.weibo.cn", r"weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9a-zA-Z]+)")
    @handle("weibo.com", r"weibo\.com/\d+/(?P<wid>[0-9a-zA-Z]+)")
    async def _parse_m_weibo_cn(self, searched: re.Match[str]):
        wid = str(searched.group("wid"))
        return await self.parse_weibo_id(wid)

    @handle("mapp.api.weibo", r"mapp\.api\.weibo\.cn/fx/[0-9A-Za-z]+\.html")
    async def _parse_mapp_api_weibo(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("weibo.com/ttarticle", r"id=(?P<id>\d+)")
    @handle("weibo.com/article", r"/id/(?P<id>\d+)")
    async def _parse_article(self, searched: re.Match[str]):
        _id = searched.group("id")
        return await self.parse_article(_id)

    async def parse_article(self, _id: str):
        from ..models.weibo.article import decoder as article_decoder

        url = "https://card.weibo.com/article/m/aj/detail"
        params = {"_rid": str(uuid4()), "id": _id, "_t": int(time() * 1000)}
        async with AsyncClient(**self.client_kwargs(
            headers=self.headers, cookies=await get_visitor_cookies(self),
        )) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        detail = article_decoder.decode(response.content)
        if detail.msg != "success":
            raise ParseException("请求失败")
        data = detail.data
        soup = BeautifulSoup(data.content, "html.parser")
        graphics: list[str | ImageContent] = []
        image_count = 0
        for element in soup.find_all(["p", "img"]):
            if not isinstance(element, Tag):
                continue
            if element.name == "p":
                text = element.get_text(strip=True).replace("\u200b", "")
                if text:
                    graphics.append(text)
            elif element.name == "img":
                # 长文里的图片数量由远端 HTML 决定，这里必须自己封顶
                if image_count >= MAX_IMAGES_PER_RESULT:
                    continue
                src = element.get("src")
                if isinstance(src, str):
                    graphics.append(self.create_image(src))
                    image_count += 1
        author = self.create_author(data.userinfo.screen_name, data.userinfo.profile_image_url)
        return self.result(url=data.url, title=data.title, author=author, timestamp=data.create_at_unix, graphics=graphics)

    async def parse_fid(self, fid: str):
        req_url = f"https://h5.video.weibo.com/api/component?page=/show/{fid}"
        # self.headers 里是小写 referer，这里必须同键名小写覆盖，否则会发出两个 Referer
        headers = {**self.headers, "referer": f"https://h5.video.weibo.com/show/{fid}", "Content-Type": "application/x-www-form-urlencoded"}
        post_content = 'data={"Component_Play_Playinfo":{"oid":"' + fid + '"}}'
        async with AsyncClient(**self.client_kwargs(
            headers=headers, cookies=await get_visitor_cookies(self),
        )) as client:
            response = await client.post(req_url, content=post_content)
            response.raise_for_status()

        play_info = self._decode_play_info(response.content, fid)
        author = self.create_author(play_info.name, play_info.avatar, play_info.description)
        result = self.result(
            title=play_info.title,
            # 必须用 clean_text：play_info.text 里的 <br/> 会原样进卡片
            text=play_info.clean_text,
            author=author,
            timestamp=play_info.real_date,
        )
        self._add_limit_warning(result, play_info.duration)
        result.contents = [self.create_video(play_info.video_url, play_info.cover_url, duration=play_info.duration)]
        return result

    async def parse_weibo_id(self, weibo_id: str):
        # 通用头在前、专用头在后：字典字面量里**后写的键覆盖先写的**，
        # 所以下面的 accept/referer 会盖掉 self.headers 里的同名键。
        headers = {
            **self.headers,
            "accept": "application/json, text/plain, */*",
            "referer": f"https://m.weibo.cn/detail/{weibo_id}",
            "origin": "https://m.weibo.cn",
            "x-requested-with": "XMLHttpRequest",
            "mweibo-pwa": "1",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        ts = int(time() * 1000)
        url = f"https://m.weibo.cn/statuses/show?id={weibo_id}&_={ts}"
        async with AsyncClient(
            **self.client_kwargs(
                headers=headers,
                follow_redirects=False,
                # 访客身份：完全不带 Cookie 时微博会返回 403/418（见文件顶部说明）
                cookies=await get_visitor_cookies(self),
            )
        ) as client:
            response = await client.get(url)
            if response.status_code != 200:
                if response.status_code in (403, 418):
                    # 访客身份可能已过期：清掉缓存，下一次请求会自动重取一份再试
                    clear_visitor_cookies()
                    raise ParseException(f"被风控拦截({response.status_code}), 可尝试更换 UA/Referer 或稍后重试")
                raise ParseException(f"获取数据失败 {response.status_code}")
            ctype = response.headers.get("content-type", "")
            if "application/json" not in ctype:
                raise ParseException(f"获取数据失败 content-type is not application/json (got: {ctype})")

        weibo_data = self._decode_status(response.content, weibo_id)
        return self._collect_result(weibo_data)

    # ---------------- 解码 + 返回作品校验 ----------------
    #
    # 抽成「只吃 bytes」的方法是为了**可测**：原先是把 decode 与校验写在
    # parse_weibo_id / parse_fid 里，那两段要经过 httpx 客户端，自检没法在不打网络的
    # 前提下覆盖 —— 于是校验很容易被后来的人删掉而没人发现。这里只吃 bytes，
    # 自检可以拿真实 JSON 直接喂，把「解码 + 校验 + 调用点接线」一起测到。

    def _decode_status(self, content: bytes, requested_id: str):
        from ..models.weibo.common import decoder as weibo_decoder

        data = weibo_decoder.decode(content).data
        self._assert_status_matches_requested(data, requested_id)
        return data

    def _decode_play_info(self, content: bytes, requested_id: str):
        from ..models.weibo.show import decoder as show_decoder

        data = show_decoder.decode(content).data
        play_info = data.Component_Play_Playinfo
        self._assert_video_matches_requested(play_info, requested_id)
        return play_info

    @staticmethod
    def _assert_status_matches_requested(data, requested_id: str) -> None:
        """校验接口返回的作品就是被请求的那条。

        请求 id 是纯数字时（``m.weibo.cn/status/<mid>``、``_mid2id`` 转出来的）比
        ``id`` / ``idstr`` / ``mid``；否则（base62 的 bid）比 ``mblogid`` / ``bid``。

        **拿不到任何可比字段时放行**：宁可不校验，也不要因为接口少给一个字段就误杀。
        """
        requested_id = str(requested_id or "").strip()
        if not requested_id:
            return
        keys = ("id", "idstr", "mid") if requested_id.isdigit() else ("mblogid", "bid")
        candidates = {
            str(getattr(data, key)).strip()
            for key in keys
            if getattr(data, key, None) not in (None, "")
        }
        if candidates and requested_id not in candidates:
            raise ParseException(
                f"微博接口返回了其他作品的数据（请求 {requested_id}，"
                f"返回 {'/'.join(sorted(candidates))}）"
            )

    @staticmethod
    def _assert_video_matches_requested(play_info, requested_id: str) -> None:
        """同上，用于 video.weibo.com 的播放数据。

        请求时是把 ``fid`` 当 ``oid`` 发出去的（见 parse_fid 的 post_content），
        所以正常情况响应里的 ``oid`` 就等于请求的 fid。
        """
        requested_id = str(requested_id or "").strip()
        if not requested_id:
            return
        candidates = {
            str(getattr(play_info, key)).strip()
            for key in ("oid", "fid", "object_id")
            if getattr(play_info, key, None) not in (None, "")
        }
        if candidates and requested_id not in candidates:
            raise ParseException(
                f"微博视频接口返回了其他作品的数据（请求 {requested_id}，"
                f"返回 {'/'.join(sorted(candidates))}）"
            )

    def _collect_result(self, data):
        author = self.create_author(data.display_name, data.user.profile_image_url)
        result = self.result(title=data.title, text=data.text_content, author=author, timestamp=data.timestamp, url=data.url)
        if video_url := data.video_url:
            self._add_limit_warning(result, data.duration)
            result.video = self.create_video(video_url, data.cover_url, data.duration)
        if image_urls := data.image_urls:
            result.contents.extend(self.create_images(image_urls))
        if data.retweeted_status:
            result.repost = self._collect_result(data.retweeted_status)
        return result

    def _base62_encode(self, number: int) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if number == 0:
            return "0"
        result = ""
        while number > 0:
            result = alphabet[number % 62] + result
            number //= 62
        return result

    def _mid2id(self, mid: str) -> str:
        from math import ceil
        mid = str(mid)[::-1]
        size = ceil(len(mid) / 7)
        result = []
        for i in range(size):
            s = mid[i * 7:(i + 1) * 7][::-1]
            s = self._base62_encode(int(s))
            if i < size - 1 and len(s) < 4:
                s = "0" * (4 - len(s)) + s
            result.append(s)
        result.reverse()
        return "".join(result)
