"""纯内存解析诊断记录。

把一次「链接 → 卡片」的处理过程拆成固定阶段（match/parse/media/render/send），
记录每个阶段的状态与**受控**的中文说明，供聊天指令与 WebUI 页面展示 / 导出。

安全约束（这里比功能更重要）：

- **只进不出**：数据只在内存里，进程退出即消失；不写盘、不联网、不回传。
- **白名单**：阶段、状态、平台、来源（via）只能取白名单里的固定字面量，
  无法识别的输入一律降级成固定兜底值，绝不原样保留。
- **消息只走原因码**：``stage(..., message=...)`` 收到的是**原因码**，
  由 ``REASON_TEXT`` 映射成固定中文文案；未知原因码一律换成通用文案。
  原始异常文本、URL、标题、Cookie 都不会进入记录。
- **有界**：最多保留 ``max_entries`` 条，超出时淘汰最旧的一条。

平台白名单只认内置平台键（与 ``core/constants.py`` 的 ``PLATFORMS`` 一致）；
用户自定义解析器的平台键不在表内，统一记成 ``custom``。
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any

__all__ = [
    "DiagnosticStore",
    "DiagnosticTrace",
    "GENERIC_TEXT",
    "REASON_TEXT",
    "STAGE_LABELS",
    "STAGE_ORDER",
    "STATES",
    "STATUSES",
]

#: 固定阶段，顺序即展示顺序。
STAGE_ORDER: tuple[str, ...] = ("match", "parse", "media", "render", "send")

#: 阶段中文 label。
STAGE_LABELS: dict[str, str] = {
    "match": "链接匹配",
    "parse": "内容解析",
    "media": "媒体获取",
    "render": "卡片渲染",
    "send": "消息发送",
}

#: 阶段状态白名单。
STATES: tuple[str, ...] = ("running", "ok", "skipped", "error", "unknown")

#: 终态白名单（与阶段状态同一套）。
STATUSES: tuple[str, ...] = STATES

#: 状态中文 label。
STATE_LABELS: dict[str, str] = {
    "running": "进行中",
    "ok": "成功",
    "skipped": "已跳过",
    "error": "失败",
    "unknown": "未知",
}

#: 来源白名单：只有聊天与 WebUI 两个入口。
VIA_CHAT: str = "chat"
VIA_WEBUI: str = "webui"
DEFAULT_VIA: str = VIA_CHAT

#: 内置平台白名单（键与 core/constants.py 的 PLATFORMS 一致）。
KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {
        "bilibili",
        "douyin",
        "kuaishou",
        "weibo",
        "xiaohongshu",
        "twitter",
        "nga",
        "acfun",
        "github",
        "pixiv",
        "steam",
        "xiaoheihe",
        "netease",
        "qqmusic",
    }
)

#: 平台兜底值：自定义解析器与未知平台都记成这个。
PLATFORM_CUSTOM: str = "custom"

#: 原因码 → 安全中文文案。**只有表里的原因码会被保留。**
REASON_TEXT: dict[str, str] = {
    "timeout": "请求超时",
    "network": "网络连接异常",
    "dns": "域名解析失败",
    "not_supported": "暂不支持该平台",
    "invalid_link": "链接格式无效",
    "no_content": "未解析到内容",
    "parse_failed": "内容解析失败",
    "no_cookie": "缺少必要凭据",
    "login_required": "需要重新登录",
    "media_missing": "未获取到媒体文件",
    "media_too_large": "媒体文件超出大小限制",
    "media_unsupported": "媒体格式不受支持",
    "render_failed": "卡片渲染失败",
    "send_failed": "消息发送失败",
    "skipped": "已按配置跳过",
    "cached": "命中本地缓存",
    "duplicate": "重复链接，已按去重规则跳过",
    "blocked": "已按权限配置拦截",
    "output_off": "所选输出均已关闭",
    "preview_only": "网页预览不向聊天发送消息",
    "queued": "已交给宿主发送，无法确认最终送达",
    "accepted": "发送接口已接受",
    "receipt_unknown": "发送回执未知，请查看聊天记录",
    "limit": "部分媒体超出配置限制",
}

#: 未知原因码的固定兜底文案（绝不回显原始异常 / URL / 标题 / Cookie）。
GENERIC_TEXT: str = "处理异常，详情未记录"


def _now_iso() -> str:
    """本地时间的 ISO 字符串（可读且 JSON 可序列化）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    """把任意入参归一成小写字符串（None → 空串）。"""
    if value is None:
        return ""
    return str(value).strip().lower()


