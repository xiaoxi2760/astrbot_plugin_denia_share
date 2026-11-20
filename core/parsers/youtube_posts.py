"""Extract the contents of a public YouTube post from its detail page."""

import json
import re
from urllib.parse import urlsplit

from ..exception import ParseException


_INITIAL_DATA = re.compile(r'\b(?:ytInitialData|window\["ytInitialData"\])\s*=\s*')
_IMAGE_HOSTS = {"yt3.ggpht.com", "yt3.googleusercontent.com", "i.ytimg.com"}


def _post_renderer(document: str, post_id: str) -> dict:
    decoder = json.JSONDecoder()
    for match in _INITIAL_DATA.finditer(document):
        try:
            data, _ = decoder.raw_decode(document, match.end())
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            break
    else:
        raise ParseException("YouTube 帖子无法公开预览，或页面没有可解析的帖子数据")

    pending = [data.get("contents")]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            post = node.get("backstagePostRenderer")
            if isinstance(post, dict) and post.get("postId") == post_id:
                return post
            pending.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            pending.extend(reversed(node))
    raise ParseException("YouTube 未返回该帖子，帖子可能已删除或限制访问")


def _text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    simple = payload.get("simpleText")
    if isinstance(simple, str):
        return simple.strip()
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return ""
    parts = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        text = run.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            emoji_id = emoji.get("emojiId")
            if isinstance(emoji_id, str) and not emoji_id.isascii():
                parts.append(emoji_id)
            elif (shortcuts := emoji.get("shortcuts")) and isinstance(shortcuts, list):
                if isinstance(shortcuts[0], str):
                    parts.append(shortcuts[0])
    return "".join(parts).strip()


def _thumbnail_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = f"https:{value}" if value.startswith("//") else value
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.netloc.lower() in _IMAGE_HOSTS and parsed.path:
        return url
    return None


def _attachment_image(wrapper: object) -> str | None:
    if not isinstance(wrapper, dict):
        return None
    renderer = wrapper.get("backstageImageRenderer")
    if not isinstance(renderer, dict):
        return None
    image = renderer.get("image")
    if not isinstance(image, dict) or not isinstance(image.get("thumbnails"), list):
        return None

    variants = []
    for item in image["thumbnails"]:
        if not isinstance(item, dict) or not (url := _thumbnail_url(item.get("url"))):
            continue
        width, height = item.get("width"), item.get("height")
        area = width * height if type(width) is int and type(height) is int and width > 0 and height > 0 else 0
        variants.append((area, url))
    if variants:
        return max(variants, key=lambda item: item[0])[1]
    return None


def extract_post(document: str, post_id: str) -> tuple[str, str, list[str]]:
    post = _post_renderer(document, post_id)
    author = _text(post.get("authorText"))
    body = _text(post.get("contentText"))
    attachment = post.get("backstageAttachment")
    urls: list[str] = []
    if isinstance(attachment, dict):
        gallery = attachment.get("postMultiImageRenderer")
        images = gallery.get("images") if isinstance(gallery, dict) else [attachment]
        if isinstance(images, list):
            urls = list(dict.fromkeys(url for image in images if (url := _attachment_image(image))))
    if not body and not urls:
        raise ParseException("YouTube 帖子没有可预览的正文或图片")
    return author, body, urls
