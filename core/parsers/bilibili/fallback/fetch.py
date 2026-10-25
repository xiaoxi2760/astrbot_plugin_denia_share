"""B站备用取流：网络层。

``streams.py`` 负责「从已有数据里挑流」（纯函数、可离线测），本模块负责
「把流取回来」。两者分开，测试不必碰网络。

本模块由项目作者维护，沿用 Apache-2.0 许可。网络请求使用 httpx，
代理与超时由调用方控制。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .streams import PLAYURL_API, StreamPair, pick_single_file, pick_streams
from .wbi import WbiKeyProvider, sign_params

__all__ = ["build_playurl_params", "fetch_streams", "fetch_single_file"]

#: DASH 形态。fnval=4048 让接口同时给音视频分离流。
_FNVAL_DASH = 4048

#: 单文件形态。fnval=1 只给 durl（音视频合并好的完整文件），用于本机没有
#: ffmpeg 时的降级路径 —— DASH 分离流没有 ffmpeg 合并不了。
_FNVAL_DURL = 1


def build_playurl_params(
    bvid: str,
    cid: int,
    *,
    qn: int = 120,
    fnval: int = _FNVAL_DASH,
) -> Dict[str, Any]:
    """playurl 的查询参数。

    注意 **只支持 bvid** —— 2026-10-06 实测 ``aid`` 参数已返回 ``-400``，
    新旧 av 号都不行，所以这里没有 aid 分支。

    ``fnval`` 决定返回形态：``4048`` 给音视频分离的 DASH（主力），
    ``1`` 给音视频一体的 durl 单文件（缺 ffmpeg 时的降级路径）。
    """
    return {
        "bvid": bvid,
        "cid": cid,
        "qn": qn or 120,
        "fnver": 0,
        "fnval": fnval,
        "fourk": 1,
        "otype": "json",
        "platform": "pc",
        "high_quality": 1,
    }


async def fetch_streams(
    client_factory,
    bvid: str,
    cid: int,
    *,
    headers: Optional[Dict[str, str]] = None,
    cookie_header: str = "",
    codec_preference: str = "",
    max_qn: int = 0,
    key_provider: Optional[WbiKeyProvider] = None,
) -> Tuple[Optional[StreamPair], Optional[StreamPair], Dict[str, Any]]:
    """取一条视频的音视频流地址。

    :returns: ``(video, audio, raw_data)``。``video`` / ``audio`` 是
        ``(主地址, 备用地址列表)``；``raw_data`` 是 playurl 的原始 ``data``，
        供上层判断可访问性（会员专享 / 试看片段）时使用。

    :raises Exception: 接口返回非 0 ``code`` 时抛出，异常类型不重要 ——
        调用方的意图就是「备用失败就退回主力」，靠 ``except`` 判定。
    """
    data = await _request_playurl(
        client_factory,
        build_playurl_params(bvid, cid),
        headers=headers,
        cookie_header=cookie_header,
        key_provider=key_provider,
    )
    video, audio = pick_streams(
        data.get("dash") or {}, codec_preference=codec_preference, max_qn=max_qn
    )
    return video, audio, data


async def _request_playurl(
    client_factory,
    params: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    cookie_header: str = "",
    key_provider: Optional[WbiKeyProvider] = None,
) -> Dict[str, Any]:
    """发一次 playurl 请求并返回 ``data``。DASH 与单文件两条路径共用。

    Cookie 要**贯穿三处**，不只是 playurl：
      1. nav（取 WBI key）—— 未登录时 nav 照样返回 key，但登录态下更稳
      2. playurl —— 这个决定清晰度（实测未登录只给到 32 档，登录能给 112）
      3. view（在 parser.parse_video 里）—— 决定标题/统计/分P
    只在 playurl 上带 cookie 而漏掉前两处，效果就是「用户扫码登录了，
    备用模式下却还是匿名解析」—— 见 parser.update_cookie 的说明。
    """
    provider = key_provider or WbiKeyProvider(client_factory)

    request_headers = dict(headers or {})
    if cookie_header:
        request_headers["Cookie"] = cookie_header

    # 拿不到 key 就**不签名照发**，不抛异常：实测该端点不校验签名。
    key = await provider.get(request_headers)
    if key:
        params = sign_params(params, key)

    async with client_factory() as client:
        resp = await client.get(PLAYURL_API, params=params, headers=request_headers)
        resp.raise_for_status()
        payload = resp.json()

    code = (payload or {}).get("code")
    if code != 0:
        raise RuntimeError(f"B站 playurl 返回 code={code}: {(payload or {}).get('message')}")

    return (payload or {}).get("data") or {}


async def fetch_single_file(
    client_factory,
    bvid: str,
    cid: int,
    *,
    headers: Optional[Dict[str, str]] = None,
    cookie_header: str = "",
    key_provider: Optional[WbiKeyProvider] = None,
) -> Tuple[Optional[StreamPair], Dict[str, Any]]:
    """取**单个完整**的音视频一体文件（durl 形态）。

    存在的唯一理由是本机**没有 ffmpeg**：DASH 是音视频分离流，必须合并才能播，
    而合并需要 ffmpeg。这条路径让备用解析在缺 ffmpeg 时仍有视频可发
    （允许降画质 —— 有完整文件比有高画质但发不出去强）。

    :returns: ``(pair, raw_data)``。``pair`` 为 ``None`` 表示拿不到可用单文件：
        没有 durl，或者 durl 是多项分段（拼起来要 ffmpeg，正是这条路径缺的）。
    """
    data = await _request_playurl(
        client_factory,
        build_playurl_params(bvid, cid, fnval=_FNVAL_DURL),
        headers=headers,
        cookie_header=cookie_header,
        key_provider=key_provider,
    )
    return pick_single_file(data), data
