"""音乐平台的共享数据类型。

平台适配器（``netease`` / ``qqmusic``）、命令控制器与登录管理都只认这里的
三个类型，避免各处再各自造一份 song 结构。字段刻意保守：只放「渲染卡片 +
下发音频」真正需要的东西，不携带任何登录凭据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PLATFORM_NETEASE: Final[str] = "netease"
PLATFORM_QQMUSIC: Final[str] = "qqmusic"

PLATFORM_NAMES: Final[dict[str, str]] = {
    PLATFORM_NETEASE: "网易云音乐",
    PLATFORM_QQMUSIC: "QQ音乐",
}


def platform_name(platform: str) -> str:
    """返回平台的中文名，未知平台原样回显。"""
    return PLATFORM_NAMES.get(str(platform or "").strip().lower(), str(platform or ""))


@dataclass
class Song:
    """一首歌的可展示信息。

    ``duration`` 以秒为单位；``media_mid`` 只对 QQ 音乐有意义（取流用的
    ``file.media_mid``，可能与分享链接里的 songmid 不同），网易云留空。
    """

    platform: str
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    cover: str = ""
    media_mid: str = ""
    url: str = ""


@dataclass
class PlayURL:
    """一次取流的结果。

    ``duration`` 是「实际能播放的秒数」，拿不到完整时长时宁可给 0 也不要谎报；
    ``preview`` 标记试听片段，调用方据此决定是否提示用户。
    """

    url: str = ""
    duration: float = 0.0
    preview: bool = False
    message: str = ""


class MusicError(Exception):
    """音乐模块对外的唯一异常类型，消息必须是可以直接展示给用户的安全文案。"""
