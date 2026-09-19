# 达妮娅分享

AstrBot 链接分享自动解析插件：解析分享链接，渲染成分享卡片发送。

支持 **B站 / 抖音 / 快手 / 微博 / 小红书 / Twitter / AcFun / NGA / GitHub / Pixiv / Steam** 十一个平台，
另有网页截图（`/shot`）与 Pixiv 关键词搜索（`/pixiv`）。
插件自带 Dashboard 页面：总览状态、手动解析预览卡片、管理解析缓存、维护全部配置。

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

## 网页界面

插件在 AstrBot Dashboard 里带一个自己的页面（侧边栏「插件 WebUI → 达妮娅分享」，
或从插件详情页的 Pages 进入），分四个标签：

| 标签 | 内容 |
| --- | --- |
| **总览** | 启用平台数、解析记录数、缓存占用、卡片渲染状态；B站扫码登录 / 检测 / 清除；媒体发送方式与风险提示；11 个平台的一键开关 |
| **解析** | 手动粘贴链接立刻预览卡片（可临时换主题/布局重渲染），另含网页截图小工具 |
| **缓存** | 解析记录的搜索、平台/来源筛选、分页、卡片预览、单条删除或连文件一起删；过期文件清理、记录与缓存文件的分别清空 |
| **配置** | 全部 30 个配置项（含原生面板没有的卡片宽度、封面裁剪、缓存清理间隔、调试日志、媒体发送） |

实现方式是 AstrBot 官方的 **Plugin Pages**：页面放在插件目录 `pages/` 下，
由 Dashboard 以受限 iframe 加载，通过 `window.AstrBotPluginPage` bridge 调用插件注册的
Web API（`context.register_web_api`）。所以：

- 不需要插件自己起 HTTP 服务，也不引入任何前端框架或 CDN 资源（离线可用）
- 页面跟随 Dashboard 的亮/暗主题
- **配置保存后即时生效**，不要求重载插件：解析器、渲染器、截图服务、下载器参数
  都会按新配置重建（只有「缓存清理间隔」需要重载才生效）
- 卡片图片不通过 URL 暴露（受限 iframe 带不上鉴权头），预览是把图片缩成
  base64 随 JSON 返回；要看原图走页面上的「下载」按钮

> 依赖 AstrBot **>= 4.25.3**（Plugin Pages 从 4.24.2 引入，侧边栏入口与主题同步是 4.25.3）。
> 在更老的版本上插件照常工作，只是没有这个页面。

## 媒体发送：Docker 分容器部署必看

AstrBot 把插件发出去的媒体交给协议端时，各类消息段处理方式并不一样
（`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py`）：

| 消息段 | AstrBot 的处理 | 跨容器 |
| --- | --- | --- |
| 图片 / 语音 | 先 `convert_to_base64()` 转成 `base64://` | ✅ 不碰文件系统，没事 |
| **视频** | `to_dict()` **原样发出**，字段是 `file:///AstrBot/data/...` | ❌ 协议端读不到这个路径 |

也就是说：**astrbot 与 NapCat 等协议端分在不同容器时，图文和语音正常，只有视频发不出去。**
两种解法，二选一即可（也可都用）：

### ① 共享缓存目录（本地文件方式）

配置「媒体发送 → 共享缓存目录」填一个**容器内路径**，并让所有相关容器都把
宿主机同一个目录挂到它上面：

```yaml
services:
  astrbot:
    volumes:
      - /srv/astrbot/data/shared:/app/sharedFolder/denia_share   # 用绝对路径
  napcat:
    volumes:
      - /srv/astrbot/data/shared:/app/sharedFolder/denia_share   # 必须是同一路径
```

然后配置里填 `/app/sharedFolder/denia_share/cache`。这样插件生成的
`file:///app/sharedFolder/denia_share/cache/xxx.mp4` 在协议端容器里也成立。

- 一定要写**同一个绝对路径**：两个 compose 文件放在不同目录时，各自的 `./data`
  指向不同位置，会出现「看着挂了其实不是同一份」
- 留空 = 用插件数据目录（默认行为不变）；填了但目录不可用/不可写会**打警告并回退**，
  不会把下载整个搞坏

### ② 媒体中转（链接方式）

配置「媒体发送 → 启用媒体中转」，把已下载的视频注册进 AstrBot 的
`file_token_service`，拿到 `{回调地址}/api/file/<token>`，用 `Video.fromURL(...)` 发送。
协议端只要能访问到那个地址就行，**不需要共享挂载**。

