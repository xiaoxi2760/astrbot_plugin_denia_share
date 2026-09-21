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

# 平台的正式名，供 WebUI 的平台开关与筛选下拉使用。键必须与 main.py 里
# parsers 字典的键、以及 DISABLED_PLATFORMS 的取值一致。
# 这个字典是**原地增删**的，所以已经 import 它的模块都能看到自定义平台。
PLATFORM_DISPLAY_NAMES: Final[dict[str, str]] = {
    key: meta.display_name for key, meta in PLATFORMS.items()
}

# 内置平台键的**快照**。「这是不是内置平台」不能靠「PLATFORMS 里有没有这个键」
# 来判断 —— 自定义解析器会被注册进 PLATFORMS，那个判断在注册之后就失真了。
BUILTIN_PLATFORM_KEYS: Final[frozenset[str]] = frozenset(PLATFORMS)


def platform_order() -> tuple[str, ...]:
    """平台键的展示顺序：内置平台在前（顺序固定），自定义平台按注册顺序追加。

    **必须走函数，不能是常量。** 自定义平台是运行时注册的，而
    ``from .constants import PLATFORM_ORDER`` 拿到的是**导入那一刻的元组快照**
    —— 之后重新赋值不会同步给已经导入过的模块，症状是「自定义平台能解析，
    但平台开关和筛选下拉里根本没有它」（2026-09-21 实测踩到）。
    ``PLATFORMS`` 本身是保序字典，所以直接派生即可。
    """
    return tuple(PLATFORMS)


def platform_meta(key: "str | PlatformEnum") -> PlatformMeta:
    """按平台键取元信息；未知键回退成「键名即展示名」。

    回退而不是抛异常：平台键来自配置项（``DISABLED_PLATFORMS``）与解析器注册表，
    未知值应该表现为「某个平台名字不认识」，而不是让整条解析链路炸掉。
    """
    name = key.value if isinstance(key, PlatformEnum) else str(key)
    return PLATFORMS.get(name) or PlatformMeta(name, name)


def is_custom_platform(key: "str | PlatformEnum") -> bool:
    """该平台键是否来自用户自定义解析器（WebUI 用它分组显示）。"""
    name = key.value if isinstance(key, PlatformEnum) else str(key)
    return name in PLATFORMS and name not in BUILTIN_PLATFORM_KEYS


def register_platform(key: str, display_name: str = "", card_name: str = "") -> None:
    """注册一个自定义平台，并同步更新派生的名字表与顺序表。

    **PLATFORMS 仍然是平台名的唯一真相** —— 这里只是把「唯一真相」从纯常量
    扩展成「常量 + 运行时注册」。所以自定义解析器也不需要手写展示名字面量：
    在文件里声明 ``PLATFORM_NAME``，由 ``custom_parsers`` 转交到这里。

    同名重复注册按「后写覆盖」处理；调用方（``custom_parsers``）负责先
    ``unregister_platform``，别指望这里帮你清理。
    """
    name = str(key).strip()
    if not name:
        return
    PLATFORMS[name] = PlatformMeta(name, display_name or name, card_name or "")
    PLATFORM_DISPLAY_NAMES[name] = PLATFORMS[name].display_name


def unregister_platform(key: str) -> None:
    """撤掉一个自定义平台。

    **内置平台不会被撤掉** —— 解析器热更新会反复走注册/反注册，万一哪里把内置
    平台的键传进来，平台列表会静默少一项（而卡片里仍会打印它的名字）。
    """
    name = str(key).strip()
    if not name or name in BUILTIN_PLATFORM_KEYS:
        return
    PLATFORMS.pop(name, None)
    PLATFORM_DISPLAY_NAMES.pop(name, None)
