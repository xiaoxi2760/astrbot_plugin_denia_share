# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct
from msgspec.json import Decoder
from ...exception import ParseException


# 清晰度偏好顺序。**它是「偏好」而不是「白名单」**：取流时必须外层遍历它、
# 内层找匹配，才能保证 1080p 优先；按远端返回顺序遍历（原实现）会让偏好形同虚设 ——
# 实测远端给 [360p, 1080p] 时返回 360p，倒过来才返回 1080p。
_QUALITY_PREFERENCE = ("1080p", "720p", "480p", "360p")


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

        外层遍历 ``_QUALITY_PREFERENCE``、内层找匹配 —— 原实现是「按远端顺序遍历、
        命中白名单里任意一档就返回」，所以偏好顺序形同虚设（实测见常量处的注释）。
        偏好档位都没有时退回远端给的第一条；**一条流都没有才抛 ParseException**，
        让三级兜底继续走，而不是裸下标 IndexError。
        """
        representations = self.currentVideoInfo.representations
        for quality in _QUALITY_PREFERENCE:
            for item in representations:
                if item.qualityType == quality:
                    return item.url
        if representations:
            return representations[0].url
        raise ParseException("AcFun 视频没有可用的播放流")


decoder = Decoder(VideoInfo)
