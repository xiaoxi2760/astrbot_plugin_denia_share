"""Telegram ingress compatibility, using AstrBot's existing message components.

AstrBot 4.29.3 expands text_link entities, but album captions after the first
item only appear in Plain components. Older adapters may leave hidden targets
in raw_message (telegram.Update). Never inspect quoted/replied messages here.
"""

from urllib.parse import urlsplit
import re

import astrbot.api.message_components as Comp


def is_telegram(event) -> bool:
    try:
        return event.get_platform_name() == "telegram"
    except Exception:
        return False


def current_message_links(event) -> list[str]:
    """Bounded HTTP(S) links from current text/captions and text_link targets."""
    if not is_telegram(event):
        return []
    message = getattr(event, "message_obj", None)
    raw = getattr(message, "raw_message", None)
    current = getattr(raw, "message", None)
    texts = [getattr(current, "text", None), getattr(current, "caption", None)]
    texts.extend(
        component.text for component in (getattr(message, "message", None) or [])[:100]
        if isinstance(component, Comp.Plain)
    )
    candidates = []
    for text in texts[:102]:
        if isinstance(text, str):
            candidates.extend(re.findall(r"https?://[^\s'\"<>()]+", text[:32768])[:32])
    for name in ("entities", "caption_entities"):
        for entity in (getattr(current, name, None) or ())[:100]:
            if getattr(entity, "type", None) == "text_link":
                candidates.append(getattr(entity, "url", None))
    links = []
    for value in candidates:
        if not isinstance(value, str) or not value or len(value) > 8192:
            continue
        if "\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            continue
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                continue
            parsed.port
        except ValueError:
            continue
        if value not in links:
            links.append(value)
        if len(links) >= 32:
            break
    return links
