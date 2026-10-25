"""HTTP 客户端与平台适配器的公共底座。

两条硬约束决定了这里的形状：

1. **不保留长连接**：每次调用新建一个 ``httpx.AsyncClient``，退出时必定
   ``aclose()``。插件是常驻进程，若把 client 挂在适配器上，账号换绑、代理
   改配之后旧连接会一直用旧配置。
2. **不吞异常细节**：网络层异常统一收窄成 :class:`MusicError`，消息只描述
   「发生了什么」，不带 URL、Cookie、响应体——这些会出现在聊天窗口里。
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Final

import httpx

from .. import http_env  # noqa: F401 -- shared NO_PROXY compatibility
from .models import MusicError

DEFAULT_TIMEOUT: Final[float] = 15.0
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@contextlib.asynccontextmanager
async def open_client(
    *,
    proxy: str | None = None,
    verify_ssl: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
    follow_redirects: bool = False,
) -> AsyncIterator[httpx.AsyncClient]:
    """打开一个请求上下文，退出时确保连接被关闭。

    ``transport`` 供离线单测注入 ``httpx.MockTransport``；真实运行时留空。
    """
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "verify": verify_ssl,
        # trust_env 让系统代理/证书环境变量照常生效；核心的 NO_PROXY 修正由
        # core.http_env 在导入期完成。
        "trust_env": True,
        "follow_redirects": follow_redirects,
    }
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    client = httpx.AsyncClient(**kwargs)
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def fetch_error(exc: BaseException) -> MusicError:
    """把传输层异常翻译成安全的 :class:`MusicError`。"""
    if isinstance(exc, MusicError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return MusicError("音乐平台响应超时，请稍后重试")
    if isinstance(exc, httpx.HTTPStatusError):
        return MusicError("音乐平台拒绝了这次请求，请稍后重试")
    if isinstance(exc, httpx.HTTPError):
        return MusicError("音乐平台连接失败，请检查网络或代理设置")
    return MusicError("音乐操作未完成，请稍后重试")


async def read_json(response: httpx.Response, *, what: str = "音乐平台") -> Any:
    """解析 JSON 响应；非 2xx 与非 JSON 都收窄成安全错误。"""
    if response.status_code >= 400:
        raise MusicError(f"{what}返回异常状态，请稍后重试")
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise MusicError(f"{what}返回了无法解析的数据") from exc


def as_dict(value: Any) -> dict[str, Any]:
    """尽力把任意 JSON 节点收窄成字典，失败时给空字典。"""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def text_of(value: Any, default: str = "") -> str:
    """只接受非空字符串，其余（数字、None、脏数据）一律回退。"""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or default
    return default


def int_of(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def float_of(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def safe_media_url(value: Any) -> str:
    """只接受真正的 http(s) 媒体地址，并拒绝 ``testfile`` 这类测试域名。

    QQ 音乐的接口在无权限时偶尔会下发 ``testfile`` 开头的占位地址，直接下发
    会让用户收到一段无法播放的音频，所以在这里统一拦掉。
    """
    url = text_of(value)
    if not url or len(url) > 2048:
        return ""
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return ""
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password:
        return ""
    if host.startswith("testfile") or ".testfile" in host:
        return ""
    return url


def cookie_value(cookie: str, name: str) -> str:
    """从 ``a=1; b=2`` 形式的 Cookie 串里取一个值（只做最小的切分）。"""
    wanted = str(name or "").strip()
    if not wanted or not cookie:
        return ""
    for part in str(cookie).split(";"):
        key, _, value = part.partition("=")
        if key.strip() == wanted:
            return value.strip()
    return ""


class BaseProvider:
    """平台适配器的公共构造与客户端管理。

    子类只实现四个业务方法；网络细节（代理、证书校验、关闭连接）都在这里。
    """

    platform: str = ""

    def __init__(
        self,
        cookie: str = "",
        proxy: str | None = None,
        verify_ssl: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cookie = str(cookie or "").strip()
        self.proxy = str(proxy).strip() if proxy else None
        self.verify_ssl = bool(verify_ssl)
        self.timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT
        self.transport = transport

    # -- 网络 ---------------------------------------------------------------
    def client(self, *, follow_redirects: bool = False):
        """返回本次调用专用的请求上下文（异步上下文管理器）。"""
        return open_client(
            proxy=self.proxy,
            verify_ssl=self.verify_ssl,
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=follow_redirects,
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        what: str = "音乐平台",
        follow_redirects: bool = False,
    ) -> Any:
        """发一个请求并解析 JSON，异常一律转成安全的 :class:`MusicError`。"""
        try:
            async with self.client(follow_redirects=follow_redirects) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, data=data, json=json
                )
                return await read_json(response, what=what)
        except MusicError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一收窄，避免把底层细节抛给用户
            raise fetch_error(exc) from None

    # -- 业务 ---------------------------------------------------------------
    async def search(self, keyword: str, limit: int = 5) -> list:
        raise NotImplementedError

    async def detail(self, identifier: str):
        raise NotImplementedError

    async def play(self, song):
        raise NotImplementedError

    async def check_login(self) -> dict[str, str]:
        raise NotImplementedError
