# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct
from msgspec.json import Decoder
from ....exception import ParseException


# 清晰度偏好顺序。**它是「偏好」而不是「白名单」**：取流时必须外层遍历它、
# 内层找匹配，才能保证 1080p 优先；按远端返回顺序遍历（原实现）会让偏好形同虚设 ——
# 实测远端给 [360p, 1080p] 时返回 360p，倒过来才返回 1080p。
#
# 配置 ``ACFUN_QUALITY`` 只决定**从哪一档开始**（用户选的档位放最前，其后按
# 该档往下的顺序继续），默认值与完整偏好一致 → 未改配置时行为不变。用户选的
# 最高偏好档不可用时仍逐档降级，不会失败（见 :func:`_quality_preference`）。
_QUALITY_PREFERENCE = ("1080p", "720p", "480p", "360p")


def _quality_preference() -> tuple[str, ...]:
    """从配置读清晰度偏好起点，返回「用户所选档位 → 更低的档位」的偏好元组。

    配置未初始化 / 读取出错 / 值不在白名单里时回退完整默认顺序。
    """
    try:
        from ....config import get_config

        chosen = str(getattr(get_config(), "ACFUN_QUALITY", "") or "").strip().lower()
    except RuntimeError:
        return _QUALITY_PREFERENCE
    if chosen not in _QUALITY_PREFERENCE:
        return _QUALITY_PREFERENCE
    index = _QUALITY_PREFERENCE.index(chosen)
    return _QUALITY_PREFERENCE[index:]


class User(Struct):
    name: str
    headUrl: str


class Representation(Struct):
    url: str
    m3u8Slice: str
    qualityType: str

    @property
    def m3u8_slice(self) -> str:
        return self.m3u8Slice.replace("\\\\n", "\n")


class AdaptationSet(Struct):
    representation: list[Representation]


class KsPlay(Struct):
    adaptationSet: list[AdaptationSet]


class CurrentVideoInfo(Struct):
    ksPlayJson: KsPlay
    durationMillis: int

    @property
    def representations(self) -> list[Representation]:
        """可用的播放流列表；没有 adaptationSet 时返回空列表。

        裸 ``adaptationSet[0]`` 会在空列表时抛 ``IndexError`` —— 这个属性被
        ``m3u8_url`` 读到，而 ``m3u8_url`` 的调用点在解析器的 try 之外，
        ``IndexError`` 不是 ``ParseException`` 的子类，内部兜底接不住。
        """
        if not self.ksPlayJson.adaptationSet:
            return []
        return self.ksPlayJson.adaptationSet[0].representation


class VideoInfo(Struct, kw_only=True):
    title: str
    description: str | None
    createTimeMillis: int
    user: User
    currentVideoInfo: CurrentVideoInfo
    coverUrl: str

    @property
    def name(self) -> str:
        return self.user.name

    @property
    def avatar_url(self) -> str:
        return self.user.headUrl

    @property
    def text(self) -> str | None:
        return self.description

    @property
    def timestamp(self) -> int:
        return self.createTimeMillis // 1000

    @property
    def duration(self) -> int:
        return self.currentVideoInfo.durationMillis // 1000

    @property
    def m3u8_url(self) -> str:
        """按清晰度偏好挑一条流。

        外层遍历偏好（起始档位来自配置 ``ACFUN_QUALITY``）、内层找匹配 ——
        原实现是「按远端顺序遍历、命中白名单里任意一档就返回」，所以偏好顺序
        形同虚设（实测见常量处的注释）。用户选的最高偏好档不可用时仍会逐档
        降级；偏好档位都没有时退回远端给的第一条；**一条流都没有才抛
        ParseException**，让三级兜底继续走，而不是裸下标 IndexError。
        """
        representations = self.currentVideoInfo.representations
        for quality in _quality_preference():
            for item in representations:
                if item.qualityType == quality:
                    return item.url
        if representations:
            return representations[0].url
        raise ParseException("AcFun 视频没有可用的播放流")


decoder = Decoder(VideoInfo)
