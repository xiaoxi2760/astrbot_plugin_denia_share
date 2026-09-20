# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct, field
from msgspec.json import Decoder
from .common import Video
from ...exception import ParseException


class Image(Struct):
    urlDefault: str


class User(Struct):
    nickname: str
    avatar: str


class NoteDetail(Struct):
    type: str
    title: str
    desc: str
    user: User
    imageList: list[Image] = field(default_factory=list)
    video: Video | None = None

    @property
    def nickname(self) -> str:
        return self.user.nickname

    @property
    def avatar_url(self) -> str:
        return self.user.avatar

    @property
    def image_urls(self) -> list[str]:
        return [item.urlDefault for item in self.imageList]

    @property
    def is_video(self) -> bool:
        return self.type == "video" and self.video is not None

    @property
    def video_cover_duration(self):
        # 用 ParseException 而不是 assert。**收益要说准**：本属性只在 parse_explore 里
        # 被调用，而 _parse_common（parsers/xiaohongshu.py:55-59）的兜底是**裸
        # except Exception**，AssertionError 会被它接住并 fallback 到 parse_discovery
        # —— 所以这里换成 ParseException 的收益只在**日志可读性与消息文案**，
        # 不是「炸掉整条链路」。
        # 对照：抖音 _parse_douyin（parsers/douyin/parser.py:63）只 except ParseException，
        # 那里 AssertionError 才真的会打断「换下一条路径」。
        # 见 discovery.NoteData.url_and_duration：那条**不在**任何 try 里，注释写法与这里不同。
        if self.video is None:
            raise ParseException("图文详情缺少 video 段")
        video_url, duration = self.video.url_and_duration
        if video_url is None:
            raise ParseException("图文详情的 video 段没有可用地址")
        return video_url, self.imageList[0].urlDefault if self.imageList else None, duration


class NoteDetailWrapper(Struct):
    note: NoteDetail


class Note(Struct):
    noteDetailMap: dict[str, NoteDetailWrapper]


class InitialState(Struct):
    note: Note


decoder = Decoder(InitialState)
