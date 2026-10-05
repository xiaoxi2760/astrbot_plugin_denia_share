"""冒烟用例表：11 个解析器 × 3 种内容类型 = 33 条。

**URL 怎么来的**，三种来源，``source`` 字段标清楚，别混为一谈：

- ``api``   —— 通过本地代理打平台公开接口拿到的，当前确定存活
- ``doc``   —— 来自平台官方/社区文档里的样例 URL，未必还活着
- ``manual``—— 我拿不到、**需要你补**的；URL 留空，脚本会跳过

判据（``expect_*``）只断言**结构**——有没有标题、有没有解析出媒体。不比对具体文本，
因为平台文案随时变，拿它当断言只会让测试变成随机失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    platform: str          # 期望派发到的解析器
    kind: str              # 内容类型（报告与文档里的人类可读名）
    url: str
    source: str = "api"    # api / doc / manual
    expect_media: bool = True
    expect_fields: tuple[str, ...] = ()
    note: str = ""
    # 短链需要先重定向，走 parse_with_redirect
    follow_redirect: bool = False
    tags: tuple[str, ...] = field(default=())
    # **预期就是报错**。用于「该拒绝的要给出可读理由」这类用例 ——
    # 默认判据是「必须解析成功」，所以负向用例必须显式声明，否则会把
    # 正确的拒绝当成失败（或者反过来，把静默成功当成通过）。
    expect_error: str | None = None


CASES: list[Case] = [
    # ==================== B站 ====================
    Case("bili-video-1", "bilibili", "普通视频",
         "https://www.bilibili.com/video/BV1c3HL6qEyq", source="api",
         note="热门接口取的第一个，短视频"),
    Case("bili-video-2", "bilibili", "多P视频",
         "https://www.bilibili.com/video/BV1LNHj68EMg", source="api",
         note="热门接口取的，验证 p= 分P 与 av 兜底"),
    Case("bili-dynamic", "bilibili", "动态图文",
         "", source="manual",
         note="需要你给一条 t.bilibili.com/<数字> 或 bilibili.com/opus/<数字>，"
              "要图文型动态（纯视频动态不测这个）"),
    Case("bili-read", "bilibili", "专栏文章",
         "", source="manual",
         note="需要你给一条 bilibili.com/read/cv<数字>"),

    # ==================== 抖音 ====================
    Case("dy-video", "douyin", "视频",
         "", source="manual",
         note="需要你给一条 douyin.com/video/<数字> 或 iesdouyin.com/share/video/<数字>"),
    Case("dy-note", "douyin", "图文笔记",
         "", source="manual",
         note="需要你给一条 douyin.com/note/<数字>，**必须是图文帖**（解析出图片而非视频）"),
    Case("dy-slides", "douyin", "图集",
         "", source="manual",
         note="需要你给一条 douyin.com/slides/<数字>"),

    # ==================== 快手 ====================
    Case("ks-video", "kuaishou", "视频",
         "", source="manual",
         note="需要你给一条 v.kuaishou.com/xxxxx 短链或 kuaishou.com/short-video/<id>"),
    Case("ks-gallery", "kuaishou", "图集",
         "", source="manual",
         note="需要你给一条快手**图集**（不是视频）"),
    Case("ks-chenzhong", "kuaishou", "快影/极速版域名",
         "", source="manual",
         note="需要你给一条 chenzhongtech.com/fw/xxxxx；没有的话这条可跳过"),

    # ==================== 微博 ====================
    # 注意：这里用 m.weibo.cn/detail/<wid> 形态。实测 m.weibo.cn/statuses/show?id=
    # 原先匹配不到（pattern 写的是 status，链接是 statuses），那是 1.3.0
    # 已修的 bug（见 core/parsers/weibo.py 与 SMOKE_TEST.md「发现 1」）。
    # 「形态能不能匹配」由 tests/test_weibo_forms.py 离线守着（不需要网络），
    # 这里只管「真解析能不能出东西」——而它需要 Cookie / 过风控，本机大概率失败，
    # 失败时的可读提示本身就是被测的一部分。
    Case("wb-single", "weibo", "单图微博",
         "https://m.weibo.cn/detail/P9M8meR0O", source="doc"),
    Case("wb-video", "weibo", "视频微博",
         "https://m.weibo.cn/detail/5054181788092183", source="doc"),
    Case("wb-multi", "weibo", "多图微博",
         "https://m.weibo.cn/detail/4977266192171722", source="doc"),

    # ==================== 小红书 ====================
    Case("xhs-image", "xiaohongshu", "图文笔记",
         "", source="manual",
         note="需要你给一条 xiaohongshu.com/explore/<24位hex>，图文型"),
    Case("xhs-video", "xiaohongshu", "视频笔记",
         "", source="manual",
         note="需要你给一条小红书**视频**笔记"),
    Case("xhs-short", "xiaohongshu", "短链",
         "https://xhslink.cn/o/7FZArxKYqOv", source="manual",
         follow_redirect=True,
         note="用户提供的真实短链（App 分享形态），走 parse_with_redirect 测重定向分支。"
              "这条是**图文帖且标题为空**（正文直接从内容开始）—— 所以不能用"
              "「必须有标题」当判据，见 expect_fields 的说明"),

    # ==================== Twitter ====================
    # 用户提供的真实链接。**必须走代理才能测到** —— vxtwitter 在国内直连会被
    # TLS 重置，报 SSLV3_ALERT_HANDSHAKE_FAILURE；run_smoke.py 默认会设
    # HTTP(S)_PROXY。这两条同时覆盖了「纯视频」与「视频 + GIF」两种取流结果。
    Case("tw-video", "twitter", "视频推",
         "https://x.com/gosari542/status/2106766026847522972", source="manual",
         note="1 个视频，0 图"),
    Case("tw-gif", "twitter", "视频 + GIF",
         "https://x.com/Wolhaiiiksong/status/2107077287913087138", source="manual",
         note="2 个视频其中 1 个是 GIF —— 专门验 is_gif 分支：GIF 不该被当视频作品"
              "（ParseResult.video 会跳过它，卡片按图文处理）"),

    # ==================== NGA ====================
    Case("nga-1", "nga", "普通帖",
         "", source="manual",
         note="需要你给一条 nga.178.com/read.php?tid=<数字>"),
    Case("nga-2", "nga", "含图帖",
         "", source="manual",
         note="需要你给一条**带图片附件**的 NGA 帖（验证 img.nga.178.com 取图）"),
    Case("nga-3", "nga", "讨论串/长帖",
         "", source="manual",
         note="需要你给一条楼层多的长帖"),

    # ==================== AcFun ====================
    Case("ac-video", "acfun", "视频",
         "https://www.acfun.cn/v/ac43445963", source="doc",
         note="社区逆向文档里的样例"),
    Case("ac-article", "acfun", "文章",
         "https://www.acfun.cn/a/ac37416587", source="doc",
         note="注意：插件只注册了 ac= / /ac 的 pattern，"
              "**文章链接 a/ac 可能匹配不上**，这条是来验证的"),
    Case("ac-bangumi", "acfun", "番剧",
         "https://www.acfun.cn/bangumi/aa5023295", source="doc",
         note="同上，bangumi 路径大概率不匹配；番剧受地区限制"),

    # ==================== GitHub ====================
    # **只有「仓库」一种内容类型。** 实测：/issues/1、/releases/tag/1.2.0、
    # /blob/main/README.md 全部只捕获 owner/repo，产出与仓库根完全相同的结果
    # —— 子路径被彻底忽略。所以下面两条测的是**边界行为**而非别的内容类型：
    # 该拒绝的要给出可读理由，而不是静默解析成仓库。
    Case("gh-repo", "github", "仓库",
         "https://github.com/xiaoxi2760/astrbot_plugin_denia_share", source="api",
         note="用插件自己的仓库，顺带验 .git 后缀与用户信息"),
    Case("gh-notrepo", "github", "非仓库路径应被拒",
         "https://github.com/features/copilot", source="api",
         expect_error="不是仓库地址",
         note="github.com 下有一堆非仓库路径（features/orgs/apps…），"
              "要给出可读拒绝而不是硬解析"),
    Case("gh-404", "github", "不存在的仓库应被拒",
         "https://github.com/xiaoxi2760/definitely_not_exist_zzz9", source="api",
         expect_error="仓库不存在"),

    # ==================== Pixiv ====================
    Case("px-1", "pixiv", "单张插画",
         "https://www.pixiv.net/artworks/87070841", source="api",
         note="ajax 接口无 Cookie 返回 200"),
    Case("px-2", "pixiv", "作品（可能多页）",
         "https://www.pixiv.net/artworks/115779535", source="api"),
    Case("px-ugoira", "pixiv", "动图 ugoira",
         "", source="manual",
         note="需要你给一条 ugoira 动图作品的链接（artworks/<数字>，类型为 ugoira）"),

    # ==================== Steam ====================
    Case("steam-app", "steam", "游戏商店页",
         "https://store.steampowered.com/app/730/CounterStrike_2/", source="doc",
         note="CS2；API 探测时走代理报 SSL EOF，可能需直连"),
    Case("steam-dlc", "steam", "DLC",
         "https://store.steampowered.com/sub/400/Terraria/", source="doc"),
    Case("steam-bundle", "steam", "合集/包",
         "https://store.steampowered.com/bundle/1145360/", source="doc"),
]


def by_platform() -> dict[str, list[Case]]:
    out: dict[str, list[Case]] = {}
    for case in CASES:
        out.setdefault(case.platform, []).append(case)
    return out


def runnable() -> list[Case]:
    """有 URL 的用例（manual 且 URL 为空的会被跳过）。"""
    return [c for c in CASES if c.url]
