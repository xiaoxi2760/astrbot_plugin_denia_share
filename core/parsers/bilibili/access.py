"""B站视频可访问性分析：把「拿不到完整视频」变成一句人话原因。

为什么需要
----------
原先受限视频（充电专属 / 大会员专享 / 付费专享 / 登录后可看）与「CDN 挂了」在产物上
长得**一模一样**：卡片少一个视频 + 一句「1 个视频下载失败」。用户无从判断该去登录、
该开会员，还是过一会儿重试 —— 这正是本仓反复强调的「产物在撒谎」。

更糟的是它连报错都报错错了：`bilibili-api` 的 `get_download_url` 对受限视频会
**抛 `ResponseCodeException`**（如 `-10403 大会员专享`），而 `download_video` 的
CDN 重试循环把它当成网络故障，逐个备用地址重试一遍，最后报
「视频下载失败，已尝试所有CDN」—— 把「没权限」说成了「网络问题」。

判定用的信号（字段都实测确认过，见 test/审查复核/修复报告.md 9.12）
------------------------------------------------------------------
- **能不能拿到完整视频**：``data.durl[].length`` 之和 vs ``data.timelength``。
  前者明显小于后者就是**只给了试看片段** —— 这个信号不需要会员账号就能看出来，
  是最可靠的一条。另有 ``data.is_preview`` 与 ``play_check.play_detail ==
  "PLAY_PREVIEW"`` 两个「直说」字段，有就直接采信。
- **是什么性质的受限**：view 接口的 ``data.rights``（``ugc_pay`` /
  ``ugc_pay_preview`` → 充电专属；``arc_pay`` / ``pay`` → 付费专享）
  加上 playurl 的 ``support_formats[].need_vip`` / ``need_login``。
  后两个字段**不是每次都在**（匿名请求实测就没有），所以只当补充。
- **接口直接报错**：抛出来的异常（带 ``code`` / ``msg``）。

设计取舍
--------
1. **纯函数、不碰网络**：自检能拿真实响应喂进去，把判定逻辑测住。
2. **认不出就当「可访问」**：宁可少说一句，也不要因为某个字段缺失就把能放的视频
   说成受限 —— 那会让用户白跑一趟去开会员。
3. ``full`` 的 message 是**空串**：正常视频不该多一句废话。
4. 时长文案复用 :func:`media_utils.fmt_duration`，不另起一份格式化 ——
   本仓在「平台展示名」上吃过「两份真相」的亏（见 constants.PLATFORMS 的说明）。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ...exception import ParseException
from ...media_utils import fmt_duration

# 受限性质 → 展示文案。改文案只动这里
RESTRICTION_LABELS: dict[str, str] = {
    "charge_exclusive": "充电专属",
    "paid_exclusive": "付费专享",
    "vip_exclusive": "大会员专享",
    "login_required": "登录后可看",
}

# 状态：full 完整 / preview_only 只有试看 / blocked 拿不到可播放流
STATUS_FULL = "full"
STATUS_PREVIEW_ONLY = "preview_only"
STATUS_BLOCKED = "blocked"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms_to_text(value: Any) -> str:
    """毫秒 → ``3:12`` / ``1:02:03``；拿不到或非正数返回空串。"""
    ms = _to_int(value)
    if ms is None or ms <= 0:
        return ""
    return fmt_duration(ms / 1000)


def unwrap_playurl(payload: Any) -> dict[str, Any]:
    """剥掉信封与 PGC 的 ``video_info`` 包装，拿到真正的播放数据。

    三种形态都要认：整个信封 ``{"code":0,"data":{...}}``、已经是 ``data``、
    以及 PGC 那种把播放数据塞在 ``video_info`` 里的写法。
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict) and inner:
        payload = inner
    video_info = payload.get("video_info")
    if isinstance(video_info, dict) and video_info:
        return video_info
    return payload


