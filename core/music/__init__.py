"""音乐模块公共出口。

这里刻意做「惰性导出」：``core.music`` 会随着 ``controller.py`` 一起被
导入，而这个包下面既有 PIL 卡片渲染（``search_card``）又有加密依赖
（``crypto`` → pycryptodome），在包初始化时就全部拉起来会把可选依赖
变成硬依赖。所以只在这里登记名字，真正用到时再按需导入。
"""

from __future__ import annotations

from typing import Any

from .models import PLATFORM_NAMES, PLATFORM_NETEASE, PLATFORM_QQMUSIC, MusicError, PlayURL, Song, platform_name

_LAZY: dict[str, tuple[str, str]] = {
    "SearchSessions": (".session", "SearchSessions"),
    "render_search_card": (".search_card", "render_search_card"),
    "NeteaseProvider": (".netease", "NeteaseProvider"),
    "QQMusicProvider": (".qqmusic", "QQMusicProvider"),
    "LoginManager": (".login", "LoginManager"),
}

__all__ = [
    "LoginManager",
    "MusicError",
    "NeteaseProvider",
    "PLATFORM_NAMES",
    "PLATFORM_NETEASE",
    "PLATFORM_QQMUSIC",
    "PlayURL",
    "QQMusicProvider",
    "SearchSessions",
    "Song",
    "platform_name",
    "render_search_card",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
