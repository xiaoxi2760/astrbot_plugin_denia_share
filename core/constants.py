"""常量和枚举定义"""

from enum import Enum
from typing import Final

from httpx import Timeout

COMMON_HEADER: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    )
}

IOS_HEADER: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
    )
}

ANDROID_HEADER: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36 Edg/132.0.0.0"
    )
}

COMMON_TIMEOUT: Final[Timeout] = Timeout(connect=15.0, read=20.0, write=10.0, pool=10.0)

DOWNLOAD_TIMEOUT: Final[Timeout] = Timeout(connect=15.0, read=240.0, write=10.0, pool=10.0)


class PlatformEnum(str, Enum):
    ACFUN = "acfun"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    GITHUB = "github"
    KUAISHOU = "kuaishou"
    NGA = "nga"
    PIXIV = "pixiv"
    STEAM = "steam"
    TWITTER = "twitter"
    WEIBO = "weibo"
    XIAOHONGSHU = "xiaohongshu"

    def __str__(self) -> str:
        return self.value


# 平台的展示名与固定顺序，供 WebUI 的平台开关与筛选下拉使用。
# 键必须与 main.py 里 parsers 字典的键、以及 DISABLED_PLATFORMS 的取值一致。
PLATFORM_DISPLAY_NAMES: Final[dict[str, str]] = {
    "bilibili": "B站",
    "douyin": "抖音",
    "kuaishou": "快手",
    "weibo": "微博",
    "xiaohongshu": "小红书",
    "twitter": "Twitter / X",
    "nga": "NGA",
    "acfun": "AcFun",
    "github": "GitHub",
    "pixiv": "Pixiv",
    "steam": "Steam",
}

PLATFORM_ORDER: Final[tuple[str, ...]] = tuple(PLATFORM_DISPLAY_NAMES)