def resolve_restriction(
    rights: Any = None,
    *,
    need_vip: bool = False,
    need_login: bool = False,
    has_cookie: bool = False,
) -> tuple[str, str]:
    """推断受限性质，返回 ``(type, label)``；认不出返回 ``("", "")``。

    顺序有讲究：**充电专属 / 付费专享是作品本身的属性**（view 接口直接给），
    比 ``need_vip`` / ``need_login`` 这种「当前身份不够」的信号更具体，所以先判。
    """
    rights = rights if isinstance(rights, dict) else {}
    if rights.get("ugc_pay") or rights.get("ugc_pay_preview"):
        return "charge_exclusive", RESTRICTION_LABELS["charge_exclusive"]
    if rights.get("arc_pay") or rights.get("pay"):
        return "paid_exclusive", RESTRICTION_LABELS["paid_exclusive"]
    if need_vip:
        return "vip_exclusive", RESTRICTION_LABELS["vip_exclusive"]
    # 已经有 Cookie 还说「登录后可看」就是误导，所以这一条要看身份
    if need_login and not has_cookie:
        return "login_required", RESTRICTION_LABELS["login_required"]
    return "", ""


def _sum_durl_length(durl: Any) -> int | None:
    """累计 ``durl`` 各段的 ``length``（毫秒）；一段都没有返回 None。"""
    total = 0
    found = False
    for item in durl or []:
        if not isinstance(item, dict):
            continue
        length = _to_int(item.get("length"))
        if length is None:
            continue
        total += length
        found = True
    return total if found else None


def available_length_ms(data: dict[str, Any]) -> int | None:
    """当前**实际可播放**的时长（毫秒）。

    优先取 ``durl``；新版接口会按清晰度分组放在 ``durls`` 里，此时要挑当前
    ``quality`` 对应的那一组，否则会把所有清晰度加起来。
    """
    direct = _sum_durl_length(data.get("durl"))
    if direct is not None:
        return direct

    current_quality = data.get("quality")
    durls = data.get("durls") or []
    for item in durls:
        if not isinstance(item, dict):
            continue
        if current_quality is not None and item.get("quality") != current_quality:
            continue
        nested = _sum_durl_length(item.get("durl"))
        if nested is not None:
            return nested

    if durls and isinstance(durls[0], dict):
        return _sum_durl_length(durls[0].get("durl"))
    return None


def _build_message(
    status: str,
    label: str,
    available_ms: int | None,
    timelength_ms: int | None,
    error_text: str,
) -> str:
    """按状态拼人话。``full`` 返回空串 —— 正常视频不该多一句废话。"""
    if status == STATUS_FULL:
        return ""

    if status == STATUS_PREVIEW_ONLY:
        available = _ms_to_text(available_ms)
        full = _ms_to_text(timelength_ms)
        if available and full:
            span = f"{available} / {full}"
        elif available:
            span = f"{available} / 未知全长"
        elif full:
            span = f"未知可解析时长 / {full}"
        else:
            span = "时长未知"
        if label:
            return f"⚠️ {label}视频只解析到试看片段（{span}），完整视频拿不到"
        return f"⚠️ 只解析到试看片段（{span}），完整视频需要更高权限"

    detail = f"（{error_text}）" if error_text else ""
    if label:
        return f"⚠️ 无法解析{label}视频{detail}，已跳过视频下载"
    return f"⚠️ 拿不到可播放的视频流{detail}，已跳过视频下载"


