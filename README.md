# 达妮娅分享

AstrBot 链接分享自动解析插件：解析分享链接，渲染成分享卡片发送。

支持 **B站 / 抖音 / 快手 / 微博 / 小红书 / Twitter / AcFun / NGA / GitHub / Pixiv / Steam** 十一个平台，
另有网页截图（`/shot`）与 Pixiv 关键词搜索（`/pixiv`）。

## 解析内核：两个插件择优合并

本插件不是在 rika_share 上简单改名，而是把两个插件的解析层逐平台对比后，各取更强的一版：

| 平台 | 采用 | 理由 |
| --- | --- | --- |
| B站 | rika（`bilibili_api`） | 视频/动态/直播/专栏/收藏夹全覆盖，含 AI 总结、清晰度档位、CDN 备份重试；代码量仅为娅娅手写版（2934 行）的 1/7 |
| 抖音 | **娅娅**（a_bogus 签名） | rika 依赖的 `iesdouyin` 分享页已基本失效；娅娅版有完整 a_bogus 签名 + ttwid 会话管理 |
| 快手 | **娅娅**（新版页面兼容） | rika 用 msgspec 严格解码 `photo`，新版页面把 `photo` 换成了 **JSON 字符串**会直接解码失败；娅娅版两种形态都兼容，且支持图集 |
| 小红书 | rika + **娅娅无水印改写** | rika 的 explore/discovery 双路径够用，但直接用 `sns-webpic` 鉴权图会带平台水印；娅娅版改写为 `sns-img-hw` 公开原图 CDN，无水印 |
| 微博 | rika + 娅娅增强 | rika 的完整度更高（长文 / 视频 fid / mid2id 转换 / 转发递归） |
| Twitter | **娅娅**（三级兜底） | rika 只走 `vxtwitter` 一家第三方镜像；娅娅版是 vxtwitter → fxtwitter → 官方 Guest GraphQL 三级兜底 |
| AcFun / NGA | rika | 娅娅无此平台 |
| GitHub | 本仓库新增 | 官方 REST API，仓库卡片（星标 / 分支 / 语言 / 许可 / topics） |
| Pixiv | 本仓库新增 | 官方 Ajax 接口，作品解析 + 关键词搜索 |
| Steam | 本仓库新增 | 官方 appdetails + CheapShark 折扣 + ITAD 史低（可选 key） |

**抖音**保留了双路径：先试零成本的 HTML 直取，失败再退到签名接口。
**Twitter**同理：两家第三方镜像都失败后才走官方 GraphQL。

### B站：未登录也能拿到 720P

B站的清晰度上限由服务端按登录态决定，与用什么库无关。但**同一匿名状态下，两条取流路径给到的结果不同**——实测（2026-09-17，BV1uth56uEz3，5 分 09 秒）：

| 路径 | 服务端 `quality` | 实际给到的流 |
| --- | --- | --- |
| DASH（`fnval=4048`） | 64（720P） | 只有 **480P** 和 360P，没有 720P 流 |
| html5 / MP4 单文件 | 64（720P） | 单个合并好的 **720P** 文件，35.3 MB（360P 只有 11.2 MB） |

`accept_quality` 里列出的 1080P+ / 1080P 是"诱饵"，匿名下并不真的给流。

娅娅版正是靠 `FNVAL_MP4` 回退拿到这个 720P。本插件保留了同样的策略：
**DASH 拿不到流、或拿到的流低于目标档位时，回退 html5 单文件 MP4**，
且只在 html5 档位确实更高时才替换（避免登录后目标设 4K、DASH 给 1080P 却被降到 720P）。

要 1080P 及以上仍然必须配置 Cookie（`/bili_login`）。

## 新增解析器（v0.5.0）

### 网页截图

两个后端，配置 `SCREENSHOT_BACKEND` 切换：

| 后端 | 凭据 | 实测 | 适用 |
| --- | --- | --- | --- |
| `thum`（默认） | 免 key | 连打 4/4 成功，900×900 | 一般资讯站、博客、商品页 |
| `cloudflare` | Account ID + API Token | 未实测（需账号） | 要截长图 / 等待 JS / 指定选择器 |

命令 `/shot <网址>`；也可开启 `SCREENSHOT_FALLBACK`，让匹配不到任何平台的链接自动截图（默认关，避免群里刷图）。

局限：强反爬站点截不到内容（实测知乎只返回 16KB 空白图），thum.io 也拿不到 JS 渲染后的长图。

### GitHub 仓库

官方 REST API，出卡片：星标 / 分支 / 未关闭 issue / 主语言 / 许可 / topics / 最近提交。

**建议填 `GITHUB_TOKEN`**：免 token 限额 60 次/小时且**按出口 IP 计算**，共享出口（代理、容器 NAT）下几乎必被限流。填了提升到 5000 次/小时。

### Pixiv

- 作品链接（`pixiv.net/artworks/{id}`）自动解析
- `/pixiv <关键词>` 搜索，最多返回 6 张

