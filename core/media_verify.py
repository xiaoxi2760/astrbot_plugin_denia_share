"""媒体响应校验：把「CDN 返回的错误页」认出来，别把它当媒体存盘发出去。

为什么需要
----------
``raise_for_status()`` 只认 4xx/5xx。CDN 在直链过期、被风控、回源失败时经常返回
**200 OK + HTML 错误页**（或 JSON 错误体），下载器会照常写盘、``os.replace`` 成
``.mp4`` / ``.jpg``，然后当媒体发出去 —— 与「缺料必须体现在产物上」是同一类问题：
**产物在撒谎**。前身插件 yaya 的 ``core/downloader/validator.py`` 就是这么做的。

设计原则：只拒绝有把握的，绝不因为「认不出」而拒绝
--------------------------------------------------
1. Content-Type 明确声明是 ``image/`` / ``video/`` / ``audio/`` → **直接放行，连内容都不看**。
   这一步必须排在内容判断之前：SVG 是合法图片，而它以 ``<`` 开头。
2. Content-Type 是 ``application/json`` 或 ``text/*`` → 拒绝。
3. 只有 Content-Type 缺失或是 ``octet-stream`` 这类泛型时，才看响应体开头：
   - 去掉 BOM 与空白后以 ``<`` / ``{`` / ``[`` 开头 → 拒绝（HTML / JSON）
   - 整段都是可打印字符且含 ``error`` / ``forbidden`` / ``denied`` / ``gateway`` /
     ``timeout`` / ``unauthorized`` 等词 → 拒绝
   - 其余（含认不出的二进制）→ **放行**

也就是说这个模块的作用是「排除明显的错误页」，不是「确认这是合法媒体」。
把签名做成白名单会在平台换容器格式时误杀，得不偿失。

判定用的是**响应体的开头一段**（``HEAD_PROBE_BYTES`` 字节），所以调用方只要在
流式下载的第一个分片上顺手调一次，不需要额外请求、也不需要把文件读完。
"""

from __future__ import annotations

# 只取开头这一段做判断。错误页（HTML / JSON）一定从头开始，
# 而合法媒体不会是「前 1KB 是 HTML、后面才是视频」。
HEAD_PROBE_BYTES = 1024

# 明确声明是媒体的 Content-Type 前缀：命中即放行，不再看内容
_DECLARED_MEDIA_PREFIXES = ("image/", "video/", "audio/")

# 明确声明不是媒体的 Content-Type
_NON_MEDIA_CONTENT_TYPES = ("application/json", "application/javascript")

# 纯文本错误页里常见的词（小写比较）
_ERROR_MARKERS = (
    b"error",
    b"forbidden",
    b"access denied",
    b"not found",
    b"unauthorized",
    b"gateway",
    b"timeout",
    b"expired",
    b"invalid",
)

_STRIP_PREFIX = b"\xef\xbb\xbf\r\n\t "


def _normalized_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _looks_like_text(data: bytes) -> bool:
    """整段是否都是可打印 ASCII / 常见空白（用于识别纯文本错误页）。"""
    return all(byte in b"\r\n\t" or 32 <= byte <= 126 for byte in data)


def classify_media_response(content_type: str | None, head: bytes) -> str | None:
    """判断响应开头像不像「错误页」。

    Args:
        content_type: 响应的 ``Content-Type``（可带参数，可为 None）。
        head: 响应体开头的一段（建议 ``HEAD_PROBE_BYTES`` 字节）。

    Returns:
        ``None`` 表示通过（可能是媒体，也可能是认不出的二进制）；
        非空字符串是**拒绝原因**，调用方应当据此判定下载失败。
    """
    ctype = _normalized_content_type(content_type)

    # 1) 明确声明是媒体 —— 直接放行。必须在内容判断之前：SVG 以 '<' 开头但是合法图片。
    if ctype.startswith(_DECLARED_MEDIA_PREFIXES):
        return None

    # 2) 明确声明不是媒体
    if ctype in _NON_MEDIA_CONTENT_TYPES or ctype.startswith("text/"):
        return f"Content-Type 是 {ctype}"

    if not head:
        # 一个字节都没读到：交给下载器自己的「0 字节」校验去报，这里不抢
        return None

    stripped = head.lstrip(_STRIP_PREFIX)
    if stripped.startswith((b"<", b"{", b"[")):
        kind = "HTML" if stripped.startswith(b"<") else "JSON"
        return f"响应体以 {kind} 开头（泛型 Content-Type：{ctype or '未声明'}）"

    if _looks_like_text(stripped):
        lowered = stripped[:512].lower()
        for marker in _ERROR_MARKERS:
            if marker in lowered:
                return f"响应体是含 {marker.decode()} 的文本（泛型 Content-Type：{ctype or '未声明'}）"

    # 3) 认不出就放行
    return None


# 图片容器的魔数 → 后缀。只列**有把握**的几种：
# 签名太短的（如 BMP 的 "BM"）容易撞，宁可不认 —— 与上面同一个原则。
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

# ftyp 容器的 brand → 后缀（ISO-BMFF，AVIF / HEIC 都走这个盒子）
_FTYP_BRANDS = {
    b"avif": ".avif",
    b"avis": ".avif",
    b"heic": ".heic",
    b"heix": ".heic",
    b"hevc": ".heic",
    b"mif1": ".heic",
}


def sniff_image_ext(head: bytes) -> str | None:
    """按开头字节判断图片真实容器，返回建议后缀；认不出返回 ``None``。

    用途是「把后缀校正成真实格式」：本仓原先按 URL 后缀定名，URL 没有后缀就
    一律 ``.jpg`` —— 于是 WebP / PNG 会被存成 ``.jpg``。后缀不对会让下游
    （协议端、图片查看器）按错的容器去解，人工排查时也看不出真实格式。

    同样只认有把握的：认不出**保持原样**，绝不猜。
    """
    if not head:
        return None
    for signature, ext in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return ext
    # WebP：RIFF....WEBP（第 4-8 字节是长度，可能是任意值，所以要跳过）
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    # AVIF / HEIC：....ftyp<brand>
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12])
    return None