def analyze_play_access(
    payload: Any = None,
    *,
    error: BaseException | None = None,
    rights: Any = None,
    has_cookie: bool = False,
) -> dict[str, Any]:
    """分析 playurl 响应（或它抛出的异常），判断这条视频能拿到多少。

    Args:
        payload: ``get_download_url`` 的原始返回（整个信封即可，内部会剥壳）。
        error: 调用抛出的异常。给了它就只看错误，不再看 payload。
        rights: view 接口的 ``data.rights``，用于判断受限性质。
        has_cookie: 当前是否带登录态，决定要不要说「登录后可看」。

    Returns:
        ``{"status", "restriction_type", "restriction_label", "message",
        "is_preview_only", "has_stream", "timelength_ms", "available_length_ms",
        "error_text"}``。``status`` 取 ``full`` / ``preview_only`` / ``blocked``。
    """
    envelope = payload if isinstance(payload, dict) else {}
    data = unwrap_playurl(envelope)

    error_text = ""
    if error is not None:
        # 异常里的 code 更有信息量（如 -10403），拼进去让日志可查
        code = getattr(error, "code", None)
        message = str(getattr(error, "msg", "") or error or "").strip()
        parts = [p for p in (f"code={code}" if code not in (None, 0) else "", message) if p]
        error_text = "；".join(parts)
        status = STATUS_BLOCKED
        is_preview = False
        has_stream = False
        timelength_ms = None
        available_ms = None
        need_vip = need_login = False
    else:
        support = data.get("support_formats") or []
        need_vip = any(
            isinstance(item, dict) and bool(item.get("need_vip")) for item in support
        )
        need_login = any(
            isinstance(item, dict) and bool(item.get("need_login")) for item in support
        )

        has_dash = bool((data.get("dash") or {}).get("video"))
        has_durl = bool(data.get("durl") or data.get("durls"))
        has_stream = has_dash or has_durl

        timelength_ms = _to_int(data.get("timelength"))
        available_ms = available_length_ms(data)

        play_check = data.get("play_check") or envelope.get("play_check") or {}
        play_detail = play_check.get("play_detail") if isinstance(play_check, dict) else None

        is_preview = bool(data.get("is_preview")) or play_detail == "PLAY_PREVIEW"
        if (
            not is_preview
            and timelength_ms
            and available_ms
            and available_ms < timelength_ms
        ):
            # 最可靠的一条：有流，但比全长短一截
            is_preview = True

        error_code = data.get("error_code")
        top_code = envelope.get("code")
        if has_stream and is_preview:
            status = STATUS_PREVIEW_ONLY
        elif has_stream:
            status = STATUS_FULL
        elif error_code not in (None, 0) or top_code not in (None, 0) or play_detail:
            status = STATUS_BLOCKED
            if not error_text:
                raw = data.get("message") or envelope.get("message") or ""
                parts = [
                    p
                    for p in (
                        f"code={error_code}" if error_code not in (None, 0) else "",
                        str(raw).strip() if raw and str(raw) != "0" else "",
                    )
                    if p
                ]
                error_text = "；".join(parts)
        else:
            # 既没有流、也没有任何「受限」的迹象：认不出，按可访问处理（不误杀）
            status = STATUS_FULL

    restriction_type, restriction_label = resolve_restriction(
        rights, need_vip=need_vip, need_login=need_login, has_cookie=has_cookie
    )

    return {
        "status": status,
        "restriction_type": restriction_type,
        "restriction_label": restriction_label,
        "is_preview_only": status == STATUS_PREVIEW_ONLY,
        "has_stream": has_stream,
        "timelength_ms": timelength_ms,
        "available_length_ms": available_ms,
        "error_text": error_text,
        "message": _build_message(
            status, restriction_label, available_ms, timelength_ms, error_text
        ),
    }


# ==================== 同群去重的「视频身份」 ====================
#
# 放在这里而不是各自解析器里：主力（``.parser``）与替补（``.fallback.parser``）
# 的 ``@handle`` 逐条对齐，身份归一也必须只有一份实现，否则两条路径的去重行为会
# 分叉 —— 那正是本仓反复吃过的「两份真相」的亏。
#
# 分两层，别混：
#   - :func:`video_dedup_identity` —— **纯本地、同步、不联网**，从已认领的匹配里
#     抽身份（裸 BV / av / 带参长链）；
#   - :func:`expand_bili_short_link` —— **异步**，只为 ``b23.tv`` / ``bili2233.cn``
#     这种「BV 藏在跳转后面」的短链补一跳；它复用的正是 ``parse_short`` 本来就要
#     跳的那一次，返回值再交给 ``parse``，整个请求仍然只跳一次。

