"""Extract QQ and NetEase song links from supported music share cards.

Card shape reference: MusicUID (MIT), commit 38e027f720.
"""

import json
import re
from urllib.parse import urlsplit

_MAX_CARD_LENGTH = 65536
_TEXT_CARD = re.compile(r"\[(?:CQ:json,data=|json:data=)")
_CARD_TYPES = {"json", "light_app", "ark"}


def is_card_component(segment_type: object) -> bool:
    return str(getattr(segment_type, "value", segment_type)).lower() in _CARD_TYPES


def decode_card(raw: object) -> dict:
    """Accept raw JSON, an already parsed object, or a OneBot data wrapper."""
    for _ in range(3):
        if isinstance(raw, str) and len(raw) <= _MAX_CARD_LENGTH:
            try:
                raw = json.loads(raw)
            except (ValueError, RecursionError):
                return {}
        if not isinstance(raw, dict):
            return {}
        if "data" in raw and "meta" not in raw:
            raw = raw["data"]
            continue
        return raw
    return {}


def text_card_payloads(text: str):
    """Decode adapter text forms without mistaking brackets within JSON for a terminator."""
    if len(text) > _MAX_CARD_LENGTH:
        return
    for match in _TEXT_CARD.finditer(text):
        tail = text[match.end():]
        if match[0].startswith("[CQ:"):
            tail = tail.split("]", 1)[0]
            for original, replacement in (("&#44;", ","), ("&#91;", "["), ("&#93;", "]"), ("&amp;", "&")):
                tail = tail.replace(original, replacement)
        try:
            payload, _ = json.JSONDecoder().raw_decode(tail.lstrip())
        except (ValueError, RecursionError):
            continue
        if isinstance(payload, dict):
            yield payload


def music_card_links(card: dict) -> list[str]:
    if card.get("app") != "com.tencent.music.lua":
        return []
    meta = card.get("meta")
    music = meta.get("music") if isinstance(meta, dict) else None
    if not isinstance(music, dict):
        return []

    from ..parsers.music import song_identity

    jump = music.get("jumpUrl") or music.get("jump_url")
    if isinstance(jump, str):
        if song_identity("netease", jump) or song_identity("qqmusic", jump):
            return [jump]
        try:
            host = urlsplit(jump).hostname
        except ValueError:
            return []
        if host in {"163cn.tv", "c.y.qq.com", "c6.y.qq.com"}:
            return [jump]

    # Some QQ Music App cards carry the ID separately and omit the song URL.
    mid = music.get("songmid") or music.get("songMid")
    if isinstance(mid, str) and re.fullmatch(r"[A-Za-z0-9]{14}", mid):
        return [f"https://y.qq.com/n/ryqq/songDetail/{mid}"]
    song_id = str(music.get("songid") or music.get("songId") or "")
    if re.fullmatch(r"[1-9][0-9]{0,19}", song_id):
        return [f"https://y.qq.com/n/ryqq/songDetail/{song_id}"]
    return []
