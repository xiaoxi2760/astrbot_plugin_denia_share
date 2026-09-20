# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from re import sub
from msgspec import Struct
from msgspec.json import Decoder


class User(Struct):
    name: str
    profile_image_url: str
    description: str


class Reward(Struct):
    user: User


class PlayInfo(Struct):
    title: str
    text: str
    reward: Reward
    cover_image: str
    stream_url: str
    real_date: int
    urls: dict[str, str]
    duration_time: float
    # 只为「返回的播放数据是不是被请求的那个对象」这个校验而声明。
    # 类型放宽成 int | str：写死会在远端换类型时让整条解码失败。
    oid: int | str | None = None
    fid: int | str | None = None
    object_id: int | str | None = None

    @property
    def name(self) -> str:
        return self.reward.user.name

    @property
    def avatar(self) -> str:
        return self.reward.user.profile_image_url

    @property
    def description(self) -> str:
        return self.reward.user.description

    @property
    def clean_text(self) -> str:
        text = sub(r"<[^>]*>", "", self.text)
        return text.replace("\n\n", "").strip()

    @property
    def cover_url(self) -> str:
        return "https:" + self.cover_image

    @property
    def video_url(self) -> str:
        url = next(iter(self.urls.values()), None)
        return "https:" + url if url else self.stream_url

    @property
    def duration(self) -> float:
        return self.duration_time


class Data(Struct):
    Component_Play_Playinfo: PlayInfo


class DataWrapper(Struct):
    data: Data


decoder = Decoder(DataWrapper)
