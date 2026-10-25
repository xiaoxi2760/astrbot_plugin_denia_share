"""B站备用取流：playurl 调用与流选择。

**备用实现**，主力是 ``bilibili-api-python``。见 :mod:`core.parsers.bilibili.fallback.wbi`
的模块文档了解为什么要留这一份。

本模块由项目作者维护，沿用 Apache-2.0 许可。

## 取流选择规则

1. **排序键是「画质优先、编码只做同画质内的 tiebreak」**。
   只按清晰度和码率排序无法体现用户的编码偏好，因此编码偏好也写入排序键，
   但放在清晰度之后。

   ⚠️ 顺序反了就是灾难：若编码优先于清晰度，qn=16 的 AVC 会赢过 qn=120 的
   AV1，画质从 4K 直接掉到 360P。

2. **读 ``backupUrl``**。实测每条流都带 2 个备用 CDN 地址，
   调用方拿到候选列表后逐个尝试。

3. **2026-10-06 实测**：每个清晰度档都返回 3 个编码变体（AVC / HEVC / AV1），
   且 HEVC 的 sample entry 已从 ``hev1`` 换代为 ``hvc1``。所以识别编码时
   **codecid 优先、``codecs`` 字符串兜底**，不能只认 ``hev1``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..access import STATUS_FULL, analyze_play_access

__all__ = [
    "PLAYURL_API",
    "CODEC_ORDER",
    "codec_rank",
    "pick_streams",
    "pick_single_file",
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


def _durl_items(data: Dict[str, Any]) -> List[Any]:
    """取出当前清晰度对应的 ``durl`` 条目列表。

    新版接口会按清晰度分组放在 ``durls`` 里，必须挑当前 ``quality`` 那一组，
    否则会把所有清晰度的分段混在一起（对照 ``core/parsers/bilibili/access.py`` 的
    ``available_length_ms`` 同款处理）。

    **组内条目一律不预过滤**（包括畸形项、非 dict）：调用方要按**原始长度**
    判断「这是不是分段流」。先滤掉畸形项再数长度，会把 ``[完好的第一段, {}]``
    洗成单文件，然后把一段残片当完整视频发出去。
    """
    direct = list(data.get("durl") or [])
    if direct:
        return direct

    groups = [g for g in (data.get("durls") or []) if isinstance(g, dict)]
    if not groups:
        return []
    current = data.get("quality")
    matched = [g for g in groups if current is not None and g.get("quality") == current]
    group = matched[0] if matched else groups[0]
    # 组内条目**原样返回**：一个都不能滤。调用方按**原始长度**判「这是不是
    # 分段流」—— 先把畸形项（``None`` / ``{}``）滤掉再数长度，会把
    # ``[完好的第一段, {}]`` 洗成单文件，然后把一段残片当完整视频发出去。
    raw = group.get("durl")
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def pick_single_file(data: Dict[str, Any]) -> Optional[StreamPair]:
    """从 playurl 数据里挑出**单个完整**的音视频一体文件（durl 形态）。

    存在的唯一理由是本机**没有 ffmpeg**：DASH 是音视频分离流，必须合并才能播，
    而合并需要 ffmpeg。这条路径让备用解析在没有 ffmpeg 时仍有视频可发。

    **多项 durl 一律拒绝**：多项 durl 是同一视频的**顺序分段**（``bili_access``
    就是靠 ``durl[].length`` 求和识别试看片段的），不是同一文件的备用地址。
    拼起来需要 ffmpeg —— 而这条路径的前提正是没有 ffmpeg，所以拿它没有办法；
    宁可返回 ``None``，也不能发一个残缺文件。

    真正的 CDN 备用地址取自**该项自带的** ``backup_url``，与 DASH 流的
    ``backupUrl`` 同义（见 :func:`_urls_of`）。

    **长度判定看的是原始列表，不是过滤后的结果**。先滤掉「没有 url 的条目」
    再看剩几个，等于把分段流悄悄洗成单文件：``[{"url": ...}, {}]`` 这种畸形
    第二段会被丢掉，然后第一段被当成完整视频发出去 —— 用户看到的是一段
    没有声音的残片。
    """
    items = _durl_items(data or {})
    if len(items) != 1:
        return None
    item = items[0]
    if not isinstance(item, dict) or not item.get("url"):
        return None

    # **单段 ≠ 完整**。一个 durl 一样可能是试看片段或被限制：地址正常、只有一段，
    # 但内容被截断。所以这里也要判可访问性 —— 只数长度会把「试看」当成完整。
    access = analyze_play_access(payload=data)
    if access.get("status") != STATUS_FULL:
        return None

    # **不能复用 _urls_of**：它取的是 DASH 流的 ``baseUrl``，而 durl 的主地址
    # 字段叫 ``url``。照搬会得到空列表，表现为「单文件回退永远拿不到」。
    backups = item.get("backup_url") or item.get("backupUrl") or []
    if isinstance(backups, str):
        backups = [backups]
    urls = [u for u in ([item["url"]] + list(backups)) if u]
    if not urls:
        return None
    return urls[0], urls[1:]
