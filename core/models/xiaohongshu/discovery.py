# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct
from msgspec.json import Decoder
from .common import Video


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
        assert self.video is not None
        video_url, duration = self.video.url_and_duration
        assert video_url is not None
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
