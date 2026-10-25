"""凭据导入的**纯静态**解析。

设计红线（对应 credential_contract.md）：

* cURL 只当文本解析，**永不执行**；
* 带域名的来源（Netscape / JSON Cookie 数组）只保留目标平台**精确域名或子域**，
  ``.bilibili.com.evil.invalid`` 这种后缀伪造必须整体拒绝；
* GitHub 只认独立 Token / ``Authorization`` 头，网站 Cookie 不拼 Token；
* 任何失败只抛 :class:`CredentialError`（消息里不含原文），不把输入回显出去。

本模块不依赖 httpx / AstrBot，纯函数，便于测试。
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Iterable

# 体积上限：256KB。超了直接拒绝，不做任何解析。
MAX_CONTENT_BYTES = 256 * 1024

#: 平台注册域名（Cookie 域匹配以它为准）。
PLATFORM_DOMAINS: dict[str, str] = {
    "bilibili": "bilibili.com",
    "xiaohongshu": "xiaohongshu.com",
    "pixiv": "pixiv.net",
    "github": "github.com",
    "netease": "music.163.com",
    "qqmusic": "qq.com",
    "xiaoheihe": "xiaoheihe.cn",
}

#: GitHub 走 Token，Cookie 输入一律拒绝。
TOKEN_PLATFORMS = frozenset({"github"})

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEADER_INJECT_RE = re.compile(r"[\r\n]")
_AUTH_RE = re.compile(
    r"^\s*(?:bearer|token)\s+(?P<token>[^\s,;]+)\s*$", re.IGNORECASE
)
_TOKEN_FIELD_KEYS = ("token", "access_token", "personal_access_token", "pat")
#: ``.env`` / 环境变量里常见的 Token 键名（小写比较）。只认这些键，别的变量一律忽略。
_ENV_TOKEN_KEYS = _TOKEN_FIELD_KEYS + ("github_token", "gh_token")
_BARE_TOKEN_RE = re.compile(r"^\s*(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\s*$")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


class CredentialImportError(ValueError):
    """导入内容不合法。消息面向用户，**不得包含原始输入**。"""


def _fail(reason: str) -> "CredentialImportError":
    return CredentialImportError(reason)


def _normalize_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches_platform(host: str, platform: str) -> bool:
    """域名是否为目标平台的**精确域名或子域**。

    ``bilibili.com`` / ``api.bilibili.com`` 为真；``bilibili.com.evil.invalid``
    与 ``evil.invalid`` 为假（后缀伪造天然被 ``.base`` 锚定排除）。
    """
    base = PLATFORM_DOMAINS.get(platform)
    if not base:
        return False
    host = _normalize_host(host)
    if not host:
        return False
    return host == base or host.endswith("." + base)


def _assert_no_injection(value: str, label: str) -> None:
    """拒绝换行/控制字符注入（防止把凭据拆成额外的请求头）。"""
    if _HEADER_INJECT_RE.search(value):
        raise _fail(f"{label}包含非法换行，已拒绝导入")
    if _CONTROL_RE.search(value):
        raise _fail(f"{label}包含非法控制字符，已拒绝导入")


def _check_size(content: str) -> str:
    if not isinstance(content, str):
        raise _fail("导入内容必须是文本")
    raw = content.strip()
    if not raw:
        raise _fail("导入内容为空")
    if len(raw.encode("utf-8", "ignore")) > MAX_CONTENT_BYTES:
        raise _fail("导入内容超过 256KB 限制")
    return raw


def _is_netscape(raw: str) -> bool:
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and "cookie" in stripped.lower():
            return True
        return stripped.startswith("#HttpOnly_") or len(stripped.split("\t")) >= 7
    return False


def _cookie_pairs_from_string(text: str, label: str) -> list[tuple[str, str]]:
    """解析 ``a=1; b=2`` 形式的 Cookie 串。"""
    pairs: list[tuple[str, str]] = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise _fail(f"{label}格式无法识别")
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or not value:
            raise _fail(f"{label}格式无法识别")
        _assert_no_injection(name, "Cookie 名")
        _assert_no_injection(value, "Cookie 值")
        if not _COOKIE_NAME_RE.fullmatch(name) or ";" in value:
            raise _fail("Cookie 条目含非法字符")
        if not _COOKIE_NAME_RE.match(name):
            raise _fail(f"{label}含非法 Cookie 名")
        pairs.append((name, value))
    if not pairs:
        raise _fail(f"{label}中没有任何 Cookie")
    return pairs


def _looks_like_cookie_dict(obj: dict[str, Any]) -> bool:
    keys = {str(k).strip().lower() for k in obj}
    if keys & {"cookies", "cookie"}:
        return True
    return all(_COOKIE_NAME_RE.match(str(k)) for k in obj) and bool(obj)


def _pairs_from_json_obj(obj: dict[str, Any], platform: str) -> list[tuple[str, str]]:
    """简单 Cookie 字典：``{"a": "1", "b": "2"}``。"""
    if not _looks_like_cookie_dict(obj):
        raise _fail("JSON 中未找到可识别的 Cookie 结构")
    return _cookie_pairs_from_string(
        "; ".join(f"{k}={v}" for k, v in obj.items() if isinstance(v, (str, int, float))),
        "Cookie 字典",
    ) if any(isinstance(v, (str, int, float)) for v in obj.values()) else _raise_empty()


def _raise_empty():
    raise _fail("JSON 中未找到可识别的 Cookie 结构")


def _pairs_from_cookie_list(items: Iterable[Any], platform: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise _fail("Cookie 数组元素必须是对象")
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value:
            raise _fail("Cookie 条目缺少 name/value")
        domain = str(item.get("domain") or item.get("host") or "").strip()
        _assert_no_injection(name, "Cookie 名")
        _assert_no_injection(value, "Cookie 值")
        if domain and not host_matches_platform(domain, platform):
            # 域名不属于目标平台 → 丢弃这一条，不是整体失败。
            continue
        pairs.append((name, value))
    if not pairs:
        raise _fail("未找到属于目标平台的 Cookie")
    return pairs


def _parse_json(raw: str, platform: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise _fail("JSON 格式错误，未导入任何内容")
    return _pairs_from_json_any(data, platform)


def _pairs_from_nested_cookie_dict(inner: dict[str, Any], platform: str) -> list[tuple[str, str]]:
    """``{"cookies": {"a": "1"}}``：只展开 cookies 里的内容。

    ``cookies`` 存在时**只**看它，绝不回退到同级的其它键——否则像
    ``{"cookies": {...}, "other": 7}`` 会把 ``other=7`` 一起导进来。
    """
    if not inner:
        raise _fail("JSON 中 cookies 字段为空")
    for key in ("cookies", "cookie"):
        if key in inner:
            # 再套一层（少见但结构合法）：继续只认 cookies。
            return _pairs_from_json_any(inner, platform)
    if not _looks_like_cookie_dict(inner):
        raise _fail("JSON 中 cookies 字段不是可识别的 Cookie 结构")
    return _pairs_from_json_obj(inner, platform)


def _pairs_from_json_any(data: Any, platform: str) -> list[tuple[str, str]]:
    if isinstance(data, list):
        # 可能被包成 {"cookies": [...]} 之外的裸数组
        return _pairs_from_cookie_list(data, platform)
    if isinstance(data, dict):
        for key in ("cookies", "cookie"):
            if key not in data:
                continue
            inner = data.get(key)
            if isinstance(inner, list):
                return _pairs_from_cookie_list(inner, platform)
            if isinstance(inner, str):
                return _cookie_pairs_from_string(inner, "cookies 字段")
            if isinstance(inner, dict):
                return _pairs_from_nested_cookie_dict(inner, platform)
            # cookies 存在但结构不认识（数字 / null / bool …）：明确拒绝，
            # 不能悄悄退回顶层字典，那会把同级元数据当成 Cookie 导入。
            raise _fail("JSON 中 cookies 字段结构无法识别")
        return _pairs_from_json_obj(data, platform)
    raise _fail("JSON 顶层结构无法识别")


def _parse_netscape(raw: str, platform: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        if stripped.startswith("#HttpOnly_"):
            stripped = stripped[len("#HttpOnly_"):]
        fields = stripped.split("\t")
        if len(fields) < 7:
            fields = re.split(r"\s{1,}", stripped)
        if len(fields) < 7:
            continue
        domain = fields[0].strip()
        name = fields[5].strip()
        value = fields[6].strip()
        _assert_no_injection(name, "Cookie 名")
        _assert_no_injection(value, "Cookie 值")
        if domain and not host_matches_platform(domain, platform):
            continue
        if name and value:
            pairs.append((name, value))
    if not pairs:
        raise _fail("未找到属于目标平台的 Cookie")
    return pairs


def _parse_curl(raw: str, platform: str) -> tuple[list[tuple[str, str]], str | None]:
    """静态提取 cURL 文本里的 Cookie / Token，并校验 URL 目标域名。"""
    try:
        words = shlex.split(raw.replace("^\n", "").replace("`\n", "").replace("\\\n", ""))
    except ValueError:
        raise _fail("cURL 引号或格式不完整") from None
    urls = []
    auth: str | None = None
    cookie_text: str | None = None
    index = 1
    while index < len(words):
        word = words[index]
        if word in ("-H", "--header", "-b", "--cookie", "--url"):
            index += 1
            if index >= len(words):
                raise _fail("cURL 参数不完整")
            value = words[index]
            if word in ("-b", "--cookie"):
                cookie_text = value
            elif word == "--url":
                urls.append(value)
            else:
                name, _, header_value = value.partition(":")
                if name.strip().lower() == "cookie":
                    cookie_text = header_value.strip()
                elif name.strip().lower() == "authorization":
                    auth = header_value.strip()
        elif word.startswith(("http://", "https://")):
            urls.append(word)
        index += 1

    if auth and platform not in TOKEN_PLATFORMS:
        # 把 GitHub 的 Authorization 头喂给 Cookie 平台 = 凭据串用，必须拒绝。
        raise _fail("cURL 中的 Authorization 头不能作为该平台的 Cookie 导入")
    if auth and platform in TOKEN_PLATFORMS and not cookie_text:
        cookie_text = None

    # cURL 的目标域名必须属于所选平台，否则等于把凭据交给别的主机。
    if not urls:
        raise _fail("cURL 中未找到目标网址")
    for url in urls:
        host = _url_host(url)
        if not host or not host_matches_platform(host, platform):
            raise _fail("cURL 目标域名与所选平台不符，已拒绝导入")

    if auth and platform in TOKEN_PLATFORMS:
        token = _normalize_token(auth, "Authorization 头")
        return [], token
    if cookie_text:
        return _cookie_pairs_from_string(cookie_text, "cURL Cookie"), None
    raise _fail("cURL 中未找到 Cookie 或 Token")


def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return _normalize_host(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def _normalize_token(value: str, label: str) -> str:
    value = (value or "").strip()
    _assert_no_injection(value, label)
    if value.lower().startswith(("bearer ", "token ")):
        value = value.split(" ", 1)[1].strip()
    if not value:
        raise _fail(f"{label}为空")
    if any(ch in value for ch in " \t;,=\"{}"):
        raise _fail(f"{label}不是合法的 Token")
    return value


def _strip_optional_quotes(value: str) -> str:
    """``.env`` 里常见的 ``KEY="值"``：只在首尾成对引号时去掉一层。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _token_candidates(raw: str) -> list[str]:
    """从文本里**逐行**抽取候选 Token（不校验换行：多行本身就是合法的 .env 片段）。

    返回的是尚未做换行/控制字符校验的原始候选值；校验只对抽取出的值做，
    绝不把整段多行文本当成一个请求头去校验。
    """
    entries = [line.strip() for line in raw.splitlines()]
    entries = [line for line in entries if line and not line.startswith("#")]
    candidates: list[str] = []
    saw_field = False
    for line in entries:
        if line.lower().startswith("authorization:"):
            line = line.partition(":")[2].strip()
            saw_field = True
            if not line:
                raise _fail("Token 为空")
        m = _AUTH_RE.match(line)
        if m:
            candidates.append(m.group("token"))
            continue
        key, sep, val = line.partition("=")
        if sep:
            name = key.strip()
            if name.lower().startswith("export "):
                name = name[len("export "):].strip()
            if name.lower() in _ENV_TOKEN_KEYS:
                saw_field = True
                candidates.append(_strip_optional_quotes(val.strip()))
                continue
            if len(entries) == 1:
                # 单行且是未知键的 k=v：更像 Cookie，保持原来的明确提示。
                raise _fail("GitHub 平台不接受 Cookie 格式的内容，请导入 Token 或 Authorization")
            # 多行片段里的其它变量（.env 常见）直接忽略，不当作 Token。
            continue
        if _BARE_TOKEN_RE.match(line):
            candidates.append(line)
            continue
    if not candidates:
        if saw_field:
            raise _fail("Token 为空")
        if len(entries) > 1:
            raise _fail("未识别到 GitHub Token")
        raise _fail("内容不是可识别的 GitHub Token")
    return candidates


