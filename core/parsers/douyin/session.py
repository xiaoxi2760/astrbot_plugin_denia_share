# 抖音分享页会话：cookie 保持 + 重试 + WAF 挑战处理。
#
# 背景
# ----
# 抖音分享页（www.iesdouyin.com/share/<ty>/<id>）只在请求**带有效 ttwid cookie**
# 时才把作品数据放进 window._ROUTER_DATA；不带 ttwid 的冷请求只返回页面上下文
# （ua / host / itemId 之类），没有 videoInfoRes。
#
# 实测（2026-09）：
#   全新 client、不带 cookie、只发一次  -> no-data  6/6 失败
#   冷请求                              -> no-data，但 Set-Cookie 带回 ttwid
#   全新 client + 只带那个 ttwid        -> HIT(1)   4/4 成功
#   全新 client + 假 ttwid=fake123      -> no-data  3/3（必须有效）
#   同一个 client 连发 6 次             -> 第 4 次才拿到 ttwid，第 5 次才出数据
#
# 也就是说：**冷请求只负责拿 ttwid，带着它再请求才有数据。**
# 连接池复用不影响，User-Agent 也不影响，只有 Cookie 头是开关。
#
# 所以本模块做两件事：
#   1. 把 cookie 跨请求存下来（会话按解析器类缓存，跨消息存活）
#   2. 「没拿到作品数据」的响应要重试，而不是直接当失败

import asyncio
import base64
import hashlib
import json
import re
from http.cookies import SimpleCookie

from httpx import AsyncClient

from astrbot.api import logger

ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
CHALLENGE_RE = re.compile(r'cs="([A-Za-z0-9+/=]+)"')

# 请求过密时抖音会返回一个 JS 挑战页（正文含 waf_js）
WAF_MARK = "waf_js"

# 承载作品数据的字段名。video 页是 videoInfoRes，图文页是 slidesInfoRes
# 或 noteDetailRes，三个都认。
WORK_KEYS = ("videoInfoRes", "slidesInfoRes", "noteDetailRes")

MAX_ATTEMPTS = 6
MAX_POW_ITER = 1_000_000


def _b64d(value: str) -> bytes:
    # 抖音的 cs 字段是不带 padding 的 base64，直接 b64decode 会报 Incorrect padding
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode()


def router_data(text: str) -> dict | None:
    """取出 window._ROUTER_DATA 的 JSON；取不到返回 None。"""
    matched = ROUTER_RE.search(text or "")
    if not matched:
        return None
    try:
        data = json.loads(matched.group(1).strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def has_work_data(text: str) -> bool:
    """_ROUTER_DATA 里是否真的带了作品数据（而不是只有页面上下文）。"""
    data = router_data(text)
    if data is None:
        return False
    for value in (data.get("loaderData") or {}).values():
        if not isinstance(value, dict):
            continue
        for key in WORK_KEYS:
            info = value.get(key)
            if isinstance(info, dict) and info.get("item_list"):
                return True
    return False


def looks_like_challenge(text: str) -> bool:
    """是不是被 WAF 挑战页拦了。"""
    return WAF_MARK in (text or "")


def solve_challenge(text: str) -> str | None:
    """解开 waf_js 的 PoW，返回 _wafchallengeid 的 cookie 值；解不开返回 None。

    挑战页里的 cs 是个 base64 的 JSON：
        {"v": {"a": <prefix 原始字节>, "c": <目标 sha256>}, ...}
    找 i 使 sha256(prefix + str(i)) == target 即可。实测 i 通常是个位数。
    """
    matched = CHALLENGE_RE.search(text or "")
    if not matched:
        return None
    try:
        payload = json.loads(_b64d(matched.group(1)))
        prefix = _b64d(payload["v"]["a"])
        target = _b64d(payload["v"]["c"]).hex()
    except Exception:
        return None

    for index in range(MAX_POW_ITER):
        if hashlib.sha256(prefix + str(index).encode()).hexdigest() == target:
            payload["d"] = _b64e(str(index).encode())
            return _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return None


class DouyinShareSession:
    """跨请求保持 cookie 的分享页会话。"""

    def __init__(self, client_kwargs):
        # client_kwargs 是 BaseParser.client_kwargs（classmethod），
        # 这样代理 / 超时 / 证书校验这些配置照样生效
        self._client_kwargs = client_kwargs
        self._cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def cookie_header(self) -> dict[str, str]:
        if not self._cookies:
            return {}
        joined = "; ".join(f"{name}={value}" for name, value in self._cookies.items())
        return {"Cookie": joined}

    def remember(self, response) -> None:
        try:
            raw = response.headers.get_list("set-cookie")
        except Exception:
            return
        for header in raw:
            jar = SimpleCookie()
            try:
                jar.load(header)
            except Exception:
                continue
            for name, morsel in jar.items():
                if morsel.value:
                    self._cookies[name] = morsel.value

    async def fetch(self, url: str, headers: dict[str, str]) -> str:
        """抓分享页，保持 cookie，并重试到真的拿到作品数据。

        第一发通常只拿回 ttwid（页面里没有作品数据），第二发带上去才有。
        """
        async with self._lock:
            text = await self._fetch_with_retries(url, headers)
            if has_work_data(text) or not self._cookies:
                return text

            # 重试完还是没有作品数据，而且我们**手里带着 cookie**：
            # 最可能是缓存的 ttwid 已失效。而带着失效 cookie 时服务端不会重新下发
            # （冷请求才会），所以必须清空 cookie 再冷来一次，否则会永久卡死。
            self._cookies.clear()
            logger.debug("[douyin] 带 cookie 重试仍未取到作品数据，清空会话冷请求一次")
            return await self._fetch_with_retries(url, headers)

    async def _fetch_with_retries(self, url: str, headers: dict[str, str]) -> str:
        async with AsyncClient(
            **self._client_kwargs(headers=headers, follow_redirects=True)
        ) as client:
            text = ""
            solved = False
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = await client.get(url, headers=self.cookie_header())
                except Exception:
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                self.remember(response)
                text = response.text or ""

                if has_work_data(text):
                    return text

                # 挑战只解一次：解完还拿不到数据说明不是挑战的问题（多半是限流），
                # 再解就是白烧 CPU（每次最多 MAX_POW_ITER 次 sha256）
                if not solved and looks_like_challenge(text):
                    solved = True
                    # PoW 是同步的 CPU 活儿，直接跑会把整个事件循环按住，丢线程池
                    challenge = await asyncio.to_thread(solve_challenge, text)
                    if challenge:
                        self._cookies["_wafchallengeid"] = challenge
                    else:
                        # 挑战解不开，重试没有意义，直接把页面交回去让上层报错
                        return text

                await asyncio.sleep(self._backoff(attempt))
            return text

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 0.3 + 0.2 * attempt


_SESSIONS: dict[type, DouyinShareSession] = {}


def get_share_session(parser_cls) -> DouyinShareSession:
    """按解析器类缓存会话，让 cookie 跨消息存活。"""
    session = _SESSIONS.get(parser_cls)
    if session is None:
        session = DouyinShareSession(parser_cls.client_kwargs)
        _SESSIONS[parser_cls] = session
    return session
