"""小黑盒（xiaoheihe）解析器。

支持四类公开分享入口：

- BBS 帖子：``/app/bbs/link/<id>``、``/v3/bbs/link/<id>``
- BBS 分享 API：``/v3/bbs/app/api/web/share?link_id=<id>``
- 游戏详情 / 专题：``/games/detail/<appid>``、``/app/topic/game/<game_type>/<appid>``
- 游戏分享：``/game/share_game_detail?appid=<appid>&game_type=<type>``

实现要点：

1. **只认完整 host + path**。正则先卡死 ``xiaoheihe.cn`` 的三种合法 host，
   再在运行时用 ``urlsplit`` 校验 scheme / 端口 / userinfo；恶意前缀、嵌套
   URL、``xiaoheihe.cn.evil.com`` 都不会被当成帖子。
2. **hkey 独立成小模块**（:mod:`.sign`）。不主动登录或生成设备指纹；只使用
   宿主显式传入的 Cookie 或 ``device_token``。未配置时尝试匿名读取，
   响应不完整会标明，要求登录或验证时停止解析。
3. **媒体全部惰性**：图片 / 视频 / m3u8 都走 ``PathTask.lazy``，仅文本或仅
   卡片模式不会提前下载。
4. **输出组合沿用 BaseParser**：``create_images`` 负责 50 张截断，``create_video``
   负责时长与体积策略，m3u8 交给下载器的 ``download_m3u8``；不支持时明确降级，
   绝不把封面当成视频。
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, urljoin, urlsplit

from httpx import HTTPError

from astrbot.api import logger

from ...base_parser import BaseParser, ParseException, PlatformEnum, handle
from ...data import Platform, platform_of
from ...exception import SilentException
from ...task import PathTask
from . import sign

# ────────────────────────────── 链接识别 ────────────────────────────── #

# 匹配必须从「独立的 URL 起点」开始：前面不能是 URL 里会出现的字符。
# 这样 ``?url=https://…`` / ``https://evil.com/https://…`` 这类嵌套链接不会
# 被从内层重新识别成一个可解析的小黑盒链接。
_START = r"(?i)(?<![A-Za-z0-9_./:=?&%#@+~-])"
_HOST = r"(?:www\.|api\.)?xiaoheihe\.cn"
# host 之后必须立刻是路径 / 查询 / 结束；排除 ``xiaoheihe.cn.evil.com``
# 与 ``xiaoheihe.cn:8443``（端口由运行时再兜一层）。
_HOST_END = r"(?![A-Za-z0-9.-])"

_BBS_APP_PATTERN = (
    _START + r"https?://" + _HOST + r"/app/bbs/link/"
    r"(?P<link_id>[A-Za-z0-9_-]{1,128})(?![\w.-])" + _HOST_END
)
_BBS_V3_PATTERN = (
    _START + r"https?://" + _HOST + r"/v3/bbs/link/"
    r"(?P<link_id>[A-Za-z0-9_-]{1,128})(?![\w.-])" + _HOST_END
)
_BBS_SHARE_PATTERN = (
    _START + r"https?://" + _HOST + r"/v3/bbs/app/api/web/share\?"
    r"(?P<query>[A-Za-z0-9._~%!$&()*+,;=:/?@#-]+)" + _HOST_END
)
_GAME_DETAIL_PATTERN = (
    _START + r"https?://" + _HOST + r"/games/detail/"
    r"(?P<appid>\d{1,20})(?!\d)" + _HOST_END
)
_GAME_TOPIC_PATTERN = (
    _START + r"https?://" + _HOST + r"/app/topic/game/"
    r"(?P<game_type>[a-z]{2,16})/(?P<appid>\d{1,20})(?!\d)" + _HOST_END
)
_GAME_SHARE_PATTERN = (
    _START + r"https?://" + _HOST + r"/game/share_game_detail\?"
    r"(?P<query>[A-Za-z0-9._~%!$&()*+,;=:/?@#-]+)" + _HOST_END
)

# 分享文字的中文标点作为 URL 边界；切分后仍须校验完整域名与路径。
_SHARE_URL_CANDIDATE_RE = re.compile(
    r"""[^\s<>"'"""
    r"""\u2010-\u206f\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff60]+"""
)

_ALLOWED_HOSTS = frozenset({"xiaoheihe.cn", "www.xiaoheihe.cn", "api.xiaoheihe.cn"})
_SUPPORTED_GAME_TYPES = frozenset({"pc", "console", "mobile"})

_API_BASE = "https://api.xiaoheihe.cn"
_WEB_BASE = "https://www.xiaoheihe.cn"
_REFERER = "https://www.xiaoheihe.cn/"

