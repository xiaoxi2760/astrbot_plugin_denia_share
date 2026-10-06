"""B站备用取流：playurl 调用与流选择。

**备用实现**，主力是 ``bilibili-api-python``。见 :mod:`core.bili_fallback.wbi`
的模块文档了解为什么要留这一份。

来源：从 ``yaya``（娅娅解析 v7.6.0，Apache-2.0）的自建 B站解析器抽出取流部分。

## 与 yaya 原实现的关键差异（都是修 bug，不是改行为）

1. **排序键是「画质优先、编码只做同画质内的 tiebreak」**。
   yaya 原版只按 ``(清晰度, 码率)`` 降序，AVC 恰好码率最高所以碰巧总选 AVC ——
   这是**意外正确**。一旦 B站调整编码策略、或某视频只有 HEVC/AV1 变体，
   就会把老客户端放不出来的编码递到用户面前。这里把编码偏好显式写进排序键，
   但**放在清晰度之后**。

   ⚠️ 顺序反了就是灾难：若编码优先于清晰度，qn=16 的 AVC 会赢过 qn=120 的
   AV1，画质从 4K 直接掉到 360P。

2. **读 ``backupUrl``**。yaya 原版全文件零引用，实测每条流都带 2 个备用 CDN
   地址，白放着不用。调用方拿到的就是候选列表，逐个尝试即可。

3. **2026-10-06 实测**：每个清晰度档都返回 3 个编码变体（AVC / HEVC / AV1），
   且 HEVC 的 sample entry 已从 ``hev1`` 换代为 ``hvc1``。所以识别编码时
   **codecid 优先、``codecs`` 字符串兜底**，不能只认 ``hev1``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "PLAYURL_API",
    "CODEC_ORDER",
    "codec_rank",
    "pick_streams",
    "StreamPair",
]

PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"

#: 编码偏好 -> 降序名次（0 最优先）。与配置项 ``BILI_CODEC`` 的三档对应。
CODEC_ORDER: Dict[str, Tuple[str, ...]] = {
    "H.264 优先": ("avc", "hev", "av1"),
    "H.265 优先": ("hev", "avc", "av1"),
    "AV1 优先": ("av1", "avc", "hev"),
}
DEFAULT_CODEC_ORDER: Tuple[str, ...] = ("avc", "hev", "av1")

# codecid -> 内部名。B站的 codecid 比 sample entry 稳定（``hev1``→``hvc1``
# 换代时 codecid 一直是 12），所以优先认它。
_CODEC_BY_ID: Dict[int, str] = {7: "avc", 12: "hev", 13: "av1", 173: "hev"}
# codecs 字符串前缀 -> 内部名，兜底用
_CODEC_BY_PREFIX = (("avc1", "avc"), ("hev1", "hev"), ("hvc1", "hev"), ("av01", "av1"))

#: 一路流：主地址 + 备用地址列表
StreamPair = Tuple[str, List[str]]


def codec_rank(stream: Dict[str, Any], order: Sequence[str]) -> int:
    """给一条流算编码名次，数字越小越优先；认不出时给最低优先级。"""
    name = None
    codecid = stream.get("codecid")
    if isinstance(codecid, int):
        name = _CODEC_BY_ID.get(codecid)
    if name is None:
        codecs = str(stream.get("codecs") or "").lower()
        for prefix, mapped in _CODEC_BY_PREFIX:
            if codecs.startswith(prefix):
                name = mapped
                break
    try:
        return order.index(name)  # type: ignore[arg-type]
    except ValueError:
        return len(order)


def _urls_of(stream: Dict[str, Any]) -> List[str]:
    """取一条流的主地址 + 全部备用地址（B站的字段名有 camel/snake 两种）。"""
    primary = stream.get("baseUrl") or stream.get("base_url") or ""
    backups = stream.get("backupUrl") or stream.get("backup_url") or []
    if isinstance(backups, str):
        backups = [backups]
    return [u for u in ([primary] + list(backups)) if u]


def _sort_key(stream: Dict[str, Any], order: Sequence[str], max_qn: int):
    """**画质优先，编码次之，码率最后。** 顺序不能动，见模块文档的警告。"""
    def qn_of(item: Dict[str, Any]) -> int:
        try:
            return int(item.get("id", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def bandwidth_of(item: Dict[str, Any]) -> int:
        try:
            return int(item.get("bandwidth", 0) or 0)
        except (TypeError, ValueError):
            return 0

    return (qn_of(stream), -codec_rank(stream, order), bandwidth_of(stream))


def _pick_one(
    streams: Sequence[Dict[str, Any]], order: Sequence[str], max_qn: int
) -> Optional[Dict[str, Any]]:
    if not streams:
        return None
    pool = [s for s in streams if isinstance(s, dict)]
    if max_qn > 0:
        limited = []
        for s in pool:
            try:
                if int(s.get("id", 0) or 0) <= max_qn:
                    limited.append(s)
            except (TypeError, ValueError):
                continue
        # 限档后一个都不剩时，保留原池：宁可给高一点的画质，也不要没有
        pool = limited or pool
    return max(pool, key=lambda s: _sort_key(s, order, max_qn))


def pick_streams(
    dash: Dict[str, Any],
    *,
    codec_preference: str = "",
    max_qn: int = 0,
) -> Tuple[Optional[StreamPair], Optional[StreamPair]]:
    """从 DASH 数据里挑出 (视频流, 音频流)，每路都是「主地址 + 备用列表」。

    :param dash: playurl 响应里的 ``data.dash``。
    :param codec_preference: ``BILI_CODEC`` 配置值，认不出来就用默认 AVC 优先。
    :param max_qn: 画质上限（清晰度数值）；``0`` 表示不限制。
    :return: ``(video, audio)``，任一路缺失时对应位置为 ``None``。
    """
    order = CODEC_ORDER.get(str(codec_preference or "").strip(), DEFAULT_CODEC_ORDER)

    def as_pair(streams: Any) -> Optional[StreamPair]:
        picked = _pick_one(streams or [], order, max_qn)
        if not picked:
            return None
        urls = _urls_of(picked)
        if not urls:
            return None
        return urls[0], urls[1:]

    return (
        as_pair((dash or {}).get("video")),
        as_pair((dash or {}).get("audio")),
    )
