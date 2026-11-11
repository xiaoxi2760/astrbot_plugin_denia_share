"""三路输出投递 / 富媒体发送 / 宿主路径映射（组合服务）。

从 ``main.py`` 抽出的职责：

- ``deliver``：文本 / 卡片 / 富媒体三路编排（任意组合合法，含 ``RENDER_ENABLED``
  总开关；一路失败**不外溢**到其它已选中的路，也绝不补发未勾选的输出）；
- 富媒体报道构建与发送：``try_send_media`` / ``send_media_item`` /
  ``iter_media_components`` / ``try_send_audio`` / ``build_audio_component``
  （语音条、音频文件、视频、图片、图集、合并转发聚合、媒体中转、voice 文件兜底）；
- 合并转发节点构建与旧旁路输出（``build_nodes_result`` / ``forward_node_nickname`` /
  ``send_plain_output`` / ``build_output``）；
- 宿主 ``platform_settings.path_mapping``（``path_mapping_rules`` /
  ``apply_audio_path_mapping``），主动发送前按宿主逻辑映射 ``Comp.File`` 路径；
- 音频显示名与兜底条目（``audio_display_name`` / ``AudioFallbackItem``）；
- 送达未知标记 ``_DELIVERY_UNCERTAIN`` —— 与 ``main`` 共用**同一个**
  ``ContextVar`` 对象（main 从本模块导入，``_process_url`` 里读取/复位）。

对外契约保持不变：插件实例仍暴露 ``_deliver`` / ``_try_send_media`` /
``_try_send_audio`` 等**薄委托**方法（``core/music/controller.py`` 与既有测试
继续按插件方法调用）；本服务组合插件对象，集中实现发送逻辑。

不 import ``main``，避免循环依赖；稳定依赖 ``core/data`` / ``core/base_parser`` /
``core/relay`` / ``core/config``。
"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
import astrbot.api.message_components as Comp

from .base_parser import apply_result_image_limit
from .config import get_config
from .data import (
    AudioContent,
    ImageContent,
    ParseResult,
    VideoContent,
    build_concise_text,
)
from .relay import register_file

try:
    from aiocqhttp.exceptions import NetworkError as _OneBotNetworkError
except ImportError:
    _UNCERTAIN_SEND_ERRORS = (TimeoutError, ConnectionError)
else:
    # OneBot converts an API receipt timeout to NetworkError.
    _UNCERTAIN_SEND_ERRORS = (TimeoutError, ConnectionError, _OneBotNetworkError)

#: 本次媒体投递是否落在「送达未知」（合并转发回执超时/断连）。
#: 见 ``_process_url``：未知时保留去重窗口但不记成功历史。
#: **所有路径必须共用这一个对象** —— main 也从本模块导入它。
_DELIVERY_UNCERTAIN = ContextVar("denia_delivery_uncertain", default=False)

#: 音频文件名里需要剔除的字符（路径分隔符 + 各家文件系统的保留字符）。
#: Windows 那几个（<>:"/\|?*）在聊天软件里同样不该出现 —— 收件人一存盘就报错。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: 兜底文件名：标题被清洗成空串（全是表情、全是标点）时用它。
_FALLBACK_AUDIO_NAME = "音频"


def audio_display_name(result: ParseResult, path: Path) -> str:
    """给「音频文件」模式起一个人类可读的名字，**后缀按真实文件走**。

    下载侧的缓存名是 URL 哈希（``<md5>.m4s``），直接当附件名发出去，用户在
    QQ 里看到的就是一串十六进制。这里换成「标题 + 真实后缀」，但只换**主名**：
    后缀必须来自磁盘上的真实文件，不能拿标题去拼 —— 那是编造格式。
    """
    suffix = path.suffix  # 含点；认不出就是空串，那就不加后缀，绝不瞎补 .mp3
    title = (getattr(result, "title", "") or "").strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", title).strip(" .")
    if not cleaned:
        cleaned = _FALLBACK_AUDIO_NAME
    # 留出后缀的位置，避免标题太长时把扩展名挤掉。
    max_stem = max(1, 120 - len(suffix))
    return f"{cleaned[:max_stem]}{suffix}"


@dataclass
class AudioFallbackItem:
    """语音发送失败后的文件兜底载体：沿用原结果与已落盘路径。"""

    result: ParseResult
    path: Path


class DeliveryService:
    """三路投递 + 富媒体发送 + 路径映射的组合服务。

    组合进 ``DeniaSharePlugin``：服务侧的插件状态一律经 ``self._plugin``
    （运行期 getattr，测试桩只挂必需属性也能跑）。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    @staticmethod
    def is_onebot(event: AstrMessageEvent) -> bool:
        """合并转发（Comp.Nodes）是 OneBot v11 独有特性。"""
        try:
            return "aiocqhttp" in event.get_platform_name().lower()
        except Exception:
            return False

    async def deliver(
        self, event: AstrMessageEvent, result: ParseResult, cache_key: str, *, on_delivered=None, diagnostic=None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """把已得到的 ParseResult 按用户选中的输出模式发出去。

        三个独立分支（文本 / 卡片 / 原始媒体），互不排斥、互不代偿：
        某一支失败**只会让那一支没有产物**，绝不自动补发用户没勾的那几路。

        链接解析与 /pixiv 搜索共用这条输出链路。
        """
        # 全局图片上限的唯一收口点：在**任何下载之前**统一截断图集图片
        # （只截 ImageContent，视频/音频/封面/头像不动，也不 resolve 媒体）。
        # 放在这里而不是 create_images：Pixiv 等平台是逐张 create_image 的，
        # 只改 create_images 会漏掉那条路径。重复交付（缓存命中重发）幂等。
        apply_result_image_limit(result)
        pconfig = get_config()
        modes = pconfig.output_modes()
        text_on = bool(modes["text"])
        card_on = bool(modes["card_effective"])
        media_on = bool(modes["media"])
        def stage(name, state, reason=''):
            if diagnostic is not None:
                diagnostic.stage(name, state, reason)

        # 全关时本就不会走到这里（入口已短路），留一道兜底防止将来新增调用方
        if not (text_on or card_on or media_on):
            logger.info("[denia_share] 三路输出全关，本次不发送任何内容")
            return

        # 缺料审计只覆盖**用户真的要的**媒体：媒体一路开着才审计全部内容，
        # 仅卡片时只审计渲染真正会去取的图（封面/头像/图集，且跳过派生任务）。
        # 审计会 get() 这些任务，也就是**这一步才是触发下载的地方** —— 全关时
        # 一次都不会被调用，一个字节都不下载。
        if media_on or card_on:
            stage('media', 'running')
            try:
                missing = await self.audit_for_modes(result, media_on=media_on)
                limited = bool(result.extra.get('limit_warnings'))
                stage('media', 'error' if missing else 'skipped' if limited else 'ok',
                    'media_missing' if missing else 'limit' if limited else '')
            except Exception:
                stage('media', 'error', 'media_missing')
                missing = {}
                logger.warning("[denia_share] 媒体下载结算失败", exc_info=True)
            if missing:
                logger.warning(
                    f"[denia_share] 本次解析有媒体未落盘: "
                    f"{'、'.join(f'{k}×{v}' for k, v in missing.items())}"
                )
        else:
            stage('media', 'skipped', 'skipped')

        # ---- 卡片 ----
        # 渲染/发送都可能抛（Pillow 缺字体、协议端拒绝、磁盘满）。它们**只能**
        # 让卡片这一路没有产物，绝不能把已经选中的文本与媒体一起带崩。
        # 同样地，这里也**不会**因为卡片失败就改发用户没勾的那几路。
        render_path: Path | None = None
        if card_on:
            stage('render', 'running')
            try:
                render_path = await self._plugin._renderer.render(
                    result, cache_key=cache_key,
                    existing=self._plugin._render_cache.get(cache_key),
                )
            except Exception:
                render_path = None
                logger.warning("[denia_share] 卡片渲染失败，继续发送已勾选的其他输出", exc_info=True)
            if render_path is not None:
                self._plugin._remember_render(cache_key, render_path)
            stage('render', 'ok' if render_path is not None else 'error', '' if render_path is not None else 'render_failed')
        else:
            stage('render', 'skipped', 'skipped')

        # ---- 文本 ----
        # 渲染失败**不**回退成文本：文本只在 text_on 时才发。反过来也一样。
        if text_on:
            async for r in self.send_text(event, result):
                yield r

        if render_path is not None:
            # 卡片发不出去（协议端拒绝/找不到会话）也不能连累已勾选的媒体
            try:
                if await self.send_image(event, render_path):
                    if on_delivered is not None:
                        on_delivered()
                else:
                    stage('send', 'error', 'send_failed')
            except Exception:
                stage('send', 'error', 'send_failed')
                logger.warning("[denia_share] 卡片发送失败，继续发送已勾选的其他输出", exc_info=True)

        # ---- 原始媒体（图片/视频/动图/音频） ----
        if media_on:
            async for r in self.try_send_media(
                event, result, on_delivered=on_delivered,
                on_failed=lambda: stage('send', 'error', 'send_failed'),
            ):
                yield r

    async def audit_for_modes(
        self, result: ParseResult, *, media_on: bool
    ) -> dict[str, int]:
        """按输出模式结算媒体下载，返回失败统计。

        - **媒体一路开着**：审计全部内容（与旧行为一致），提示会进
          ``extra["limit_warnings"]``，由已选中的文本/卡片如实展示。
        - **仅卡片**：只审计渲染会去取的图，且**跳过 ``needs_media`` 的派生任务**
          （用 ffmpeg 抽帧那种）。否则为了给卡片补一张封面就得先下整段视频，
          与「仅卡片只要封面和头像」的意图相反。

        这里走 :meth:`ParseResult.audit_missing_media` 之外的精简路径时，
        仍遵守它「幂等、只撤自己写的那条」的所有权约定。
        """
        if media_on:
            return await result.audit_missing_media()

        # 仅卡片：把派生任务（needs_media）从审计范围里摘掉后再走同一条通道
        return await result.audit_missing_media(img_only=True)

    async def send_image(self, event: AstrMessageEvent, path: Path) -> bool:
        """绕开结果装饰发送卡片；QQ 官方事件保留被动回复上下文。"""
        try:
            try:
                platform_name = str(event.get_platform_name() or "").lower()
            except Exception:
                platform_name = ""
            if platform_name in {"qq_official", "qq_official_webhook"}:
                # 官方群的主动消息需要额外开启权限；使用当前事件携带 msg_id，
                # 让宿主处理被动回复与过期回退。send() 正常返回 None 也表示成功。
                sent = await event.send(MessageChain().file_image(str(path)))
                if sent is False:
                    logger.warning(f"解析卡片事件发送返回失败: {path.name}")
                    return False
                return True
            sent = await self._plugin.context.send_message(
                event.unified_msg_origin, MessageChain().file_image(str(path)),
            )
            if not sent:
                logger.warning(f"解析卡片主动发送未找到匹配平台会话: {path.name}")
            return bool(sent)
        except Exception as e:
            logger.warning(f"解析卡片发送异常: {e}")
            return False

    @staticmethod
    def forward_node_nickname() -> str:
        """合并转发节点的显示昵称；只改节点显示名，不改实际发送账号。

        配置为空白（或读取失败 / 旧测试桩未初始化）时回退默认「希望解析器」。
        """
        try:
            value = str(get_config().FORWARD_NODE_NICKNAME or "").strip()
            if value:
                return value
        except Exception:
            pass
        return "希望解析器"

    @staticmethod
    async def build_nodes_result(event: AstrMessageEvent, header: str, items: list[list]):
        sender_name = DeliveryService.forward_node_nickname()
        sender_id = event.get_self_id()
        nodes = Comp.Nodes([])

        def _node(content):
            # AstrBot 4.x 的 Node.__init__(content, **_) 会把 uin/name 吞进 **_ 丢弃，
            # 必须构造后赋值属性，否则合并转发里昵称与 QQ 号全空
            node = Comp.Node(content=content)
            node.uin = sender_id
            node.name = sender_name
            return node

        if header:
            nodes.nodes.append(_node([Comp.Plain(header)]))
        for item in items:
            nodes.nodes.append(_node(item))
        return event.chain_result([nodes])

    async def build_output(self, result: ParseResult) -> tuple[str, list[list]]:
        """统一构建 (标题头, 内容节点列表)。

        **已不再用于发送**：三路输出模式里文本走 :meth:`send_text`（一条普通
        消息，不含长简介/AI 总结/合并转发），图片归富媒体一路。这里保留是为了
        兼容仍在引用它的旁路（如调试与既有测试），不要再在发送链路里用它。
        """
        header = f"希望解析器 | {result.platform.display_name} - {result.content_type}"
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
            # exists() 兜底：PathTask 记着首次下载的路径，之后一律直接返回、
            # 不碰文件系统。文件被缓存清理任务删掉时，这里会把一个不存在的
            # 路径交给协议端（图片则渲染成裂图）。视频那条链路已有同样检查。
            if path and path.exists():
                nodes.append([Comp.Image.fromFileSystem(str(path))])

        for graphic in result.graphics:
            if isinstance(graphic, ImageContent):
                path = await graphic.path_task.safe_get()
                if path and path.exists():
                    nodes.append([Comp.Image.fromFileSystem(str(path))])
            elif isinstance(graphic, str):
                nodes.append([Comp.Plain(graphic)])

        return header, nodes

    async def send_plain_output(self, event: AstrMessageEvent, header: str, nodes_content: list[list]):
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

    async def send_text(self, event: AstrMessageEvent, result: ParseResult):
        """发送**一条**普通文本消息（标题 + 干净地址 + 简洁播放信息 + 必要提示）。

        刻意**不**包含：``result.text`` 长简介、``graphics`` 正文、B站
        ``extra["info"]`` 里的 AI 总结，也不走 OneBot 合并转发 —— 一条普通
        ``Plain`` 消息，客户端里就是一条干净的文本。
        """
        lines = build_concise_text(result)
        if not lines:
            logger.info("[denia_share] 精简文本为空，跳过文本发送")
            return
        yield event.chain_result([Comp.Plain("\n".join(lines))])

    async def try_send_media(
        self, event: AstrMessageEvent, result: ParseResult, *, on_delivered=None,
        on_failed=None,
    ):
        """按聚合配置发送富媒体，语音和文件保持独立发送。"""
        pconfig = get_config()
        # 每条媒体链路开始前先清标记：上一次「送达未知」不能继承到下一条链接。
        _DELIVERY_UNCERTAIN.set(False)
        try:
            official = str(event.get_platform_name() or "").lower() in {
                "qq_official", "qq_official_webhook",
            }
        except Exception:
            official = False
        if official and pconfig.QQ_OFFICIAL_MEDIA_MODE == "compact":
            async for output in self.send_official_compact(
                event, result, on_delivered=on_delivered, on_failed=on_failed,
            ):
                yield output
            return
        if not self.is_onebot(event) or pconfig.AGGREGATION_MODE == "none":
            async for parts in self.iter_media_components(result):
                async for output in self.send_media_item(
                    event, parts, on_delivered=on_delivered, on_failed=on_failed
                ):
                    yield output
            return

        messages = [parts async for parts in self.iter_media_components(result)]
        # 只把图片、视频送进转发，避免一段语音或文件使整份图集发送失败。
        eligible = [
            parts for parts in messages
            if not isinstance(parts, AudioFallbackItem)
            and all(isinstance(part, (Comp.Image, Comp.Video)) for part in parts)
        ]
        image_count = sum(isinstance(part, Comp.Image) for parts in eligible for part in parts)
        video_count = sum(isinstance(part, Comp.Video) for parts in eligible for part in parts)
        if not eligible or not pconfig.should_aggregate_nodes(image_count, video_count, len(eligible)):
            for parts in messages:
                async for output in self.send_media_item(
                    event, parts, on_delivered=on_delivered, on_failed=on_failed
                ):
                    yield output
            return

        try:
            forward = await self.build_nodes_result(event, "", eligible)
            # 主动发送才能捕获协议端拒绝；yield 的发送异常在宿主管线中发生。
            sent = await event.send(forward)
            if sent is False:
                raise RuntimeError("合并转发未发送成功")
        except _UNCERTAIN_SEND_ERRORS:
            # 回执丢失时转发可能已经送达，不能自动重放整套图片/视频。
            # 标记「送达未知」而不是「失败」：调用方据此保留去重窗口、
            # 但不记成功历史（见 _process_url 的对应分支）。
            _DELIVERY_UNCERTAIN.set(True)
            logger.warning("[denia_share] 合并转发送达状态未知，不自动重发图片视频，继续发送独立音频", exc_info=True)
            for parts in messages:
                if isinstance(parts, AudioFallbackItem) or not all(
                    isinstance(part, (Comp.Image, Comp.Video)) for part in parts
                ):
                    async for output in self.send_media_item(
                        event, parts, on_delivered=on_delivered, on_failed=on_failed
                    ):
                        yield output
            return
        except Exception:
            logger.warning("[denia_share] 合并转发失败，改为逐条发送已选媒体", exc_info=True)
            for parts in messages:
                async for output in self.send_media_item(
                    event, parts, on_delivered=on_delivered, on_failed=on_failed
                ):
                    yield output
            return

        if on_delivered is not None:
            on_delivered()
        for parts in messages:
            if isinstance(parts, AudioFallbackItem) or not all(
                isinstance(part, (Comp.Image, Comp.Video)) for part in parts
            ):
                async for output in self.send_media_item(
                    event, parts, on_delivered=on_delivered, on_failed=on_failed
                ):
                    yield output

    async def send_media_item(
        self, event: AstrMessageEvent, item, *, on_delivered=None, on_failed=None
    ):
        """仅失败兜底模式主动发送，其他媒体仍交给宿主管线。"""
        if not isinstance(item, AudioFallbackItem):
            yield event.chain_result(item)
            return
        pconfig = get_config()
        sent = await self.try_send_audio(
            event, item.result, item.path, pconfig.MEDIA_RELAY_ENABLED,
            pconfig.MEDIA_RELAY_CALLBACK_URL, pconfig.MEDIA_RELAY_TTL,
        )
        if sent:
            if on_delivered is not None:
                on_delivered()
        elif on_failed is not None:
            on_failed()

    @staticmethod
    def source_url(result: ParseResult) -> str:
        """Accept an actual HTTP(S) source URL, never a local/data/file URI."""
        value = str(result.url or "").strip()
        if not value or len(value) > 8192 or "\\" in value or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
        ):
            return ""
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and not (
                parsed.username or parsed.password
            ):
                parsed.port  # Validate malformed port values too.
                return value
        except ValueError:
            pass
        return ""

    async def send_official_compact(
        self, event, result: ParseResult, *, on_delivered=None, on_failed=None,
    ):
        """Official API compact delivery; no Nodes and no claimed original delivery."""
        source = self.source_url(result)
        results = [result]
        if result.repost is not None:
            results.append(result.repost)
        images = [image for item in results for image in (
            list(item.img_contents) + [g for g in item.graphics if isinstance(g, ImageContent)]
        )]
        videos = sum(isinstance(c, VideoContent) for item in results for c in item.contents)
        compact_images = bool(source and len(images) > 1)
        compact_videos = bool(source and videos > 1)
        notices = []
        pages = []
        if compact_images:
            try:
                from .media_collage import build_previews
                paths = []
                for image in images:
                    path = await image.path_task.safe_get()
                    if path is None or not path.is_file():
                        raise ValueError("preview source image is missing")
                    paths.append(path)
                cache_root = Path(self._plugin.cache_dir) / "qq_previews"
                pages, represented, animated = await asyncio.to_thread(build_previews, paths, cache_root)
                notices.append(f"图片共 {len(images)} 张，已生成 {represented} 张静态图片的缩略预览（非原图）")
                if animated:
                    notices.append(f"其中 {animated} 张动态图片未静态化发送")
                if represented + animated < len(images):
                    notices.append("其余图片未在预览中展示")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[denia_share] 官方图片预览生成失败，保留源链接，不逐张补发", exc_info=True)
                notices.append(f"共 {len(images)} 张图片，预览生成失败，未逐张发送")
        if compact_videos:
            notices.append(f"共 {videos} 个视频，为避免刷屏未逐个发送")
        if notices:
            caption = Comp.Plain(
                "；".join(notices) + "。查看原图、动态图或完整视频请打开源链接：\n" + source
            )
            if pages:
                for index, page in enumerate(pages):
                    parts = [Comp.Image.fromFileSystem(str(page))]
                    if index == len(pages) - 1:
                        parts.append(caption)
                    yield event.chain_result(parts)
            else:
                yield event.chain_result([caption])
        async for parts in self.iter_media_components(
            result, skip_images=compact_images, skip_videos=compact_videos,
        ):
            async for output in self.send_media_item(
                event, parts, on_delivered=on_delivered, on_failed=on_failed,
            ):
                yield output

    async def iter_media_components(
        self, result: ParseResult, *, skip_images: bool = False, skip_videos: bool = False,
    ):
        """构建原始媒体组件，复用下载、缓存和中转规则。

        旧实现里图片是塞进文本节点的（``build_output``），而精简文本按设计
        **只含标题/地址/播放信息**，所以图片必须在这里显式发出去 —— 否则
        关掉文本就等于把图集弄丢了。卡片图片走 ``send_image``，与这里无关。

        **视频为什么需要特殊处理**：AstrBot 把 ``Video`` 段的 ``file:///...``
        原样交给协议端（图片 / 语音会先转成 base64，所以没这个问题），
        而那个路径是 astrbot 容器内的路径。协议端在另一个容器里读不到，
        视频就发不出去。

        所以开了「媒体中转」就先把文件注册成 ``{回调地址}/api/file/<token>``
        用 URL 发送；没开、注册失败、或地址不合法时，回退成原来的本地文件发送。
        """
        pconfig = get_config()
        relay_enabled = pconfig.MEDIA_RELAY_ENABLED
        callback_base = pconfig.MEDIA_RELAY_CALLBACK_URL
        ttl = pconfig.MEDIA_RELAY_TTL

        # 图片在 contents 里（contents/graphics 混排的平台两种都有），逐个发
        for image in result.img_contents:
            if skip_images:
                continue
            path = await image.path_task.safe_get()
            if path is None or not path.exists():
                continue
            yield [Comp.Image.fromFileSystem(str(path))]

        for graphic in result.graphics:
            if skip_images or not isinstance(graphic, ImageContent):
                continue
            path = await graphic.path_task.safe_get()
            if path is None or not path.exists():
                continue
            yield [Comp.Image.fromFileSystem(str(path))]

        # 转发的原帖图片同样归富媒体（微博/NGA 那种 repost 里有图集的情况）。
        # 递归只走一层：repost 不会自己再套 repost。
        if result.repost is not None:
            async for parts in self.iter_media_components(
                result.repost, skip_images=skip_images, skip_videos=skip_videos,
            ):
                yield parts

        for cont in result.contents:
            if not isinstance(cont, (VideoContent, AudioContent)):
                continue
            if skip_videos and isinstance(cont, VideoContent):
                continue
            path = await cont.path_task.safe_get()
            if path is None:
                continue
            # 双保险：正常情况下主流程已经拦掉了「缓存命中但文件已清理」的情况
            # （见 result_cache 的 _cache_media_gone）。这里再挡一次 —— 清理任务
            # 可能正好在 _get_cached_result 检查之后、这里之前把文件删了。
            if not path.exists():
                logger.warning(
                    f"[denia_share] 媒体文件已不存在，跳过发送: {path.name}"
                )
                continue

            if isinstance(cont, VideoContent):
                url = None
                if relay_enabled:
                    url = await register_file(path, callback_base, ttl)
                    if url is None:
                        logger.info(
                            f"[denia_share] 视频中转不可用，回退本地文件发送: {path.name}"
                        )
                if url:
                    yield [Comp.Video.fromURL(url)]
                else:
                    yield [Comp.Video.fromFileSystem(str(path))]
            elif pconfig.AUDIO_SEND_MODE == "voice":
                yield AudioFallbackItem(result, path)
            else:
                yield [await self.build_audio_component(
                    result, path, relay_enabled, callback_base, ttl
                )]

    async def try_send_audio(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
    ) -> bool:
        """优先发送语音，接口失败时仅尝试一次文件兜底。

        这里的发送是主动 ``event.send``，不经过宿主 RespondStage，所以
        RespondStage 的文件路径映射不会自动生效，需要在这条路径上自行补上。
        """
        for mode in ("voice", "file"):
            try:
                component = await self.build_audio_component(
                    result, path, relay_enabled, callback_base, ttl, mode=mode
                )
                self.apply_audio_path_mapping(event, component)
                sent = await event.send(event.chain_result([component]))
                if sent is False:
                    raise RuntimeError("音频发送接口返回失败")
            except asyncio.CancelledError:
                raise
            except Exception:
                if mode == "voice":
                    logger.warning(
                        "[denia_share] 语音发送失败，尝试音频文件兜底", exc_info=True
                    )
                else:
                    logger.warning("[denia_share] 音频文件兜底发送失败", exc_info=True)
            else:
                logger.debug(f"[denia_share] 音频发送接口已接受: mode={mode}")
                return True
        return False

    def path_mapping_rules(self, event: AstrMessageEvent) -> list:
        """读取当前会话在宿主配置里的 ``platform_settings.path_mapping``。"""
        get_host_config = getattr(self._plugin.context, "get_config", None)
        if not callable(get_host_config):
            return []
        host_config = get_host_config(event.unified_msg_origin)
        if host_config is None:
            return []
        settings = host_config.get("platform_settings") or {}
        rules = settings.get("path_mapping") or []
        if not rules:
            return []
        if not isinstance(rules, (list, tuple)):
            raise ValueError(
                f"path_mapping 应为列表，实际为 {type(rules).__name__}"
            )
        return list(rules)

    def apply_audio_path_mapping(self, event: AstrMessageEvent, component) -> None:
        """主动发送前按宿主逻辑映射本地 ``Comp.File`` 的路径。

        URL 中转与 Record 都不需要映射；没有规则时保留原路径。映射本身
        失败时直接抛出，让本次发送按失败处理 —— 不能把协议端读不到的
        容器内路径当成发送成功。
        """
        file_cls = getattr(Comp, "File", None)
        if file_cls is None or not isinstance(component, file_cls):
            return
        if getattr(component, "url", ""):
            return
        local_path = getattr(component, "file", "")
        if not local_path:
            return
        rules = self.path_mapping_rules(event)
        if not rules:
            return
        # 延迟导入：离线单测的 astrbot 是轻量桩，宿主没装全时不能让插件
        # 顶层 import 失败；只有真的要映射时才需要宿主工具。
        try:
            from astrbot.core.utils.path_util import path_Mapping
        except ImportError as exc:
            raise RuntimeError("宿主缺少文件路径映射工具") from exc
        mapped = path_Mapping(rules, str(local_path))
        if not isinstance(mapped, str) or not mapped:
            raise RuntimeError("宿主文件路径映射返回了空路径")
        component.file = mapped

    async def build_audio_component(
        self,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
        *,
        mode: str | None = None,
    ):
        """构建音频组件；显式 mode 用于语音失败后的文件兜底。

        Record 由宿主转 base64；File 沿用媒体中转或共享本地路径，
        因此文件发送仍需要协议端能够访问音频。
        """
        if mode is None:
            mode = get_config().AUDIO_SEND_MODE
        if mode == "file":
            name = audio_display_name(result, path)
            url = None
            if relay_enabled:
                url = await register_file(path, callback_base, ttl)
                if url is None:
                    logger.info(
                        f"[denia_share] 音频中转不可用，回退本地路径发送: {path.name}"
                    )
            if url:
                return Comp.File(name=name, url=url)
            return Comp.File(name=name, file=str(path))

        # 语音条：沿用既有 Record 路径（AstrBot 侧转 base64，无需中转）。
        return Comp.Record(file=str(path))
