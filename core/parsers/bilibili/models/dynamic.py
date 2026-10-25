# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from typing import Any
from msgspec import Struct, convert, field


class AuthorInfo(Struct):
    name: str
    face: str
    mid: int
    pub_time: str
    pub_ts: int | str


class VideoArchive(Struct):
    aid: str
    bvid: str
    title: str
    desc: str
    cover: str
    duration_text: str = ""

    @property
    def duration_seconds(self) -> float:
        if not self.duration_text:
            return 0.0
        parts = self.duration_text.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            pass
        return 0.0


class OpusImage(Struct):
    url: str


class OpusSummary(Struct):
    text: str


class OpusContent(Struct):
    jump_url: str
    pics: list[OpusImage]
    summary: OpusSummary
    title: str | None = None


class DrawItem(Struct):
    """九宫格动态里的一张图。"""

    src: str


class DrawContent(Struct):
    items: list[DrawItem] = field(default_factory=list)


class DynamicMajor(Struct):
    type: str | None = None
    archive: VideoArchive | None = None
    opus: OpusContent | None = None
    desc: OpusSummary | None = None
    # MAJOR_TYPE_DRAW（九宫格图集）走这个字段。原先没声明它，于是九宫格动态
    # 只发出文字、图片全丢，而且**零警告** —— 和「抖音图集变成视频」同一类问题。
    draw: DrawContent | None = None

    @property
    def title(self) -> str | None:
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.title
        if self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return self.opus.title
        return None

    @property
    def text(self) -> str | None:
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.desc
        elif self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return self.opus.summary.text
        elif self.desc:
            return self.desc.text
        return None

    @property
    def image_urls(self) -> list[str]:
        if self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return [pic.url for pic in self.opus.pics]
        elif self.type == "MAJOR_TYPE_ARCHIVE" and self.archive and self.archive.cover:
            return [self.archive.cover]
        elif self.type == "MAJOR_TYPE_DRAW" and self.draw:
            # 九宫格图集：原先这里没有分支，图片被整段丢掉
            return [item.src for item in self.draw.items]
        return []

    @property
    def cover_url(self) -> str | None:
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.cover
        return None

    @property
    def duration(self) -> float:
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.duration_seconds
        return 0.0


class DynamicModule(Struct):
    module_author: AuthorInfo
    module_dynamic: dict[str, Any] | None = None
    module_stat: dict[str, Any] | None = None
    # 这是 ``major`` 属性的懒加载缓存，不是远端数据。**msgspec 不把下划线前缀当
    # 非字段**（``msgspec.structs.fields()`` 里就有它），所以它确实出现在字段表里；
    # 但远端 JSON 没有这个键、解码时走默认 None，本仓也不 encode 这些模型，
    # 所以只是语义不够纯，没有功能影响。
    # **不要改成 ClassVar** —— 那是类级共享，不同动态实例会串缓存（读到别人的 major）。
    _cached_major: DynamicMajor | None = None

    @property
    def author_name(self) -> str:
        return self.module_author.name

    @property
    def author_face(self) -> str:
        return self.module_author.face

    @property
    def pub_ts(self) -> int:
        ts = self.module_author.pub_ts
        if isinstance(ts, str):
            return int(ts)
        return ts

    @property
    def _major_info(self) -> dict[str, Any] | None:
        if self.module_dynamic:
            if major := self.module_dynamic.get("major"):
                return major
            return self.module_dynamic
        return None

    @property
    def major(self) -> DynamicMajor | None:
        if self._cached_major is None:
            major_info = self._major_info
            if major_info:
                self._cached_major = convert(major_info, DynamicMajor)
        return self._cached_major

    @property
    def desc_text(self) -> str | None:
        if self.module_dynamic:
            desc = self.module_dynamic.get("desc")
            if desc and isinstance(desc, dict):
                return desc.get("text")
        return None


class DynamicInfo(Struct):
    id_str: str
    type: str
    visible: bool
    modules: DynamicModule
    basic: dict[str, Any] | None = None
    orig: "DynamicInfo | None" = None

    @property
    def name(self) -> str:
        return self.modules.author_name

    @property
    def avatar(self) -> str:
        return self.modules.author_face

    @property
    def timestamp(self) -> int:
        return self.modules.pub_ts

    @property
    def title(self) -> str | None:
        if major := self.modules.major:
            return major.title

    @property
    def text(self) -> str | None:
        if desc_text := self.modules.desc_text:
            return desc_text
        if major := self.modules.major:
            return major.text

    @property
    def image_urls(self) -> list[str]:
        if major := self.modules.major:
            return major.image_urls
        return []

    def is_video(self) -> bool:
        major = self.modules.major
        return major is not None and major.archive is not None


class DynamicWrapper(Struct):
    item: DynamicInfo
