"""Optional OneBot music cards using the MusicUID-compatible signing API."""

import asyncio
import json
import re

import httpx
from astrbot.api import logger

from .client import safe_media_url

_TIMEOUT = 8
_MAX_RESPONSE_BYTES = 64 * 1024


def _custom_parameters(raw: str) -> dict[str, str]:
    pairs = {}
    for line in re.split(r"[\r\n,，]+", raw):
        key, separator, value = line.partition(":")
        if separator and key.strip():
            pairs[key.strip()] = value.strip()
    return pairs


def _validated_card(payload: object, audio_url: str, source_url: str) -> str:
    if not isinstance(payload, dict) or str(payload.get("code")) not in {"0", "1", "200"}:
        return ""
    message = payload.get("message")
    try:
        card = json.loads(message) if isinstance(message, str) and message.lstrip().startswith("{") else payload.get("data")
    except (ValueError, RecursionError):
        return ""
    if not isinstance(card, dict) or card.get("app") != "com.tencent.music.lua" or card.get("view") != "music":
        return ""
    config = card.get("config")
    meta = card.get("meta")
    music = meta.get("music") if isinstance(meta, dict) else None
    if not isinstance(config, dict) or not config.get("token") or not isinstance(music, dict):
        return ""
    link = music.get("musicUrl")
    if music.get("jumpUrl") != source_url or not isinstance(link, str) or link.replace(" ", "+") != audio_url:
        return ""
    # Keep the original string when the signing service supplied one.
    return message if isinstance(message, str) and message.lstrip().startswith("{") else json.dumps(card, ensure_ascii=False)


async def sign_music_card(endpoint: str, song, audio_url: str, *, format_name: str, custom_params: str) -> str:
    if not (safe_media_url(endpoint) and safe_media_url(audio_url) and safe_media_url(song.url)):
        return ""
    params = _custom_parameters(custom_params)
    params.update(url=audio_url, jump=song.url, song=song.title, singer=song.artist, format=format_name)
    params["cover"] = safe_media_url(song.cover)
    request_url = httpx.URL(endpoint).copy_merge_params(params)
    async with asyncio.timeout(_TIMEOUT):
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", request_url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return ""
                    body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return ""
    return _validated_card(payload, audio_url, song.url)


async def send_signed_card(event, config, result) -> None:
    """Send an optional card without interfering with ordinary audio delivery."""
    endpoint = str(getattr(config, "MUSIC_CARD_SIGN_URL", "") or "").strip()
    if not endpoint:
        return
    try:
        group_id = str(event.get_group_id() or "")
        target_id = group_id or str(event.get_sender_id() or "")
        action = getattr(getattr(event, "bot", None), "call_action", None)
        if not target_id.isdecimal() or not callable(action):
            return
        for audio in result.audio_contents:
            loader = audio.music_card_loader
            path = audio.path_task.resolved
            if loader is None or path is None or not path.is_file():
                continue
            card = await loader(endpoint)
            if card:
                id_key = "group_id" if group_id else "user_id"
                async with asyncio.timeout(_TIMEOUT):
                    await action(
                        "send_group_msg" if group_id else "send_private_msg",
                        **{id_key: int(target_id), "message": [{"type": "json", "data": {"data": card}}]},
                    )
            return
    except Exception as exc:
        # Neither signing parameters nor stream URLs belong in chat or logs.
        logger.debug("[denia_share] 签名音乐卡片发送失败（%s）", type(exc).__name__)
