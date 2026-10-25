# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct


class RoomInfo(Struct):
    title: str
    cover: str
    keyframe: str
    tags: str
    area_name: str
    parent_area_name: str


class BaseInfo(Struct):
    uname: str
    face: str
    gender: str


class LiveInfo(Struct):
    level: int
    level_color: int
    score: int


class AnchorInfo(Struct):
    base_info: BaseInfo
    live_info: LiveInfo


class RoomData(Struct):
    room_info: RoomInfo
    anchor_info: AnchorInfo

    @property
    def title(self) -> str:
        return f"直播 - {self.room_info.title}"

    @property
    def cover(self) -> str:
        return self.room_info.cover

    @property
    def detail(self) -> str:
        return f"分区: {self.room_info.area_name} | {self.room_info.parent_area_name}\n标签: {self.room_info.tags}"

    @property
    def keyframe(self) -> str:
        return self.room_info.keyframe

    @property
    def name(self) -> str:
        return self.anchor_info.base_info.uname

    @property
    def avatar(self) -> str:
        return self.anchor_info.base_info.face
