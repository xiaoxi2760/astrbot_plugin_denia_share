"""CookieCloud 端到端解密与按平台筛选。

只做三件事：

* 从自建 CookieCloud 服务拉取 ``GET /get/{uuid}`` 的 ``encrypted``；
* 在本机用 UUID 与密码解密（密码不上传、不写日志）；
* 把按域名组织的 Cookie 整理成插件可直接导入的 Cookie 串。

错误消息只描述失败类别，不回显 UUID、密码、密文或 Cookie 值。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import httpx
from .. import http_env  # noqa: F401 -- 与插件其他请求共用代理环境兼容处理

from .importers import (
    MAX_CONTENT_BYTES,
    PLATFORM_DOMAINS,
    TOKEN_PLATFORMS,
    CredentialImportError,
    host_matches_platform,
    parse_credential,
)

try:  # pycryptodome 是插件依赖；缺失时给出可读错误而不是 ImportError
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except Exception:  # pragma: no cover - 仅在依赖损坏的环境触发
    AES = None  # type: ignore[assignment]
    unpad = None  # type: ignore[assignment]

#: 响应体上限 2MiB；服务端异常时也不把内存拉爆。
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15.0

_SALTED_PREFIX = b"Salted__"
_BLOCK_SIZE = 16
_EVP_KEY_MATERIAL = 48  # AES-256 的 32 字节 key + 16 字节 IV

_UUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$")

#: 共享托管/容器域名上的 Cookie 可能来自其他租户，不作为平台登录态。
_CONTAINER_DOMAINS = frozenset(
    {
        "github.io",
        "gitlab.io",
        "vercel.app",
        "netlify.app",
        "pages.dev",
        "workers.dev",
        "herokuapp.com",
        "appspot.com",
        "cloudfront.net",
        "azurewebsites.net",
        "firebaseapp.com",
        "web.app",
        "blogspot.com",
        "wordpress.com",
        "wixsite.com",
        "weebly.com",
        "myshopify.com",
        "sharepoint.com",
        "notion.site",
        "readthedocs.io",
    }
)

#: CookieCloud 扩展用来保存 GitHub Token 的特殊 Cookie 名，不能当 Cookie 导入。
_GITHUB_TOKEN_NAMES = frozenset({"githubtoken", "github_token"})


class CookieCloudError(Exception):
    """面向用户的 CookieCloud 错误；消息里不含任何敏感原文。"""


def _endpoint(server_url: str, uuid: str) -> str:
    """校验服务地址与 UUID，拼出唯一的 GET 目标。"""
    raw = str(server_url or "").strip()
    key = str(uuid or "").strip()
    if not raw:
        raise CookieCloudError("请先填写 CookieCloud 服务地址")
    if len(raw) > 2048 or any(ch in raw for ch in "\r\n\t\\"):
        raise CookieCloudError("CookieCloud 服务地址无效")
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # 端口非法时在这里抛 ValueError
    except ValueError:
        raise CookieCloudError("CookieCloud 服务地址无效") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise CookieCloudError("CookieCloud 服务地址只支持 http 或 https")
    if not host or any(ch.isspace() for ch in parts.netloc):
        raise CookieCloudError("CookieCloud 服务地址无效")
    if parts.username is not None or parts.password is not None:
        raise CookieCloudError("CookieCloud 服务地址不能包含账号密码")
    if parts.query or parts.fragment:
        raise CookieCloudError("CookieCloud 服务地址不能包含查询参数或片段")
    if not _UUID_RE.fullmatch(key):
        raise CookieCloudError("CookieCloud UUID 格式无效")

    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    base = f"{parts.scheme.lower()}://{parts.netloc}{path}"
    return f"{base}/get/{quote(key, safe='')}"


async def _download(endpoint: str) -> bytes:
    """流式读取响应，拒绝重定向并限制体积。"""
    chunks: list[bytes] = []
    total = 0
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            async with client.stream("GET", endpoint) as response:
                if 300 <= response.status_code < 400:
                    raise CookieCloudError("CookieCloud 返回了重定向，已拒绝")
                if response.status_code != 200:
                    raise CookieCloudError(
                        f"CookieCloud 服务返回 HTTP {response.status_code}"
                    )
                length = response.headers.get("content-length")
                if length:
                    try:
                        if int(length) > MAX_RESPONSE_BYTES:
                            raise CookieCloudError("CookieCloud 响应超过 2MiB 限制")
                    except ValueError:
                        pass
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise CookieCloudError("CookieCloud 响应超过 2MiB 限制")
                    chunks.append(chunk)
    except CookieCloudError:
        raise
    except (httpx.HTTPError, OSError, ValueError):
        raise CookieCloudError("无法连接 CookieCloud 服务，请检查地址") from None
    return b"".join(chunks)


def _evp_bytes_to_key(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey（MD5、1 次迭代）派生 32 字节 key 与 16 字节 IV。"""
    material = b""
    previous = b""
    while len(material) < _EVP_KEY_MATERIAL:
        previous = hashlib.md5(previous + passphrase + salt).digest()
        material += previous
    return material[:32], material[32:_EVP_KEY_MATERIAL]