**内容过滤是硬性的**：Pixiv 的 `xRestrict` 字段 0=全年龄、1=R18、2=R18G，
本插件对非 0 一律拦截，**不提供开关**。实测未登录时搜索结果 `xRestrict` 恒为 0，
但配置 Cookie 后收录更全的同时也会开始出现 R18 —— 过滤逻辑与是否登录无关，
配 Cookie 不会放宽。这是为了不在群里发出违规内容。

### Steam 与「历史价格」

三层数据源，任何一层失败都不影响其它层：

| 层 | 来源 | 凭据 | 提供 |
| --- | --- | --- | --- |
| 1 | Steam 官方 `appdetails` | 免 key | 名称 / 简介 / 开发商 / 发行日期 / 类型 / **国区价格** |
| 2 | CheapShark | 免 key | Metacritic、Steam 好评率、当前折扣率 |
| 3 | IsThereAnyDeal | **需 key** | 真史低 `historyLow`（全部 / 近一年 / 近三月） |

关于史低的实测结论（2026-09-19）：

- Steam 官方接口**不提供**任何历史价格
- SteamDB 的 `steamdb.info/api` 已 403 被 Cloudflare 拦死，社区也明确禁止爬取
- CheapShark 的 `cheapestPriceEver` 字段**实测三个游戏恒为 None**，已废弃不可依赖
- 所以真史低只能用 ITAD，且必须申请 key（免费：isthereanydeal.com/apps）

**没填 key 时插件照常工作，只是不显示史低这一行。**

注意价格显示：国区价来自官方接口（¥），折扣率那行的美元价来自 CheapShark，
两者不同源，已标注「美元区」避免混淆。

CheapShark 有个坑：必须带描述性 User-Agent，用 httpx 默认 UA 或浏览器 UA
都会返回 400 `Missing or generic User-Agent header detected`。代码里已处理。

实测样例（2026-09-19，国区）：

```
艾尔登法环  FromSoftware, Inc.
价格: ¥ 298.00
当前折扣: -10%（美元区 $53.88 / 原价 $59.99）
Metacritic: 94
Steam 评价: Very Positive (94%)
```

## 与 yaya（astrbot_plugin_media_parser）的差异

娅娅版功能面很大（13 平台 + LLM 翻译 + 热评 + 归档 + 媒体中转 + 权限/限流），
本插件是有意做减法的精简版：

- ❌ **LLM 文本翻译**（10 语言 × 10 厂商接口）—— 与链接解析无关
- ❌ **视频仅发送封面** —— 边缘功能
- ❌ B站 Cookie 定时监控与失效通知（保留扫码登录 + 持久化）
- ❌ 热评、ZIP 归档、媒体中转、权限白黑名单、频率限制
- ✅ 保留：平台解析、卡片渲染、OneBot 合并转发 / 其他平台直发、JSON 卡片（QQ 小程序）提取、B站扫码登录
- ✅ 网页截图只保留 4 个配置项（娅娅/rika 的 Cloudflare 实现有 20+ 项）

配置项从娅娅版的几十个压到 **22 项**（上游 rika 同期为 40+ 项且仍在增加）。

## 配置项

WebUI 里只有 6 组，常用在前、折腾在后。

### 解析设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `DISABLED_PLATFORMS` | 空 | 禁用平台，逗号分隔 |
| `VIDEO_DURATION_MAXIMUM` | 480 | 视频最大时长（秒），超限不下载但保留标题封面 |
| `VIDEO_SIZE_MAXIMUM_MB` | 100 | 视频体积上限，建议 ≤60（QQ 大文件上传易失败） |
| `SEND_ERROR_MESSAGES` | 关 | 解析失败是否回消息；关闭只写日志，群里更安静 |

### B站设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `BILI_CK` | 空 | 建议用 `/bili_login` 扫码；配了才能下 1080P+ |
| `BILI_QUALITY` | 1080P | 360P ~ 8K |

### Steam 设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `STEAM_REGION` | cn | 价格地区，决定官方价格的货币，同时用作 ITAD 的 country |
| `ITAD_API_KEY` | 空 | 只有要「历史最低价」才需要，免费申请。留空不显示史低，其余照常 |

### 网页截图

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `SCREENSHOT_BACKEND` | thum | `thum` 免 key；`cloudflare` 功能更强但需账号 |
| `SCREENSHOT_FALLBACK` | 关 | 匹配不到平台的链接是否自动截图 |
| `CF_ACCOUNT_ID` | 空 | 仅 cloudflare 后端需要 |
| `CF_API_TOKEN` | 空 | 仅 cloudflare 后端需要，需带 Browser Rendering 权限 |

### 卡片外观

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `RENDER_ENABLED` | 开 | 关闭回退纯文本 |
| `RENDER_THEME` | dark | dark / light |
| `RENDER_LAYOUT` | standard | standard / magazine / immersive / feed |

