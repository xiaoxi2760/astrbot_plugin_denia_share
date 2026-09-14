"""
达妮娅分享 - 链接分享自动解析插件

支持 B站 | 抖音 | 快手 | 微博 | 小红书 | Twitter | AcFun | NGA

解析内核合并自两个插件，取各自更强的一版：
- astrbot_plugin_rika_share（架构 / 渲染 / B站 / 微博 / 小红书 / AcFun / NGA）
- astrbot_plugin_media_parser 娅娅版（抖音 a_bogus 签名 / 快手新版兼容 / Twitter GraphQL）

有意不实现的能力：LLM 文本翻译、视频仅发送封面。
"""

import re
import json
import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

import aiohttp
import qrcode
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register, StarTools

from .core.utils import cleanup_cache_dir
from .core.config import init_config, get_config
from .core.download import StreamDownloader
from .core.data import ParseResult, ImageContent, VideoContent, AudioContent
from .core.exception import (
    ParseException, IgnoreException, DownloadException, SilentException,
)
from .core.render import ShareCardRenderer
from .core.parsers import (
    BilibiliParser, DouyinParser, KuaiShouParser, WeiBoParser,
    XiaoHongShuParser, TwitterParser, NGAParser, AcfunParser,
)

PLUGIN_NAME = "astrbot_plugin_denia_share"

# ========== B站扫码登录 API ==========
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_CODE_SCANNED = 86090
QR_CODE_EXPIRED = 86038
QR_CODE_SUCCESS = 0
QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5


def _get_plugin_data_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


# ========== URL 匹配模式 ==========
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}|av\d{6,})")
DOUYIN_PATTERN = re.compile(r"(v\.douyin\.com|douyin\.com|iesdouyin\.com|m\.douyin\.com|jx\.douyin\.com|jingxuan\.douyin\.com)")
KUAISHOU_PATTERN = re.compile(r"(v\.kuaishou\.com|kuaishou\.com|chenzhongtech\.com)")
WEIBO_PATTERN = re.compile(r"(weibo\.com|weibo\.cn|m\.weibo\.cn|video\.weibo\.com|mapp\.api\.weibo\.cn)")
XHS_PATTERN = re.compile(r"(xhslink\.com|xhslink\.cn|xiaohongshu\.com)")
TWITTER_PATTERN = re.compile(r"(x\.com|twitter\.com)")
NGA_PATTERN = re.compile(r"(nga\.178\.com|ngabbs\.com|bbs\.nga\.cn)")
ACFUN_PATTERN = re.compile(r"acfun\.cn")

URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


class _EventUrlWrapper:
    """把任意 URL 伪装成一个只含该 URL 的事件，供解析流程复用。"""

    def __init__(self, event: AstrMessageEvent, url: str):
        self._event = event
        self.message_str = url

    def __getattr__(self, name):
        return getattr(self._event, name)


@register("达妮娅分享", "xiaoxi2760",
          "链接分享自动解析，支持 B站|抖音|快手|微博|小红书|Twitter|AcFun|NGA", "0.2.0")
