# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from msgspec import Struct
from msgspec.json import Decoder


class UserInfo(Struct):
    screen_name: str
    profile_image_url: str


class Data(Struct):
    url: str
    title: str
    content: str
    userinfo: UserInfo
    create_at_unix: int


class Detail(Struct):
    code: str
    msg: str
    data: Data


decoder = Decoder(Detail)
