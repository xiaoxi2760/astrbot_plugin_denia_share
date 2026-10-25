"""QQ 音乐适配器：搜索、单曲详情与取流。

QQ 音乐有两个容易混淆的 id：

* ``songmid``：歌曲在曲库里的稳定标识，搜索与详情返回的 ``mid``；
* ``file.media_mid``：真正用于拼音频文件名的媒体标识，多数歌曲与 songmid
  相同，但翻唱/特殊版本会不同，用错就会拿到 404 或别人的音频。

因此这里两者都保留：:attr:`Song.id` 存 songmid，:attr:`Song.media_mid` 存
``file.media_mid``，取流时用 ``media_mid`` 拼 filename。
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import urljoin

from .client import (
    BaseProvider,
    as_dict,
    as_list,
    cookie_value,
    int_of,
    safe_media_url,
    text_of,
)
from .models import MusicError, PlayURL, Song

MUSICU_URL: Final[str] = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DETAIL_URL: Final[str] = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
SEARCH_MODULE: Final[str] = "music.search.SearchCgiService"
SEARCH_METHOD: Final[str] = "DoSearchForQQMusicDesktop"
COVER_URL: Final[str] = "https://y.qq.com/music/photo_new/T002R300x300M000{}.jpg"
SONG_URL: Final[str] = "https://y.qq.com/n/ryqq/songDetail/{}"

HEADERS: Final[dict[str, str]] = {"Referer": "https://y.qq.com/portal/player.html"}
# 先试 320kbps mp3，匿名或免费曲目拿不到时退到 m4a
QUALITY_TIERS: Final[tuple[tuple[str, str], ...]] = (("M800", "mp3"), ("C400", "m4a"))
# 取流固定 guid；登录态缺失或曲目权限不足时服务端返回该 code
LOGIN_REQUIRED: Final[int] = 104003
GUID: Final[str] = "10000"

DEFAULT_TITLE: Final[str] = "未知歌曲"
DEFAULT_ARTIST: Final[str] = "未知歌手"


def _singers(node: Any) -> str:
    names = []
    for raw in as_list(node):
        name = text_of(as_dict(raw).get("name") or as_dict(raw).get("title"))
        if name:
            names.append(name)
    return "/".join(names)


def _qq_song(item: Any) -> Song | None:
    """把搜索/详情里的歌曲节点统一成 :class:`Song`。"""
    node = as_dict(item)
    mid = text_of(node.get("mid") or node.get("songmid"))
    if not mid or len(mid) > 32 or not mid.isalnum():
        return None
    album = as_dict(node.get("album"))
    media_mid = text_of(as_dict(node.get("file")).get("media_mid"))
    return Song(
        platform="qqmusic",
        id=mid,
        title=text_of(node.get("title") or node.get("name"), DEFAULT_TITLE),
        artist=_singers(node.get("singer")) or DEFAULT_ARTIST,
        album=text_of(album.get("name")),
        duration=float(int_of(node.get("interval"))) if int_of(node.get("interval")) > 0 else 0.0,
        cover=safe_media_url(COVER_URL.format(text_of(album.get("mid"))) if text_of(album.get("mid")) else ""),
        media_mid=media_mid or mid,
        url=SONG_URL.format(mid),
    )


class QQMusicProvider(BaseProvider):
    """QQ 音乐公开搜歌与单曲信息；取流按账号实际权限处理。"""

    platform = "qqmusic"

    # -- 内部 ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = dict(HEADERS)
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _uin(self) -> str:
        """取 Cookie 里的真实 QQ 号；没有登录态时用 ``0`` 走匿名请求。"""
        return cookie_value(self.cookie, "uin") or "0"

    def _song_or_error(self, song: Any) -> Song:
        if isinstance(song, Song):
            return song
        value = song if isinstance(song, dict) else {
            "platform": getattr(song, "platform", "qqmusic"),
            "id": getattr(song, "id", ""),
            "title": getattr(song, "title", ""),
            "duration": getattr(song, "duration", 0.0),
            "media_mid": getattr(song, "media_mid", ""),
        }
        mid = str(value.get("id") or "").strip()
        if not mid:
            raise MusicError("QQ 音乐歌曲 id 无效，请重新搜索后再试")
        duration = value.get("duration") or 0.0
        return Song(
            platform="qqmusic",
            id=mid,
            title=text_of(value.get("title"), DEFAULT_TITLE),
            artist=text_of(value.get("artist"), DEFAULT_ARTIST),
            album=text_of(value.get("album")),
            duration=float(duration) if isinstance(duration, (int, float)) else 0.0,
            cover=safe_media_url(value.get("cover")),
            media_mid=text_of(value.get("media_mid")) or mid,
            url=SONG_URL.format(mid),
        )

    async def _post_musicu(self, payload: dict[str, Any], what: str) -> dict[str, Any]:
        body = await self._request_json(
            "POST", MUSICU_URL, headers=self._headers(), json=payload, what=what
        )
        return as_dict(body)

    # -- 对外 ---------------------------------------------------------------
    async def search(self, keyword: str, limit: int = 5) -> list[Song]:
        query = str(keyword or "").strip()
        if not query:
            return []
        size = max(1, min(int(limit or 5), 10))
        payload = {
            "comm": {"ct": "19", "cv": "1859", "uin": self._uin()},
            "req": {
                "method": SEARCH_METHOD,
                "module": SEARCH_MODULE,
                "param": {
                    "grp": 1,
                    "num_per_page": size,
                    "page_num": 1,
                    "query": query,
                    "search_type": 0,
                },
            },
        }
        body = await self._post_musicu(payload, "QQ 音乐搜索")
        req = as_dict(body.get("req"))
        if int_of(req.get("code"), 0) not in (0, 200):
            raise MusicError("QQ 音乐搜索失败，请稍后重试或重新登录")
        node = as_dict(as_dict(as_dict(req.get("data")).get("body")).get("song"))
        songs: list[Song] = []
        for raw in as_list(node.get("list")):
            song = _qq_song(raw)
            if song is not None:
                songs.append(song)
            if len(songs) >= size:
                break
        return songs

    async def detail(self, identifier: str) -> Song:
        value = str(identifier or "").strip()
        if not value:
            raise MusicError("QQ 音乐歌曲 id 无效，请检查分享链接")
        params: dict[str, Any] = {
            "format": "json",
            "platform": "yqq",
            "inCharset": "utf8",
            "outCharset": "utf-8",
        }
        params["songid" if value.isdigit() else "songmid"] = value
        body = await self._request_json(
            "GET", DETAIL_URL, headers=self._headers(), params=params, what="QQ 音乐歌曲详情"
        )
        for raw in as_list(as_dict(body).get("data")):
            song = _qq_song(raw)
            if song is not None:
                return song
        raise MusicError("未能获取这首歌的信息，可能已下架或需要会员")

    async def play(self, song: Song) -> PlayURL:
        target = self._song_or_error(song)
        media_mid = target.media_mid or target.id
        uin = self._uin()
        headers = self._headers()
        last_code = 0
        for prefix, suffix in QUALITY_TIERS:
            payload = {
                "req_1": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "filename": [f"{prefix}{media_mid}.{suffix}"],
                        "guid": GUID,
                        "songmid": [target.id],
                        "songtype": [0],
                        "uin": uin,
                        "loginflag": 1,
                        "platform": "20",
                    },
                },
                "loginUin": uin,
                "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0},
            }
            body = await self._post_musicu(payload, "QQ 音乐取流")
            data = as_dict(as_dict(body.get("req_1")).get("data"))
            infos = [as_dict(item) for item in as_list(data.get("midurlinfo"))]
            purl = text_of(infos[0].get("purl")) if infos else ""
            hosts = [text_of(item) for item in as_list(data.get("sip")) if text_of(item)]
            if purl and hosts:
                url = safe_media_url(urljoin(hosts[0], purl))
                if url:
                    return PlayURL(url=url, duration=target.duration, preview=False)
            last_code = int_of(infos[0].get("result")) if infos else last_code
        if uin == "0":
            return PlayURL(message='暂未取得音频，可登录 QQ音乐账号后重试；部分歌曲需要会员')
        if last_code == LOGIN_REQUIRED:
            return PlayURL(message='当前账号未取得这首歌的播放权限，请检查登录状态或会员权限')
        return PlayURL(message='暂时无法取得这首歌的音频，可能受到会员或版权限制')

    async def check_login(self) -> dict[str, str]:
        if not self.cookie or not cookie_value(self.cookie, "uin"):
            return {"status": "invalid", "message": "未配置 QQ 音乐 Cookie"}
        # Playback endpoints also return code=0 for anonymous / stale accounts;
        # a successful request alone must never label a Cookie valid.
        payload = {
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": ["M800003OUlho2HcRHC.mp3"],
                    "guid": GUID,
                    "songmid": ["003OUlho2HcRHC"],
                    "songtype": [0],
                    "uin": self._uin(),
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "loginUin": self._uin(),
            "comm": {"uin": self._uin(), "format": "json", "ct": 24, "cv": 0},
        }
        body = await self._post_musicu(payload, "QQ 音乐账号状态")
        return {"status": "unknown", "message": "凭据已保存，暂时无法确认登录状态，可尝试点歌验证"}


__all__ = ["QQMusicProvider", "SONG_URL", "LOGIN_REQUIRED"]