def _parse_raw_token(raw: str) -> str:
    # 先抽取候选值，再对**抽取结果**做换行/控制字符校验（拒绝头注入）。
    tokens: list[str] = []
    for candidate in _token_candidates(raw):
        token = _normalize_token(candidate, "Token")
        if token not in tokens:
            tokens.append(token)
    if len(tokens) > 1:
        # 不猜、不取第一个：多个不同 Token 一律拒绝，避免写错账号。
        raise _fail("检测到多个不同的 GitHub Token，请只保留一个后再导入")
    return tokens[0]


def _parse_raw_cookie(raw: str, platform: str) -> list[tuple[str, str]]:
    """无格式提示时的兜底：只接受 Cookie 串或 ``Cookie:`` 头。"""
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    elif text.lower().startswith("cookie\t"):
        text = text.split("\t", 1)[1].strip()
    return _cookie_pairs_from_string(text, "Cookie")


def parse_credential(platform: str, content: str, fmt: str = "auto") -> str:
    """把任意来源的凭据文本解析成**最终要保存的字符串**。

    返回值就是写进配置项的内容（Cookie 平台为 ``k=v; k2=v2``，GitHub 为 token）。
    失败抛 :class:`CredentialImportError`。
    """
    if platform not in PLATFORM_DOMAINS:
        raise _fail("未知平台")
    fmt = (fmt or "auto").strip().lower()
    if fmt not in {"auto", "cookie", "token", "curl", "json", "netscape"}:
        raise _fail("不支持的导入格式")

    raw = _check_size(content)

    if platform in TOKEN_PLATFORMS:
        return _parse_any_token(raw, fmt)

    pairs = _parse_any_cookie(raw, platform, fmt)
    for name, value in pairs:
        _assert_no_injection(name, "Cookie 名")
        _assert_no_injection(value, "Cookie 值")
        if not _COOKIE_NAME_RE.fullmatch(name) or ";" in value:
            raise _fail("Cookie 条目含非法字符")
    return "; ".join(f"{name}={value}" for name, value in pairs)


