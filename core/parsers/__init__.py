"""解析器包 - 导出所有平台解析器。

来源说明（合并自两个插件，取各自更强的一版）：
- bilibili / xiaohongshu / weibo / acfun / nga —— rika_share 原实现
- douyin  —— rika HTML 路径 + 娅娅 a_bogus 签名接口（双路径）
- kuaishou —— rika 结构 + 娅娅对新版「photo 为 JSON 字符串」的兼容
- twitter —— rika vxtwitter + 娅娅 fxtwitter / Guest GraphQL（三级兜底）
- github / pixiv —— 本仓库新增（github 走官方 API；pixiv 走 Ajax 接口并强制过滤 R18）
"""

from .bilibili import BilibiliParser
from .douyin import DouyinParser
from .kuaishou import KuaiShouParser
from .weibo import WeiBoParser
from .xiaohongshu import XiaoHongShuParser
from .twitter import TwitterParser
from .nga import NGAParser
from .acfun import AcfunParser
from .github import GitHubParser
from .pixiv import PixivParser

__all__ = [
    "BilibiliParser",
    "DouyinParser",
    "KuaiShouParser",
    "WeiBoParser",
    "XiaoHongShuParser",
    "TwitterParser",
    "NGAParser",
    "AcfunParser",
    "GitHubParser",
    "PixivParser",
]
