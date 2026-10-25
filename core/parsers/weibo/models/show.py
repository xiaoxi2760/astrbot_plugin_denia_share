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

    @staticmethod
    def _absolute(url: str | None) -> str | None:
        """补全协议头；已经是绝对地址就不动。

        微博给的多是协议相对地址（``//wx1.sinaimg.cn/...``），所以原先统一加
        ``"https:"``。但远端偶尔直接给 ``https://`` 开头的绝对地址，那时会拼成
        ``https:https://...`` —— 两条属性都要走这里，别再各自手写前缀。
        """
        if not url:
            return None
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith(("http://", "https://")):
            return url
        # 既不是协议相对也不是绝对地址：原样返回，不猜（猜错会拼出更糟的 URL）
        return url

    @property
    def cover_url(self) -> str | None:
        return self._absolute(self.cover_image)

    @property
    def video_url(self) -> str | None:
        url = next(iter(self.urls.values()), None)
        # 兜底分支也要补协议头 —— 原先直接返回 stream_url，与上面那条分支不一致
        return self._absolute(url) or self._absolute(self.stream_url)

    @property
    def duration(self) -> float:
        return self.duration_time


class Data(Struct):
    Component_Play_Playinfo: PlayInfo


class DataWrapper(Struct):
    data: Data


decoder = Decoder(DataWrapper)