class DeniaSharePlugin(Star):

    @staticmethod
    def _is_onebot(event: AstrMessageEvent) -> bool:
        """合并转发（Comp.Nodes）是 OneBot v11 独有特性。"""
        try:
            return "aiocqhttp" in event.get_platform_name().lower()
        except Exception:
            return False

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = _get_plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir = data_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        pconfig = init_config(config, self.cache_dir, self.config_dir)

        try:
            from .core.config import migrate_grouped_config

            if migrate_grouped_config(config):
                save = getattr(config, "save_config", None)
                if callable(save):
                    save()
        except Exception:
            logger.warning("旧版配置迁移失败，将使用兼容回退读取", exc_info=True)

        self.downloader = StreamDownloader(
            self.cache_dir,
            proxies=pconfig.PROXY or None,
            max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
        )
        self.disabled_platforms = pconfig.DISABLED_PLATFORMS

        self.parsers: dict[str, Any] = {}
        self._init_parsers()
        self._result_cache: dict[str, ParseResult] = {}
        self._render_cache: dict[str, Path] = {}
        self._cache_cleanup_task: asyncio.Task | None = None

        self._renderer = ShareCardRenderer(
            self.cache_dir,
            enabled=pconfig.RENDER_ENABLED,
            width=pconfig.RENDER_WIDTH,
            theme=pconfig.RENDER_THEME,
            font_path=pconfig.RENDER_FONT_PATH or None,
            layout=pconfig.RENDER_LAYOUT,
            cover_full_size=pconfig.RENDER_COVER_FULL_SIZE,
        )

        # ========== B站 Cookie ==========
        self._bili_cookie: str = ""
        self._bili_http_session: aiohttp.ClientSession | None = None
        self._bili_login_tasks: Dict[str, asyncio.Task] = {}
        self._bili_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._bili_data_dir.mkdir(parents=True, exist_ok=True)
        self._bili_cookie_file = self._bili_data_dir / "bili_cookie.json"

        logger.info(
            f"[denia_share] 已启用平台: {', '.join(self.parsers.keys()) or '无'}；"
            f"卡片渲染: {'开' if self._renderer.enabled else '关'}"
        )

    def _init_parsers(self):
        pconfig = get_config()
        disabled = self.disabled_platforms
        if "bilibili" not in disabled:
            self.parsers["bilibili"] = BilibiliParser(
                self.downloader, bili_ck=pconfig.BILI_CK, config_dir=self.config_dir
            )
        if "douyin" not in disabled:
            self.parsers["douyin"] = DouyinParser(self.downloader)
        if "kuaishou" not in disabled:
            self.parsers["kuaishou"] = KuaiShouParser(self.downloader)
        if "weibo" not in disabled:
            self.parsers["weibo"] = WeiBoParser(self.downloader)
        if "xiaohongshu" not in disabled:
            self.parsers["xiaohongshu"] = XiaoHongShuParser(self.downloader, xhs_ck=pconfig.XHS_CK)
        if "twitter" not in disabled:
            self.parsers["twitter"] = TwitterParser(self.downloader)
        if "nga" not in disabled:
            self.parsers["nga"] = NGAParser(self.downloader)
        if "acfun" not in disabled:
            self.parsers["acfun"] = AcfunParser(self.downloader)

    async def initialize(self):
        pconfig = get_config()
        ttl = pconfig.CACHE_TTL_HOURS
        if ttl > 0:
            interval = max(pconfig.CACHE_CLEANUP_INTERVAL_MINUTES, 1) * 60

            async def _cache_cleanup_loop():
                try:
                    while True:
                        await cleanup_cache_dir(self.cache_dir, ttl_hours=ttl)
                        self._result_cache.clear()
                        self._render_cache.clear()
                        await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise

            self._cache_cleanup_task = asyncio.create_task(_cache_cleanup_loop())

        await self._bili_load_cookie()
        self._bili_http_session = aiohttp.ClientSession()
        if self._bili_cookie:
            self._bili_apply_cookie_to_parser(self._bili_cookie)

    # ==================== 平台处理器 ====================

    async def _dispatch(self, event: AstrMessageEvent, name: str):
        if self._has_json_component(event):
            return
        parser = self.parsers.get(name)
        if not parser:
            return
        async for r in self._process_url(event, parser):
            yield r

    @filter.regex(BILIBILI_PATTERN)
    async def bilibili_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "bilibili"):
            yield r

    @filter.regex(DOUYIN_PATTERN)
    async def douyin_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "douyin"):
            yield r

    @filter.regex(KUAISHOU_PATTERN)
    async def kuaishou_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "kuaishou"):
            yield r

    @filter.regex(WEIBO_PATTERN)
    async def weibo_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "weibo"):
            yield r

    @filter.regex(XHS_PATTERN)
    async def xiaohongshu_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "xiaohongshu"):
            yield r

    @filter.regex(TWITTER_PATTERN)
    async def twitter_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "twitter"):
            yield r

    @filter.regex(NGA_PATTERN)
    async def nga_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "nga"):
            yield r

    @filter.regex(ACFUN_PATTERN)
    async def acfun_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        async for r in self._dispatch(event, "acfun"):
            yield r

    # ==================== JSON 卡片（QQ 小程序分享） ====================

    @filter.regex(r".*")
    async def json_card_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        if not self._has_json_component(event):
            return
        links = self._extract_links_from_event(event)
        if not links:
            return
        for link in dict.fromkeys(links):
            for parser in self.parsers.values():
                try:
                    parser.search_url(link)
                    async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                        yield r
                    return
                except (SilentException, ParseException):
                    continue

    # ==================== 核心流程 ====================

    async def _process_url(
        self, event: AstrMessageEvent, parser: Any
    ) -> AsyncGenerator[MessageEventResult, None]:
        url = event.message_str.strip()
        try:
            cache_key = url[:64]
            result = self._result_cache.get(cache_key)
            if result is None:
                keyword, searched = parser.search_url(url)
                result = await parser.parse(keyword, searched)
                self._result_cache[cache_key] = result

            header, nodes_content = await self._build_output(result)

            render_path: Path | None = None
            if self._renderer.enabled:
                render_path = await self._renderer.render(
                    result, cache_key=cache_key, existing=self._render_cache.get(cache_key),
                )
                if render_path is not None:
                    self._render_cache[cache_key] = render_path

            warnings = result.extra.get("limit_warnings") or []

            if render_path is not None:
                await self._send_image(event, render_path)
                # 卡片已承载标题/作者/统计/时长，视频场景不再重复发文字
                skip_text = bool(result.video_contents)
                header_text = "" if skip_text else header
                text_items = [] if skip_text else list(nodes_content)
            else:
                header_text = header
                text_items = list(nodes_content)
                for w in warnings:
                    text_items.append([Comp.Plain(w)])

            if text_items:
                if self._is_onebot(event):
                    yield await self._build_nodes_result(event, header_text, text_items)
                else:
                    async for r in self._send_plain_output(event, header_text, text_items):
                        yield r

            async for r in self._try_send_media(event, result):
                yield r

        except SilentException:
            return
        except IgnoreException as e:
            yield event.plain_result(f"ℹ️ {e.message}")
        except ParseException as e:
            yield event.plain_result(f"❌ 解析失败: {e.message}")
        except DownloadException as e:
            yield event.plain_result(f"⚠️ 下载失败: {e.message}")
        except Exception as e:
            logger.exception("解析异常")
            yield event.plain_result(f"❌ 处理出错: {str(e)[:100]}")

    async def _send_image(self, event: AstrMessageEvent, path: Path):
        """主动发送图片，绕开事件回复管线，避免被附加「引用回复 / @」。"""
        try:
            sent = await self.context.send_message(
                event.unified_msg_origin, MessageChain().file_image(str(path)),
            )
            if not sent:
                logger.warning(f"解析卡片主动发送未找到匹配平台会话: {path.name}")
        except Exception as e:
            logger.warning(f"解析卡片主动发送异常: {e}")

    @staticmethod
    async def _build_nodes_result(event: AstrMessageEvent, header: str, items: list[list]):
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        nodes = Comp.Nodes([])
        if header:
            nodes.nodes.append(Comp.Node(uin=sender_id, name=sender_name, content=[Comp.Plain(header)]))
        for item in items:
            nodes.nodes.append(Comp.Node(uin=sender_id, name=sender_name, content=item))
        return event.chain_result([nodes])

    async def _build_output(self, result: ParseResult) -> tuple[str, list[list]]:
        """统一构建 (标题头, 内容节点列表)。"""
        header = f"达妮娅分享 | {result.platform.display_name} - {result.content_type}"
        if result.author:
            header += f" @{result.author.name}"

        nodes: list[list] = []
        if result.title:
            nodes.append([Comp.Plain(result.title)])
        if result.text:
            nodes.append([Comp.Plain(result.text[:300])])

        for key, prefix in (
            ("stats_line", ""),
            ("duration", "⏱ 时长："),
            ("online", ""),
            ("info", "📝 "),
        ):
            if value := result.extra.get(key):
                nodes.append([Comp.Plain(f"{prefix}{value}")])

        for image in result.img_contents:
            path = await image.path_task.safe_get()
            if path:
                nodes.append([Comp.Image.fromFileSystem(str(path))])
        for graphic in result.graphics:
            if isinstance(graphic, ImageContent):
                path = await graphic.path_task.safe_get()
                if path:
                    nodes.append([Comp.Image.fromFileSystem(str(path))])
            elif isinstance(graphic, str):
                nodes.append([Comp.Plain(graphic)])

        return header, nodes

    async def _send_plain_output(self, event: AstrMessageEvent, header: str, nodes_content: list[list]):
        """非 OneBot 平台：合并成一条消息链。"""
        parts: list = []
        if header:
            parts.append(Comp.Plain(header + "\n"))

        text_lines: list[str] = []
        for node_content in nodes_content:
            for comp in node_content:
                if isinstance(comp, Comp.Plain):
                    text_lines.append(comp.text)
                elif isinstance(comp, Comp.Image):
                    parts.append(comp)
        if text_lines:
            parts.append(Comp.Plain("\n".join(text_lines)))

        if parts:
            yield event.chain_result(parts)

    async def _try_send_media(self, event: AstrMessageEvent, result: ParseResult):
        """单独发送视频 / 音频。图片已在文本节点中，不重复发送。"""
        for cont in result.contents:
            if not isinstance(cont, (VideoContent, AudioContent)):
                continue
            path = await cont.path_task.safe_get()
            if path is None:
                continue
            if isinstance(cont, VideoContent):
                yield event.chain_result([Comp.Video.fromFileSystem(str(path))])
            else:
                yield event.chain_result([Comp.Record(file=str(path))])

    # ==================== B站扫码登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_qr_login(self, event: AstrMessageEvent):
        """B站扫码登录，Cookie 会持久化保存"""
        sender_id = event.get_sender_id()
        if sender_id in self._bili_login_tasks and not self._bili_login_tasks[sender_id].done():
            yield event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")
            return

        try:
            if not self._bili_http_session:
                self._bili_http_session = aiohttp.ClientSession()

            async with self._bili_http_session.get(
                BILI_QR_GENERATE_URL, headers=self._bili_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()

            if data.get("code") != 0:
                yield event.plain_result(f"❌ 获取二维码失败: {data.get('message', '未知错误')}")
                return

            qrcode_url = data["data"]["url"]
            qrcode_key = data["data"]["qrcode_key"]
            if not qrcode_key:
                yield event.plain_result("❌ 获取 qrcode_key 失败")
                return

            try:
                qr_image = qrcode.make(qrcode_url)
            except Exception:
                yield event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库")
                return

            qr_path = self._bili_data_dir / f"qrcode_{sender_id}.png"
            qr_image.save(str(qr_path), "PNG")
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "📱 请使用 **B站App** 扫描上方二维码\n⏱️ 有效期约3分钟\n📋 扫码后请在手机上点击「确认登录」"
            )

            self._bili_login_tasks[sender_id] = asyncio.create_task(
                self._bili_poll_qr_login(sender_id, qrcode_key, qr_path)
            )
        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.plain_result(f"❌ 生成二维码失败: {e}")

    @filter.command("bili_check")
    async def bili_check_cookie(self, event: AstrMessageEvent):
        """检测B站Cookie是否有效"""
        if not self._bili_cookie:
            yield event.plain_result("⚠️ 尚未配置B站Cookie，请使用 /bili_login 扫码登录")
            return
        result = await self._bili_check_cookie_valid()
        if result["valid"]:
            yield event.plain_result(
                f"✅ B站Cookie有效\n用户: {result.get('username', '未知')}\nUID: {result.get('uid', 0)}"
            )
        else:
            yield event.plain_result(f"❌ B站Cookie失效\n错误: {result.get('error', '未知错误')}")

    # ---------- 内部方法 ----------

    @staticmethod
    def _bili_headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _bili_poll_qr_login(self, sender_id: str, qrcode_key: str, qr_path: Path):
        import time as _time

        start = _time.time()
        try:
            while _time.time() - start < QR_CODE_EXPIRE_TIME:
                if not self._bili_http_session or self._bili_http_session.closed:
                    break
                try:
                    async with self._bili_http_session.get(
                        BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key},
                        headers=self._bili_headers(), timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)
                if code == QR_CODE_EXPIRED:
                    break
                if code == QR_CODE_SUCCESS:
                    cookie_dict: dict[str, str] = {}
                    for header in set_cookie_headers:
                        part = header.split(";")[0].strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookie_dict[k.strip()] = v.strip()
                    if not cookie_dict:
                        break
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    self._bili_cookie = cookie_str
                    await self._bili_save_cookie(cookie_str)
                    self._bili_apply_cookie_to_parser(cookie_str)
                    result = await self._bili_check_cookie_valid()
                    if result["valid"]:
                        await self._bili_notify(
                            sender_id,
                            f"🎉 登录成功！\n👤 用户: {result.get('username')}\n🆔 UID: {result.get('uid', 0)}",
                        )
                    break
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("扫码轮询出错")
        finally:
            self._bili_login_tasks.pop(sender_id, None)
            if qr_path.exists():
                try:
                    qr_path.unlink()
                except Exception:
                    pass

    def _bili_apply_cookie_to_parser(self, cookie_str: str):
        parser = self.parsers.get("bilibili")
        if parser and hasattr(parser, "update_cookie"):
            parser.update_cookie(cookie_str)
            logger.info("B站 Cookie 已应用到解析器")

    async def _bili_check_cookie_valid(self) -> dict:
        if not self._bili_cookie or not self._bili_http_session:
            return {"valid": False, "error": "Cookie 为空或会话未初始化"}
        headers = {**self._bili_headers(), "Cookie": self._bili_cookie}
        try:
            async with self._bili_http_session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                u = data["data"]
                return {"valid": True, "username": u.get("uname", ""), "uid": u.get("mid", 0)}
            return {"valid": False, "error": data.get("message", "未知错误")}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    async def _bili_load_cookie(self):
        try:
            if self._bili_cookie_file.exists():
                saved = json.loads(self._bili_cookie_file.read_text(encoding="utf-8")).get("cookie", "")
                if saved:
                    self._bili_cookie = saved
                    logger.info("已从持久化文件加载B站 Cookie")
                    return
        except Exception as e:
            logger.warning(f"加载B站 Cookie 失败: {e}")
        if get_config().BILI_CK:
            self._bili_cookie = get_config().BILI_CK

    async def _bili_save_cookie(self, cookie_str: str):
        try:
            self._bili_cookie_file.write_text(
                json.dumps({"cookie": cookie_str}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存B站 Cookie 失败: {e}")

    async def _bili_notify(self, sender_id: str, message: str):
        try:
            umo = sender_id if ":" in sender_id else f"default:FriendMessage:{sender_id}"
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送消息给 {sender_id} 失败: {e}")

    # ==================== JSON 卡片工具 ====================

    @staticmethod
    def _has_json_component(event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for c in event.message_obj.message:
            if isinstance(c, Comp.Json):
                return True
            t = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
            if t and t != "reply" and "json" in str(t).lower():
                return True
        return False

    def _extract_links_from_event(self, event: AstrMessageEvent) -> list[str]:
        links: list[str] = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for c in event.message_obj.message:
                if isinstance(c, Comp.Json):
                    links.extend(self._extract_links_from_json(c.data))
                elif isinstance(c, Comp.Plain):
                    links.extend(URL_PATTERN.findall(c.text or ""))
                elif isinstance(c, dict) and "json" in str(c.get("type", "")).lower():
                    links.extend(self._extract_links_from_json(c.get("data", c)))
        links.extend(URL_PATTERN.findall(event.message_str or ""))
        return links

    @staticmethod
    def _extract_links_from_json(data) -> list[str]:
        links: list[str] = []
        try:
            if isinstance(data, str):
                data = json.loads(data)

            def search(obj):
                found = []
                if isinstance(obj, dict):
                    meta = obj.get("meta") or {}
                    if isinstance(meta, dict):
                        for dk in ("detail_1", "detail", "news", "music"):
                            d = meta.get(dk) or {}
                            if isinstance(d, dict):
                                for uk in ("qqdocurl", "url", "jumpUrl"):
                                    v = d.get(uk)
                                    if isinstance(v, str) and v:
                                        found.append(v)
                    for v in obj.values():
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            found.append(v)
                        elif isinstance(v, (dict, list)):
                            found.extend(search(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(search(item))
                return found

            links.extend(search(data))
        except Exception as e:
            logger.warning(f"解析 JSON 消息组件失败: {e}")
        return links

    # ==================== 管理命令 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_status")
    async def denia_status(self, event: AstrMessageEvent):
        """查看达妮娅分享的运行状态"""
        from . import __version__

        platforms = "、".join(self.parsers) if self.parsers else "（无）"
        yield event.plain_result(
            f"达妮娅分享 v{__version__}\n"
            f"已启用平台：{platforms}\n"
            f"卡片渲染：{'开' if self._renderer.enabled else '关'}\n"
            f"B站 Cookie：{'已配置' if self._bili_cookie else '未配置'}"
        )

    async def terminate(self):
        if self._cache_cleanup_task is not None and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
        await self.downloader.aclose()

        for task in list(self._bili_login_tasks.values()):
            if not task.done():
                task.cancel()
        self._bili_login_tasks.clear()

        if self._bili_http_session and not self._bili_http_session.closed:
            await self._bili_http_session.close()
