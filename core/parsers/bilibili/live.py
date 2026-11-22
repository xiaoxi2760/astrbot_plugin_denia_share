"""B站直播间公开卡片资料，主解析器与备用解析器共用。"""

import asyncio

import httpx
from astrbot.api import logger
from bs4 import BeautifulSoup

from ...exception import ParseException

LIVE_HOST = "https://api.live.bilibili.com"
LIVE_PATTERN = r"live\.bilibili\.com/(?:blanc/)?(?P<room_id>\d+)(?![a-zA-Z0-9_])"


async def _get_live_data(client, endpoint, params):
    response = await client.get(endpoint, params=params)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if payload.get("code") != 0 or not isinstance(data, dict):
        raise ParseException(f"B站直播接口返回错误（{payload.get('code')}）")
    return data


async def parse_live_room(parser, room_id: int):
    url = f"https://live.bilibili.com/{room_id}"
    async with parser.new_client(headers={**parser.headers, "Referer": url}) as client:
        room, anchor = await asyncio.gather(
            _get_live_data(client, f"{LIVE_HOST}/room/v1/Room/get_info", {"room_id": room_id}),
            _get_live_data(client, f"{LIVE_HOST}/live_user/v1/UserInfo/get_anchor_in_room", {"roomid": room_id}),
            return_exceptions=True,
        )
    if isinstance(room, (httpx.HTTPError, ValueError, ParseException)):
        raise ParseException("无法获取 B站直播间资料") from room
    if isinstance(room, BaseException):
        raise room
    if not room.get("title"):
        raise ParseException("B站直播间没有返回标题")

    author = None
    if isinstance(anchor, dict):
        info = anchor.get("info") or {}
        if info.get("uname"):
            author = parser.create_author(info["uname"], info.get("face") or None)
    elif isinstance(anchor, asyncio.CancelledError):
        raise anchor
    elif isinstance(anchor, Exception):
        logger.debug("B站主播资料暂不可用，保留直播间预览")

    canonical_id = room.get("room_id") or room_id
    cover_urls = dict.fromkeys(room.get(key) for key in ("user_cover", "keyframe") if room.get(key))
    images = [parser.create_image(cover) for cover in cover_urls]
    description = BeautifulSoup(room.get("description") or "", "html.parser").get_text(" ", strip=True)
    categories = " / ".join(filter(None, (room.get("parent_area_name"), room.get("area_name"))))
    status = {0: "未开播", 1: "直播中", 2: "轮播"}.get(room.get("live_status"))
    detail = "\n".join(filter(None, (status, categories, description)))
    return parser.result(
        url=f"https://live.bilibili.com/{canonical_id}",
        title=f"直播 - {room['title']}",
        text=detail,
        contents=images,
        author=author,
        extra={"content_type": "直播", "stats_line": status or ""},
    )
