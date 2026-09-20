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

from typing import Any

from .media_utils import fmt_duration

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
