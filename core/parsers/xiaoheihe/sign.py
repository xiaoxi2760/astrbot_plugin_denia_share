"""小黑盒网页端请求签名（hkey）。

协议来源（算法，不是逐行复制的实现）：

- https://github.com/huandu/heybox-url （MIT，Copyright (c) 2023 Huan Du；
  许可全文见 LICENSES/Heybox-URL-MIT.txt）
- https://github.com/DIYgod/RSSHub/blob/master/lib/routes/xiaoheihe/util.ts
  （仅用于定位原始协议来源，不移植 RSSHub 项目代码）

小黑盒会对 ``api.xiaoheihe.cn`` 的部分读接口校验（游戏简介接口除外）
``hkey`` + ``_time`` + ``nonce`` 三件套。本模块只依赖标准库，不包含设备指纹、
登录态或任何账号凭据；签名内容是「请求路径 + 时间 + 随机串」，不是用户身份。

本模块采用公开移动分享接口的签名协议。2026-10-20 公开样本的
``/bbs/app/link/tree`` 返回过 ``status=ok``，但部分帖子仍要求登录或验证；
签名成功不代表具有内容访问权限。官网另一套网页签名不能据此判定失效。
"""

from __future__ import annotations

import hashlib
import secrets
import time

#: 协议取值表。注意不是字母表顺序，不能随意替换。
_DICT = "JKMNPQRTX1234OABCDFG56789H"

#: 路径里允许出现的字符（用于把 ``//a///b`` 规整成 ``/a/b/``）。
__all__ = ["sign_path", "signed_params", "web_signed_params"]


def _md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _convert_byte(value: int) -> int:
    """GF(2^8) 上乘 2（多项式 0x1B）。"""
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _c3(value: int) -> int:
    return _convert_byte(value) ^ value


def _c2(value: int) -> int:
    return _c3(_convert_byte(value))


def _c1(value: int) -> int:
    return _c2(_c3(_convert_byte(value)))


def _c0(value: int) -> int:
    return _c1(value) ^ _c2(value) ^ _c3(value)


def _checksum(data: list[int]) -> int:
    """对 4 个字节做一次「类 AES 列混合」，返回 0..99 的校验值。"""
    values = [
        _c0(data[0]) ^ _c1(data[1]) ^ _c2(data[2]) ^ _c3(data[3]),
        _c3(data[0]) ^ _c0(data[1]) ^ _c1(data[2]) ^ _c2(data[3]),
        _c2(data[0]) ^ _c3(data[1]) ^ _c0(data[2]) ^ _c1(data[3]),
        _c1(data[0]) ^ _c2(data[1]) ^ _c3(data[2]) ^ _c0(data[3]),
    ]
    return sum(values) % 100


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def sign_path(path: str, timestamp: int | None = None, nonce: str = "") -> str:
    """计算 ``hkey``。

    Args:
        path: 接口路径，例如 ``/bbs/app/link/tree``；有无首尾斜杠、重复斜杠都可以。
        timestamp: 秒级时间戳；缺省取当前时间。
        nonce: 随机串；缺省随机生成。测试里传固定值即可得到确定结果。

    Returns:
        7 个字符的 ``hkey``（5 位前缀 + 2 位校验和）。
    """
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or _md5_hex(f"{timestamp}{secrets.token_hex(16)}").upper()

    normalized = "/" + "/".join(part for part in path.split("/") if part) + "/"
    # 服务端把 _time 当「发起时间」，签名内部用 +1；固定按协议加一。
    stamp = timestamp + 1

    nonce_hash = _md5_hex(_digits_only(nonce + _DICT)).lower()
    digits = _digits_only(_md5_hex(f"{stamp}{normalized}{nonce_hash}"))[:9]
    digits = digits.ljust(9, "0")

    key = ""
    current = int(digits)
    for _ in range(5):
        key += _DICT[current % len(_DICT)]
        current //= len(_DICT)

    suffix = str(_checksum([ord(ch) for ch in key[-4:]])).zfill(2)
    return key + suffix


def signed_params(
    path: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str | int]:
    """返回可直接拼进 query 的 ``hkey/_time/nonce``。"""
    current = int(time.time()) if timestamp is None else int(timestamp)
    actual_nonce = (
        _md5_hex(f"{current}{secrets.token_hex(16)}").upper()
        if nonce is None
        else nonce
    )
    return {
        "hkey": sign_path(path, current, actual_nonce),
        "_time": current,
        "nonce": actual_nonce,
    }


def web_signed_params(path: str, *, timestamp: int | None = None, nonce: str | None = None) -> dict[str, str | int]:
    """官网 web 客户端的签名，与 mobile 参数组配套的签名分别使用。

    协议核对自官网公开脚本 website/app/index-B0hvTGhe.js 的 Tr/Vwe；
    固定向量已和该脚本对照，不依赖设备指纹服务或额外加密库。
    """
    current = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or _md5_hex(f"{current}{secrets.token_hex(16)}").upper()
    table = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
    normalized = "/" + "/".join(part for part in path.split("/") if part) + "/"
    values = (
        "".join(table[:-2][ord(char) % len(table[:-2])] for char in str(current + 1)),
        "".join(table[ord(char) % len(table)] for char in normalized),
        "".join(table[ord(char) % len(table)] for char in nonce),
    )
    interleaved = "".join(value[index] for index in range(max(map(len, values))) for value in values if index < len(value))[:20]
    digest = _md5_hex(interleaved)
    tail = [ord(char) for char in digest[-6:]]
    suffix = (_checksum(tail[:4]) + sum(tail[4:])) % 100
    prefix = "".join(table[:-4][ord(char) % len(table[:-4])] for char in digest[:5])
    return {"hkey": f"{prefix}{suffix:02d}", "_time": current, "nonce": nonce}