def _decrypt(encrypted: str, uuid: str, password: str, crypto_type: str = 'legacy') -> dict[str, Any]:
    """解开 ``Salted__`` + AES-256-CBC 的 CookieCloud 密文。"""
    if AES is None or unpad is None:
        raise CookieCloudError("缺少加密依赖 pycryptodome，无法解密 CookieCloud 数据")
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (ValueError, TypeError):
        raise CookieCloudError("CookieCloud 加密数据格式无效") from None
    passphrase = hashlib.md5(
        f"{uuid}-{password}".encode("utf-8")
    ).hexdigest()[:16].encode("ascii")
    if crypto_type == 'aes-128-cbc-fixed':
        key, iv, ciphertext = passphrase, bytes(16), raw
    elif crypto_type == 'legacy':
        if not raw.startswith(_SALTED_PREFIX) or len(raw) <= 16:
            raise CookieCloudError("CookieCloud 加密数据不是受支持的 Salted 格式")
        key, iv = _evp_bytes_to_key(passphrase, raw[8:16])
        ciphertext = raw[16:]
    else:
        raise CookieCloudError('CookieCloud 加密类型暂不支持')
    if not ciphertext or len(ciphertext) % _BLOCK_SIZE:
        raise CookieCloudError("CookieCloud 加密数据格式无效")
    try:
        plain = unpad(
            AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), _BLOCK_SIZE
        )
        data = json.loads(plain.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise CookieCloudError("CookieCloud 解密失败，请检查 UUID 与密码") from None
    if not isinstance(data, dict):
        raise CookieCloudError("CookieCloud 数据格式无效")

    cookie_data = data.get("cookie_data")
    if isinstance(cookie_data, dict):
        return cookie_data

    # 旧版服务直接返回「域名 -> Cookie 列表」，没有 cookie_data 包装。
    legacy = {
        key: value
        for key, value in data.items()
        if key != "local_storage_data" and isinstance(value, (list, dict))
    }
    if legacy:
        return legacy
    raise CookieCloudError("CookieCloud 数据中未找到 Cookie")


async def fetch_cookies(server_url: str, uuid: str, password: str) -> dict[str, Any]:
    """从自建服务拉取并本地解密，返回按域名组织的 ``cookie_data``。

    密码只参与本地解密，不会出现在 URL、请求头或请求体中。
    """
    endpoint = _endpoint(server_url, uuid)
    if not isinstance(password, str) or not password:
        raise CookieCloudError('请填写 CookieCloud 加密密码')
    body = await _download(endpoint)
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise CookieCloudError("CookieCloud 响应不是有效 JSON") from None
    if not isinstance(payload, dict):
        raise CookieCloudError("CookieCloud 响应格式无效")
    encrypted = payload.get("encrypted")
    if not isinstance(encrypted, str) or not encrypted.strip():
        raise CookieCloudError("CookieCloud 响应缺少加密数据")
    return _decrypt(encrypted.strip(), str(uuid or "").strip(), password, payload.get('crypto_type') or 'legacy')


def _domain_allowed(domain: str, platform: str) -> bool:
    """只接受该 Cookie 自己 domain 的精确域名或子域。"""
    host = str(domain or "").strip().lower().rstrip(".")
    if host.startswith("."):
        host = host[1:]
    if not host or len(host) > 253 or host.startswith(".") or ".." in host:
        return False
    if not _DOMAIN_RE.fullmatch(host):
        return False
    if host in _CONTAINER_DOMAINS or any(
        host.endswith("." + base) for base in _CONTAINER_DOMAINS
    ):
        return False
    return host_matches_platform(host, platform)


def _expired(entry: dict[str, Any], now: float) -> bool:
    value = entry.get("expirationDate")
    if value is None:
        value = entry.get("expires")
    if value is None or value == "":
        return False  # 会话 Cookie
    if isinstance(value, bool):
        return True
    try:
        expires = float(value)
    except (TypeError, ValueError):
        return True
    if expires <= 0:
        return False
    if expires > 100_000_000_000:  # 毫秒时间戳
        expires /= 1000.0
    return expires <= now


def _entries(raw: Any) -> Iterable[dict[str, Any]]:
    if isinstance(raw, dict):
        yield raw
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item


def _join_credentials(platform: str, entries: list[dict[str, Any]]) -> str:
    pairs: list[str] = []
    total = 0
    for entry in entries:
        try:
            parsed = parse_credential(
                platform, json.dumps([entry], ensure_ascii=False)
            )
        except (CredentialImportError, ValueError, TypeError):
            continue
        if not parsed:
            continue
        total += len(parsed.encode("utf-8"))
        if total > MAX_CONTENT_BYTES:
            return ""
        pairs.append(parsed)
    return "; ".join(pairs)


def credentials_from_cookies(
    data: dict[str, Any], platforms: Iterable[str]
) -> dict[str, str]:
    """把 ``cookie_data`` 转成 ``{平台 id: Cookie 字符串}``。

    只返回确实可用的平台；某条 Cookie 的 domain 缺失、过期、属于伪后缀、
    共享容器域名或带 ``GitHubToken`` 名称时会被丢弃，不影响同平台其他 Cookie。
    """
    if not isinstance(data, dict):
        raise CookieCloudError("CookieCloud 数据格式无效")
    nested = data.get("cookie_data")
    if isinstance(nested, dict):
        data = nested

    if isinstance(platforms, str):
        platforms = [platforms]
    now = time.time()
    result: dict[str, str] = {}
    for platform in dict.fromkeys(platforms or ()):
        if (
            not isinstance(platform, str)
            or platform not in PLATFORM_DOMAINS
            or platform in TOKEN_PLATFORMS
        ):
            continue
        cookies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for domain_key, group in data.items():
            if domain_key == "local_storage_data":
                continue
            for entry in _entries(group):
                name = entry.get("name")
                value = entry.get("value")
                domain = entry.get("domain")
                if domain is None:
                    domain = entry.get("host")
                if (
                    not isinstance(name, str)
                    or not isinstance(value, str)
                    or not name.strip()
                    or not value
                    or not isinstance(domain, str)
                ):
                    continue
                if name.strip().lower() in _GITHUB_TOKEN_NAMES:
                    continue
                if not _domain_allowed(domain, platform):
                    continue
                if _expired(entry, now):
                    continue
                key = name.strip()
                if key in seen:
                    continue
                seen.add(key)
                cookies.append(entry)
        if cookies:
            cookie = _join_credentials(platform, cookies)
            if cookie:
                result[platform] = cookie
    return result


__all__ = ["CookieCloudError", "credentials_from_cookies", "fetch_cookies"]