def _safe_state(value: Any) -> str:
    text = _text(value)
    return text if text in STATES else "unknown"


def _safe_status(value: Any) -> str:
    text = _text(value)
    return text if text in STATUSES else "unknown"


def _safe_via(value: Any) -> str:
    text = _text(value)
    return text if text in (VIA_CHAT, VIA_WEBUI) else DEFAULT_VIA


def _safe_platform(value: Any) -> str:
    text = _text(value)
    return text if text in KNOWN_PLATFORMS else PLATFORM_CUSTOM


def _safe_message(value: Any) -> str:
    """原因码 → 固定中文文案；未知原因码降级成通用文案。"""
    text = _text(value)
    if not text:
        return ""
    return REASON_TEXT.get(text, GENERIC_TEXT)


class DiagnosticTrace:
    """一次「链接 → 卡片」处理的诊断轨迹。"""

    __slots__ = (
        "id",
        "via",
        "platform",
        "started_at",
        "status",
        "_stages",
        "_t0",
        "_finished_ms",
    )

    def __init__(self, via: str = DEFAULT_VIA, platform: str = "") -> None:
        self.id: str = uuid.uuid4().hex[:12]
        self.via: str = _safe_via(via)
        self.platform: str = _safe_platform(platform)
        self.started_at: str = _now_iso()
        self.status: str = "running"
        self._stages: dict[str, dict[str, str]] = {}
        self._t0: float = time.monotonic()
        self._finished_ms: int | None = None

    def stage(self, name: str, state: str, message: str = "") -> None:
        """记录一个阶段。非白名单阶段名直接丢弃，不做任何透传。"""
        key = _text(name)
        if key not in STAGE_LABELS:
            return
        self._stages[key] = {
            "name": key,
            "label": STAGE_LABELS[key],
            "state": _safe_state(state),
            "message": _safe_message(message),
        }

    def finish(self, status: str) -> None:
        """结束轨迹并冻结耗时。非白名单终态记成 ``unknown``。"""
        self.status = _safe_status(status)
        if self._finished_ms is None:
            self._finished_ms = self.elapsed_ms

    @property
    def elapsed_ms(self) -> int:
        """已用毫秒；未结束时按当前时刻实时计算。"""
        if self._finished_ms is not None:
            return self._finished_ms
        return int((time.monotonic() - self._t0) * 1000)

    def to_dict(self) -> dict[str, Any]:
        """脱敏后的字典（JSON 可序列化）。"""
        from .constants import PLATFORM_DISPLAY_NAMES
        return {
            "id": self.id,
            "via": self.via,
            "platform": self.platform,
            "platform_display": PLATFORM_DISPLAY_NAMES.get(self.platform, '自定义'),
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "status": self.status,
            "stages": [dict(self._stages[k]) for k in STAGE_ORDER if k in self._stages],
        }


class DiagnosticStore:
    """有界的内存诊断仓库（不持久化、不联网）。"""

    def __init__(self, max_entries: int = 100) -> None:
        self.max_entries: int = max(1, int(max_entries))
        self._traces: dict[str, DiagnosticTrace] = {}
        self._lock = threading.RLock()

    def start(self, via: str = DEFAULT_VIA, platform: str = "") -> DiagnosticTrace:
        """开一条新轨迹并登记进仓库（登记时就可见，便于展示「处理中」）。"""
        trace = DiagnosticTrace(via=via, platform=platform)
        with self._lock:
            self._traces[trace.id] = trace
            while len(self._traces) > self.max_entries:
                self._traces.pop(next(iter(self._traces)), None)
        return trace

    def snapshot(self) -> dict[str, Any]:
        """全部轨迹的脱敏快照，**最新优先**。"""
        with self._lock:
            traces = list(self._traces.values())
        return {"items": [trace.to_dict() for trace in reversed(traces)]}

    def export(self, trace_id: str) -> dict[str, Any] | None:
        """按 id 导出单条脱敏字典；查不到返回 ``None``。"""
        with self._lock:
            trace = self._traces.get(str(trace_id))
        return trace.to_dict() if trace is not None else None


def store_for(plugin) -> DiagnosticStore:
    store = getattr(plugin, '_diagnostics', None)
    if not isinstance(store, DiagnosticStore):
        store = DiagnosticStore()
        plugin._diagnostics = store
    return store
