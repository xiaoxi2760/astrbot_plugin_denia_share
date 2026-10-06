"""解析器包 - 导出所有平台解析器。

来源说明（合并自两个插件，取各自更强的一版）：
- bilibili / xiaohongshu / weibo / acfun / nga —— rika_share 原实现
- douyin  —— rika HTML 路径 + 娅娅 a_bogus 签名接口（双路径）
- kuaishou —— rika 结构 + 娅娅对新版「photo 为 JSON 字符串」的兼容
- twitter —— rika vxtwitter + 娅娅 fxtwitter / Guest GraphQL（三级兜底）
- github / pixiv / steam —— 本仓库新增
  （github 走官方 API；pixiv 走 Ajax 接口并强制过滤 R18；
   steam 走官方 appdetails + CheapShark 折扣 + ITAD 史低）
"""

from .douyin import DouyinParser
from .kuaishou import KuaiShouParser
from .weibo import WeiBoParser
from .xiaohongshu import XiaoHongShuParser
from .twitter import TwitterParser
from .nga import NGAParser
from .acfun import AcfunParser
from .github import GitHubParser
from .pixiv import PixivParser
from .steam import SteamParser

# bilibili 单独兜底：它是本包唯一带第三方强依赖的解析器（bilibili-api-python，
# GPL-3.0-or-later）。原先它在顶层裸 import，一旦这个依赖装不上、版本不兼容或
# 被 yank，异常会顺着 .bilibili → 本模块 → main.py 一路冒泡，
# 最终让**抖音 / 快手 / 微博 / 小红书 / Twitter / NGA / AcFun / GitHub / Pixiv /
# Steam 这 10 个与 B站毫无关系的平台一起起不来** —— 一个边缘平台的可选依赖
# 拥有整个插件的爆炸半径。
#
# 现在收在两级：
#   1. 主力解析器导入失败 → 换**自建替补**（core/bili_fallback），B站视频仍可用，
#      代价是动态 / 直播 / 收藏夹 / 专栏这些只有主力才有的功能不可用；
#   2. 替补也起不来（比如 httpx 都没了）→ 才真的把 BilibiliParser 置为 None，
#      由 main.py 跳过注册并打日志，其余十个平台照常。
#
# ⚠️ 三个标志必须在**每条分支**上都绑定，包括主力成功那条。否则
# `from .core.parsers import *` 会因为 FALLBACK_ERROR 只在 except 里赋值而
# 直接 ImportError —— 而 main.py 恰好只在 `BilibiliParser is None` 分支里才
# import 它，属于「靠分支躲开」。这与 webui.py 读缓存那次是同一类错误。
BILIBILI_IMPORT_ERROR: Exception | None = None
BILIBILI_FALLBACK_ERROR: Exception | None = None
BILIBILI_IS_FALLBACK = False

try:
    from .bilibili import BilibiliParser
except Exception as exc:  # noqa: BLE001 —— 依赖缺失/版本不兼容都在此收口
    BILIBILI_IMPORT_ERROR = exc
    try:
        from ..bili_fallback.parser import FallbackBilibiliParser as BilibiliParser

        BILIBILI_IS_FALLBACK = True
    except Exception as fallback_exc:  # noqa: BLE001 —— 连 httpx 都没了
        BilibiliParser = None
        BILIBILI_FALLBACK_ERROR = fallback_exc

__all__ = [
    "BilibiliParser",
    "BILIBILI_IMPORT_ERROR",
    "BILIBILI_IS_FALLBACK",
    "BILIBILI_FALLBACK_ERROR",
    "DouyinParser",
    "KuaiShouParser",
    "WeiBoParser",
    "XiaoHongShuParser",
    "TwitterParser",
    "NGAParser",
    "AcfunParser",
    "GitHubParser",
    "PixivParser",
    "SteamParser",
]
