# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from random import choice
from msgspec import Struct, field
from msgspec.json import Decoder


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str:
        return choice(self.author.avatar_thumb.url_list)

    @property
    def image_urls(self) -> list[str]:
        """静态图直链。

        **必须跳过 url_list 为空的条目**：实拍图集里有些条目只带内嵌视频，
        `choice([])` 会直接抛 IndexError，把整条作品解析炸掉。
        """
        return [choice(image.url_list) for image in self.images if image.url_list]

    @property
    def dynamic_urls(self) -> list[str]:
        """实拍图集里每张图内嵌的那段视频（不是静态图）。

        取的时候同样要防空列表。调用方**不要**在还有静态图时用它 ——
        用它就会把一条图集发成一串视频。
        """
        return [
            choice(image.video.play_addr.url_list)
            for image in self.images
            if image.video and image.video.play_addr.url_list
        ]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)


decoder = Decoder(SlidesInfo)