### 高级设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `XHS_CK` | 空 | 小红书 Cookie |
| `PIXIV_CK` | 空 | Pixiv Cookie，留空也能搜；填了收录更全（R18 仍会被过滤） |
| `GITHUB_TOKEN` | 空 | 建议填，免 token 时 60 次/小时且按出口 IP 计 |
| `PROXY` | 空 | 全局代理，如 `http://127.0.0.1:7897`，作用于下载与自建请求 |
| `TWITTER_MEDIA_PROXY_BASE` | 空 | twimg 反代根地址，服务器连不上 X CDN 时填 |
| `CACHE_TTL_HOURS` | 24 | 缓存保留时长，0 = 不自动清理 |
| `RENDER_FONT_PATH` | 空 | 卡片出现方块字时才需要指定 |

**为了简洁而移除的旋钮**（改为代码内固定值）：缓存清理间隔（60 分钟）、
卡片宽度（800）、封面裁剪模式（关闭）、调试日志开关（默认开）。
这些键仍可从配置文件手动覆盖，只是不再出现在 WebUI。

## 开发状态

| 模块 | 状态 |
| --- | --- |
| 仓库骨架 / 插件注册 | ✅ |
| 解析层（`core/parsers/`） | ✅ 11 平台（3 个为合并改写，3 个为新增） |
| 分享卡片渲染（`core/render.py`） | ✅ 来自 rika |
| 下载器（`core/download.py`） | ✅ 来自 rika，加了体积上限与代理 |
| 网页截图（`core/screenshot.py`） | ✅ 双后端，thum 免 key / Cloudflare 需账号 |
| 配置（`_conf_schema.json`） | ✅ 6 组 22 项 |

## 命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/shot <网址>` | 网页截图 | 全部 |
| `/pixiv <关键词>` | Pixiv 搜索，最多 6 张，强制过滤非全年龄 | 全部 |
| `/bili_login` | B站扫码登录，Cookie 持久化 | 管理员 |
| `/bili_check` | 检查 B站 Cookie 是否有效 | 全部 |
| `/denia_status` | 查看插件运行状态 | 管理员 |
| `/denia_clear` | 立即清空解析缓存 | 管理员 |

## 上游同步

上游 [rika_share](https://github.com/iris1598/astrbot_plugin_rika_share) 在
2026-08 至 09 有 6 次提交（HEAD `8a05728`），本插件已同步其中有用的部分：

| 上游改动 | 处理 |
| --- | --- |
| `download.py` chunked 编码修复 | ✅ 已同步（**重要**：旧代码把缺失的 `Content-Length` 当成 0，会取消下载，抖音视频全部下不来） |
| `bilibili.py` -504 退避重试 + AI 总结失败不再阻断解析 | ✅ 已同步 |
| `base_parser.py` `PathTask` 包装修复（抽封面与内容并发 await 同一协程会报错） | ✅ 已同步 |
| `utils.py` 新增 `clear_cache_dir` | ✅ 已同步，接了 `/denia_clear` |
| `twitter.py` 媒体反代 | ✅ 已同步（改为单键 `TWITTER_MEDIA_PROXY_BASE`，省掉开关） |
| `config.py` `SEND_ERROR_MESSAGES` | ✅ 已同步 |
| `douyin.py` 改回无签名 `aweme/detail` + `open.douyin.com` 头 | ⏸ 暂不跟：我们已有签名兜底，未签名路径待实测 |

上游的 `_conf_schema.json` 同期从 22.5KB 涨到 24.5KB（继续加配置项），与本项目做减法的方向相反，故不跟随。

## 安装

1. 将 `astrbot_plugin_denia_share` 放入 AstrBot 的 `data/plugins/` 目录
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot，在 WebUI 插件管理中启用

## 许可

MIT License。详见 [LICENSE](LICENSE)。

本项目包含移植自以下项目的代码，保留其原始版权声明：

- [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share) —— MIT
- 抖音 `a_bogus` 签名（`core/douyin/sign.py`）移植自 [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) —— **Apache-2.0**

## 卡片渲染器的归属

`core/render.py`（`ShareCardRenderer`，约 2300 行 Pillow 实现，4 种布局 × 深浅双主题）
**是 rika_share 的原创作品**（作者 MIKU1598 / iris1598），不是从 nonebot-plugin-parser 移植的。

容易混淆的一点：nonebot-plugin-parser 也有 `renders/` 目录，但它走的是
**Jinja2 HTML 模板 + 浏览器截图**（`card.html.jinja2`），与这里的纯 Pillow 无头渲染是两套完全不同的实现。
rika 的致谢里只把「解析库逻辑」归给 nonebot-plugin-parser，渲染器并未归给任何上游。

本项目直接把该模块拷贝过来使用，改动仅限：
把水印文字抽成常量 `WATERMARK_TAG`（娅娅版也是这么做的），并改为「达妮娅分享」。