#: BVID：``BV`` + 10 位 base58 字符（大小写敏感，不能 lower）。
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
#: av 号：6 位以上数字；转 int 以吃掉前导零（``av170001`` 与 ``av0170001`` 同一条）。
_AV_RE = re.compile(r"av(\d{6,})", re.IGNORECASE)
#: 分 P：``?p=2`` / ``&p=2``。只读不改写，值原样交给 ``int``。
_PAGE_QUERY_RE = re.compile(r"[?&]p=(\d{1,3})")
#: 裸号后的分P：``BV1xx411c7mD 2`` / ``av170001 2``（解析器的 ``page_num`` 组）。
_BARE_PAGE_RE = re.compile(r"(?:BV[0-9A-Za-z]{10}|av\d{6,})\s+(\d{1,3})\s*$", re.IGNORECASE)


def video_dedup_identity(searched: Any) -> str | None:
    """从解析器**实际认领的那一段**里抽出「同一个视频页」的规范身份。

    背景：同群去重原来的键是「匹配到的原文」，于是同一个视频换一种发法就认不出来 ——
    分享卡片里的 ``…/video/BV1xx?share_source=…&vd_source=…`` 与后来者直接发的裸
    ``BV1xx`` 是两条完全不同的字符串，第二个人仍会被解析、群里再出一张卡片。

    归一目标（**同一条视频、同一个分P** 必须得到同一个字符串）：

    - ``BV1GJ411x7h7`` / ``BV1GJ411x7h7 1`` / ``bilibili.com/video/BV1GJ411x7h7``
      / ``bilibili.com/video/BV1GJ411x7h7?spm_id_from=…``
      / ``m.bilibili.com/BV1GJ411x7h7?vd_source=…``  → ``bv:BV1GJ411x7h7:p1``
    - ``av170001`` 与 ``bilibili.com/video/av170001?p=2`` → ``av:170001`` / ``…:p2``

    **多P边界**：``p`` 参与身份，``p=2`` 与 ``p=1``（以及不带 p）不是同一条，
    去重不能把它们并掉 —— 不然第二个分P别人就再也发不出来了。

    **认不出就返回 None**：短链（``b23.tv`` / ``bili2233.cn``）的 BV 只能靠跳转
    拿到，本函数不联网，所以对短链返回 None。短链的跨形式去重由
    :func:`expand_bili_short_link` 先展开、再对本函数取身份；**认不出的短链
    绝不互相合并** —— 返回 None 让调用方回落到原文键，宁可少去重一次，
    也不能把两个不同视频归一成同一条。
    """
    text = ""
    groups: dict[str, Any] = {}
    try:
        # 真实现是 ``re.Match``：优先用命名组，避免「解析器认的是这段、身份又去
        # 正则重扫全文」这种二次解析走偏；group(0) 只作兜底。
        groups = searched.groupdict()
        text = searched.group(0) or ""
    except Exception:
        try:
            text = str(searched or "")
        except Exception:
            return None

    bvid = str(groups.get("bvid") or "").strip()
    if not bvid:
        match = _BV_RE.search(text)
        bvid = match.group(0) if match else ""

    if bvid:
        identity = f"bv:{bvid}"
    else:
        avid = str(groups.get("avid") or "").strip()
        if not avid:
            match = _AV_RE.search(text)
            avid = match.group(1) if match else ""
        if not avid:
            return None
        try:
            identity = f"av:{int(avid)}"
        except (TypeError, ValueError):
            return None

    page_text = str(groups.get("page_num") or "").strip()
    if not page_text:
        match = _PAGE_QUERY_RE.search(text) or _BARE_PAGE_RE.search(text)
        page_text = match.group(1) if match else ""
    try:
        page = int(page_text)
    except (TypeError, ValueError):
        page = 1
    # 主备解析器会把 p=0 映射到最后一 P，不能与默认第 1 P 合并。
    # 本地不知道总 P 数时保留原文键，交给解析器维持既有处理方式。
    if page <= 0:
        return None
    return f"{identity}:p{page}"


#: B站短链主机关键词，与两套解析器 ``@handle`` 的 keyword 保持一致。
SHORT_LINK_KEYWORDS = ("b23.tv", "bili2233")

