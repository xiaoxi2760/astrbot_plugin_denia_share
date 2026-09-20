"""常量和枚举定义"""

from enum import Enum
from typing import Final
from dataclasses import dataclass

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


@dataclass(frozen=True)
class PlatformMeta:
    """一个平台的元信息。

    **这里是平台名的唯一真相。** 以前同样的信息散在三处：``PlatformEnum`` 的值、
    ``PLATFORM_DISPLAY_NAMES`` 里的裸串、以及每个解析器里硬写的
    ``Platform(display_name="…")``，另加 ``main.py`` 里一处直接拼的字符串字面量。
    改一个平台名要翻三四处，漏一处就出现「卡片写哔哩哔哩、列表写 B站」这种
    自相矛盾 —— 而且不会有任何报错。

    Attributes:
        key: 与 ``PlatformEnum`` 的值、``main.py`` 里 parsers 字典的键一致。
        display_name: 正式名。WebUI 的平台开关、筛选下拉、日志与报错文案用它。
        card_name: 卡片与聊天消息里的称呼。可以更俏皮，留空表示与正式名相同。
            「猴山 / 小蓝鸟 / 哔哩哔哩」这类叫法是插件性格的一部分，所以这里
            刻意保留了一个字段，而不是硬把它们统一成正式名。想全部统一，
            把那三行的 card_name 清空即可，不用动任何别的地方。
    """

    key: str
    display_name: str
    card_name: str = ""

    @property
    def label(self) -> str:
        """卡片与消息里实际显示的名字。"""
        return self.card_name or self.display_name


PLATFORMS: Final[dict[str, PlatformMeta]] = {
    "bilibili": PlatformMeta("bilibili", "B站", "哔哩哔哩"),
    "douyin": PlatformMeta("douyin", "抖音"),
    "kuaishou": PlatformMeta("kuaishou", "快手"),
    "weibo": PlatformMeta("weibo", "微博"),
    "xiaohongshu": PlatformMeta("xiaohongshu", "小红书"),
    "twitter": PlatformMeta("twitter", "Twitter / X", "小蓝鸟"),
    "nga": PlatformMeta("nga", "NGA"),
    "acfun": PlatformMeta("acfun", "AcFun", "猴山"),
    "github": PlatformMeta("github", "GitHub"),
    "pixiv": PlatformMeta("pixiv", "Pixiv"),
    "steam": PlatformMeta("steam", "Steam"),
}

# 平台的正式名与固定顺序，供 WebUI 的平台开关与筛选下拉使用。
# 键必须与 main.py 里 parsers 字典的键、以及 DISABLED_PLATFORMS 的取值一致。
# 这两个名字保留是因为 WebUI 里已经在用，但**值都从 PLATFORMS 派生**，
# 不要再往这里手写平台名。
PLATFORM_DISPLAY_NAMES: Final[dict[str, str]] = {
    key: meta.display_name for key, meta in PLATFORMS.items()
}

PLATFORM_ORDER: Final[tuple[str, ...]] = tuple(PLATFORMS)


def platform_meta(key: "str | PlatformEnum") -> PlatformMeta:
    """按平台键取元信息；未知键回退成「键名即展示名」。

    回退而不是抛异常：平台键来自配置项（``DISABLED_PLATFORMS``）与解析器注册表，
    未知值应该表现为「某个平台名字不认识」，而不是让整条解析链路炸掉。
    """
    name = key.value if isinstance(key, PlatformEnum) else str(key)
    return PLATFORMS.get(name) or PlatformMeta(name, name)