def _parse_any_token(raw: str, fmt: str) -> str:
    if fmt in {"curl", "auto"} and _looks_like_curl(raw):
        _, token = _parse_curl(raw, "github")
        if token:
            return token
    if fmt in {"json", "auto"} and raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            if fmt == "json":
                raise _fail("JSON 格式错误，未导入任何内容")
        else:
            token = _token_from_json(data)
            if token:
                return token
            if fmt == "json":
                raise _fail("JSON 中未找到 GitHub Token 字段")
    return _parse_raw_token(raw)


def _token_from_json(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in _TOKEN_FIELD_KEYS:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return _normalize_token(val, "Token 字段")
        for key in ("authorization", "Authorization"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return _normalize_token(val, "Authorization 字段")
    return None


def _looks_like_curl(raw: str) -> bool:
    head = raw.lstrip()[:200].lower()
    return head.startswith(("curl ", "curl\t", "$ curl")) or "curl " in head


def _parse_any_cookie(raw: str, platform: str, fmt: str) -> list[tuple[str, str]]:
    if fmt == "curl" or (fmt == "auto" and _looks_like_curl(raw)):
        pairs, token = _parse_curl(raw, platform)
        if token:
            raise _fail("该 cURL 只包含 Token，无法作为 Cookie 导入")
        return pairs
    if fmt == "netscape" or (fmt == "auto" and _is_netscape(raw)):
        return _parse_netscape(raw, platform)
    if fmt == "json" or (fmt == "auto" and raw.lstrip()[:1] in "[{\""):
        if raw.lstrip()[:1] in "{[" or fmt == "json":
            return _parse_json(raw, platform)
    return _parse_raw_cookie(raw, platform)


__all__ = [
    "MAX_CONTENT_BYTES",
    "PLATFORM_DOMAINS",
    "TOKEN_PLATFORMS",
    "CredentialImportError",
    "host_matches_platform",
    "parse_credential",
]