# BBS 接口的公开参数。这是网页端 / RSSHub 都在用的最小集合；不包含账号字段。
_BBS_COMMON_PARAMS: dict[str, str] = {
    "os_type": "web",
    "app": "heybox",
    "client_type": "mobile",
    "version": "999.0.3",
    "x_client_type": "web",
    "x_os_type": "Mac",
    "x_app": "heybox",
    "heybox_id": "-1",
}

# 游戏接口沿用网页端参数。game_introduction 不需要签名，其余需要 hkey。
_GAME_COMMON_PARAMS: dict[str, str] = {
    "os_type": "web",
    "app": "heybox",
    "client_type": "web",
    "version": "999.0.4",
    "web_version": "2.5",
    "x_client_type": "web",
    "x_app": "heybox_website",
    "heybox_id": "",
    "x_os_type": "Windows",
    "device_info": "Chrome",
}

# 捕获组让 re.split 保留图片标签，供后续提取地址。
_HTML_IMG_RE = re.compile(r"(<img\b[^>]*>)", re.IGNORECASE)
_HTML_IMG_SRC_RE = re.compile(
    r"""(?:data-original|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
#: 单独取懒加载大图与 ``data-src``：通用规则会把 ``data-src`` 里的 ``src``
#: 也认出来，靠属性书写顺序决定结果，不可预测。
_HTML_IMG_DATA_ORIGINAL_RE = re.compile(
    r"""data-original\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_HTML_IMG_DATA_SRC_RE = re.compile(
    r"""data-src\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_HTML_IMG_SRC_ATTR_RE = re.compile(
    r"""(?<![\w-])src\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_HTML_SOURCE_RE = re.compile(
    r"""<source\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif|gif|bmp)(?:[?#]|$)", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|m4v|mov|webm)(?:[?#]|$)", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    """去标签 / 解实体 / 收紧空白。"""
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _http_url(value: Any) -> str:
    """仅接受 http(s) 且带 host 的媒体地址；``data:`` / 相对路径一律丢弃。"""
    if value is None:
        return ""
    url = html.unescape(str(value)).strip()
    if not url or any(ch in url for ch in ("\n", "\r", "\t", " ")):
        return ""
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username or parsed.password:
        return ""
    return url


def _image_key(url: str) -> str:
    """图片去重键：忽略查询串与大小写 host，合并 ``imgheybox1`` 镜像域。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host == "imgheybox1.max-c.com":
        host = "imgheybox.max-c.com"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _unique_http_urls(values: list[str], *, image: bool) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        url = _http_url(value)
        if not url:
            continue
        key = _image_key(url) if image else url
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def _load_blocks(value: Any) -> list[Any]:
    """把 ``text`` / ``content`` / ``item_list`` 统一成块列表。

    接口既可能给 JSON 字符串，也可能已经反序列化成 list/dict；解析失败时
    退化成单段纯文本，不把整条帖子丢掉。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return [{"type": "text", "text": raw}]
        return _load_blocks(decoded)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("content", "item_list", "items", "blocks", "text"):
            nested = value.get(key)
            if nested is not None and nested is not value:
                blocks = _load_blocks(nested)
                if blocks:
                    return blocks
        return [value]
    return []


def _duration_seconds(*values: Any) -> float | None:
    """把时长归一成秒。接口同时出现过秒与毫秒，按数值大小做保守判断。"""
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, dict):
            milliseconds = value.get("milliseconds")
            if milliseconds is not None:
                try:
                    return max(float(milliseconds) / 1000.0, 0.0) or None
                except (TypeError, ValueError):
                    pass
            seconds = value.get("seconds")
            if seconds is not None:
                try:
                    return max(float(seconds), 0.0) or None
                except (TypeError, ValueError):
                    pass
            nested = _duration_seconds(value.get("duration"))
            if nested is not None:
                return nested
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        # 大于 1000 的常见值是毫秒；一个超过 16 分钟的秒数也会被误判成毫秒，
        # 但接口的时长字段通常是毫秒，且这里只用于上限判定，宁可少下不超时。
        if number > 1000:
            number /= 1000.0
        return number
    return None


def _query_value(url: str, key: str) -> str:
    try:
        values = parse_qs(urlsplit(url).query).get(key)
    except ValueError:
        return ""
    return str(values[0]) if values else ""


class XiaoheiheParser(BaseParser):
    """小黑盒 BBS 帖子与游戏分享解析。"""

    platform: ClassVar[Platform] = platform_of(PlatformEnum.XIAOHEIHE)

    def __init__(self, downloader, device_token: str = "", transport=None, cookie: str = ""):
        """构造解析器。

        Args:
            downloader: 宿主注入的 ``StreamDownloader``；与其他解析器一致。
            device_token: 可选的 ``x_xhh_tokenid``（形如 ``B…``）。匿名接口
                只返回标题 / 摘要时，宿主可把已配置的设备令牌传进来以恢复
                完整正文。**默认空**，解析器不会自己去申请设备指纹。
            transport: 可选的 ``httpx`` transport，仅测试用；生产保持 ``None``。
            cookie: 可选的用户登录态，仅发给固定 API，不随媒体请求发送。
        """
        super().__init__(downloader)
        # 登录态只发送给固定 API，不进入媒体和头像共用的请求头。
        from ...credentials.importers import CredentialImportError, parse_credential
        from ...cookie_utils import ck2dict
        self._cookie_invalid = False
        self._cookie = ""
        if cookie:
            try:
                self._cookie = parse_credential("xiaoheihe", cookie)
            except CredentialImportError:
                self._cookie_invalid = True
        cookie_values = ck2dict(self._cookie) if self._cookie else {}
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": _REFERER,
                "Origin": _WEB_BASE,
            }
        )
        token = (device_token or cookie_values.get("x_xhh_tokenid", "")).strip()
        if token and not token.startswith("B"):
            token = "B" + token
        self._device_token = token
        self._device_id = token[1:] if token.startswith("B") else ""
        self._account_id = cookie_values.get("heybox_id", "")
        self._transport = transport

    # ────────────────────────────── 入口 ────────────────────────────── #

    @handle("xiaoheihe", _BBS_APP_PATTERN)
    @handle("xiaoheihe", _BBS_V3_PATTERN)
    @handle("xiaoheihe", _BBS_SHARE_PATTERN)
    async def _parse_bbs(self, searched: re.Match[str]):
        link_id = self._bbs_link_id(searched)
        return await self._parse_bbs_by_id(link_id)

    @handle("xiaoheihe", _GAME_DETAIL_PATTERN)
    @handle("xiaoheihe", _GAME_TOPIC_PATTERN)
    @handle("xiaoheihe", _GAME_SHARE_PATTERN)
    async def _parse_game(self, searched: re.Match[str]):
        appid, game_type = self._game_target(searched)
        return await self._parse_game_by_appid(appid, game_type)

    # ────────────────────────────── URL 校验 ────────────────────────────── #

    @classmethod
    def search_url(cls, url: str) -> tuple[str, re.Match[str]]:
        """先走 BaseParser 的 ``search``，再校验命中的完整 URL。

        ``BaseParser`` 的 keyword 匹配是 ``in`` + ``pattern.search``；这里补上
        host / 端口 / userinfo / path 白名单，避免「正则匹配到子串」就被当成
        合法分享链接。
        """
        for keyword, pattern in cls._key_patterns:
            if keyword not in url.lower():
                continue
            searched = pattern.search(url)
            if searched is None:
                continue
            candidate = _SHARE_URL_CANDIDATE_RE.match(url[searched.start():])
            full_url = candidate.group(0).rstrip(".,!?);]}") if candidate else searched.group(0)
            cls._validate_matched_url(full_url)
            return keyword, searched
        raise SilentException("不是支持的小黑盒链接")

    @classmethod
    def _validate_matched_url(cls, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise SilentException(f"不是有效的小黑盒链接: {url}") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise SilentException("小黑盒链接只支持 http/https")
        if parsed.username or parsed.password:
            raise SilentException("小黑盒链接不允许包含用户信息")
        if port not in (None, 80, 443):
            raise SilentException("小黑盒链接不允许非标准端口")
        host = (parsed.hostname or "").lower()
        if host not in _ALLOWED_HOSTS:
            raise SilentException(f"不是小黑盒域名: {host}")
        segments = [part for part in parsed.path.split("/") if part]
        if any(part in {".", ".."} for part in segments):
            raise SilentException("小黑盒链接不允许路径穿越")
        paths = (
            r"/(?:app|v3)/bbs/link/[A-Za-z0-9_-]{1,128}/?",
            r"/v3/bbs/app/api/web/share",
            r"/games/detail/\d{1,20}/?",
            r"/app/topic/game/[a-z]{2,16}/\d{1,20}/?",
            r"/game/share_game_detail",
        )
        if not any(re.fullmatch(path, parsed.path, re.I) for path in paths):
            raise SilentException("不是支持的小黑盒路径")

    @staticmethod
    def _bbs_link_id(searched: re.Match[str]) -> str:
        groups = searched.groupdict()
        link_id = str(groups.get("link_id") or "").strip()
        if not link_id:
            query = str(groups.get("query") or "")
            link_id = _query_value("https://api.xiaoheihe.cn/?" + query, "link_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", link_id or ""):
            raise SilentException("小黑盒帖子 ID 无效")
        return link_id

    @staticmethod
    def _game_target(searched: re.Match[str]) -> tuple[str, str]:
        groups = searched.groupdict()
        appid = str(groups.get("appid") or "").strip()
        game_type = str(groups.get("game_type") or "").strip().lower()
        if not appid:
            query = str(groups.get("query") or "")
            appid = _query_value("https://api.xiaoheihe.cn/?" + query, "appid")
            game_type = _query_value("https://api.xiaoheihe.cn/?" + query, "game_type")
            game_type = game_type.strip().lower()
        if not game_type and "/games/detail/" in searched.group(0):
            # 老式详情页没有 game_type 段，按历史语义固定 pc。
            game_type = "pc"
        if not re.fullmatch(r"\d{1,20}", appid or ""):
            raise SilentException("小黑盒游戏 appid 无效")
        if game_type not in _SUPPORTED_GAME_TYPES:
            raise ParseException(f"暂不支持的小黑盒游戏类型: {game_type or '未知'}")
        return appid, game_type

    @classmethod
    def dedup_identity(cls, searched: re.Match[str]) -> str | None:
        """同一条资源的不同链接形态归一到同一个身份。

        - BBS：短链 / v3 / 分享 API 都归一到 ``bbs:<link_id>``；
        - 游戏：``game:<game_type>:<appid>``，**保留 game_type**，避免 PC 与
          主机同名 appid 被合并成一条。
        """
        try:
            if "/bbs/" in searched.group(0).lower():
                return f"xiaoheihe:bbs:{cls._bbs_link_id(searched)}"
            appid, game_type = cls._game_target(searched)
            return f"xiaoheihe:game:{game_type}:{appid}"
        except (SilentException, ParseException):
            return None

    # ────────────────────────────── BBS ────────────────────────────── #

    async def _parse_bbs_by_id(self, link_id: str):
        payload = await self._fetch_link_tree(link_id)
        result_data = payload.get("result")
        if not isinstance(result_data, dict):
            raise ParseException("小黑盒帖子数据为空")
        link = result_data.get("link")
        if not isinstance(link, dict):
            raise ParseException("小黑盒帖子数据缺少 link 字段")
        self._assert_same_post(link, link_id)

        title = _clean_text(link.get("title")) or "小黑盒帖子"
        author = self._bbs_author(link)
        timestamp = self._timestamp(link)
        text, images, videos = self._bbs_content(link)
        has_body = any(_load_blocks(link.get(key)) for key in ("text", "content", "item_list"))
        if not text:
            summary = _clean_text(link.get("description"))
            if summary:
                text = summary

        image_urls = _unique_http_urls(images, image=True)
        video_items = self._dedup_videos(videos)
        if not text and not image_urls and not video_items:
            raise ParseException("小黑盒帖子没有可提取的图文内容")

        canonical_id = _query_value(str(link.get("share_url") or ""), "link_id")
        canonical = f"{_WEB_BASE}/app/bbs/link/{canonical_id or link_id}"
        result = self.result(
            title=title,
            text=text or None,
            author=author,
            timestamp=timestamp,
            url=canonical,
            extra={
                "source_kind": "xiaoheihe_bbs",
                "content_type": "帖子",
                "has_video": bool(video_items),
            },
        )
        if not has_body:
            result.extra.setdefault("limit_warnings", []).append(
                "ℹ️ 接口未返回完整正文，仅显示可获取的摘要或配图"
            )

        for url, cover, duration in video_items:
            content = self._make_video(result, url, cover, duration)
            if content is not None:
                result.contents.append(content)
        result.contents.extend(self.create_images(image_urls))
        return result

    async def _fetch_link_tree(self, link_id: str) -> dict[str, Any]:
        path = "/bbs/app/link/tree"
        use_web = bool(self._cookie or self._device_token)
        params = dict(_GAME_COMMON_PARAMS if use_web else _BBS_COMMON_PARAMS)
        params.update({"link_id": link_id, "owner_only": "1"})
        if self._account_id:
            params["heybox_id"] = self._account_id
        params.update(sign.web_signed_params(path) if use_web else sign.signed_params(path))
        headers = dict(self.headers)
        if self._device_token:
            params["device_id"] = self._device_id
            headers["Cookie"] = f"x_xhh_tokenid={self._device_token}"

        payload = await self._request_json(
            f"{_API_BASE}{path}", params=params, headers=headers
        )
        self._raise_for_api_status(payload, "帖子")
        return payload

    @staticmethod
    def _raise_for_api_status(payload: dict[str, Any], what: str) -> None:
        """把上游状态翻成**短**提示，绝不把原始 JSON / query 回显给用户。"""
        status = str(payload.get("status") or "").strip()
        if status == "ok":
            return
        message = str(payload.get("msg") or "")
        lowered = message.lower()
        if status in {"show_captcha", "need_captcha", "lack_token", "need_login", "relogin"}:
            raise ParseException("小黑盒要求登录或验证，请在账号凭据中导入 Cookie；若仍受限，请先在网页完成验证")
        if "频繁" in message or "rate" in lowered:
            raise ParseException("小黑盒访问过于频繁，请稍后再试")
        if "验证参数" in message or "非法请求" in message or "缺少必要参数" in message:
            raise ParseException("小黑盒接口校验失败，可能触发了风控")
        if "错误" in message or status == "failed":
            raise ParseException(f"小黑盒{what}接口返回异常")
        raise ParseException(f"小黑盒{what}接口返回异常: {status or '未知状态'}")

    @staticmethod
    def _assert_same_post(link: dict[str, Any], requested: str) -> None:
        """响应里带 ID 时必须与请求一致，防止把别的帖子渲染出来。"""
        returned_short = _query_value(str(link.get("share_url") or ""), "link_id")
        returned_numeric = str(link.get("linkid") or link.get("link_id") or "").strip()
        candidates = {value for value in (returned_short, returned_numeric) if value}
        if not candidates:
            return
        if requested in candidates:
            return
        # 请求是短 ID、响应只给数字 ID 时无法本地映射，按「认不出」放过；
        # 反过来请求数字 ID、响应只给短 ID 也一样。两者都给出却不匹配才是错帖。
        if returned_short and returned_numeric:
            raise ParseException("小黑盒返回了其他帖子的数据")
        if requested.isdigit() and returned_numeric and requested != returned_numeric:
            raise ParseException("小黑盒返回了其他帖子的数据")
        if not requested.isdigit() and returned_short and requested != returned_short:
            raise ParseException("小黑盒返回了其他帖子的数据")

    @staticmethod
    def _timestamp(link: dict[str, Any]) -> int | None:
        for key in ("create_at", "created_at", "timestamp", "modify_at"):
            value = link.get(key)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 10_000_000_000:  # 毫秒时间戳
                number //= 1000
            if number > 0:
                return number
        return None

    def _bbs_author(self, link: dict[str, Any]):
        user = link.get("user") or link.get("author") or {}
        if not isinstance(user, dict):
            return None
        name = _clean_text(user.get("username") or user.get("nickname") or "")
        if not name:
            return None
        avatar = _http_url(user.get("avatar") or user.get("avartar"))
        return self.create_author(name, avatar or None)

    def _bbs_content(self, link: dict[str, Any]) -> tuple[str, list[str], list[tuple[str, str, float | None]]]:
        text_parts: list[str] = []
        images: list[str] = []
        videos: list[tuple[str, str, float | None]] = []
        for source in ("text", "content", "item_list"):
            blocks = _load_blocks(link.get(source))
            if not blocks:
                continue
            self._consume_blocks(blocks, text_parts, images, videos)

        if not images:
            for key in ("image", "cover", "thumb", "thumbnail"):
                images.append(_http_url(link.get(key)))
            raw_images = link.get("images")
            if isinstance(raw_images, list):
                for item in raw_images:
                    if isinstance(item, dict):
                        images.append(_http_url(item.get("url") or item.get("src")))
                    else:
                        images.append(_http_url(item))
            elif isinstance(raw_images, dict):
                images.append(_http_url(raw_images.get("url") or raw_images.get("src")))

        if not videos and (
            link.get("has_video")
            or link.get("video_url")
            or isinstance(link.get("video"), dict)
        ):
            video = link.get("video") if isinstance(link.get("video"), dict) else {}
            url = _http_url(link.get("video_url") or video.get("url") or video.get("video_url"))
            if url:
                cover = _http_url(
                    link.get("video_cover")
                    or link.get("cover")
                    or link.get("image")
                    or video.get("cover")
                    or video.get("thumbnail")
                )
                duration = _duration_seconds(
                    link.get("video_duration"),
                    link.get("duration"),
                    video.get("duration"),
                )
                videos.append((url, cover, duration))

        text = "\n\n".join(part for part in text_parts if part).strip()
        return text, images, videos

    def _consume_blocks(
        self,
        blocks: list[Any],
        text_parts: list[str],
        images: list[str],
        videos: list[tuple[str, str, float | None]],
    ) -> None:
        for block in blocks:
            if isinstance(block, str):
                cleaned = _clean_text(block)
                if cleaned:
                    text_parts.append(cleaned)
                continue
            if isinstance(block, list):
                self._consume_blocks(block, text_parts, images, videos)
                continue
            if not isinstance(block, dict):
                continue

            block_type = str(block.get("type") or "").strip().lower()
            if block_type in {"img", "image", "photo"}:
                images.append(
                    _http_url(
                        block.get("url")
                        or block.get("src")
                        or block.get("image")
                        or block.get("data_original")
                    )
                )
                continue
            if block_type in {"video", "gif", "movie"}:
                url = _http_url(
                    block.get("url")
                    or block.get("video_url")
                    or block.get("play_url")
                    or block.get("src")
                )
                if not url:
                    continue
                is_gif = block_type == "gif" or url.lower().split("?", 1)[0].endswith(".gif")
                if is_gif and _IMAGE_EXT_RE.search(url):
                    images.append(url)
                else:
                    cover = _http_url(
                        block.get("cover")
                        or block.get("thumbnail")
                        or block.get("poster")
                    )
                    duration = _duration_seconds(
                        block.get("duration"), block.get("video_duration")
                    )
                    videos.append((url, cover, duration))
                continue
            if block_type in {"item_list", "list", "items"}:
                nested = block.get("items") or block.get("item_list") or block.get("content")
                nested_blocks = _load_blocks(nested)
                if nested_blocks:
                    self._consume_blocks(nested_blocks, text_parts, images, videos)
                continue

            raw_text = block.get("text")
            if raw_text is None:
                raw_text = block.get("content") or block.get("value")
            if isinstance(raw_text, str) and raw_text.strip():
                if block_type == "html" or "<" in raw_text:
                    self._consume_html(raw_text, text_parts, images)
                else:
                    cleaned = _clean_text(raw_text)
                    if cleaned:
                        text_parts.append(cleaned)
                continue

            # 有些响应把图文放在 ``data`` / ``children`` 里。
            for key in ("data", "children", "item_list", "items"):
                nested_blocks = _load_blocks(block.get(key))
                if nested_blocks:
                    self._consume_blocks(nested_blocks, text_parts, images, videos)
                    break

    def _consume_html(
        self, fragment: str, text_parts: list[str], images: list[str]
    ) -> None:
        """按图片标签拆分 HTML，分别保留正文和图片的出现顺序。"""
        for part in _HTML_IMG_RE.split(fragment):
            if not part:
                continue
            if _HTML_IMG_RE.fullmatch(part):
                url = self._img_url(part)
                if url:
                    images.append(url)
                continue
            cleaned = _clean_text(part)
            if cleaned:
                text_parts.append(cleaned)

    @staticmethod
    def _img_url(tag: str) -> str:
        """优先取原图，再取 src / data-src；仅接受有效 http(s) 地址。"""
        for pattern in (
            _HTML_IMG_DATA_ORIGINAL_RE,
            _HTML_IMG_SRC_ATTR_RE,
            _HTML_IMG_DATA_SRC_RE,
        ):
            for value in pattern.findall(tag):
                url = _http_url(value)
                if url:
                    return url
        return ""

    @staticmethod
    def _dedup_videos(
        videos: list[tuple[str, str, float | None]]
    ) -> list[tuple[str, str, float | None]]:
        seen: set[str] = set()
        result: list[tuple[str, str, float | None]] = []
        for url, cover, duration in videos:
            normalized = _http_url(url)
            if not normalized:
                continue
            key = normalized.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            result.append((normalized, _http_url(cover), duration))
        return result

    def _make_video(
        self,
        result,
        url: str,
        cover: str,
        duration: float | None,
    ):
        is_m3u8 = ".m3u8" in url.lower()
        self._add_limit_warning(result, duration)
        if is_m3u8:
            downloader = self.downloader
            if not hasattr(downloader, "download_m3u8"):
                result.extra.setdefault("limit_warnings", []).append(
                    "ℹ️ 该视频是 m3u8，当前下载器不支持，已跳过"
                )
                return None
            task = PathTask.lazy(
                lambda: downloader.download_m3u8(url, ext_headers=self.headers)
            )
            return self.create_video(task, cover or None, duration)
        return self.create_video(url, cover or None, duration)

    # ────────────────────────────── 游戏 ────────────────────────────── #

    async def _parse_game_by_appid(self, appid: str, game_type: str):
        game_type = game_type.lower()
        if game_type not in _SUPPORTED_GAME_TYPES:
            raise ParseException(f"暂不支持的小黑盒游戏类型: {game_type or '未知'}")

        game: dict[str, Any] = {}
        detail_error: HTTPError | None = None
        detail_warning = ""
        try:
            game = await self._fetch_game_detail(appid, game_type)
        except HTTPError as exc:
            # 详情接口是可选增强；失败时先保留异常，若后面的公开简介也拿不到
            # 任何东西，再把原始 httpx 异常抛出去，不把网络故障伪装成空结果。
            detail_error = exc
            logger.debug(
                f"[xiaoheihe] 游戏详情接口不可用: {type(exc).__name__}"
            )
        except ParseException as exc:
            # 只将上游访问状态作为降级原因；身份不一致必须继续拒绝。
            if "返回了其他" in str(exc):
                raise
            detail_warning = str(exc)

        try:
            intro = await self._fetch_game_intro(appid) if game_type == "pc" else {}
        except HTTPError:
            if not game:
                raise
            intro = {}

        topic_detail = game.get("topic_detail")
        topic_name = (
            topic_detail.get("name") if isinstance(topic_detail, dict) else None
        )
        name = _clean_text(game.get("name") or game.get("name_en") or topic_name)
        if not name:
            name = await self._fetch_share_title(appid, game_type)

        raw_about = str(
            game.get("about_the_game") or intro.get("about_the_game") or ""
        )
        about = _clean_text(raw_about)
        images, videos = self._game_media(game, raw_about)

        if not name and not about and not images and not videos:
            if detail_error is not None:
                raise detail_error
            if detail_warning:
                raise ParseException(detail_warning)
            raise ParseException("未获取到小黑盒游戏资料")

        if not name:
            name = f"小黑盒游戏 {appid}"
            missing_name = True
        else:
            missing_name = False

        result = self.result(
            title=name,
            text=about or None,
            author=self._game_author(game, intro),
            url=f"{_WEB_BASE}/app/topic/game/{game_type}/{appid}",
            extra={
                "content_type": "游戏",
                "source_kind": "xiaoheihe_game",
                "game_appid": appid,
                "game_type": game_type,
                "steam_appid": game.get("steam_appid") or intro.get("steam_appid"),
            },
        )
        info_lines = self._game_info_lines(game, intro)
        if detail_warning:
            result.extra.setdefault("limit_warnings", []).append(f"ℹ️ 游戏详情获取受限：{detail_warning}")
        if info_lines:
            result.extra["info"] = "\n".join(info_lines)
        if missing_name:
            result.extra.setdefault("limit_warnings", []).append(
                "ℹ️ 小黑盒未返回游戏名称，标题使用 appid 代替"
            )
        if not game and intro:
            result.extra.setdefault("limit_warnings", []).append(
                "ℹ️ 仅获取到游戏简介（开发商 / 发行日期），无封面或截图"
            )

        for url, cover, duration in self._dedup_videos(videos):
            content = self._make_video(result, url, cover, duration)
            if content is not None:
                result.contents.append(content)
        result.contents.extend(self.create_images(_unique_http_urls(images, image=True)))
        return result

    async def _fetch_game_detail(self, appid: str, game_type: str) -> dict[str, Any]:
        path = "/game/get_game_detail/"
        params = dict(_GAME_COMMON_PARAMS)
        params.update({"appid": appid, "game_type": game_type})
        if self._account_id:
            params["heybox_id"] = self._account_id
        params.update(sign.web_signed_params(path))
        headers = dict(self.headers)
        if self._device_token:
            params["device_id"] = self._device_id
            headers["Cookie"] = f"x_xhh_tokenid={self._device_token}"
        payload = await self._request_json(
            f"{_API_BASE}{path}", params=params, headers=headers
        )
        self._raise_for_api_status(payload, "游戏")
        result = payload.get("result")
        if not isinstance(result, dict):
            return {}
        self._check_game_identity(result, appid, game_type)
        return result

    async def _fetch_game_intro(self, appid: str) -> dict[str, Any]:
        payload = await self._request_json(
            f"{_API_BASE}/game/game_introduction/",
            params={"steam_appid": appid, "return_json": "1"},
            headers=self.headers,
        )
        if str(payload.get("status") or "") != "ok":
            return {}
        result = payload.get("result")
        if not isinstance(result, dict):
            return {}
        returned = result.get("steam_appid")
        if returned is not None and str(returned) != appid:
            raise ParseException("小黑盒返回了其他游戏的简介")
        return result

    @staticmethod
    def _check_game_identity(game: dict[str, Any], appid: str, game_type: str) -> None:
        for key in ("appid", "app_id"):
            if game.get(key) is not None and str(game[key]) != appid:
                raise ParseException("小黑盒返回了其他游戏的数据")
        if game.get("game_type") and str(game["game_type"]).lower() != game_type:
            raise ParseException("小黑盒返回了其他类型的游戏")

    async def _fetch_share_title(self, appid: str, game_type: str) -> str:
        """游戏分享 URL 会 302 到专题页，并在 query 里带上标题。"""
        url = f"{_API_BASE}/game/share_game_detail"
        try:
            async with self.new_client(
                headers=self.headers, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.get(
                    url, params={"appid": appid, "game_type": game_type}
                )
                if response.is_redirect:
                    target = urlsplit(urljoin(url, response.headers.get("Location", "")))
                    expected_path = f"/app/topic/game/{game_type}/{appid}"
                    query = parse_qs(target.query)
                    same_game = target.path.rstrip("/") == expected_path
                    if target.path.rstrip("/") == "/game/share_game_detail":
                        same_game = query.get("appid", [""])[0] == appid and query.get("game_type", [""])[0] == game_type
                    if target.hostname in _ALLOWED_HOSTS and not target.username and not target.password and same_game:
                        return _clean_text(query.get("title", [""])[0])
                    return ""
                response.raise_for_status()
                title = response.url.params.get("title")
                return _clean_text(title)
        except HTTPError:
            return ""

    def _game_author(self, game: dict[str, Any], intro: dict[str, Any]):
        developers = self._name_values(game.get("developers")) or self._name_values(
            intro.get("developers")
        )
        publishers = self._name_values(game.get("publishers")) or self._name_values(
            intro.get("publishers")
        )
        name = "、".join(developers or publishers)
        if not name:
            return None
        return self.create_author(name)

    @staticmethod
    def _name_values(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("value") or item.get("name") or item.get("key")
            text = _clean_text(item)
            if text and text not in names:
                names.append(text)
        return names

    def _game_info_lines(self, game: dict[str, Any], intro: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        genres = self._name_values(game.get("genres") or game.get("tags"))
        if genres:
            lines.append("类型: " + "、".join(genres[:5]))
        release = game.get("release_date") or intro.get("release_date")
        if isinstance(release, dict):
            release = release.get("date") or release.get("value")
        release_text = _clean_text(release)
        if release_text:
            lines.append(f"发行: {release_text}")
        developers = self._name_values(game.get("developers")) or self._name_values(
            intro.get("developers")
        )
        if developers:
            lines.append("开发商: " + "、".join(developers[:3]))
        publishers = self._name_values(game.get("publishers")) or self._name_values(
            intro.get("publishers")
        )
        if publishers:
            lines.append("发行商: " + "、".join(publishers[:3]))
        return lines

    def _game_media(
        self, game: dict[str, Any], about: str
    ) -> tuple[list[str], list[tuple[str, str, float | None]]]:
        images: list[str] = []
        videos: list[tuple[str, str, float | None]] = []
        for key in ("image", "header_img", "header_image", "cover", "logo"):
            images.append(_http_url(game.get(key)))

        screenshots = game.get("screenshots")
        if isinstance(screenshots, list):
            for item in screenshots:
                if not isinstance(item, dict):
                    images.append(_http_url(item))
                    continue
                media_type = str(item.get("type") or "").strip().lower()
                values = [
                    item.get(key)
                    for key in (
                        "url",
                        "video_url",
                        "videoUrl",
                        "src",
                        "source",
                        "play_url",
                        "download_url",
                    )
                ]
                if media_type in {"movie", "video"}:
                    for value in values:
                        url = _http_url(value)
                        if not url:
                            continue
                        if _IMAGE_EXT_RE.search(url):
                            images.append(url)
                        else:
                            videos.append(
                                (
                                    url,
                                    _http_url(item.get("thumbnail")),
                                    _duration_seconds(item.get("duration")),
                                )
                            )
                else:
                    for value in values:
                        images.append(_http_url(value))
                images.append(_http_url(item.get("thumbnail")))

        for url in _HTML_SOURCE_RE.findall(about):
            videos.append((_http_url(url), "", None))
        for url in _HTML_IMG_SRC_RE.findall(about):
            images.append(_http_url(url))

        videos = self._dedup_videos(videos)
        return _unique_http_urls(images, image=True), videos

    # ────────────────────────────── HTTP ────────────────────────────── #

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._cookie_invalid:
            raise ParseException("小黑盒 Cookie 格式无效，请在账号凭据中重新导入")
        request_headers = dict(headers or self.headers)
        if self._cookie and urlsplit(url).hostname == "api.xiaoheihe.cn":
            request_headers["Cookie"] = self._cookie
        async with self.new_client(
            headers=request_headers, transport=self._transport, follow_redirects=False
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise ParseException("小黑盒接口返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise ParseException("小黑盒接口返回格式异常")
        return payload
