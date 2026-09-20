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
        return [choice(image.url_list) for image in self.images]

    @property
    def dynamic_urls(self) -> list[str]:
        return [choice(image.video.play_addr.url_list) for image in self.images if image.video]


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)


decoder = Decoder(SlidesInfo)
