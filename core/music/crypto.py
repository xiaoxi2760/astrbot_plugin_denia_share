"""网易云 weapi 与 QQ 登录用到的加密/摘要小工具。

weapi 的规矩是固定的两步：

1. 明文 JSON 先用固定密钥做一次 AES-CBC，再用随机密钥做第二次；
2. 随机密钥本身用 RSA 公钥加密成 ``encSecKey`` 一起提交。

随机密钥只活在一次请求里，用完即弃——它不是凭据，也不写日志。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Final

from .models import MusicError

try:  # pycryptodome 是插件依赖；缺失时给出可读提示而不是 ImportError 栈
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except Exception:  # pragma: no cover - 仅在依赖损坏的环境触发
    AES = None  # type: ignore[assignment]
    pad = None  # type: ignore[assignment]

_PRESET_KEY: Final[bytes] = b"0CoJUm6Qyw8W8jud"
_IV: Final[bytes] = b"0102030405060708"
_PUBLIC_KEY: Final[str] = "010001"
_MODULUS: Final[str] = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4"
    "ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813c"
    "fe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
_SECRET_CHARS: Final[str] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_QR_ALPHABET: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _aes(text: str, key: bytes) -> str:
    if AES is None or pad is None:
        raise MusicError("缺少加密依赖 pycryptodome，无法使用网易云登录与取流")
    cipher = AES.new(key, AES.MODE_CBC, _IV)
    raw = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return base64.b64encode(raw).decode("ascii")


def _rsa(text: str) -> str:
    reversed_text = text[::-1]
    number = int(reversed_text.encode("utf-8").hex(), 16)
    encrypted = pow(number, int(_PUBLIC_KEY, 16), int(_MODULUS, 16))
    return format(encrypted, "x").zfill(256)


def weapi_form(payload: dict[str, Any]) -> dict[str, str]:
    """把明文 payload 变成 weapi 需要的两个表单字段。

    Args:
        payload: 明文请求体。

    Returns:
        ``{"params": ..., "encSecKey": ...}``，可直接作为 form data 提交。
    """
    if not isinstance(payload, dict):
        raise MusicError("网易云请求参数无效")
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    secret = "".join(secure_choice(_SECRET_CHARS) for _ in range(16))
    encrypted = _aes(_aes(text, _PRESET_KEY), secret.encode("ascii"))
    return {"params": encrypted, "encSecKey": _rsa(secret)}


def secure_choice(alphabet: str) -> str:
    """从给定字符集里取一个字符（用 ``secrets`` 级别的随机源）。"""
    return alphabet[os.urandom(1)[0] % len(alphabet)]


def random_ascii(length: int) -> str:
    """生成指定长度的随机 ASCII 串，用于一次性参数（如 OAuth 的 ui）。"""
    size = max(1, int(length))
    return "".join(secure_choice(_QR_ALPHABET) for _ in range(size))


def hash33(text: str, seed: int = 0) -> int:
    """腾讯系页面参数用的 ``hash33`` 变体（逐字符左移 5 位后累加异或）。"""
    value = int(seed)
    for char in str(text or ""):
        value = (value << 5) + value + ord(char)
    return 2147483647 & value


def music_u_of(cookie: str) -> str:
    """从 Cookie 串里抽出 ``MUSIC_U``，没有时返回空串。"""
    if not cookie:
        return ""
    for part in str(cookie).split(";"):
        key, _, value = part.partition("=")
        if key.strip() == "MUSIC_U":
            return value.strip()
    return ""
