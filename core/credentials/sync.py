"""CookieCloud 同步编排；配置、响应和状态中不回传明文凭据。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_config
from .cookiecloud import CookieCloudError, credentials_from_cookies, fetch_cookies
from .service import PLATFORMS, CredentialError

COOKIE_PLATFORMS = {p.id: p.label for p in PLATFORMS if p.kind == 'cookie'}


def service_for(plugin):
    from .service import CredentialManager
    service = getattr(plugin, '_cookiecloud_sync', None)
    if service is None:
        manager = getattr(plugin, '_credentials_manager', None) or CredentialManager(plugin)
        plugin._credentials_manager = manager
        service = CookieCloudSync(manager)
        plugin._cookiecloud_sync = service
    return service


class CookieCloudSync:
    def __init__(self, manager):
        self.manager = manager
        self.lock = asyncio.Lock()
        self.task = None
        self.state_path = Path(get_config().config_dir) / 'cookiecloud-state.json'
        self.state = {'last_attempt': '', 'last_success': '', 'message': '', 'results': []}
        try:
            saved = json.loads(self.state_path.read_text(encoding='utf-8'))
            for key in ('last_attempt', 'last_success'):
                self.state[key] = str(saved.get(key) or '')[:40]
            sources = saved.get('sources', {})
            if isinstance(sources, dict):
                self.manager._sources.update({k: v for k, v in sources.items()
                    if k in COOKIE_PLATFORMS and isinstance(v, str) and len(v) == 64})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.manager._source_listener = self._save_state

    def settings(self):
        values = get_config().current_values
        return values() if callable(values) else values

    def snapshot(self):
        cfg = self.settings()
        selected = self.selected(cfg)
        return {**self.state, 'configured': all(cfg.get(k) for k in
            ('COOKIECLOUD_URL', 'COOKIECLOUD_UUID', 'COOKIECLOUD_PASSWORD')),
            'platforms': selected,
            'interval_minutes': int(cfg.get('COOKIECLOUD_INTERVAL_MINUTES') or 0),
            'busy': self.lock.locked()}

    @staticmethod
    def selected(cfg):
        raw = str(cfg.get('COOKIECLOUD_PLATFORMS') or '').replace('，', ',')
        aliases = {'B站': 'bilibili', '小红书': 'xiaohongshu', '网易云': 'netease',
                   '网易云音乐': 'netease', 'QQ音乐': 'qqmusic', '小黑盒': 'xiaoheihe'}
        return list(dict.fromkeys(aliases.get(p.strip(), p.strip().lower())
            for p in raw.split(',') if aliases.get(p.strip(), p.strip().lower()) in COOKIE_PLATFORMS))

    def _save_state(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({**self.state, 'sources': self.manager._sources}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.state_path)
        except OSError:
            self.state['message'] += '；同步记录未能保存'

    async def sync(self, *, overwrite=False):
        if self.lock.locked():
            raise CookieCloudError('正在同步，请稍后再试')
        async with self.lock:
            cfg = self.settings()
            selected = self.selected(cfg)
            if not selected:
                raise CookieCloudError('请先在 CookieCloud 配置中选择同步平台')
            self.state['last_attempt'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.state['results'] = []
            try:
                data = await fetch_cookies(str(cfg.get('COOKIECLOUD_URL') or ''),
                    str(cfg.get('COOKIECLOUD_UUID') or ''), str(cfg.get('COOKIECLOUD_PASSWORD') or ''))
                values = credentials_from_cookies(data, selected)
                # 拉取期间若用户换了服务/密码/平台，本轮旧数据不得回写。
                if any(cfg.get(k) != self.settings().get(k) for k in cfg if k.startswith('COOKIECLOUD_')):
                    raise CookieCloudError('同步配置已变更，请重新同步')
                for platform in selected:
                    item = {'platform': platform, 'label': COOKIE_PLATFORMS[platform]}
                    value = values.get(platform)
                    if not value:
                        item.update(status='skipped', message='没有找到可用 Cookie')
                    else:
                        try:
                            result = await self.manager.import_and_save(platform, value,
                                overwrite_existing=overwrite, source='cookiecloud')
                            if result.get('import_skipped'):
                                item.update(status='skipped', message='保留现有登录态')
                            else:
                                item.update(status='ok', message='已同步并应用')
                        except CredentialError:
                            item.update(status='error', message='保存或应用未完成，请检查配置后重试')
                    self.state['results'].append(item)
                imported = sum(i['status'] == 'ok' for i in self.state['results'])
                errors = any(i['status'] == 'error' for i in self.state['results'])
                self.state['message'] = f'已同步 {imported} 个平台' + ('；部分保存失败' if errors else '')
                if imported and not errors:
                    self.state['last_success'] = self.state['last_attempt']
            except CookieCloudError:
                self.state['message'] = '同步未完成，请检查地址、密钥与加密密码'
                raise
            finally:
                self._save_state()
            return self.snapshot()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        elapsed = 0
        while True:
            await asyncio.sleep(30)
            minutes = int(self.settings().get('COOKIECLOUD_INTERVAL_MINUTES') or 0)
            elapsed = elapsed + 30 if minutes else 0
            if minutes and elapsed >= max(5, minutes) * 60:
                elapsed = 0
                try:
                    await self.sync()
                except Exception:
                    # 后台同步失败只更新脱敏状态，绝不影响聊天解析。
                    self.state['message'] = '自动同步未完成，可手动重试'

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
