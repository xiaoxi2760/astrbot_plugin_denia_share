# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct
from msgspec.json import Decoder
from .common import Video
from ...exception import ParseException


class Image(Struct):
    url: str
    urlSizeLarge: str | None = None


class User(Struct):
    nickName: str
    avatar: str


class NoteData(Struct):
    type: str
    title: str
    desc: str
    user: User
    time: int
    lastUpdateTime: int
    imageList: list[Image] = []
    video: Video | None = None

    @property
    def image_urls(self) -> list[str]:
        return [item.url for item in self.imageList]

    @property
    def is_video(self) -> bool:
        return self.type == "video" and self.video is not None

    @property
    def url_and_duration(self):
        # 与 explore.NoteDetail.video_cover_duration 同形，但**这条真的会被调用到**：
        # parse_discovery（xiaohongshu.py）在 is_video 分支里直接用它。
        # 用 ParseException 而不是 assert：AssertionError 虽然也是 Exception 子类、
        # 会被 _process_url 最后一级接住，但日志只剩「解析异常」+ 堆栈，
        # 看不出是「这条作品的视频流是空的」。
        if self.video is None:
            raise ParseException("图文详情缺少 video 段")
        video_url, duration = self.video.url_and_duration
        if video_url is None:
            raise ParseException("图文详情的 video 段没有可用地址")
        return video_url, duration


class NormalNotePreloadData(Struct):
    title: str
    desc: str
    imagesList: list[Image] = []

    @property
    def image_urls(self) -> list[str]:
        return [item.urlSizeLarge or item.url for item in self.imagesList]


class NoteDataWrapper(Struct):
    noteData: NoteData


class NoteDataContainer(Struct):
    data: NoteDataWrapper
    normalNotePreloadData: NormalNotePreloadData | None = None


class InitialState(Struct):
    noteData: NoteDataContainer


decoder = Decoder(InitialState)
