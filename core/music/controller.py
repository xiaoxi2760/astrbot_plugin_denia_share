"""AstrBot integration for shared music providers, login and search cards."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..config import get_config
from ..constants import PLATFORM_DISPLAY_NAMES

PLATFORM_ALIASES = {'qq':'qqmusic', 'QQ':'qqmusic', 'qq音乐':'qqmusic', 'QQ音乐':'qqmusic',
                    'qqmusic':'qqmusic', '网易':'netease', '网易云':'netease', '网易云音乐':'netease',
                    'netease':'netease', 'ncm':'netease', '全部':'both', 'both':'both'}


def _command_set(value):
    return {part for part in re.split(r'[,，、\s]+', str(value or ''))
            if part and '/' not in part and len(part) <= 20}


def music_command(message):
    """Fast, side-effect-free command routing with configurable aliases."""
    options = get_config().current_values()
    if not options.get('MUSIC_COMMANDS_ENABLED', True):
        return None
    parts = str(message or '').strip().lstrip('/').split(maxsplit=1)
    if not parts:
        return None
    command = parts[0]
    if command in _command_set(options.get('MUSIC_SEARCH_COMMANDS', '点歌')):
        return 'search', parts[1].strip() if len(parts)>1 else ''
    play_commands = _command_set(options.get('MUSIC_PLAY_COMMANDS', '听'))
    if command in play_commands:
        return 'play', parts[1].strip() if len(parts)>1 else ''
    if len(parts) == 1:
        for alias in sorted(play_commands, key=len, reverse=True):
            suffix = command[len(alias):] if command.startswith(alias) else ''
            if suffix and suffix.isdecimal():
                return 'play', suffix
    return None


class MusicController:
    def __init__(self, plugin):
        from .session import SearchSessions
        from .login import LoginManager
        self.plugin = plugin
        cfg = get_config()
        self.sessions = SearchSessions(ttl=int(cfg.current_values().get('MUSIC_SESSION_TTL_SECONDS',300)))
        self.login = LoginManager(Path(plugin._data_dir)/'music', self._cookie, self._save_cookie, self._client_options)

    @staticmethod
    def _client_options(platform):
        cfg = get_config()
        return {'proxy':cfg.proxy_for(platform), 'verify_ssl':cfg.HTTP_VERIFY_SSL}

    @staticmethod
    def _cookie(platform):
        cfg = get_config()
        return cfg.NETEASE_CK if platform == 'netease' else cfg.QQMUSIC_CK

    async def _save_cookie(self, platform, value):
        from .models import MusicError
        cfg = get_config()
        key = 'NETEASE_CK' if platform == 'netease' else 'QQMUSIC_CK'
        changed, errors = cfg.apply_updates({key:value})
        if errors or not changed and not cfg.save():
            raise MusicError('登录凭据未能持久保存，请检查配置存储后重试')
        await self.plugin.apply_runtime_config()

    async def close(self):
        self.sessions.clear()
        await self.login.close()

    def reconfigure(self):
        ttl = int(get_config().current_values().get('MUSIC_SESSION_TTL_SECONDS', 300))
        if self.sessions.ttl != ttl:
            from .session import SearchSessions
            self.sessions = SearchSessions(ttl=ttl)

    async def _refresh(self, platform):
        from .models import MusicError
        try:
            await self.login.ensure_fresh(platform)
        except MusicError:
            # A stale login must not prevent public search or playable free songs.
            pass

    async def _search_covers(self, songs):
        """Fetch bounded artwork through each platform's existing image downloader."""
        covers = {}
        semaphore = asyncio.Semaphore(3)

        async def one(song):
            async with semaphore:
                parser = self.plugin.parsers.get(song.platform)
                if parser is None:
                    return
                try:
                    url = song.cover
                    if not url:
                        detail = await asyncio.wait_for(parser._provider().detail(song.id), timeout=4)
                        url = detail.cover
                    if not isinstance(url, str) or len(url) > 2048:
                        return
                    parsed = urlsplit(url)
                    host = (parsed.hostname or '').lower()
                    allowed = ('music.126.net', 'music.163.com') if song.platform == 'netease' else ('y.qq.com', 'qpic.cn', 'gtimg.cn')
                    if (parsed.scheme not in {'https', 'http'} or parsed.username or parsed.password
                            or parsed.port not in (None, 80, 443)
                            or not any(host == domain or host.endswith('.' + domain) for domain in allowed)):
                        return
                    path = await asyncio.wait_for(parser.downloader.download_img(url, ext_headers=parser.headers), timeout=5)
                    covers[f'{song.platform}:{song.id}'] = Path(path)
                except Exception:
                    # An absent or slow cover must not discard otherwise usable results.
                    return

        try:
            await asyncio.wait_for(asyncio.gather(*(one(song) for song in songs[:10])), timeout=8)
        except TimeoutError:
            pass
        return covers

    async def handle(self, event, action, argument):
        from .models import MusicError
        from .search_card import render_search_card
        plugin = self.plugin
        if plugin._access_denied(event):
            return
        if not argument:
            yield event.plain_result('用法：点歌 [QQ/网易云] 歌名；听1 播放第 1 首')
            return
        options = get_config().current_values()
        scope = plugin._event_scope(event)
        sender = str(event.get_sender_id())
        try:
            if action == 'play' and argument.isdecimal():
                # 编号播放只发音频：先看富媒体一路是否打开。这里**不能**退回
                # 普通解析卡片/文本 —— 用户点的是一个编号，不是要一份链接解析。
                if not get_config().output_modes()['media']:
                    yield event.plain_result('编号播放只发送歌曲音频，请先在输出设置里开启「富媒体」后重试。')
                    return
                songs = self.sessions.get(scope,sender)
                index = int(argument) if len(argument)<5 else 0
                if not songs:
                    yield event.plain_result('选歌列表已过期或尚未搜歌，请先发送：点歌 歌名')
                    return
                if not 1<=index<=len(songs):
                    yield event.plain_result(f'请选择 1—{len(songs)} 之间的编号')
                    return
                song = songs[index-1]
            else:
                platform = options.get('MUSIC_DEFAULT_PLATFORM','both')
                query = argument
                first, _, rest = argument.partition(' ')
                if first in PLATFORM_ALIASES:
                    platform, query = PLATFORM_ALIASES[first],rest.strip()
                if not query or len(query)>100:
                    yield event.plain_result('请输入 1—100 字的歌名或歌手名')
                    return
                keys = ['netease','qqmusic'] if platform=='both' else [platform]
                keys = [key for key in keys if key in plugin.parsers]
                if not keys:
                    yield event.plain_result('所选音乐平台已禁用，请先在配置中启用')
                    return
                limit = max(1,min(10,int(options.get('MUSIC_SEARCH_LIMIT',5))))
                async def search(key):
                    await self._refresh(key)
                    return await plugin.parsers[key].search_songs(query, limit)
                responses = await asyncio.gather(*(search(key) for key in keys),return_exceptions=True)
                groups = [[song for song in value if str(getattr(song, 'title', '')).strip()]
                          if isinstance(value,list) else [] for value in responses]
                songs = [group[index] for index in range(limit) for group in groups if index<len(group)][:limit]
                if not songs:
                    message = '暂时无法搜索，请检查网络或登录状态后重试' if any(isinstance(x,Exception) for x in responses) else '没有找到相关歌曲，请换个关键词'
                    yield event.plain_result(message)
                    return
                # 「搜歌」与「点歌 关键词」都只回一张搜索结果卡片并记下这轮列表：
                # 关键词点歌**不是**「直接播放第一首」，只是确定要搜什么。
                appearance = get_config().renderer_options()
                covers = await self._search_covers(songs)
                card = await render_search_card(songs,query,plugin.cache_dir,
                    dark=get_config().RENDER_THEME=='dark', accent_color=appearance.get('accent_color'),
                    limit=limit, font_path=appearance.get('font_path'),
                    width=appearance.get('width'), watermark=appearance.get('watermark'),
                    gradient_top=appearance.get('gradient_top'), gradient_bottom=appearance.get('gradient_bottom'),
                    covers=covers,
                    play_command=next((part for part in re.split(r'[,，、\s]+',str(options.get('MUSIC_PLAY_COMMANDS') or '听')) if part), '听'))
                # Explicit search is an image-card command, independent of automatic-link output modes.
                if not await plugin._send_image(event,card):
                    yield event.image_result(str(card))
                self.sessions.put(scope,sender,songs)
                failed=[PLATFORM_DISPLAY_NAMES.get(key,key) for key,value in zip(keys,responses) if isinstance(value,Exception)]
                if failed:
                    yield event.plain_result('、'.join(failed)+' 暂时不可用，已显示其他平台结果')
                return
            parser = plugin.parsers.get(song.platform)
            if parser is None:
                yield event.plain_result('这首歌所属的平台已禁用，请重新搜歌')
                return
            await self._refresh(song.platform)
            result = await parser.song_result(song)
            # Album artwork belongs to the search/share card, not the audio-only reply.
            for content in result.contents:
                if hasattr(content, 'cover'):
                    content.cover = None
            # 编号播放只走富媒体一路：复用现成的下载、限大、语音/文件与中转链路，
            # 但**不**经过 _deliver —— 那里会按输出模式补发普通解析卡片。
            await plugin._audit_for_modes(result, media_on=True)
            delivered = False
            send_failed = False

            def mark_delivered():
                nonlocal delivered
                delivered = True

            def mark_failed():
                nonlocal send_failed
                send_failed = True

            async for output in plugin._try_send_media(
                event, result, on_delivered=mark_delivered, on_failed=mark_failed
            ):
                delivered = True
                yield output
            if delivered:
                await plugin._record_history(result, None, via='command')
            elif send_failed:
                yield event.plain_result('这首歌暂时发不出来：语音和音频文件发送均失败，详情见 AstrBot 日志')
            else:
                # 下载失败/无权限/体积或时长超限都不能沉默：给一句带原因的小提示。
                yield event.plain_result(f'这首歌暂时发不出来：{self._delivery_reason(result)}')
        except (MusicError, ValueError, OSError):
            yield event.plain_result('音乐操作未完成，请检查网络、登录状态或本地字体后重试')

    @staticmethod
    def _delivery_reason(result):
        """从已结算的缺料提示里挑一句最短原因，供编号播放失败时如实告知。"""
        for warning in result.extra.get('limit_warnings') or []:
            text = str(warning).strip()
            for prefix in ('⚠️','⚠'):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
            text = text.removesuffix('，未包含在本次内容中').strip()
            if text:
                return text
        return '音频没有下载成功，请稍后重试'

