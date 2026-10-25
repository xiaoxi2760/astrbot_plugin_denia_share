"""Music share adapters; providers, commands and login share core.music."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from ..base_parser import BaseParser, handle
from ..config import get_config
from ..constants import PlatformEnum
from ..data import AudioContent, platform_of
from ..exception import IgnoreException, ParseException
from ..media_utils import fmt_duration
from ..task import PathTask
from ..music.models import MusicError

_URL_END = r"[^\s\"'<>，。！？、）】]+"
_NETEASE_HOSTS = frozenset({'music.163.com', 'y.music.163.com'})
_QQ_HOSTS = frozenset({'y.qq.com', 'i.y.qq.com', 'i2.y.qq.com'})


def song_identity(platform: str, url: str) -> str | None:
    """Recognize only a single-song page; albums/playlists never become a song."""
    try:
        if len(url) > 2048:
            return None
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 80, 443):
            return None
        host = (parsed.hostname or '').lower()
        if platform == 'netease':
            if host not in _NETEASE_HOSTS:
                return None
            target = urlsplit(parsed.fragment) if parsed.fragment.startswith('/') else parsed
            if target.path.rstrip('/') not in {'/song', '/m/song'}:
                match = re.fullmatch(r'/(?:m/)?song/(\d+)', target.path)
                return match[1] if match and int(match[1]) > 0 else None
            values = parse_qs(target.query).get('id', [])
            return values[0] if len(values) == 1 and values[0].isdigit() and int(values[0]) > 0 else None
        if host not in _QQ_HOSTS:
            return None
        match = re.fullmatch(r'/n/(?:ryqq(?:_v2)?/songDetail|yqq/song)/([A-Za-z0-9]+?)(?:\.html)?/?', parsed.path)
        if match:
            value = match[1]
        elif parsed.path in {'/v8/playsong.html', '/n3/other/pages/playsong/index.html'}:
            query = parse_qs(parsed.query)
            mids, ids = query.get('songmid', []), query.get('songid', [])
            value = mids[0] if len(mids) == 1 and mids[0] else ids[0] if len(ids) == 1 else ''
        else:
            return None
        return value if value.isdigit() and int(value) > 0 or re.fullmatch(r'[A-Za-z0-9]{14}', value) else None
    except (ValueError, TypeError, OverflowError):
        return None


class _MusicMethods:
    """Provider calls get current credentials; audio resolution stays lazy."""

    def _provider(self):
        from ..music.netease import NeteaseProvider
        from ..music.qqmusic import QQMusicProvider
        key = str(self.platform.name)
        cfg = get_config()
        cls = NeteaseProvider if key == 'netease' else QQMusicProvider
        cookie = cfg.NETEASE_CK if key == 'netease' else cfg.QQMUSIC_CK
        return cls(cookie=cookie, proxy=cfg.proxy_for(key), verify_ssl=cfg.HTTP_VERIFY_SSL)

    @classmethod
    def dedup_identity(cls, searched):
        identity = song_identity(str(cls.platform.name), searched.group('url'))
        return f'{cls.platform.name}:song:{identity}' if identity else None

    async def _expand(self, url):
        platform = str(self.platform.name)
        hosts = _NETEASE_HOSTS | {'163cn.tv'} if platform == 'netease' else _QQ_HOSTS | {'c.y.qq.com','c6.y.qq.com'}
        current = url
        async with self.new_client(follow_redirects=False) as client:
            for _ in range(5):
                parsed = urlsplit(current)
                if parsed.hostname not in hosts or parsed.scheme not in {'http','https'} or parsed.username or parsed.password or parsed.port not in (None,80,443):
                    raise ParseException('音乐分享链接跳转到了不支持的地址')
                identity = song_identity(platform, current)
                if identity:
                    return current
                async with client.stream('GET', current) as response:
                    if response.status_code not in {301,302,303,307,308}:
                        break
                    location = response.headers.get('location')
                    if not location:
                        break
                    current = urljoin(current, location)
        raise ParseException('未找到单曲链接，暂不展开歌单或专辑')

    async def dedup_expand(self, keyword, searched):
        url = searched.group('url')
        identity = song_identity(str(self.platform.name), url)
        if str(self.platform.name) == 'qqmusic' and identity and identity.isdigit():
            # QQ share cards may use numeric ids while web links use songmid.
            try:
                song = await self._provider().detail(identity)
            except MusicError as exc:
                raise ParseException('QQ音乐歌曲信息暂不可用，请稍后重试') from exc
            return self.search_url(song.url)
        if identity:
            return None
        return self.search_url(await self._expand(url))

    async def search_songs(self, keyword, limit=5):
        return await self._provider().search(keyword, limit=limit)

    async def song_result(self, song):
        cover = song.cover
        warnings = []
        result = self.result(title=song.title, url=song.url, author=self.create_author(song.artist),
                             contents=[], extra={'content_type':'音乐', 'duration':fmt_duration(song.duration),
                                                'stats_line':song.album, 'limit_warnings':warnings})
        async def download():
            from ..music.models import MusicError
            try:
                before_play = getattr(self, 'before_play', None)
                if before_play is not None:
                    await before_play(str(self.platform.name))
                source = await self._provider().play(song)
            except MusicError:
                message = '暂时无法取得音频，请检查登录状态或稍后重试'
                if message not in warnings:
                    warnings.append(message)
                raise IgnoreException(message) from None
            message = source.message or ('当前仅可获取试听片段' if source.preview else '')
            if message and message not in warnings:
                warnings.append(message)
            if not source.url:
                reason = message or '当前账号暂无这首歌的播放权限，请检查登录状态'
                if reason not in warnings:
                    warnings.append(reason)
                raise IgnoreException(reason)
            duration = source.duration if source.preview else source.duration or song.duration
            if duration > get_config().VIDEO_DURATION_MAXIMUM:
                reason = f'音频时长({fmt_duration(duration)})超过限制，跳过下载'
                if reason not in warnings:
                    warnings.append(reason)
                raise IgnoreException(reason)
            result.contents[0].duration = duration
            return await self.downloader.download_audio(source.url, ext_headers=self.headers)
        # Permission-limited previews may be shorter than the full track.
        # Apply the duration limit only after resolving the actual playable audio.
        audio = AudioContent(PathTask.lazy(download), duration=song.duration)
        result.contents.append(audio)
        if isinstance(cover, str) and cover:
            audio.cover = self.create_image(cover).path_task
        else:
            async def missing_cover():
                from ..music.models import MusicError
                try:
                    detail = await asyncio.wait_for(self._provider().detail(song.id), timeout=6)
                    if isinstance(detail.cover, str) and detail.cover:
                        return await self.downloader.download_img(detail.cover, ext_headers=self.headers)
                except (MusicError, TimeoutError):
                    pass
                raise IgnoreException('暂未取得专辑封面')
            audio.cover = PathTask.lazy(missing_cover)
        return result

    async def _share(self, searched):
        from ..music.models import MusicError
        url = searched.group('url')
        identity = song_identity(str(self.platform.name), url)
        if not identity:
            url = await self._expand(url)
            identity = song_identity(str(self.platform.name), url)
        try:
            song = await self._provider().detail(identity)
        except MusicError:
            raise ParseException('未能获取歌曲信息，请稍后重试') from None
        return await self.song_result(song)


class NeteaseMusicParser(_MusicMethods, BaseParser):
    platform = platform_of(PlatformEnum.NETEASE)

    @handle('music.163.com', r'(?<![\w./@-])(?P<url>https?://(?:y\.)?music\.163\.com/' + _URL_END + ')')
    @handle('163cn.tv', r'(?<![\w./@-])(?P<url>https?://163cn\.tv/' + _URL_END + ')')
    async def _parse_song(self, searched):
        return await self._share(searched)


class QQMusicParser(_MusicMethods, BaseParser):
    platform = platform_of(PlatformEnum.QQMUSIC)

    @handle('y.qq.com', r'(?<![\w./@-])(?P<url>https?://(?:(?:i2?|c6?)\.)?y\.qq\.com/' + _URL_END + ')')
    async def _parse_song(self, searched):
        return await self._share(searched)
