"""确认下载控制器（阶段3：confirm 模式的服务端状态机与确认流程）。

负责「先展示封面卡片 + 媒体信息 + 确认短编号，用户确认后才下载源媒体并立即
按既有投递方式发送」的全部服务端状态：

- 待确认条目（:class:`ConfirmOffer`）的登记 / 复用 / 过期 / 容量控制；
- 按「机器人实例 + 平台 + 物理会话」隔离，同一物理群内不同成员都可确认
  同一条 offer（不被 ``unified_msg_origin`` 里的发送者隔离误挡）；
- 原子认领 ``pending -> downloading``，防重复下载 / 重复发送；
- 四项确认权限（任何人 / 原分享者 / QQ群主管理员 / AstrBot管理员）OR，全关拒绝；
- 确认时**重新解析**规范分享链接以刷新签名地址 / 权限 / 描述，并校验身份一致；
- 时长 / 大小（含未知长度流式）仍按既有配置与 2GB 硬上限校验，明确标出已知超限；
- 预览用**复制的 ParseResult 投影**，不污染原 result、不触发源媒体下载、不抽帧。

对外媒体契约**唯一**依赖 ``core/media_probe.describe_result(result) -> dict``
（``{quality, size_bytes, estimated, media_count, unavailable_reason}``），以及
``MediaContent.info_loader``（kw-only 可选）。两者均由另一成员先建立契约，
本模块在函数内**延迟 import**，另一成员产出缺位时优雅降级为「未知 / 计数」，
绝不因契约未落地而让确认流程无法启动。

**刻意不做**：任何无限制策略。日常配置上限与 2GB 硬上限全部保留，
多文件 / 估算大小以实际下载器逐文件与最终合并检查为准，这里只做单文件且
非估算的已知大小提前精确拒绝。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from astrbot.api import logger

from .config import get_config
from .constants import is_custom_platform
from .data import (
    AudioContent,
    ImageContent,
    ParseResult,
    VideoContent,
    build_concise_text,
)
from .exception import IgnoreException
from .task import PathTask
from .delivery import _DELIVERY_UNCERTAIN

#: 每个物理会话最多同时保留的待确认条目（容量上限，超出时拒绝新条目并提示）。
MAX_PENDING_PER_SCOPE = 8

#: 全局待确认条目总容量：防止大量不同会话把内存与后端配额撑爆。
MAX_PENDING_GLOBAL = 128
MAX_STORED_OFFERS = 512


def _now() -> float:
    """待确认状态统一使用单调时钟（不受系统时间回拨影响）。"""
    return time.monotonic()

#: 已认领（downloading）条目在 TTL 之外再多滞留的宽限秒数；超过即视为陈旧失败
#: （覆盖「确认协程异常退出后状态卡死」的崩溃恢复场景）。
_STALE_DOWNLOADING_GRACE = 600

#: QQ 群信息查询（``AiocqhttpMessageEvent.get_group()``）的超时预算；超时一律拒绝。
QQ_GROUP_LOOKUP_TIMEOUT = 5.0

#: 描述字典里**允许**控制器读取的键白名单：媒体描述可能由另一成员聚合而来，
#: 控制器只依赖这份公开契约，绝不把 URL / 凭据 / 路径任务闭包带进卡片或日志。
_SAFE_DESC_KEYS = ("quality", "size_bytes", "estimated", "media_count", "unavailable_reason")


class OfferReparseError(RuntimeError):
    """确认时重新解析失败（链接失效 / 解析器异常等），不是内容变化。"""


class _PreviewCardInfo:
    def __init__(self, source, preview):
        self.source, self.preview = source, preview

    async def load(self, include_ai=False):
        await self.source.card_info_loader.load(include_ai=include_ai)
        for key in ("online", "ai_summary", "ai_summary_status"):
            if key in self.source.extra:
                self.preview.extra[key] = self.source.extra[key]


@dataclass(slots=True)
class ConfirmOffer:
    """一条待确认分享。

    ``result`` 是 offer 创建时刻的**原始解析结果**引用，仅作「确认前的预览
    信息」来源；真正下载 / 投递用的是确认时重新解析出的新结果。
    """

    id: str
    scope: str
    platform: str
    url: str
    cache_key: str
    reuse_key: str
    sender_id: str
    identity: str
    title: str
    description: dict[str, Any]
    result: Any
    created_at: float
    expires_at: float
    state: str = "pending"          # pending -> downloading -> done | failed
    history_id: str | None = None
    preview_path: str | None = None
    note: str = ""
    limits: list[str] = field(default_factory=list)
    finalized_at: float | None = None


class ConfirmController:
    """待确认状态与确认流程的服务端；组合进 ``DeniaSharePlugin``。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        #: offer id -> ConfirmOffer
        self._offers: dict[str, ConfirmOffer] = {}
        #: scope -> [offer id]，会话隔离的索引
        self._scope_index: dict[str, list[str]] = {}
        #: 正在执行的确认协程；close() 时取消并等待，避免关停后仍在下载/发送。
        self._active: set[asyncio.Task] = set()
        self._closed = False
        self._downloading: dict[str, asyncio.Task] = {}
        #: 最近一次配置指纹（只用于判断哪些变更真的会让 pending 失效）。
        self._config_snapshot: dict[str, Any] | None = None
        self._capacity_reason = ""

    # ==================== 配置读取（另一成员在 config.py 接线；缺失即默认） ====================

    def mode(self) -> str:
        """当前媒体下载模式：``auto`` / ``confirm``；默认 auto（零行为变化）。"""
        try:
            value = str(getattr(get_config(), "MEDIA_DOWNLOAD_MODE", "auto") or "auto").strip().lower()
        except Exception:
            return "auto"
        return value if value in ("auto", "confirm") else "auto"

    def _bool_cfg(self, name: str, default: bool) -> bool:
        try:
            return bool(getattr(get_config(), name, default))
        except Exception:
            return default

    def allow_anyone(self) -> bool:
        return self._bool_cfg("CONFIRM_ALLOW_ANYONE", False)

    def allow_sender(self) -> bool:
        return self._bool_cfg("CONFIRM_ALLOW_SENDER", True)

    def allow_qq_admin(self) -> bool:
        return self._bool_cfg("CONFIRM_ALLOW_QQ_ADMIN", False)

    def allow_bot_admin(self) -> bool:
        return self._bool_cfg("CONFIRM_ALLOW_BOT_ADMIN", False)

    def ttl_seconds(self) -> int:
        """待确认 TTL；读到脏值回退 300，并钳位到 30..1800。"""
        try:
            raw = int(getattr(get_config(), "CONFIRM_DOWNLOAD_TTL_SECONDS", 300))
        except (TypeError, ValueError):
            raw = 300
        return max(30, min(1800, raw))

    # ==================== 会话与权限 ====================

    @staticmethod
    def _event_attr(event: Any, name: str, default: Any = None) -> Any:
        getter = getattr(event, name, None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return default
        return default

    def scope_of(self, event: Any) -> str | None:
        """物理会话隔离键；任一关键身份缺失都返回 ``None``（fail closed）。

        群聊：``platform 实例ID + self_id + message_type + group_id``（同群成员
        一致，不会被发送者隔离误挡）；私聊：同样四段，用物理会话 id。
        """
        platform_id = str(self._event_attr(event, "get_platform_id") or "").strip()
        self_id = str(self._event_attr(event, "get_self_id") or "").strip()
        group_id = str(self._event_attr(event, "get_group_id") or "").strip()
        mt = self._event_attr(event, "get_message_type")
        message_type = str(getattr(mt, "value", mt) or "").strip()
        # self_id 是机器人实例身份的一部分：缺了就会把两个 bot 合成一个 scope。
        if not platform_id or not self_id or not message_type:
            return None
        if group_id:
            return f"p:{platform_id}|s:{self_id}|t:{message_type}|g:{group_id}"
        try:
            private = bool(event.is_private_chat())
        except Exception:
            private = False
        if not private:
            # 群消息却拿不到群号：不能退回会话 id（可能含 sender）而合并 unknown。
            return None
        session = str(self._event_attr(event, "get_session_id") or "").strip()
        if not session:
            return None
        return f"p:{platform_id}|s:{self_id}|t:{message_type}|u:{session}"

    def sender_of(self, event: Any) -> str:
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    @property
    def closed(self) -> bool:
        return self._closed

    # ==================== 结果判定 ====================

    def _walk(self, result: ParseResult):
        seen: set[int] = set()

        def _iter(node: ParseResult | None):
            if node is None or id(node) in seen:
                return
            seen.add(id(node))
            yield node
            repost = getattr(node, "repost", None)
            if repost is not None:
                yield from _iter(repost)

        yield from _iter(result)

    def has_media(self, result: ParseResult) -> bool:
        """这份结果里有没有「需要确认后才下载」的源媒体。"""
        for node in self._walk(result):
            if node.contents:
                return True
            if any(isinstance(g, ImageContent) for g in node.graphics):
                return True
        return False

    def has_inflight_media(self, result: ParseResult) -> bool:
        """是否有源媒体在 offer 建立前就已开始下载（急式解析器 / 复用中的任务）。

        确认模式承诺「确认前不下载源媒体」。这里一旦为 ``True`` 就无法兑现，
        调用方必须**明确拒绝本次确认并提示**；禁止静默回退自动下载。
        应在生成预览投影**之前**调用（预览本身会合法地取封面）。
        """
        for node in self._walk(result):
            tasks = [c.path_task for c in node.contents]
            tasks.extend(
                g.path_task for g in node.graphics if isinstance(g, ImageContent)
            )
            for cont in node.contents:
                cover = getattr(cont, "cover", None)
                if cover is not None:
                    tasks.append(cover)
            for task in tasks:
                if getattr(task, "started", False) and getattr(task, "resolved", None) is None:
                    return True
        return False

    # ==================== 媒体描述（唯一契约 = media_probe.describe_result） ====================

    def _fallback_description(self, result: ParseResult) -> dict[str, Any]:
        """契约未落地（另一成员尚未产出）时的降级描述：只给媒体计数，其余未知。"""
        return {
            "quality": "",
            "size_bytes": None,
            "estimated": False,
            "media_count": self._fallback_media_count(result),
            "unavailable_reason": "",
        }

    def _fallback_media_count(self, result: ParseResult) -> int:
        """准确计数（按 PathTask 去重）：每张图/每段视频/音频各算一项。"""
        seen: set[int] = set()
        count = 0
        for node in self._walk(result):
            tasks = [c.path_task for c in node.contents]
            tasks.extend(
                g.path_task for g in node.graphics if isinstance(g, ImageContent)
            )
            for task in tasks:
                key = id(task)
                if key not in seen:
                    seen.add(key)
                    count += 1
        return count

    async def describe(self, result: ParseResult) -> dict[str, Any]:
        """描述一份结果的媒体（画质 / 合计大小 / 数量），只依赖公开字典契约。

        另一成员产出（``core/media_probe.describe_result``）在函数内**延迟 import**：
        - 可用 → 用它的聚合结果（含 unavailable_reason）；
        - 不可用 / 异常 → 降级：只给计数，大小未知（**不是 0**），画质为空串。
        返回前按白名单洗一遍，杜绝 URL / 凭据混进卡片与历史。
        """
        desc: dict[str, Any] | None = None
        try:
            from .media_probe import describe_result  # 另一成员的契约产出

            if callable(describe_result):
                raw = await describe_result(result)
                if isinstance(raw, dict):
                    desc = dict(raw)
        except Exception:
            # 契约未落地 / 描述失败：未知优于猜 0，绝不武断
            desc = None
        if desc is None:
            desc = self._fallback_description(result)
        cleaned: dict[str, Any] = {}
        for key in _SAFE_DESC_KEYS:
            cleaned[key] = desc.get(key) if key in desc else None
        size = cleaned.get("size_bytes")
        cleaned["size_bytes"] = size if isinstance(size, int) else None
        try:
            cleaned["media_count"] = max(0, int(cleaned.get("media_count") or 0))
        except (TypeError, ValueError):
            cleaned["media_count"] = self._fallback_media_count(result)
        cleaned["quality"] = str(cleaned.get("quality") or "").strip()
        cleaned["estimated"] = bool(cleaned.get("estimated"))
        cleaned["unavailable_reason"] = str(cleaned.get("unavailable_reason") or "").strip()
        return cleaned

    async def media_count(self, result: ParseResult) -> int:
        d = await self.describe(result)
        return int(d.get("media_count") or 0)

    # ==================== 预览投影 ====================

    def _blocked_body_task(self, kind: str) -> PathTask:
        """确认模式下**绝对不自动下载**的源媒体任务占位。

        预览投影把视频 / 音频的本体替换成它：任何路径若误触 get()，只会抛
        ``IgnoreException``（策略跳过信号），绝不会真的发起下载。
        """

        async def _skip():
            raise IgnoreException(f"确认模式下不下载源{kind}，确认后自动下载")

        return PathTask.lazy(lambda: _skip())

    def build_preview(self, result: ParseResult) -> ParseResult:
        """复制预览数据；整条分享最多一张图集封面，不取本体或抽帧。"""
        visited: set[int] = set()
        first_image_kept = False

        def project(source):
            nonlocal first_image_kept
            if source is None or id(source) in visited:
                return None
            visited.add(id(source))
            extra = dict(source.extra or {})
            extra["limit_warnings"] = list(extra.get("limit_warnings") or [])
            preview = ParseResult(
                platform=source.platform, author=source.author, title=source.title,
                text=source.text, timestamp=source.timestamp, url=source.url,
                extra=extra, render_image=source.render_image,
                graphics=[g for g in source.graphics if not isinstance(g, ImageContent)],
            )
            for cont in source.contents:
                if isinstance(cont, ImageContent):
                    if not first_image_kept and not cont.path_task.needs_media:
                        preview.contents.append(cont)
                        first_image_kept = True
                elif isinstance(cont, (VideoContent, AudioContent)):
                    cover = cont.cover
                    if cover is not None and cover.needs_media:
                        cover = None
                    body = self._blocked_body_task("视频" if isinstance(cont, VideoContent) else "音频")
                    if isinstance(cont, VideoContent):
                        preview.contents.append(VideoContent(body, cover=cover, duration=cont.duration, is_gif=cont.is_gif))
                    else:
                        preview.contents.append(AudioContent(body, duration=cont.duration, cover=cover))
            if not first_image_kept:
                for graphic in source.graphics:
                    if isinstance(graphic, ImageContent) and not graphic.path_task.needs_media:
                        preview.contents.append(graphic)
                        first_image_kept = True
                        break
            preview.repost = project(source.repost)
            if source.card_info_loader is not None:
                preview.card_info_loader = _PreviewCardInfo(source, preview)
            return preview

        return project(result)

    @staticmethod
    def format_size(size_bytes: int) -> str:
        if size_bytes >= 1024 ** 3:
            return f"{size_bytes / 1024 ** 3:.1f}GB"
        if size_bytes >= 1024 ** 2:
            return f"{size_bytes / 1024 ** 2:.1f}MB"
        if size_bytes >= 1024:
            return f"{size_bytes // 1024}KB"
        return f"{size_bytes}B"

    # ==================== offer 生命周期 ====================

    def _new_id(self) -> str:
        while True:
            candidate = secrets.token_hex(2).upper()
            if candidate not in self._offers:
                return candidate

    def prune(self) -> None:
        """清理过期 / 陈旧条目（同步，无 await 点）。"""
        now = _now()
        for oid in list(self._offers):
            offer = self._offers[oid]
            if offer.state == "pending" and offer.expires_at < now:
                self._drop(oid)
            elif offer.state == "downloading" and offer.expires_at + _STALE_DOWNLOADING_GRACE < now:
                running = self._downloading.get(oid)
                if running is not None and not running.done():
                    continue
                # 确认协程异常退出后的崩溃恢复：过期很久仍卡在 downloading → 作废
                offer.state = "failed"
                offer.note = "stale"
                self._drop(oid)
            elif offer.state in ("done", "failed", "uncertain") and offer.expires_at < now:
                self._drop(oid)

    def find_pending_by_reuse_key(self, scope: str, reuse_key: str) -> ConfirmOffer | None:
        """同一会话里、同一链接的**仍有效**待确认项（防重复建 offer / 重复堆记录）。"""
        if not scope or not reuse_key:
            return None
        now = _now()
        for oid in self._scope_index.get(scope, []):
            offer = self._offers.get(oid)
            if offer is None:
                continue
            if offer.reuse_key == reuse_key and offer.state == "pending" and offer.expires_at >= now:
                return offer
        return None

    def create_or_refresh(
        self,
        *,
        scope: str,
        platform: str,
        url: str,
        cache_key: str,
        reuse_key: str,
        sender_id: str,
        identity: str,
        title: str,
        result: ParseResult,
        preview_path: str | None,
        description: dict[str, Any],
        limits: list[str] | None = None,
        history_id: str | None = None,
    ) -> tuple[ConfirmOffer, bool] | None:
        """登记一条待确认项；同会话同链接已有 pending 则复用并刷新 TTL。

        Reuse **不得**改写 ``sender_id / identity / result``：否则同群另一人
        重发同一链接就能把「原分享者」换掉。容量同时受每会话与全局上限约束。

        Returns:
            ``(offer, is_new)``；容量超限（或控制器已关闭）返回 ``None``。
        """
        if self._closed:
            return None
        self.prune()
        if len(self._offers) >= MAX_STORED_OFFERS:
            for oid, old in list(self._offers.items()):
                if old.state not in ("pending", "downloading"):
                    self._drop(oid)
                    if len(self._offers) < MAX_STORED_OFFERS:
                        break
        now = _now()
        existing = self.find_pending_by_reuse_key(scope, reuse_key)
        if existing is not None:
            existing.expires_at = now + self.ttl_seconds()
            if preview_path:
                existing.preview_path = str(preview_path)
            existing.description = dict(description)
            existing.limits = list(limits or [])
            if history_id and not existing.history_id:
                existing.history_id = history_id
            return existing, False

        def pending(oid: str) -> bool:
            item = self._offers.get(oid)
            return item is not None and item.state == "pending"

        if sum(1 for oid in self._scope_index.get(scope, []) if pending(oid)) >= MAX_PENDING_PER_SCOPE:
            self._capacity_reason = "scope"
            return None
        if sum(1 for oid in self._offers if pending(oid)) >= MAX_PENDING_GLOBAL:
            self._capacity_reason = "global"
            return None
        if len(self._offers) >= MAX_STORED_OFFERS:
            self._capacity_reason = "global"
            return None
        offer = ConfirmOffer(
            id=self._new_id(),
            scope=scope,
            platform=platform,
            url=url,
            cache_key=cache_key,
            reuse_key=reuse_key,
            sender_id=str(sender_id or "").strip(),
            identity=identity,
            title=title or "",
            description=dict(description),
            result=result,
            created_at=now,
            expires_at=now + self.ttl_seconds(),
            history_id=history_id,
            preview_path=str(preview_path) if preview_path else None,
            limits=list(limits or []),
        )
        self._offers[offer.id] = offer
        self._scope_index.setdefault(scope, []).append(offer.id)
        return offer, True

    def attach_history(self, offer_id: str, history_id: str | None) -> None:
        offer = self._offers.get(offer_id)
        if offer is not None and history_id:
            offer.history_id = history_id

    def find_for_confirm(self, scope: str | None, number: str | None):
        """在**当前物理会话**内查找待确认项。

        不传编号：唯一的有效 pending 直接返回；多个要求短编号；没有则 none。
        传编号：按 id 精确查找（含 done / failed / downloading，用于幂等提示）。
        """
        if not scope:
            return "none", None
        ids = list(self._scope_index.get(scope, []))
        if not number:
            now = _now()
            candidates = [
                self._offers[oid]
                for oid in ids
                if self._offers.get(oid) is not None
                and self._offers[oid].state == "pending"
                and self._offers[oid].expires_at >= now
            ]
            if len(candidates) == 1:
                return "ok", candidates[0]
            if len(candidates) > 1:
                return "list", candidates
            return "none", None
        target = str(number).strip().lower()
        for oid in ids:
            offer = self._offers.get(oid)
            if offer is not None and offer.id.lower() == target:
                return "ok", offer
        return "missing", None

    def claim(self, offer_id: str) -> str:
        """原子认领：``pending -> downloading``（同步临界区，无 await 点）。"""
        offer = self._offers.get(offer_id)
        if offer is None:
            return "missing"
        if offer.state == "downloading":
            return "downloading"
        if offer.expires_at < _now():
            self._drop(offer_id)
            return "expired"
        if offer.state == "pending":
            offer.state = "downloading"
            return "ok"
        if offer.state == "downloading":
            return "downloading"
        if offer.state == "done":
            return "done"
        if offer.state == "uncertain":
            return "uncertain"
        return "failed"

    def finalize(self, offer_id: str, state: str, note: str = "") -> None:
        offer = self._offers.get(offer_id)
        if offer is None:
            return
        offer.state = state
        offer.note = note
        offer.finalized_at = _now()

    def invalidate_all(self, reason: str = "config_changed") -> None:
        """只丢弃尚未认领的 pending；已认领的让其跑完。"""
        for oid in list(self._offers):
            offer = self._offers[oid]
            if offer.state == "pending":
                self._drop(oid)

    async def close(self) -> None:
        """关闭控制器：取消并等待活跃确认协程，避免关停后仍下载/发送。"""
        self._closed = True
        current = asyncio.current_task()
        tasks = [
            task for task in self._active
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._offers.clear()
        self._scope_index.clear()

    # ==================== 确认权限（四项 bool OR） ====================

    async def _is_qq_group_admin(self, event: Any, sender_id: str) -> bool:
        """QQ 群主 / 群管理判定：只对可信 QQ OneBot 群事件使用。

        - 平台必须是 ``aiocqhttp`` OneBot 群消息；
        - 私聊直接拒绝该角色；
        - ``get_group()`` 结果里取 ``group_owner`` / ``group_admins``；
          超时 / 异常 / 结果缺失 / 未知角色一律**拒绝**（不信任用户文字，
          也不把 ``event.is_admin()`` 当 QQ 群管）。
        """
        try:
            platform_name = str(event.get_platform_name() or "").lower()
        except Exception:
            return False
        if "aiocqhttp" not in platform_name:
            return False
        try:
            if event.is_private_chat():
                return False
        except Exception:
            return False
        if not sender_id:
            return False
        if not str(self._event_attr(event, "get_group_id") or "").strip():
            return False
        get_group = getattr(event, "get_group", None)
        if not callable(get_group):
            return False
        try:
            group = await asyncio.wait_for(get_group(), timeout=QQ_GROUP_LOOKUP_TIMEOUT)
        except Exception:
            return False
        if group is None:
            return False
        owner = str(getattr(group, "group_owner", None) or "").strip()
        admin_ids = getattr(group, "group_admins", None) or []
        admins = {str(a) for a in admin_ids}
        return (bool(owner) and owner == sender_id) or sender_id in admins

    async def role_allowed(self, event: Any, offer: ConfirmOffer) -> bool:
        """所选确认角色：任何人 / 原分享者 / QQ群主管理员 / AstrBot管理员，四项 OR。

        全关（四项都 False）→ 一律拒绝。任何人仍受插件原有访问权
        （白黑名单 / 平台开关）约束 —— 那是调用方在调用本方法之前就检查了的。
        """
        anyone = self.allow_anyone()
        sender_ok = self.allow_sender()
        qq_admin_ok = self.allow_qq_admin()
        bot_admin_ok = self.allow_bot_admin()
        if not (anyone or sender_ok or qq_admin_ok or bot_admin_ok):
            return False
        sender_id = self.sender_of(event)
        if anyone:
            return True
        if sender_ok and sender_id and sender_id == str(offer.sender_id or "").strip():
            return True
        if bot_admin_ok:
            is_admin = getattr(event, "is_admin", None)
            if callable(is_admin):
                try:
                    if is_admin():
                        return True
                except Exception:
                    pass
        if qq_admin_ok and await self._is_qq_group_admin(event, sender_id):
            return True
        return False

    # ==================== 确认时重新解析与限额 ====================

    def identity_of(self, parser: Any, searched: Any, fallback: str) -> str:
        """解析器规范身份；无 ``dedup_identity`` 时按**本次匹配原文**算缓存键。"""
        resolver = getattr(parser, "dedup_identity", None)
        if callable(resolver):
            try:
                identity = resolver(searched)
                if isinstance(identity, str) and identity.strip():
                    from .result_cache import ResultCache

                    return "dedup:" + ResultCache.result_cache_key(identity.strip())
            except Exception:
                pass
        try:
            matched = searched.group(0)
        except Exception:
            matched = ""
        from .result_cache import ResultCache

        return "cache:" + ResultCache.result_cache_key(str(matched or fallback))

    def canonical_target(self, parser, result, fallback, searched):
        """优先保留可重新匹配的规范分享地址，避免确认时再次依赖短链。"""
        if result.url:
            try:
                _, canonical = parser.search_url(result.url)
                return canonical.group(0), canonical
            except Exception:
                pass
        return fallback, searched

    async def reparse(self, parser: Any, offer: ConfirmOffer) -> ParseResult | None:
        """确认时重新解析规范分享链接，刷新签名地址 / 权限 / 描述。

        Returns:
            新 ParseResult；内容身份与 offer 不一致时返回 ``None``（调用方拒绝）。
        Raises:
            :class:`OfferReparseError`: 链接无法重匹配 / 重新解析失败。
        """
        from .base_parser import apply_result_image_limit

        try:
            keyword, searched = parser.search_url(offer.url)
        except Exception as exc:
            raise OfferReparseError("无法重新匹配该分享链接") from exc
        try:
            fresh = await parser.parse(keyword, searched)
        except Exception as exc:
            raise OfferReparseError(f"重新解析失败（{type(exc).__name__}）") from exc
        apply_result_image_limit(fresh)
        fresh_url, fresh_match = self.canonical_target(parser, fresh, offer.url, searched)
        new_identity = self.identity_of(parser, fresh_match, fresh_url)
        if not new_identity or new_identity != offer.identity:
            return None
        return fresh

    def _effective_media_limit(self) -> int:
        """单文件生效上限 = min(用户配置, 2GB 硬上限)；配置 0 时硬上限仍生效。"""
        from .download import HARD_MAX_MEDIA_BYTES

        try:
            maximum_mb = int(get_config().VIDEO_SIZE_MAXIMUM_MB)
        except Exception:
            maximum_mb = 100
        configured = maximum_mb * 1024 * 1024 if maximum_mb > 0 else 0
        if configured <= 0:
            return HARD_MAX_MEDIA_BYTES
        return min(configured, HARD_MAX_MEDIA_BYTES)

    async def over_limits(
        self, result: ParseResult, desc: dict[str, Any] | None = None
    ) -> list[str]:
        """确认前明确标出「已知超限、确认后不会下载」的条目原因。

        ``desc`` 可由调用方传入（同一条结果只 describe 一次）。多文件 / 估算
        大小 / 未知长度流式不在此拒绝，由下载器逐文件及流式中途兜底。
        """
        reasons: list[str] = []
        if desc is None:
            desc = await self.describe(result)
        size = desc.get("size_bytes")
        if isinstance(size, int) and not desc.get("estimated") and int(desc.get("media_count") or 0) == 1:
            limit = self._effective_media_limit()
            items = [c for node in self._walk(result) for c in (*node.contents, *node.graphics)
                     if isinstance(c, (VideoContent, AudioContent, ImageContent))]
            if items and all(isinstance(c, ImageContent) for c in items):
                from .download import HARD_MAX_IMAGE_BYTES
                limit = min(limit, HARD_MAX_IMAGE_BYTES)
            if size > limit:
                reasons.append(
                    f"⚠️ 媒体大小（{self.format_size(size)}）超过上限"
                    f"（{self.format_size(limit)}），无法下载"
                )
        try:
            duration_max = int(get_config().VIDEO_DURATION_MAXIMUM)
        except Exception:
            duration_max = 480
        from .media_utils import fmt_duration

        for node in self._walk(result):
            for cont in node.contents:
                if isinstance(cont, (VideoContent, AudioContent)) and cont.duration:
                    if cont.duration > duration_max:
                        kind = "视频" if isinstance(cont, VideoContent) else "音频"
                        reasons.append(
                            f"⚠️ {kind}时长（{fmt_duration(cont.duration)}）超过上限"
                            f"（{fmt_duration(duration_max)}），确认后不会下载"
                        )
        return reasons

    # ==================== 配置失效 / 入口预检 ====================

    def _config_fingerprint(self) -> dict[str, Any] | None:
        try:
            pconfig = get_config()
            modes = pconfig.output_modes()
            return {
                "mode": self.mode(),
                "media": bool(modes["media"]),
                "text": bool(modes["text"]),
                "card": bool(modes["card_effective"]),
                "size": int(pconfig.VIDEO_SIZE_MAXIMUM_MB),
                "duration": int(pconfig.VIDEO_DURATION_MAXIMUM),
                "disabled": tuple(
                    sorted(str(x) for x in (pconfig.DISABLED_PLATFORMS or []))
                ),
            }
        except Exception:
            return None

    def refresh_config(self) -> None:
        """保存配置后**只**失效真正会让 pending 不可信的键。

        权限开关不进指纹：``role_allowed`` 每次都实时读取，改了立即生效却不必
        清掉任何人的待确认项。无关的外观 / 历史 / 音乐保存也不会误伤。
        """
        snap = self._config_fingerprint()
        if snap is None:
            return
        prev = self._config_snapshot
        self._config_snapshot = snap
        if prev is None:
            return
        if snap["mode"] != "confirm" or not snap["media"] or not (snap["text"] or snap["card"]):
            self.invalidate_all("output")
            return
        if (snap["size"], snap["duration"]) != (prev["size"], prev["duration"]):
            self.invalidate_all("limits")
            return
        added = set(snap["disabled"]) - set(prev["disabled"])
        if added:
            for oid in list(self._offers):
                offer = self._offers[oid]
                if offer.state == "pending" and offer.platform in added:
                    self._drop(oid)

    def preflight(self, parser_key: str) -> str | None:
        """在 parse 之前拒绝无法兑现延迟下载的来源。"""
        if self._closed:
            return "❌ 确认下载功能已关闭"
        if self.mode() != "confirm":
            return None
        if is_custom_platform(parser_key):
            return (
                "❌ 自定义解析器无法保证「确认前不下载源媒体」，"
                "本次不创建确认项；请改用自动下载或更换来源。"
            )
        return None

    def _all_over_limit(
        self, result: ParseResult, desc: dict[str, Any], reasons: list[str]
    ) -> bool:
        """已知提示是否意味着「一项都下不了」。单项超限直接判定；多项则看是否
        还有图片或未超时长的视频/音频。"""
        if not reasons:
            return False
        if int(desc.get("media_count") or 0) <= 1:
            return True
        try:
            duration_max = int(get_config().VIDEO_DURATION_MAXIMUM)
        except Exception:
            duration_max = 480
        for node in self._walk(result):
            for cont in node.contents:
                if isinstance(cont, ImageContent):
                    return False
                if isinstance(cont, (VideoContent, AudioContent)):
                    if not cont.duration or cont.duration <= duration_max:
                        return False
            for graphic in node.graphics:
                if isinstance(graphic, ImageContent):
                    return False
        return True

    def _info_lines(self, offer: ConfirmOffer) -> list[str]:
        desc = offer.description or {}
        quality = str(desc.get("quality") or "").strip() or "未知"
        size = desc.get("size_bytes")
        if isinstance(size, int) and size > 0:
            size_text = self.format_size(size)
            if desc.get("estimated"):
                size_text += "（估算）"
        else:
            size_text = "未知"
        count = int(desc.get("media_count") or 0)
        lines = [f"🎞 媒体信息：{quality} · {size_text} · 共 {count} 项"]
        lines.extend(offer.limits or [])
        remaining = max(0, int(offer.expires_at - _now()))
        lines.append(f"🆔 确认编号：{offer.id}（剩余 {remaining} 秒）")
        lines.append(f"回复「确认下载 {offer.id}」立即下载并发送")
        return lines

    @staticmethod
    def _warning_text(result: ParseResult) -> str:
        seen: list[str] = []
        for warning in result.extra.get("limit_warnings") or []:
            text = str(warning).strip()
            if text and text not in seen:
                seen.append(text)
        return "\n".join(seen)

    async def _update_history(
        self,
        offer: ConfirmOffer,
        result: ParseResult,
        state: str,
        desc: dict[str, Any],
    ) -> None:
        """把源文件与最终状态补进**同一条**预览记录；记录被删则不重建。"""
        if not offer.history_id:
            return
        try:
            plugin = self._plugin
            detail = plugin._history_detail(result)
            detail["confirm_state"] = state
            detail["quality"] = str(desc.get("quality") or "")
            detail["size_bytes"] = (
                desc.get("size_bytes") if isinstance(desc.get("size_bytes"), int) else None
            )
            detail["estimated"] = bool(desc.get("estimated"))
            ok = await asyncio.to_thread(
                plugin.history.update,
                offer.history_id,
                media_files=plugin._history_media_files(result, plugin.cache_dir),
                detail=detail,
            )
            if not ok:
                logger.info("[denia_share] 确认记录已被删除，未重建历史记录")
        except Exception:
            logger.warning("[denia_share] 更新确认历史记录失败", exc_info=True)

    # ==================== offer：确认前只取封面 ====================

    async def offer(
        self,
        event: Any,
        *,
        parser: Any,
        parser_key: str,
        result: ParseResult,
        matched: str,
        cache_key: str,
        searched: Any,
    ) -> AsyncGenerator[Any, None]:
        """登记 offer、渲染预览卡片（仅封面），并把媒体信息与短编号发出去。

        源视频 / 音频 / 整套图集在确认前一次都不 ``get()``；急式解析器或已
        开始下载的结果明确拒绝，**不静默回退**自动模式。
        """
        if self._closed or self.mode() != "confirm":
            return
        if self.has_inflight_media(result):
            yield event.plain_result(
                "❌ 该来源在解析阶段已开始下载源媒体，无法保证「确认后再下载」；"
                "本次不发送。请改用自动下载或更换来源。"
            )
            return
        modes = get_config().output_modes()
        if not self.has_media(result):
            # 没有需要确认的源媒体：按原有文本 / 卡片链路正常输出。
            async for r in self._plugin._deliver(event, result, cache_key):
                yield r
            return
        if not modes["media"] or not (modes["text"] or modes["card_effective"]):
            yield event.plain_result(
                "❌ 确认下载需要开启富媒体输出，并用文本或卡片展示确认信息。"
            )
            return

        from .base_parser import apply_result_image_limit
        apply_result_image_limit(result)

        desc = await self.describe(result)
        reason = str(desc.get("unavailable_reason") or "").strip()
        if reason:
            yield event.plain_result(f"❌ 该内容当前不可下载：{reason}")
            return
        limits = await self.over_limits(result, desc=desc)
        if self._all_over_limit(result, desc, limits):
            yield event.plain_result("\n".join(limits))
            return

        scope = self.scope_of(event)
        if scope is None:
            yield event.plain_result("❌ 无法确定会话身份，未创建确认项。")
            return
        canonical_url, canonical_match = self.canonical_target(parser, result, matched, searched)
        identity = self.identity_of(parser, canonical_match, canonical_url)
        created = self.create_or_refresh(
            scope=scope,
            platform=str(parser_key or ""),
            url=canonical_url,
            cache_key=cache_key,
            reuse_key=identity,
            sender_id=self.sender_of(event),
            identity=identity,
            title=result.title or "",
            result=result,
            preview_path=None,
            description=desc,
            limits=limits,
        )
        if created is None:
            if self._capacity_reason == "global":
                text = f"❌ 全站待确认项已达上限（{MAX_PENDING_GLOBAL}），请稍后再试。"
            else:
                text = f"❌ 本会话待确认项已达上限（{MAX_PENDING_PER_SCOPE}），请先确认或等待过期。"
            yield event.plain_result(text)
            return
        offer, is_new = created

        preview = self.build_preview(result)
        lines = self._info_lines(offer)
        preview.extra.setdefault("limit_warnings", []).extend(lines)
        text_on = bool(modes["text"])
        card_on = bool(modes["card_effective"])
        preview_path = None
        if card_on:
            try:
                preview_path = await self._plugin._renderer.render(
                    preview, cache_key=f"confirm:{offer.id}:{cache_key}"
                )
            except Exception:
                logger.warning("[denia_share] 确认卡片渲染失败", exc_info=True)
        offer.preview_path = str(preview_path) if preview_path else None
        if is_new:
            record = await self._plugin._record_history(
                preview, preview_path, via="auto"
            )
            if record is not None:
                offer.history_id = record.id
                await self._update_history(offer, result, "pending", desc)

        card_sent = False
        if card_on and preview_path is not None:
            card_sent = await self._plugin._delivery.send_image(event, preview_path)
        text = "\n".join(build_concise_text(preview)) if text_on else ""
        if text:
            yield event.plain_result(text)
        if not card_sent and not text:
            if is_new:
                self._drop(offer.id)
            return

    # ==================== handle：确认后下载并立即发送 ====================

    def _offer_status_text(self, status: str) -> str:
        return {
            "downloading": "⏳ 该确认项正在下载，请勿重复确认。",
            "done": "✅ 该确认项已处理过，不会重复下载或发送。",
            "uncertain": "⚠️ 该确认项的协议端回执未知；为避免重复发送，不会再次投递。",
            "failed": "❌ 该确认项已失败；请重新分享链接后再确认。",
            "expired": "⌛ 该确认项已过期；请重新分享链接。",
            "missing": "没有找到该确认项。",
        }.get(status, "没有可确认的下载。")

    async def handle(
        self, event: Any, number: str | None = None
    ) -> AsyncGenerator[Any, None]:
        """确认下载：查会话内 offer → 校验角色 → 原子认领 → 重解析 → 下载 → 发送。"""
        task = asyncio.current_task()
        claimed_offer = None
        if task is not None:
            self._active.add(task)
        try:
            if self._closed:
                return
            scope = self.scope_of(event)
            if scope is None:
                yield event.plain_result("❌ 无法确定会话身份，本次确认已拒绝。")
                return
            if self._plugin._access_denied(event):
                return
            status, offer = self.find_for_confirm(scope, number)
            if status == "list":
                lines = [f"当前有 {len(offer)} 个待确认项，请回复「确认下载 编号」："]
                for item in offer:
                    remaining = max(0, int(item.expires_at - _now()))
                    title = (item.title or "未命名")[:40]
                    lines.append(f"• {item.id}｜{title}｜剩余 {remaining} 秒")
                yield event.plain_result("\n".join(lines))
                return
            if status != "ok" or offer is None:
                if status == "missing" and number:
                    yield event.plain_result(f"没有找到编号为 {number} 的待确认项。")
                else:
                    yield event.plain_result("当前没有待确认的下载。")
                return

            if offer.state == "pending" and offer.expires_at < _now():
                self._drop(offer.id)
                yield event.plain_result("⌛ 该确认项已过期；请重新分享链接。")
                return
            if not await self.role_allowed(event, offer):
                yield event.plain_result("❌ 你没有确认该下载的权限。")
                return

            claimed = self.claim(offer.id)
            if claimed != "ok":
                yield event.plain_result(self._offer_status_text(claimed))
                return
            claimed_offer = offer
            if task is not None:
                self._downloading[offer.id] = task

            if self.mode() != "confirm":
                self.finalize(offer.id, "failed", "mode_changed")
                yield event.plain_result("❌ 下载时机已切回自动模式，本次确认未执行。")
                return
            pconfig = get_config()
            if not pconfig.OUTPUT_MEDIA_ENABLED:
                self.finalize(offer.id, "failed", "media_off")
                yield event.plain_result("❌ 富媒体输出已关闭，无法下载并发送源媒体。")
                return
            parser = self._plugin.parsers.get(offer.platform)
            if parser is None:
                self.finalize(offer.id, "failed", "platform_disabled")
                yield event.plain_result("❌ 该平台已被禁用，无法完成确认下载。")
                return
            if self._plugin._access_denied(event):
                self.finalize(offer.id, "failed", "blocked")
                return

            try:
                fresh = await self.reparse(parser, offer)
            except OfferReparseError as exc:
                self.finalize(offer.id, "failed", "reparse")
                yield event.plain_result(f"❌ {exc}；请重新分享链接。")
                return
            if fresh is None:
                self.finalize(offer.id, "failed", "identity_changed")
                yield event.plain_result("❌ 内容身份已变化，本次确认已取消；请重新分享链接。")
                return
            if not self.has_media(fresh):
                self.finalize(offer.id, "failed", "no_media")
                yield event.plain_result("❌ 重新解析后已没有可下载的媒体。")
                return

            desc = await self.describe(fresh)
            unavailable = str(desc.get("unavailable_reason") or "").strip()
            if unavailable:
                self.finalize(offer.id, "failed", "unavailable")
                yield event.plain_result(f"❌ 该内容当前不可下载：{unavailable}")
                return
            limits = await self.over_limits(fresh, desc=desc)
            if self._all_over_limit(fresh, desc, limits):
                self.finalize(offer.id, "failed", "limit")
                yield event.plain_result("\n".join(limits))
                return

            if (desc.get("quality"), desc.get("size_bytes"), desc.get("estimated")) != (
                offer.description.get("quality"), offer.description.get("size_bytes"),
                offer.description.get("estimated"),
            ):
                quality = str(desc.get("quality") or "未知")
                size = desc.get("size_bytes")
                size_text = self.format_size(size) if isinstance(size, int) and size > 0 else "大小未知"
                if desc.get("estimated"):
                    size_text += "（估算）"
                yield event.plain_result(f"媒体信息已更新：{quality} · {size_text}，将按当前可用来源下载。")

            yield event.plain_result(
                f"⏳ 已确认 {offer.id}，正在下载源媒体并发送…"
            )
            try:
                await fresh.audit_missing_media()
            except asyncio.CancelledError:
                self.finalize(offer.id, "failed", "cancelled")
                raise
            except Exception:
                logger.warning("[denia_share] 确认下载结算异常", exc_info=True)

            media_files = self._plugin._history_media_files(
                fresh, self._plugin.cache_dir
            )
            if not media_files:
                await self._update_history(offer, fresh, "failed", desc)
                self.finalize(offer.id, "failed", "download")
                text = self._warning_text(fresh) or (
                    "❌ 源媒体下载失败或超出限额，未发送任何媒体。"
                )
                yield event.plain_result(text)
                return

            delivered = False
            delivery_error = False

            def mark_delivered():
                nonlocal delivered
                delivered = True

            def mark_failed():
                nonlocal delivery_error
                delivery_error = True

            try:
                async for r in self._plugin._try_send_media(
                    event, fresh, on_delivered=mark_delivered, on_failed=mark_failed
                ):
                    yield r
                    delivered = True
            except asyncio.CancelledError:
                self.finalize(offer.id, "failed", "cancelled")
                raise
            except Exception:
                delivery_error = True
                logger.warning("[denia_share] 确认媒体投递失败", exc_info=True)

            uncertain = _DELIVERY_UNCERTAIN.get()
            state = "uncertain" if uncertain else "done" if delivered else "failed"
            note = "partial" if (delivered and delivery_error) else (
                "" if delivered else "delivery"
            )
            if uncertain:
                note = "delivery_uncertain"
            await self._update_history(offer, fresh, state, desc)
            self.finalize(offer.id, state, note)
            warnings = self._warning_text(fresh)
            if warnings:
                yield event.plain_result(warnings)
            if uncertain:
                yield event.plain_result("⚠️ 协议端回执未知，媒体可能已经送达；未自动重发，请先查看聊天记录。")
            elif delivery_error and delivered:
                yield event.plain_result(
                    "⚠️ 部分媒体投递失败，未自动重发；请查看聊天记录。"
                )
            elif not delivered:
                yield event.plain_result(
                    "❌ 媒体投递失败，未记录为已发送；请稍后重试或改用自动下载。"
                )
        except (asyncio.CancelledError, GeneratorExit):
            if claimed_offer is not None and claimed_offer.state == "downloading":
                self.finalize(claimed_offer.id, "failed", "cancelled")
            raise
        except Exception:
            if claimed_offer is not None and claimed_offer.state == "downloading":
                self.finalize(claimed_offer.id, "failed", "internal")
            raise
        finally:
            if claimed_offer is not None:
                self._downloading.pop(claimed_offer.id, None)
            if task is not None:
                self._active.discard(task)

    def _drop(self, offer_id: str) -> None:
        offer = self._offers.pop(offer_id, None)
        if offer is None:
            return
        ids = self._scope_index.get(offer.scope)
        if ids:
            try:
                ids.remove(offer_id)
            except ValueError:
                pass
            if not ids:
                self._scope_index.pop(offer.scope, None)
