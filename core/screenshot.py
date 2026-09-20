# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""网页截图服务

两个后端，由配置 `SCREENSHOT_BACKEND` 选择：

- `thum`（默认）：image.thum.io，免 key、开箱即用。
  实测连打 4/4 成功，输出 900×900 的视窗截图。
  缺点：无法等待 JS、无法指定视窗尺寸、强反爬站点只能截到空白。

- `cloudflare`：Cloudflare Browser Rendering，需要 Account ID + API Token。
  功能强得多（视窗尺寸 / deviceScaleFactor / fullPage / waitUntil /
  指定选择器 / 自定义 UA / Cookie），适合要截长图或 JS 重页面的场景。
  实现参考 rika_share 的 cloudflare_screenshot.py，本文件为其精简版。

两者都直接把图片落到 cache_dir，返回 Path 供发送。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from astrbot.api import logger

THUM_BASE: Final = "https://image.thum.io/get"
CF_API_BASE: Final = "https://api.cloudflare.com/client/v4"

_PNG_MAGIC = b"\x89PNG"
_JPEG_MAGIC = b"\xff\xd8\xff"

# CF 的 waitUntil 合法取值
_WAIT_UNTIL_VALUES = ("load", "domcontentloaded", "networkidle0", "networkidle2")

# snake_case → Cloudflare API 的 camelCase
_CF_KEY_MAP: Final[dict[str, str]] = {
    "goto_options": "gotoOptions",
    "screenshot_options": "screenshotOptions",
    "wait_until": "waitUntil",
    "full_page": "fullPage",
    "omit_background": "omitBackground",
    "device_scale_factor": "deviceScaleFactor",
}


def _to_cf_keys(value: Any) -> Any:
    """把 body 里的 snake_case key 转成 Cloudflare 要求的 camelCase。"""
    if isinstance(value, dict):
        return {_CF_KEY_MAP.get(k, k): _to_cf_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_cf_keys(v) for v in value]
    return value


class ScreenshotService:
    """按配置选择后端，把网页截成图片保存到本地。"""

    def __init__(
        self,
        cache_dir: Path,
        backend: str = "thum",
        *,
        cf_account_id: str = "",
        cf_api_token: str = "",
        proxy: str = "",
        timeout: float = 60.0,
        verify_ssl: bool = True,
    ):
        self.cache_dir = cache_dir / "screenshot"
        self.backend = (backend or "thum").strip().lower()
        if self.backend not in {"thum", "cloudflare"}:
            self.backend = "thum"
        self.cf_account_id = (cf_account_id or "").strip()
        self.cf_api_token = (cf_api_token or "").strip()
        self.proxy = (proxy or "").strip() or None
        self.timeout = timeout
        # Cloudflare 后端会带 Authorization: Bearer <token>，默认必须校验证书
        self.verify_ssl = bool(verify_ssl)
        self.last_error: str | None = None

    @property
    def is_configured(self) -> bool:
        if self.backend == "thum":
            return True
        return bool(self.cf_account_id) and bool(self.cf_api_token)

    async def capture(self, url: str, *, full_page: bool = False) -> Path | None:
        """截图并返回本地文件路径，失败返回 None（原因见 self.last_error）。"""
        self.last_error = None
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            self.last_error = "不是合法的 http(s) 链接"
            return None
        if not self.is_configured:
            self.last_error = (
                "Cloudflare 后端未配置 Account ID / API Token"
                if self.backend == "cloudflare"
                else "截图服务未配置"
            )
            logger.warning(f"[screenshot] {self.last_error}")
            return None

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.backend == "cloudflare":
                return await self._capture_cloudflare(url, full_page=full_page)
            return await self._capture_thum(url)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            logger.warning(f"[screenshot] 截图失败 {url}: {self.last_error}")
            return None

    # ────────────── thum.io ────────────── #

    async def _capture_thum(self, url: str) -> Path | None:
        # width 固定 900：thum.io 免费版只认这一档左右，再大不稳定
        target = f"{THUM_BASE}/width/900/noanimate/{url}"
        path = self.cache_dir / f"thum_{uuid.uuid4().hex[:12]}.png"

        async with httpx.AsyncClient(
            proxy=self.proxy, timeout=self.timeout, follow_redirects=True,
            verify=self.verify_ssl,
        ) as client:
            resp = await client.get(target)

        if resp.status_code >= 400:
            self.last_error = f"thum.io 返回 HTTP {resp.status_code}"
            return None
        data = resp.content
        if not data.startswith(_PNG_MAGIC):
            self.last_error = f"thum.io 返回非图片内容（{len(data)} 字节）"
            return None
        if len(data) < 5000:
            # 实测：强反爬站点（知乎等）会返回约 16KB 的空白图
            self.last_error = "页面疑似被反爬拦截，截图为空白"
            return None

        path.write_bytes(data)
        logger.info(f"[screenshot] thum.io 成功: {url} -> {path.name} ({len(data)}B)")
        return path

    # ────────────── Cloudflare Browser Rendering ────────────── #

    async def _capture_cloudflare(self, url: str, *, full_page: bool) -> Path | None:
        wait_until = "networkidle0"
        if wait_until not in _WAIT_UNTIL_VALUES:
            wait_until = "networkidle0"

        body = _to_cf_keys({
            "url": url,
            "viewport": {"width": 1280, "height": 900, "device_scale_factor": 1},
            "goto_options": {"wait_until": wait_until, "timeout": 45000},
            "screenshot_options": {"type": "png", "full_page": full_page},
        })
        endpoint = f"{CF_API_BASE}/accounts/{self.cf_account_id}/browser-rendering/screenshot"
        headers = {
            "Authorization": f"Bearer {self.cf_api_token}",
            "Content-Type": "application/json",
        }
        path = self.cache_dir / f"cf_{uuid.uuid4().hex[:12]}.png"

        async with httpx.AsyncClient(
            proxy=self.proxy, timeout=self.timeout, follow_redirects=True,
            verify=self.verify_ssl,
        ) as client:
            resp = await client.post(endpoint, headers=headers, json=body)

        if resp.status_code >= 400 or "application/json" in resp.headers.get(
            "content-type", ""
        ):
            self.last_error = self._format_cf_error(resp.status_code, resp.text)
            return None

        data = resp.content
        if not (data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)):
            self.last_error = "Cloudflare 返回的不是图片数据"
            return None

        path.write_bytes(data)
        logger.info(f"[screenshot] Cloudflare 成功: {url} -> {path.name} ({len(data)}B)")
        return path

    def _format_cf_error(self, status: int, raw: str) -> str:
        raw = raw[:400]
        try:
            import json
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        message = raw
        if isinstance(data, dict) and data.get("errors"):
            message = str(data["errors"])
        # 先脱敏再截断：反过来的话 token 会被截断成半截，replace 匹配不上而残留
        token = self.cf_api_token or ""
        if token:
            message = message.replace(token, "***REDACTED***")
        message = message[:300]
        return f"Cloudflare API 错误 (HTTP {status}): {message}"


def is_probably_screenshotable(url: str) -> bool:
    """过滤掉不该拿去截图的地址（本地地址、纯文件等）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False
    return "." in host