- 「AstrBot 回调地址」留空时回退 AstrBot 全局 `callback_api_base`
- 同一 Docker 网络内可以直接用容器名：`http://astrbot:6185`
- 地址必须带 `http://` / `https://`，否则不启用（避免生成协议端无法识别的链接）
- 任何一步失败（没配地址、文件不存在、注册异常）都会**回退成本地文件发送**，
  不会把视频丢掉

实现移植自作者的另一个插件 [astrbot_plugin_media_parser（娅娅版）](https://github.com/xiaoxi2760)，
那边的对应配置是 `download.cache_dir` 与 `media_relay.*`。

> 「首页 → 总览」里有一张「媒体发送」卡片，会用 ⚠️ 提示「在容器里但两条路都没铺」的情况。

## 与 yaya（astrbot_plugin_media_parser）的差异

娅娅版功能面很大（13 平台 + LLM 翻译 + 热评 + 归档 + 媒体中转 + 权限/限流），
本插件是有意做减法的精简版：

- ❌ **LLM 文本翻译**（10 语言 × 10 厂商接口）—— 与链接解析无关
- ❌ **视频仅发送封面** —— 边缘功能
- ❌ B站 Cookie 定时监控与失效通知（保留扫码登录 + 持久化）
- ❌ 热评、ZIP 归档、媒体中转、权限白黑名单、频率限制
- ✅ 保留：平台解析、卡片渲染、OneBot 合并转发 / 其他平台直发、JSON 卡片（QQ 小程序）提取、B站扫码登录
- ✅ 网页截图只保留 4 个配置项（娅娅/rika 的 Cloudflare 实现有 20+ 项）

配置项从娅娅版的几十个压到 **30 项**（上游 rika 同期为 40+ 项且仍在增加）。

## 配置项

全部 30 项都在网页界面的「配置」标签里维护（原生插件配置面板已隐藏，避免两处入口）。
分成 8 组，常用在前、折腾在后。

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
| `RENDER_WIDTH` | 800 | 卡片宽度 520~1080 |
| `RENDER_COVER_FULL_SIZE` | 关 | 封面按原始尺寸铺满，不裁切 |
| `RENDER_FONT_PATH` | 空 | 卡片出现方块字时才需要指定字体 |

### 媒体发送

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_DIR` | 空 | 共享缓存目录（容器内路径）。留空=插件数据目录；填了要让各容器挂成同一路径 |
| `MEDIA_RELAY_ENABLED` | 关 | 把已下载的视频注册成 AstrBot 的临时 HTTP 链接再发送 |
| `MEDIA_RELAY_CALLBACK_URL` | 空 | 协议端可达的回调地址，如 `http://astrbot:6185`；留空回退全局 `callback_api_base` |
| `MEDIA_RELAY_TTL` | 300 | 中转链接有效期（秒），最小 30 |

详见上面的「媒体发送：Docker 分容器部署必看」。

### 高级设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `XHS_CK` | 空 | 小红书 Cookie |
| `PIXIV_CK` | 空 | Pixiv Cookie，留空也能搜；填了收录更全（R18 仍会被过滤） |
| `GITHUB_TOKEN` | 空 | 建议填，免 token 时 60 次/小时且按出口 IP 计 |
| `PROXY` | 空 | 全局代理，如 `http://127.0.0.1:7897`，作用于下载与自建请求 |
| `TWITTER_MEDIA_PROXY_BASE` | 空 | twimg 反代根地址，服务器连不上 X CDN 时填 |

### 维护

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_TTL_HOURS` | 24 | 缓存保留时长，0 = 不自动清理 |
| `CACHE_CLEANUP_INTERVAL_MINUTES` | 60 | 后台清理任务间隔，改动需重载插件 |
| `DEBUG_LOG_ENABLED` | 开 | 关闭后只保留警告与错误日志 |

`_conf_schema.json` 由 `tools/gen_conf_schema.py` 从 `core/config.py` 的 `CONFIG_META`
**单向生成**，不要手改：AstrBot 加载配置时会剔除 schema 之外的键，两边一旦不一致，
用户保存的值会在下次重载时静默丢失。插件启动时会自检并在不一致时打警告。

## 目录结构

```
astrbot_plugin_denia_share/
├── main.py                  插件入口：平台分发、命令、发送、WebUI 注册
├── metadata.yaml            插件元数据（AstrBot 读取）
├── _conf_schema.json        配置契约（由 tools/gen_conf_schema.py 生成）
├── .astrbot-plugin/i18n/    插件页面与元数据的多语言文案
├── pages/denia/             网页界面（单页面 + 内部标签导航）
│   ├── index.html
│   ├── style.css            主题变量、布局与组件样式
│   ├── ui.js                DOM / 提示条 / 弹窗 / 格式化等通用件
│   ├── app.js               页面框架：bridge、导航、视图挂载
│   └── views/               overview / parse / cache / config 四个标签页
└── core/
    ├── base_parser.py       解析器基类：URL 注册、懒下载、媒体构建
    ├── data.py              解析结果数据模型（ParseResult 等）
    ├── task.py              PathTask：结果的懒加载包装
    ├── download.py          下载器：流式下载、体积上限、代理
    ├── card_renderer.py     分享卡片渲染（约 2300 行 Pillow，来自 rika）
    ├── screenshot.py        网页截图（thum / Cloudflare 双后端）
    ├── webui.py             WebUI 的后端接口层（18 个路由）
    ├── relay.py             媒体中转：本地文件 → 临时 HTTP 链接（file_token_service）
    ├── history.py           解析记录持久化（history.jsonl）
    ├── config.py            配置元数据与读写（CONFIG_META 是唯一来源）
    ├── constants.py         常量与平台枚举
    ├── exception.py         异常类型
    ├── media_utils.py       媒体与文件工具（ffmpeg、缓存清理、时长格式化）
    ├── cookie_utils.py      Cookie 工具
    ├── models/              平台接口数据模型（msgspec）
    │   ├── bilibili/        视频 / 动态 / 专栏 / 直播 / 收藏夹
    │   ├── douyin/          视频 / 图集
    │   ├── weibo/           长文 / 详情 / 通用
    │   ├── xiaohongshu/     explore / discovery
    │   ├── kuaishou/
    │   └── acfun/
    └── parsers/             平台解析器，一个平台一个模块
        ├── bilibili.py  douyin/  kuaishou.py  weibo.py
        ├── xiaohongshu.py  twitter.py  acfun.py  nga.py
        ├── github.py  pixiv.py  steam.py
        └── douyin/          抖音（唯一需要拆包的平台）
            ├── parser.py    解析主流程
            ├── sign.py      a_bogus 签名（移植自 f2，Apache-2.0）
            └── web.py       网页客户端（ttwid 会话）
```

命名约定：`models/` 下的子目录名与 `parsers/` 下的解析器名一一对应，
`parsers/bilibili.py` 对应 `models/bilibili/`，不用缩写。

## 开发状态

| 模块 | 状态 |
| --- | --- |
| 仓库骨架 / 插件注册 | ✅ |
| 解析层（`core/parsers/`） | ✅ 11 平台（3 个为合并改写，3 个为新增） |
| 分享卡片渲染（`core/card_renderer.py`） | ✅ 来自 rika |
| 下载器（`core/download.py`） | ✅ 来自 rika，加了体积上限与代理 |
| 网页截图（`core/screenshot.py`） | ✅ 双后端，thum 免 key / Cloudflare 需账号 |
| 网页界面（`pages/` + `core/webui.py`） | ✅ 总览 / 解析 / 缓存 / 配置 四个标签 |
| 媒体发送（`core/relay.py`） | ✅ 共享缓存目录 + 媒体中转两套，移植自娅娅版 |
| 解析记录（`core/history.py`） | ✅ JSONL 落盘，默认保留 500 条 |
| 配置（`_conf_schema.json` + `core/config.py`） | ✅ 8 组 30 项，页面内维护、保存即生效 |

自检脚本：`test/webui/selfcheck.py`（离线，stub 掉 astrbot 环境后真跑插件构造与 18 个接口），
报告输出到 `test/webui/selfcheck_result.txt`，当前 **109 项全部通过**。

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
| `utils.py` 新增 `clear_cache_dir` | ✅ 已同步，接了 `/denia_clear`（现位于 `core/media_utils.py`） |
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
- 抖音 `a_bogus` 签名（`core/parsers/douyin/sign.py`）移植自 [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) —— **Apache-2.0**

## 卡片渲染器的归属

`core/card_renderer.py`（`ShareCardRenderer`，约 2300 行 Pillow 实现，4 种布局 × 深浅双主题）
**是 rika_share 的原创作品**（作者 MIKU1598 / iris1598），不是从 nonebot-plugin-parser 移植的。

容易混淆的一点：nonebot-plugin-parser 也有 `renders/` 目录，但它走的是
**Jinja2 HTML 模板 + 浏览器截图**（`card.html.jinja2`），与这里的纯 Pillow 无头渲染是两套完全不同的实现。
rika 的致谢里只把「解析库逻辑」归给 nonebot-plugin-parser，渲染器并未归给任何上游。

本项目直接把该模块拷贝过来使用，改动仅限：
把水印文字抽成常量 `WATERMARK_TAG`（娅娅版也是这么做的），并改为「达妮娅分享」。
