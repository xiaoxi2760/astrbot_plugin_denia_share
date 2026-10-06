"""B站备用取流：WBI 签名。

**这是备用实现，不是主力。** 主力仍是 ``bilibili-api-python``（上游在维护，
能跟住 B站接口变化）。本模块存在的唯一理由是：那个依赖哪天装不上、版本不兼容
或被下架时，插件不至于整个加载失败 —— 见 :mod:`core.parsers` 里的隔离说明。

来源：从 ``yaya``（娅娅解析 v7.6.0，Apache-2.0）的自建 B站解析器抽出签名部分。
算法本身没有版权问题（B站公开的 WBI 规则），但代码结构沿用 yaya 的写法，
按 Apache-2.0 保留来源说明。

## 两条 2026-10-06 实测结论，**决定了本模块的形态**

1. ``x/player/wbi/playurl`` **不校验 WBI 签名** —— 错误 ``w_rid``、错误
   ``mixin_key``、过期 ``wts`` 全部返回 ``code=0``。真正校验签名的是热评接口
   ``x/v2/reply/wbi/main``（不带签名返回 ``-403 访问权限不足``）。
2. 拿不到 key 时应当**不签名照发**，而不是抛异常 —— 这正是本模块存在的意义。

所以 ``get_mixin_key_or_none`` 返回 ``None`` 是**正常路径**而非异常路径：
调用方拿到 ``None`` 就直接发无签名请求，仍然能取到流。

## 负缓存为什么必须有

``nav`` 接口挂掉时，若不缓存失败，每次取流都要重打一次 nav。而取流路径上有
锁、锁内超时 15 秒，并发下会串成一条长队（实测配置允许 10 并发，最坏 150 秒）。
所以失败也要有 TTL —— 取 60 秒：远小于 key 的 6 小时有效期，够挡住并发雪崩，
又不至于让恢复后的第一次请求白等。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

__all__ = [
    "NAV_API",
    "WBI_KEY_TTL",
    "WBI_KEY_FAIL_TTL",
    "extract_key",
    "mixin_key",
    "sign_params",
    "av2bv",
    "WbiKeyProvider",
]

NAV_API = "https://api.bilibili.com/x/web-interface/nav"

#: 拿到 key 后的缓存时长。B站每天换一轮 key，6 小时足够安全。
WBI_KEY_TTL = 6 * 60 * 60
#: 取 key 失败后的「负缓存」时长，理由见模块文档。
WBI_KEY_FAIL_TTL = 60

# B站的 key 混排表：把 img_key + sub_key 拼成的 64 字符串按下标重排，取前 32 位。
_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)

# WBI 规定要从参数值里剔除的字符
_REMOVE_CHARS = "!'()*"

# ---- av 号 / BV 号互转（备用取流需要自己认 av 链接时用）----
_BV_TABLE = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_XOR_CODE = 23442827791579
_MAX_AID = 1 << 51
_BASE = 58


def extract_key(url: str) -> str:
    """从 ``.../wbi/<32位hex>.png`` 里取出那段 hex。"""
    return Path(urlparse(url).path).stem


def mixin_key(img_key: str, sub_key: str) -> str:
    """按 WBI 规则混排出 32 位 mixin_key。"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def sign_params(params: Dict[str, Any], key: str) -> Dict[str, Any]:
    """给参数加上 ``wts`` 与 ``w_rid``。

    B站的签名规则：按 key 排序 → 剔掉值里的 ``!'()*`` → urlencode → 拼 mixin_key
    → md5。顺序和剔除动作都会影响结果，少一步就签不出来。

    ⚠️ **返回值里所有值都是字符串**（含 ``wts``、``cid``）：剔除字符那一步统一
    做了 ``str()``。直接丢给 httpx/requests 的 ``params=`` 没问题（本来就要
    urlencode），但别指望拿它做整数比较。

    :returns: 新的参数字典，**不修改**传入的 ``params``。
    """
    signed = dict(params)
    signed["wts"] = int(time.time())
    signed = dict(sorted(signed.items(), key=lambda item: item[0]))

    cleaned: Dict[str, Any] = {}
    for name, value in signed.items():
        text = str(value)
        for ch in _REMOVE_CHARS:
            text = text.replace(ch, "")
        cleaned[name] = text

    cleaned["w_rid"] = hashlib.md5(
        (urlencode(cleaned) + key).encode("utf-8")
    ).hexdigest()
    return cleaned


