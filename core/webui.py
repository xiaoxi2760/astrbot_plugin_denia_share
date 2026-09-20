"""插件 WebUI 的后端接口层。

页面（``pages/denia/``）通过 AstrBot 的 bridge 调用这里注册的接口。
分工：

- 本模块只做「取参数 → 校验 → 调用插件已有能力 → 拼 JSON」，
  不重复实现解析/渲染/登录逻辑，全部复用 ``DeniaSharePlugin`` 上的方法。
- 路由统一带插件名前缀（``/astrbot_plugin_denia_share/xxx``），
  这是 AstrBot 的要求；页面侧通过 bridge 写的是去掉前缀的相对路径。
- 图片不靠 URL 暴露（受限 iframe 带不上鉴权头），而是缩成 base64 data URL
  随 JSON 返回；需要原图/原文件时走 ``bridge.download``。

安全约定：页面运行在受限 iframe 里，但后端仍按「不可信输入」处理——
配置只接受白名单键、缓存文件只允许访问 ``cache_dir`` 内且能被记录引用的文件、
链接长度与格式都做校验。
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from astrbot.api import logger

from .card_renderer import LAYOUT_NAMES, ShareCardRenderer
from .config import config_meta_payload, get_config, verify_schema_alignment
from .constants import PLATFORM_DISPLAY_NAMES, PLATFORM_ORDER
from .exception import IgnoreException, ParseException
from .media_utils import clear_cache_dir, cleanup_cache_dir, is_docker_environment
from .relay import resolve_callback_base
from .screenshot import is_probably_screenshotable


def _astrbot_callback_base() -> str:
    """读取 AstrBot 全局 callback_api_base（取不到就返回空串）。"""
    try:
        return resolve_callback_base("")
    except Exception:
        return ""

PLUGIN_NAME = "astrbot_plugin_denia_share"

# 缩略图/预览图的最大宽度与体积上限。
# 预览图要经 postMessage 传进 iframe，几 MB 的 base64 会明显卡顿，所以宁可不给。
PREVIEW_MAX_WIDTH = 640
PREVIEW_MAX_BYTES = 3 * 1024 * 1024
MAX_URL_LENGTH = 2048
MAX_QUERY_LIMIT = 100


class PreviewOverridesError(Exception):
    """预览覆盖参数不合法。

    刻意和「没有覆盖项」区分开：没有覆盖项时用常驻渲染器按当前配置出图，
    参数非法时必须报错——否则用户改了一个非法颜色，看到的却是按旧配置渲染的
    图，还以为自己改的值生效了。
    """

try:  # Pillow 是插件依赖，缺了只影响预览图，不影响其余接口
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


def _json_response(data: Any, status_code: int = 200):
    from astrbot.api.web import json_response

    return json_response(data, status_code=status_code)


def _error(message: str, status_code: int = 400):
    from astrbot.api.web import error_response

    return error_response(message, status_code=status_code)


def _dir_stats(path: Path) -> tuple[int, int]:
    """统计目录下的文件数与总字节数。"""
    files = 0
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return files, total


def _image_data_url(path: Path, max_width: int = PREVIEW_MAX_WIDTH) -> str | None:
    """把本地图片缩成 base64 data URL；不可用或过大时返回 None。"""
    if Image is None or not path.is_file():
        return None
    try:
        resample = getattr(Image, "Resampling", Image).LANCZOS
    except Exception:  # pragma: no cover - 老版本 Pillow
        resample = None

    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            if max_width > 0 and image.width > max_width:
                ratio = max_width / image.width
                size = (max_width, max(1, int(image.height * ratio)))
                image = image.resize(size, resample) if resample else image.resize(size)

            buffer = io.BytesIO()
            has_alpha = image.mode in {"RGBA", "LA", "P"} and _has_alpha(image)
            if has_alpha:
                image.save(buffer, "PNG", optimize=True)
                mime = "png"
            else:
                image.convert("RGB").save(buffer, "JPEG", quality=86, optimize=True)
                mime = "jpeg"
            data = buffer.getvalue()
    except Exception:
        logger.debug("生成预览图失败: %s", path, exc_info=True)
        return None

    if len(data) > PREVIEW_MAX_BYTES:
        logger.debug("预览图过大，已跳过: %s (%d 字节)", path, len(data))
        return None
    return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")


def _has_alpha(image: Any) -> bool:
    """判断图片是否真的用到了透明通道（纯不透明的 PNG 转 JPEG 能小很多）。"""
    try:
        if image.mode not in {"RGBA", "LA"}:
            return False
        alpha = image.getchannel("A")
        return alpha.getextrema()[0] < 255
    except Exception:
        return False


def _safe_under(base: Path, candidate: Path) -> Path | None:
    """把 candidate 解析到 base 之内，越界返回 None（防目录穿越）。"""
    try:
        base_resolved = base.resolve()
        target = (base_resolved / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        target.relative_to(base_resolved)
    except (OSError, ValueError):
        return None
    return target


class WebUIApi:
    """插件 WebUI 的接口集合。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    # ==================== 注册 ==================== #

    def register(self) -> None:
        context = self.plugin.context
        routes: tuple[tuple[str, Callable[..., Any], tuple[str, ...], str], ...] = (
            ("/overview", self.overview, ("GET",), "插件总览与状态"),
            ("/config", self.get_config, ("GET",), "读取全部配置项"),
            ("/config", self.save_config, ("POST",), "保存配置项"),
            ("/config/reset", self.reset_config, ("POST",), "恢复默认配置"),
            ("/platforms", self.toggle_platform, ("POST",), "启用/禁用平台"),
            ("/parse", self.parse_url, ("POST",), "手动解析链接"),
            ("/render", self.render_cached, ("POST",), "按指定外观重新渲染卡片"),
            ("/preview/sample", self.sample_preview, ("POST",), "离线示例卡片预览"),
            ("/appearance", self.get_appearance, ("GET",), "读取界面外观偏好"),
            ("/appearance", self.save_appearance, ("POST",), "保存界面外观偏好"),
            ("/screenshot", self.screenshot, ("POST",), "网页截图测试"),
            ("/cache", self.list_cache, ("GET",), "解析记录列表"),
            ("/cache/delete", self.delete_cache, ("POST",), "删除解析记录"),
            ("/cache/clear", self.clear_cache, ("POST",), "清空解析记录/缓存文件"),
            ("/cache/cleanup", self.cleanup_cache, ("POST",), "按过期时间清理缓存文件"),
            ("/cache/thumbnail", self.cache_thumbnail, ("GET",), "解析记录卡片缩略图"),
            ("/cache/download", self.cache_download, ("GET",), "下载解析记录卡片原图"),
            ("/bili/status", self.bili_status, ("GET",), "B站登录状态"),
            ("/bili/qrcode", self.bili_qrcode, ("POST",), "获取B站登录二维码"),
            ("/bili/qrcode/status", self.bili_qrcode_status, ("GET",), "B站扫码状态"),
            ("/bili/logout", self.bili_logout, ("POST",), "清除B站 Cookie"),
        )
        for suffix, handler, methods, desc in routes:
            context.register_web_api(f"/{PLUGIN_NAME}{suffix}", handler, list(methods), desc)

    # ==================== 总览 ==================== #

    async def overview(self):
        plugin = self.plugin
        pconfig = get_config()

        platforms = [
            {
                "name": name,
                "label": PLATFORM_DISPLAY_NAMES.get(name, name),
                "enabled": name in plugin.parsers,
            }
            for name in PLATFORM_ORDER
        ]
        files, size = await asyncio.to_thread(_dir_stats, plugin.cache_dir)
        history = await asyncio.to_thread(plugin.history.stats)

        active_login = any(
            not task.done() for task in plugin._bili_login_tasks.values()
        )

        return _json_response(
            {
                "version": _plugin_version(),
                "platforms": platforms,
                "enabled_count": sum(1 for item in platforms if item["enabled"]),
                "disabled_platforms": pconfig.DISABLED_PLATFORMS,
                "render": {
                    "enabled": plugin._renderer.enabled,
                    "theme": plugin._renderer.theme_name,
                    "layout": plugin._renderer.layout_name,
                    "width": plugin._renderer.width,
                    "cover_full_size": plugin._renderer.cover_full_size,
                },
                "screenshot": {
                    "backend": plugin.screenshot.backend,
                    "configured": plugin.screenshot.is_configured,
                    "fallback": plugin._screenshot_fallback,
                },
                "bili": {
                    "configured": bool(plugin._bili_cookie),
                    "quality": pconfig.BILI_QUALITY,
                    "login_active": active_login,
                },
                "cache": {
                    "files": files,
                    "size_bytes": size,
                    "size_text": fmt_size_bytes(size),
                    "dir": str(plugin.cache_dir),
                    "source": getattr(plugin, "cache_dir_source", "default"),
                    "ttl_hours": pconfig.CACHE_TTL_HOURS,
                },
                "media": {
                    "docker": is_docker_environment(),
                    "cache_dir": str(plugin.cache_dir),
                    "cache_dir_source": getattr(plugin, "cache_dir_source", "default"),
                    "shared_dir_configured": bool(pconfig.CACHE_DIR),
                    "relay_enabled": pconfig.MEDIA_RELAY_ENABLED,
                    "relay_callback": pconfig.MEDIA_RELAY_CALLBACK_URL
                    or str(_astrbot_callback_base()),
                    "relay_callback_from_config": bool(pconfig.MEDIA_RELAY_CALLBACK_URL),
                    "relay_ttl": pconfig.MEDIA_RELAY_TTL,
                },
                "history": history,
                "memory": {
                    "result_cache": len(plugin._result_cache),
                    "render_cache": len(plugin._render_cache),
                },
                "send_error_messages": plugin._send_errors,
                "data_dir": str(plugin.cache_dir.parent),
            }
        )

    # ==================== 配置 ==================== #

    async def get_config(self):
        pconfig = get_config()
        payload = config_meta_payload()
        payload["values"] = pconfig.current_values()
        payload["platforms"] = [
            {"name": name, "label": PLATFORM_DISPLAY_NAMES.get(name, name)}
            for name in PLATFORM_ORDER
        ]
        payload["problems"] = self._schema_problems()
        return _json_response(payload)

    async def save_config(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict) or not values:
            return _error("没有需要保存的配置项")

        pconfig = get_config()
        changed, errors = pconfig.apply_updates(values)
        runtime: dict[str, Any] = {}
        runtime_ok = True
        if changed:
            try:
                runtime = await self.plugin.apply_runtime_config()
            except Exception as exc:
                runtime_ok = False
                logger.warning("[denia_share] 应用新配置失败", exc_info=True)
                errors.append(f"配置已保存，但运行时热更新失败：{str(exc)[:120]}")
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime_ok": runtime_ok,
                "runtime": runtime,
                "values": pconfig.current_values(),
            }
        )

    async def reset_config(self):
        pconfig = get_config()
        changed = pconfig.reset_to_defaults()
        runtime: dict[str, Any] = {}
        errors: list[str] = []
        runtime_ok = True
        if changed:
            try:
                runtime = await self.plugin.apply_runtime_config()
            except Exception as exc:
                runtime_ok = False
                logger.warning("[denia_share] 恢复默认配置后热更新失败", exc_info=True)
                errors.append(f"已恢复默认，但运行时热更新失败：{str(exc)[:120]}")
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime_ok": runtime_ok,
                "runtime": runtime,
                "values": pconfig.current_values(),
            }
        )

    async def toggle_platform(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        name = str(body.get("name") or "").strip().lower()
        enabled = bool(body.get("enabled"))
        if name not in PLATFORM_DISPLAY_NAMES:
            return _error("未知平台")

        pconfig = get_config()
        disabled = [item for item in pconfig.DISABLED_PLATFORMS if item != name]
        if not enabled:
            disabled.append(name)
        # 保持 PLATFORM_ORDER 的顺序，读起来稳定
        disabled = [item for item in PLATFORM_ORDER if item in disabled]

        changed, errors = pconfig.apply_updates(
            {"DISABLED_PLATFORMS": ",".join(disabled)}
        )
        runtime: dict[str, Any] = {}
        if changed:
            runtime = await self.plugin.apply_runtime_config()
        return _json_response(
            {
                "changed": changed,
                "errors": errors,
                "runtime": runtime,
                "disabled_platforms": get_config().DISABLED_PLATFORMS,
                "enabled": name in self.plugin.parsers,
            }
        )

    # ==================== 手动解析 ==================== #

    async def parse_url(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        url = str(body.get("url") or "").strip()
        if not url:
            return _error("请填写要解析的链接")
        if len(url) > MAX_URL_LENGTH:
            return _error("链接过长")
        if not url.startswith(("http://", "https://")):
            return _error("链接需要以 http:// 或 https:// 开头")

        plugin = self.plugin
        name = plugin._match_parser(url)
        if name is None:
            return _error("没有平台规则能匹配这个链接；可以改用「网页截图」试试", 422)
        parser = plugin.parsers.get(name)
        if parser is None:
            return _error(f"{PLATFORM_DISPLAY_NAMES.get(name, name)} 平台当前是禁用状态", 409)

        started = time.perf_counter()
        try:
            keyword, searched = parser.search_url(url)
            result = await parser.parse(keyword, searched)
        except IgnoreException as exc:
            return _error(f"已忽略：{exc.message}", 422)
        except ParseException as exc:
            return _error(f"解析失败：{exc.message}", 422)
        except Exception as exc:
            logger.exception("[denia_share] WebUI 手动解析出错")
            return _error(f"解析出错：{str(exc)[:160]}", 500)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 与聊天链路的 _process_url 用同一个键，否则同一链接会各缓存一份
        cache_key = plugin.result_cache_key(url)
        plugin._result_cache[cache_key] = result

        payload = await self._preview_payload(
            result,
            cache_key,
            elapsed_ms=elapsed_ms,
            overrides=body.get("preview"),
            record=True,
        )
        return _json_response(payload)

    async def render_cached(self):
        """用内存里缓存的解析结果，按指定外观重新渲染（不重新联网解析）。"""
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        cache_key = str(body.get("cache_key") or "").strip()
        if not cache_key:
            return _error("缺少 cache_key")
        result = self.plugin._result_cache.get(cache_key)
        if result is None:
            return _error("解析结果已过期（插件重启或缓存被清理），请重新解析一次", 410)

        payload = await self._preview_payload(
            result,
            cache_key,
            elapsed_ms=None,
            overrides=body.get("preview"),
            record=False,
        )
        return _json_response(payload)

    async def _preview_payload(
        self,
        result: Any,
        cache_key: str,
        *,
        elapsed_ms: int | None,
        overrides: Any = None,
        record: bool,
    ) -> dict[str, Any]:
        """把 ParseResult 整理成页面需要的预览结构。"""
        card = await self._render_card(result, cache_key, overrides)

        record_id = ""
        if record:
            saved = await self.plugin._record_history(
                result, Path(card["path"]) if card else None,
                via="webui", elapsed_ms=elapsed_ms,
            )
            record_id = saved.id if saved else ""

        payload: dict[str, Any] = {
            "cache_key": cache_key,
            "record_id": record_id,
            "platform": result.platform.name,
            "platform_display": result.platform.display_name,
            "content_type": result.content_type,
            "title": result.title or "",
            "author": result.author.name if result.author else "",
            "url": result.url or "",
            "text": (result.text or "")[:1000],
            "detail": self.plugin._history_detail(result),
            "elapsed_ms": elapsed_ms,
            "card": None,
        }
        if card is not None:
            payload["card"] = {
                "file": card["file"],
                "data_url": card["data_url"],
                "theme": card["theme"],
                "layout": card["layout"],
                "width": card["width"],
            }
        return payload

    async def _render_card(
        self, result: Any, cache_key: str, overrides: Any = None
    ) -> dict[str, Any] | None:
        """渲染卡片并生成预览图。

        ``overrides`` 非空时用一份临时渲染器（只预览、不写进插件缓存），
        这样「换主题看看效果」不需要改动用户的实际配置。
        """
        renderer = self.plugin._renderer
        temporary = False
        if isinstance(overrides, dict) and overrides:
            try:
                renderer = self._build_renderer(overrides)
            except PreviewOverridesError as exc:
                # 解析链路里卡片只是附带产物，参数填错不该把整条解析判死，
                # 记日志后按当前配置出图（页面会另外提示参数无效）。
                logger.warning("[denia_share] 预览覆盖参数无效，回退当前配置：%s", exc)
                renderer = self.plugin._renderer
            else:
                temporary = True

        if renderer is None or not renderer.enabled:
            return None
        try:
            path = await renderer.render(result, cache_key=cache_key)
        except Exception:
            logger.warning("[denia_share] WebUI 渲染卡片失败", exc_info=True)
            return None

        if path is None:
            return None
        if not temporary:
            self.plugin._render_cache[cache_key] = path

        data_url = await asyncio.to_thread(_image_data_url, path)
        if data_url is None:
            return None
        return {
            "path": str(path),
            "file": path.name,
            "data_url": data_url,
            "theme": renderer.theme_name,
            "layout": renderer.layout_name,
            "width": renderer.width,
        }

    def _build_renderer(self, overrides: dict[str, Any]) -> ShareCardRenderer:
        """按预览参数构造临时渲染器。

        以当前配置为底，仅覆盖预览里显式给出的外观项，这样「换个颜色看看」
        不需要改动用户的实际配置。

        参数不合法时抛 ``PreviewOverridesError``——调用方必须显式处理，
        不能当成「没有覆盖项」静默回退。
        """
        pconfig = get_config()
        opts = pconfig.renderer_options()
        opts["enabled"] = True

        theme = str(overrides.get("theme") or opts["theme"]).strip().lower()
        layout = str(overrides.get("layout") or opts["layout"]).strip().lower()
        if theme not in {"dark", "light"}:
            raise PreviewOverridesError(f"主题只能是 dark 或 light，收到 {theme!r}")
        if layout not in LAYOUT_NAMES:
            raise PreviewOverridesError(
                f"布局只能是 {'/'.join(sorted(LAYOUT_NAMES))}，收到 {layout!r}"
            )
        opts["theme"] = theme
        opts["layout"] = layout

        if "width" in overrides and overrides.get("width") is not None:
            try:
                opts["width"] = max(520, min(1080, int(overrides.get("width"))))
            except (TypeError, ValueError):
                raise PreviewOverridesError(
                    f"宽度需要是数字，收到 {overrides.get('width')!r}"
                ) from None
        if isinstance(overrides.get("cover_full_size"), bool):
            opts["cover_full_size"] = overrides["cover_full_size"]
        if isinstance(overrides.get("show_avatar"), bool):
            opts["show_avatar"] = overrides["show_avatar"]
        if isinstance(overrides.get("show_play_button"), bool):
            opts["show_play_button"] = overrides["show_play_button"]
        if overrides.get("desc_max_lines") is not None:
            try:
                opts["desc_max_lines"] = max(0, min(12, int(overrides.get("desc_max_lines"))))
            except (TypeError, ValueError):
                raise PreviewOverridesError(
                    f"正文行数需要是数字，收到 {overrides.get('desc_max_lines')!r}"
                ) from None
        for key in ("accent_color", "gradient_top", "gradient_bottom"):
            if key in overrides:
                raw = overrides.get(key)
                if raw in (None, ""):
                    opts[key] = None
                    continue
                from .card_renderer import normalize_hex_color

                normalized = normalize_hex_color(str(raw))
                if normalized is None:
                    raise PreviewOverridesError(
                        f"{key} 需要是 #RRGGBB 形式的颜色，收到 {raw!r}"
                    )
                opts[key] = normalized
        if "watermark" in overrides:
            opts["watermark"] = str(overrides.get("watermark") or "").strip()[:12]

        try:
            return ShareCardRenderer(self.plugin.cache_dir, **opts)
        except Exception:
            logger.warning("[denia_share] 构造临时渲染器失败", exc_info=True)
            raise PreviewOverridesError("这些外观参数无法生成渲染器") from None

    # ==================== 离线示例预览 ==================== #

    _SAMPLE_LOCK: asyncio.Lock | None = None

    async def sample_preview(self):
        """用内置示例数据渲染一张卡片预览（不联网，不写解析记录）。

        供「外观」页实时预览：改一个参数立刻看到效果，无需真的去解析一条链接。
        """
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        overrides = body.get("preview")
        overrides = overrides if isinstance(overrides, dict) else {}

        renderer = None
        if overrides:
            try:
                renderer = self._build_renderer(overrides)
            except PreviewOverridesError as exc:
                # 预览的全部意义就是「所见即所填」，参数非法必须显式报错，
                # 不能悄悄按当前配置出图。
                return _error(str(exc))
        if renderer is None:
            # 没有覆盖项时直接用常驻渲染器；没开渲染时也要能给预览（强制启用）
            renderer = self.plugin._renderer
            if renderer is None or not renderer.enabled:
                opts = get_config().renderer_options()
                opts["enabled"] = True
                renderer = ShareCardRenderer(self.plugin.cache_dir, **opts)

        result = self.plugin.build_sample_result()
        # 示例渲染不进入渲染缓存，也不污染记录；文件名带外观指纹自然去重
        try:
            if WebUIApi._SAMPLE_LOCK is None:
                WebUIApi._SAMPLE_LOCK = asyncio.Lock()
            async with WebUIApi._SAMPLE_LOCK:
                path = await renderer.render(result, cache_key=None)
        except Exception:
            logger.warning("[denia_share] 示例卡片预览失败", exc_info=True)
            return _error("示例卡片渲染失败，请检查字体配置", 500)
        if path is None:
            return _error("示例卡片渲染失败，请检查字体配置", 500)

        data_url = await asyncio.to_thread(_image_data_url, path, 720)
        if data_url is None:
            return _error("预览图生成失败", 500)
        return _json_response(
            {
                "data_url": data_url,
                "theme": renderer.theme_name,
                "layout": renderer.layout_name,
                "width": renderer.width,
                "file": path.name,
            }
        )

    # ==================== 界面外观偏好 ==================== #

    _APPEARANCE_KEYS = {
        "accent": (str, ""),
        "accent_custom": (str, ""),
        "radius": (str, "m"),
        "compact": (bool, False),
        "animations": (bool, True),
        "theme_override": (str, "follow"),
    }

    def _appearance_path(self) -> Path:
        return self.plugin.history.path.parent / "webui_appearance.json"

    async def get_appearance(self):
        prefs = await asyncio.to_thread(self._read_appearance)
        return _json_response({"prefs": prefs})

    def _read_appearance(self) -> dict[str, Any]:
        path = self._appearance_path()
        try:
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        prefs: dict[str, Any] = {}
        for key, (kind, default) in self._APPEARANCE_KEYS.items():
            value = raw.get(key, default)
            if kind is bool:
                prefs[key] = bool(value)
            else:
                value = str(value or "").strip()
                # 白名单收敛，避免把脏数据写进 CSS 变量
                if key == "radius" and value not in {"s", "m", "l"}:
                    value = "m"
                if key == "theme_override" and value not in {"follow", "light", "dark"}:
                    value = "follow"
                if key in {"accent", "accent_custom"}:
                    import re as _re

                    if value and not _re.match(r"^#[0-9a-fA-F]{6}$", value):
                        value = ""
                prefs[key] = value
        return prefs

    async def save_appearance(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        prefs = body.get("prefs") if isinstance(body, dict) else None
        if not isinstance(prefs, dict):
            return _error("缺少 prefs 对象")
        # 只接受白名单键，值在 _read_appearance 回读时还会再校验一次
        cleaned = {k: prefs[k] for k in self._APPEARANCE_KEYS if k in prefs}
        try:
            await asyncio.to_thread(self._write_appearance, cleaned)
        except OSError as exc:
            logger.warning("[denia_share] 外观偏好写入失败", exc_info=True)
            return _error(f"写入失败：{str(exc)[:120]}", 500)
        return _json_response({"prefs": self._read_appearance()})

    def _write_appearance(self, prefs: dict[str, Any]) -> None:
        import json
        import os

        path = self._appearance_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = self._read_appearance()
        merged.update(prefs)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ==================== 网页截图 ==================== #

    async def screenshot(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        url = str(body.get("url") or "").strip()
        if not url:
            return _error("请填写要截图的网址")
        if len(url) > MAX_URL_LENGTH:
            return _error("网址过长")
        if not is_probably_screenshotable(url):
            return _error("这个地址不支持截图（本地地址或非法主机名）")

        service = self.plugin.screenshot
        if not service.is_configured:
            return _error("当前截图后端未配置：使用 Cloudflare 时请先填 Account ID 与 API Token", 409)

        path = await service.capture(url)
        if path is None:
            return _error(f"截图失败：{service.last_error or '未知原因'}", 502)

        data_url = await asyncio.to_thread(_image_data_url, path, 720)
        return _json_response(
            {
                "url": url,
                "backend": service.backend,
                "file": path.name,
                "size_text": fmt_size_bytes(path.stat().st_size),
                "data_url": data_url,
            }
        )

    # ==================== 解析记录 / 缓存 ==================== #

    async def list_cache(self):
        from astrbot.api.web import request

        keyword = (request.query.get("keyword") or "").strip()
        platform = (request.query.get("platform") or "").strip().lower()
        via = (request.query.get("via") or "").strip().lower()
        def _query_int(name: str, default: int, upper: int) -> int:
            """query 参数转 int，非法值回退默认（不依赖框架 type=int 的失败语义）。"""
            raw = request.query.get(name)
            if raw is None or raw == "":
                return default
            try:
                return max(0 if name == "offset" else 1, min(upper, int(raw)))
            except (TypeError, ValueError):
                return default

        limit = _query_int("limit", 20, MAX_QUERY_LIMIT)
        offset = _query_int("offset", 0, 10_000)

        store = self.plugin.history
        items, total = await asyncio.to_thread(
            store.query,
            keyword=keyword,
            platform=platform,
            via=via,
            limit=limit,
            offset=offset,
        )
        stats = await asyncio.to_thread(store.stats)
        cache_dir = self.plugin.cache_dir

        def _assemble_records():
            """逐条检查卡片/媒体文件是否还在盘上（含 resolve 磁盘 IO），放线程里做。"""
            assembled = []
            for record in items:
                data = record.to_dict()
                card_exists = False
                if record.card_file:
                    target = _safe_under(cache_dir, Path(record.card_file))
                    card_exists = bool(target and target.is_file())
                data["card_exists"] = card_exists
                data["media_existing"] = sum(
                    1
                    for name in record.media_files
                    if (target := _safe_under(cache_dir, Path(name))) and target.is_file()
                )
                assembled.append(data)
            return assembled

        records = await asyncio.to_thread(_assemble_records)

        files, size = await asyncio.to_thread(_dir_stats, cache_dir)
        return _json_response(
            {
                "items": records,
                "total": total,
                "limit": limit,
                "offset": offset,
                "stats": stats,
                "cache": {
                    "files": files,
                    "size_bytes": size,
                    "size_text": fmt_size_bytes(size),
                    "dir": str(cache_dir),
                    "ttl_hours": get_config().CACHE_TTL_HOURS,
                },
                "platforms": [
                    {"name": name, "label": PLATFORM_DISPLAY_NAMES.get(name, name)}
                    for name in PLATFORM_ORDER
                ],
            }
        )

    async def delete_cache(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        raw_ids = body.get("ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list) or not raw_ids:
            return _error("请提供要删除的记录 id")
        if len(raw_ids) > 200:
            return _error("一次最多删除 200 条")
        ids = [str(item) for item in raw_ids]
        purge = bool(body.get("purge_files"))

        store = self.plugin.history
        removed_files = 0
        if purge:
            removed_files = await asyncio.to_thread(self._purge_files, ids)
        removed = await asyncio.to_thread(store.delete, ids)
        return _json_response(
            {"removed": removed, "removed_files": removed_files, "ids": ids}
        )

    async def clear_cache(self):
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        scope = str(body.get("scope") or "all").strip().lower()
        if scope not in {"records", "files", "all"}:
            return _error("scope 只能是 records / files / all")

        result: dict[str, Any] = {"scope": scope, "removed_records": 0, "removed_files": 0}

        if scope in {"records", "all"}:
            result["removed_records"] = await asyncio.to_thread(
                self.plugin.history.clear
            )
        if scope in {"files", "all"}:
            try:
                result["removed_files"] = await clear_cache_dir(self.plugin.cache_dir)
            except Exception as exc:
                logger.warning("[denia_share] 清空缓存目录失败", exc_info=True)
                return _error(f"清空缓存文件失败：{str(exc)[:160]}", 500)
            self.plugin._result_cache.clear()
            self.plugin._render_cache.clear()

        files, size = await asyncio.to_thread(_dir_stats, self.plugin.cache_dir)
        result["cache"] = {"files": files, "size_bytes": size, "size_text": fmt_size_bytes(size)}
        return _json_response(result)

    async def cleanup_cache(self):
        """按 CACHE_TTL_HOURS 清理过期的缓存文件（不删除解析记录）。"""
        from astrbot.api.web import request

        body = await request.json(default={}) or {}
        ttl = body.get("ttl_hours")
        if ttl is None:
            ttl = get_config().CACHE_TTL_HOURS
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            return _error("ttl_hours 需要是整数")
        if ttl <= 0:
            return _error("ttl_hours 需要大于 0（0 表示不自动清理）")

        removed = await cleanup_cache_dir(self.plugin.cache_dir, ttl_hours=ttl)
        files, size = await asyncio.to_thread(_dir_stats, self.plugin.cache_dir)
        return _json_response(
            {
                "ttl_hours": ttl,
                "removed_files": removed,
                "cache": {"files": files, "size_bytes": size, "size_text": fmt_size_bytes(size)},
            }
        )

    async def cache_thumbnail(self):
        from astrbot.api.web import request

        record = await self._record_or_error()
        if not isinstance(record, dict):
            return record

        path = self._record_card_path(record)
        if path is None:
            return _error("卡片文件已被清理", 404)
        data_url = await asyncio.to_thread(_image_data_url, path, 900)
        if data_url is None:
            return _error("缩略图生成失败", 500)
        return _json_response({"id": record["id"], "data_url": data_url})

    async def cache_download(self):
        from astrbot.api.web import file_response

        record = await self._record_or_error()
        if not isinstance(record, dict):
            return record
        path = self._record_card_path(record)
        if path is None:
            return _error("卡片文件已被清理", 404)
        safe_name = f"denia_card_{record['id']}{path.suffix or '.png'}"
        return file_response(
            path,
            filename=safe_name,
            content_type="image/png" if path.suffix.lower() == ".png" else None,
        )

    async def _record_or_error(self):
        """按 id 取记录；出错时直接返回响应对象。"""
        from astrbot.api.web import request

        record_id = (request.query.get("id") or "").strip()
        if not record_id:
            return _error("缺少记录 id")

        record = await asyncio.to_thread(self.plugin.history.get, record_id)
        if record is None:
            return _error("记录不存在", 404)
        return record.to_dict()

    def _record_card_path(self, record: dict[str, Any]) -> Path | None:
        card_file = record.get("card_file")
        if not card_file:
            return None
        target = _safe_under(self.plugin.cache_dir, Path(str(card_file)))
        if target is None or not target.is_file():
            return None
        return target

    def _purge_files(self, record_ids: Iterable[str]) -> int:
        """删除记录引用的卡片与媒体文件，返回删除的文件数。"""
        store = self.plugin.history
        removed = 0
        for record_id in record_ids:
            record = store.get(record_id)
            if record is None:
                continue
            names = [record.card_file] if record.card_file else []
            names.extend(record.media_files)
            for name in names:
                if not name:
                    continue
                target = _safe_under(self.plugin.cache_dir, Path(str(name)))
                if target is None or not target.is_file():
                    continue
                try:
                    target.unlink()
                    removed += 1
                except OSError:
                    continue
        # 渲染缓存按 cache_key 索引、存的是路径，删了文件后难以精确对应，
        # 直接整体清掉：代价只是下次重新渲染一次。
        if removed:
            self.plugin._render_cache.clear()
        return removed

    # ==================== B站登录 ==================== #

    async def bili_status(self):
        from astrbot.api.web import request

        plugin = self.plugin
        pconfig = get_config()
        check = (request.query.get("check") or "").strip() in {"1", "true", "yes"}
        payload: dict[str, Any] = {
            "configured": bool(plugin._bili_cookie),
            "quality": pconfig.BILI_QUALITY,
            "cookie_length": len(plugin._bili_cookie or ""),
            "login_active": any(
                not task.done() for task in plugin._bili_login_tasks.values()
            ),
            "has_config_cookie": bool(str(pconfig.BILI_CK or "").strip()),
            "valid": None,
            "username": "",
            "uid": 0,
            "error": "",
        }
        if check and payload["configured"]:
            result = await plugin.bili_cookie_status()
            payload.update(
                valid=result.get("valid"),
                username=result.get("username", ""),
                uid=result.get("uid", 0),
                error=result.get("error", ""),
            )
        return _json_response(payload)

    async def bili_qrcode(self):
        plugin = self.plugin
        try:
            qrcode_url, qrcode_key = await plugin.bili_qr_create()
        except ParseException as exc:
            return _error(f"获取二维码失败：{exc.message}", 502)
        except Exception as exc:
            logger.warning("[denia_share] WebUI 获取B站二维码失败", exc_info=True)
            return _error(f"获取二维码失败：{str(exc)[:160]}", 502)

        try:
            png = plugin.render_qr_png(qrcode_url)
        except Exception as exc:
            return _error(f"二维码生成失败：{str(exc)[:120]}（请确认已安装 qrcode）", 500)

        task_id = uuid.uuid4().hex[:12]
        plugin.bili_login_start(task_id, qrcode_key)
        return _json_response(
            {
                "task_id": task_id,
                "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                "state": "waiting",
                "message": "等待扫码",
                "expires_in": plugin.bili_login_state(task_id).get("expires_in", 180),
            }
        )

    async def bili_qrcode_status(self):
        from astrbot.api.web import request

        task_id = (request.query.get("task_id") or "").strip()
        if not task_id:
            return _error("缺少 task_id")
        state = self.plugin.bili_login_state(task_id)
        if state.get("state") == "success":
            state["configured"] = True
        return _json_response(state)

    async def bili_logout(self):
        ok = await self.plugin.bili_logout()
        if not ok:
            return _error("清除 Cookie 失败，请检查插件数据目录权限", 500)
        return _json_response({"configured": False})

    # ==================== 工具 ==================== #

    def _schema_problems(self) -> list[str]:
        try:
            import json

            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            return ["无法读取 _conf_schema.json"]
        return verify_schema_alignment(schema)


def fmt_size_bytes(size: int) -> str:
    """把字节数格式化成便于阅读的字符串。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _plugin_version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:
        return "unknown"