#: 短链 → 展开结果 的缓存上限（条）。热链被一群人连着转发时，只有第一个人需要
#: 真的跳一次；后来者直接命中缓存拿身份，连跳转都省掉。
SHORT_LINK_CACHE_MAX = 128
#: 缓存生命周期（秒）。短链本身可能过期 / 被重指，缓存不能无限期信；600 秒与
#: 「重复解析间隔」默认值同量级，够覆盖一轮刷屏，又不会长期留住过期跳转。
SHORT_LINK_CACHE_TTL_SECONDS = 600.0

#: 缓存挂在解析器实例上的属性名。解析器随配置热重建时缓存自然一起清空。
_SHORT_LINK_CACHE_ATTR = "_bili_short_link_cache"


def _short_link_cache(parser: Any) -> dict[str, tuple[float, str, Any]] | None:
    """取（或惰性建）解析器实例上的短链缓存。拿不到实例属性就返回 None（不缓存）。"""
    cache = getattr(parser, _SHORT_LINK_CACHE_ATTR, None)
    if isinstance(cache, dict):
        return cache
    try:
        cache = {}
        setattr(parser, _SHORT_LINK_CACHE_ATTR, cache)
    except Exception:
        return None
    return cache


async def expand_bili_short_link(
    parser: Any, keyword: str, searched: Any
) -> tuple[str, Any] | None:
    """把 B站短链**跳一次**展开成解析器真正认领的 ``(keyword, searched)``。

    为什么需要：短链里没有 BV，去重要按「视频身份」判定就必须先知道它指向哪个
    视频。这里复用解析器现有的 :meth:`BaseParser.get_redirect_url`（与
    ``BaseParser.parse_with_redirect`` 同一条路径、同一套 headers / proxy），
    返回值由调用方**直接传给 ``parse``** —— 所以整个请求仍然只跳一次：
    这里的跳转就是原来 ``_parse_short_link`` 本来要跳的那一次，不是额外开销，
    也不会出现「先跳一次拿身份、parse 又跳一次」。

    展开结果按短链 URL 记入**有界缓存**（容量 :data:`SHORT_LINK_CACHE_MAX`、
    生命周期 :data:`SHORT_LINK_CACHE_TTL_SECONDS` 秒，挂在解析器实例上随热重建
    清空）：同一条热链被多人转发时只有第一个人真的跳，后来者直接命中缓存。
    **失败不缓存**，所以「跳转恢复后重试」不会被之前的失败挡住。

    返回 ``None`` = **不是短链，无需展开**（长链 / 裸 BV / av 一律走同步身份）。

    展开失败时按**现有解析失败策略**抛 :class:`ParseException`（与
    ``parse_with_redirect`` 认不出目的地时一致）：调用方会撤销去重占位并让
    下一个人重试；**不同短链各自失败、各自回落原文键，绝不互相合并**。
    """
    if keyword not in SHORT_LINK_KEYWORDS:
        return None
    text = str(searched.group(0) or "").strip()
    if not text:
        return None

    url = text if text.startswith(("http://", "https://")) else f"https://{text}"
    cache = _short_link_cache(parser)
    now = time.monotonic()
    if cache is not None:
        cached = cache.get(url)
        if cached is not None and cached[0] > now:
            return cached[1], cached[2]
        cache.pop(url, None)

    redirect_url = await parser.get_redirect_url(
        url, headers=parser.headers, proxy=parser.proxies
    )
    if not redirect_url or redirect_url == url:
        # 与 BaseParser.parse_with_redirect 的失败分支同一句话，便于日志对照
        raise ParseException(f"无法重定向 URL: {url}")
    # 目的地认不出（SilentException）时也照现有策略抛出：调用方撤销占位、放行。
    expanded = parser.search_url(redirect_url)

    if cache is not None:
        cache[url] = (now + SHORT_LINK_CACHE_TTL_SECONDS, expanded[0], expanded[1])
        while len(cache) > SHORT_LINK_CACHE_MAX:
            cache.pop(next(iter(cache)), None)
    return expanded