def av2bv(av: int) -> str:
    """AV 号 → BV 号。备用取流要自己认 ``/video/av123`` 形式的链接时用。"""
    table = ["B", "V", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0"]
    index = len(table) - 1
    tmp = (_MAX_AID | av) ^ _XOR_CODE
    while tmp > 0:
        table[index] = _BV_TABLE[tmp % _BASE]
        tmp //= _BASE
        index -= 1
    table[3], table[9] = table[9], table[3]
    table[4], table[7] = table[7], table[4]
    return "".join(table)


class WbiKeyProvider:
    """按需取 WBI key，带正缓存与负缓存。

    用法::

        key = await provider.get()
        params = sign_params(base, key) if key else base

    ``get()`` 返回 ``None`` 表示「拿不到 key，走无签名」——这是**正常结果**，
    不要当异常处理（``x/player/wbi/playurl`` 不校验签名，见模块文档）。
    """

    def __init__(
        self,
        client_factory,
        *,
        key_ttl: float = WBI_KEY_TTL,
        fail_ttl: float = WBI_KEY_FAIL_TTL,
    ) -> None:
        """
        :param client_factory: 无参可调用，返回一个 ``httpx.AsyncClient`` 上下文。
            传进来而不是内部 new，是为了让调用方控制代理 / TLS / 超时。
        :param key_ttl: 正缓存时长（秒）。
        :param fail_ttl: 负缓存时长（秒），别设成 0，否则并发下会一直穿透。
        """
        self._client_factory = client_factory
        self._key_ttl = key_ttl
        self._fail_ttl = fail_ttl
        self._key: str = ""
        self._expires_at: float = 0.0
        self._fail_until: float = 0.0

    def peek(self) -> Optional[str]:
        """同步读缓存，不发请求。给测试和排障用。"""
        return self._key or None

    def _cached(self, now: float) -> Optional[str]:
        if self._fail_until and now < self._fail_until:
            return None
        if self._key and now < self._expires_at:
            return self._key
        return None

    async def get(self, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        now = time.monotonic()
        hit = self._cached(now)
        if hit is not None:
            return hit
        if self._fail_until and now < self._fail_until:
            return None  # 负缓存命中

        try:
            key = await self._fetch(headers or {})
        except Exception:
            # 拿不到就算了 —— 调用方会走无签名请求。
            self._fail_until = time.monotonic() + self._fail_ttl
            return None

        self._key = key
        self._expires_at = time.monotonic() + self._key_ttl
        self._fail_until = 0.0
        return key

    async def _fetch(self, headers: Dict[str, str]) -> str:
        request_headers = dict(headers)
        request_headers.setdefault("Accept", "application/json, text/plain, */*")
        async with self._client_factory() as client:
            resp = await client.get(NAV_API, headers=request_headers)
            resp.raise_for_status()
            payload = resp.json()

        wbi = ((payload or {}).get("data") or {}).get("wbi_img") or {}
        img_url = str(wbi.get("img_url") or "").strip()
        sub_url = str(wbi.get("sub_url") or "").strip()
        if not img_url or not sub_url:
            raise ValueError("nav 响应里没有 wbi_img.img_url / sub_url")

        img_key, sub_key = extract_key(img_url), extract_key(sub_url)
        if not img_key or not sub_key:
            raise ValueError("wbi key 解析为空")
        return mixin_key(img_key, sub_key)
