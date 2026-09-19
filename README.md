# 达妮娅分享

AstrBot 链接分享自动解析插件：把分享链接解析成结构化内容，渲染成分享卡片发送。

支持 **B站 / 抖音 / 快手 / 微博 / 小红书 / Twitter(X) / AcFun / NGA / GitHub / Pixiv / Steam** 十一个平台，
另有网页截图（`/shot`）与 Pixiv 关键词搜索（`/pixiv`）。

插件在 AstrBot Dashboard 里带一个自己的页面：状态总览、手动解析预览卡片、解析缓存管理、全部配置项维护。

## 功能

| 平台 | 解析内容 |
| --- | --- |
| B站 | 视频 / 动态 / 直播 / 专栏 / 收藏夹 |
| 抖音 | 视频 / 图集 |
| 快手 | 视频 / 图集 |
| 微博 | 正文 / 视频 / 长文 |
| 小红书 | 笔记（图文 / 视频），图片与封面统一改写为无水印原图 |
| Twitter / X | 推文（图片 / 视频） |
| AcFun | 视频 |
| NGA | 帖子 |
| GitHub | 仓库卡片：星标 / 分支 / 未关闭 issue / 主语言 / 许可 / topics / 最近提交 |
| Pixiv | 作品解析 + 关键词搜索（强制过滤非全年龄内容） |
| Steam | 商店页：名称 / 简介 / 开发商 / 发行日期 / 类型 / 国区价格 / 折扣，可选历史最低价 |

解析结果可渲染成分享卡片（**4 种布局 × 深浅双主题**），也可关闭渲染回退为纯文本。

另外支持 QQ 小程序分享卡片（`Json` 消息段）里的链接提取，以及 OneBot 合并转发发送。

## 安装

1. 把 `astrbot_plugin_denia_share` 放进 AstrBot 的 `data/plugins/` 目录
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot，在 WebUI 的插件管理里启用

> 网页界面需要 AstrBot **>= 4.25.3**（用到了官方的 Plugin Pages 机制）。
> 在更老的版本上插件照常工作，只是没有这个页面。

## 命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/shot <网址>` | 网页截图 | 全部 |
| `/pixiv <关键词>` | Pixiv 搜索，最多 6 张，强制过滤非全年龄 | 全部 |
| `/bili_login` | B站扫码登录，Cookie 持久化 | 管理员 |
| `/bili_check` | 检查 B站 Cookie 是否有效 | 全部 |
| `/denia_status` | 查看插件运行状态 | 管理员 |
| `/denia_clear` | 立即清空解析缓存 | 管理员 |

链接解析不需要命令：群里直接发链接即可。

## 网页界面

插件在 Dashboard 里带一个页面（侧边栏「插件 WebUI → 达妮娅分享」，或从插件详情页的 Pages 进入），
分四个标签：

| 标签 | 内容 |
| --- | --- |
| **总览** | 启用平台数、解析记录数、缓存占用、卡片渲染状态；B站扫码登录 / 检测 / 清除；媒体发送方式与风险提示；11 个平台的一键开关 |
| **解析** | 手动粘贴链接立刻预览卡片（可临时换主题 / 布局重渲染），另含网页截图小工具 |
| **缓存** | 解析记录的搜索、平台筛选、来源筛选、分页、卡片预览、单条删除或连文件一起删；过期文件清理、记录与缓存文件的分别清空 |
| **配置** | 全部 30 个配置项（含卡片宽度、封面裁剪、缓存清理间隔、调试日志、媒体发送） |

实现用的是 AstrBot 官方的 **Plugin Pages**：页面放在插件目录 `pages/` 下，
由 Dashboard 以受限 iframe 加载，通过 `window.AstrBotPluginPage` bridge 调用插件注册的 Web API。
因此：

- 插件**不需要自己起 HTTP 服务**，也不引入任何前端框架或 CDN 资源，离线可用
- 页面跟随 Dashboard 的亮 / 暗主题
- **配置保存后即时生效**，不需要重载插件：解析器、渲染器、截图服务、下载器参数都会按新配置重建
  （只有「缓存清理间隔」要重载才生效）
- 卡片图片不通过 URL 暴露（受限 iframe 带不上鉴权头），预览是把图片缩成 base64 随 JSON 返回；
  要看原图走页面上的「下载」按钮

原生插件配置面板已隐藏，避免两处入口写同一份配置。

## 配置项

全部 30 项，分 8 组，常用在前。这 8 组与页面里的分组一一对应。

### 解析设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `DISABLED_PLATFORMS` | 空 | 禁用的平台，逗号分隔 |
| `VIDEO_DURATION_MAXIMUM` | 480 秒 | 视频最大时长，超限不下载但保留标题封面 |
| `VIDEO_SIZE_MAXIMUM_MB` | 100 | 单个视频体积上限，建议 ≤60（QQ 大文件上传易失败） |
| `SEND_ERROR_MESSAGES` | 关 | 解析失败是否回消息；关闭只写日志，群里更安静 |

### B站设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `BILI_CK` | 空 | 建议用 `/bili_login` 扫码；配了才能下 1080P 及以上 |
| `BILI_QUALITY` | 1080P | 360P / 480P / 720P / 1080P / 1080P+ / 4K / 8K |

### Steam 设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `STEAM_REGION` | `cn` | 价格地区，决定官方价格的货币，同时用作史低数据源的 country |
| `ITAD_API_KEY` | 空 | 只有要「历史最低价」才需要，免费申请。留空不显示史低，其余照常 |

### 网页截图

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `SCREENSHOT_BACKEND` | `thum` | `thum` 免 key；`cloudflare` 功能更强但需账号 |
| `SCREENSHOT_FALLBACK` | 关 | 匹配不到平台的链接是否自动截图 |
| `CF_ACCOUNT_ID` | 空 | 仅 cloudflare 后端需要 |
| `CF_API_TOKEN` | 空 | 仅 cloudflare 后端需要，需带 Browser Rendering 权限 |

### 卡片外观

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `RENDER_ENABLED` | 开 | 关闭后回退纯文本 |
| `RENDER_THEME` | `dark` | `dark` / `light` |
| `RENDER_LAYOUT` | `standard` | `standard` 标准 / `magazine` 杂志 / `immersive` 沉浸 / `feed` 信息流 |
| `RENDER_WIDTH` | 800 px | 卡片宽度 520~1080 |
| `RENDER_COVER_FULL_SIZE` | 关 | 封面按原始尺寸展示，不裁切 |
| `RENDER_FONT_PATH` | 空 | 卡片出现方块字时才需要指定字体文件 |

### 媒体发送

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_DIR` | 空 | 共享缓存目录（容器内路径）。留空 = 插件数据目录 |
| `MEDIA_RELAY_ENABLED` | 关 | 把已下载的视频注册成临时 HTTP 链接再发送 |
| `MEDIA_RELAY_CALLBACK_URL` | 空 | 协议端可达的回调地址，如 `http://astrbot:6185`；留空回退全局 `callback_api_base` |
| `MEDIA_RELAY_TTL` | 300 秒 | 中转链接有效期，最小 30 |

详见下面「媒体发送：Docker 分容器部署必看」。

### 高级设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `XHS_CK` | 空 | 小红书 Cookie |
| `PIXIV_CK` | 空 | Pixiv Cookie，留空也能搜；填了收录更全（R18 仍会被过滤） |
| `GITHUB_TOKEN` | 空 | 建议填，免 token 时 60 次 / 小时且按出口 IP 计 |
| `PROXY` | 空 | 全局代理，如 `http://127.0.0.1:7897`，作用于媒体下载与自建请求 |
| `TWITTER_MEDIA_PROXY_BASE` | 空 | twimg 反代根地址，服务器连不上 X 的 CDN 时填 |

### 维护

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_TTL_HOURS` | 24 小时 | 缓存保留时长，0 = 不自动清理 |
| `CACHE_CLEANUP_INTERVAL_MINUTES` | 60 分钟 | 后台清理任务间隔，改动需重载插件 |
| `DEBUG_LOG_ENABLED` | 开 | 关闭后只保留警告与错误日志 |

`_conf_schema.json` 必须与 `core/config.py` 里的 `CONFIG_META` 保持一致：
它是配置项的唯一定义来源，`_conf_schema.json` 由它生成。AstrBot 加载配置时会**剔除 schema 之外的键**，
两边一旦不一致，用户保存的值会在下次重载时静默丢失 —— 插件启动时会自检并在不一致时打警告。

## 媒体发送：Docker 分容器部署必看

AstrBot 把插件发出的媒体交给协议端时，各类消息段的处理方式并不一样：

| 消息段 | AstrBot 的处理 | 跨容器 |
| --- | --- | --- |
| 图片 / 语音 | 先转成 `base64://` | ✅ 不碰文件系统，没事 |
| **视频** | 原样发出，字段是 `file:///AstrBot/data/...` | ❌ 协议端读不到这个路径 |

也就是说：**astrbot 与 NapCat 等协议端分在不同容器时，图文和语音正常，只有视频发不出去。**
两种解法，二选一即可（也可以都配）。

### ① 共享缓存目录（本地文件方式）

配置「媒体发送 → 共享缓存目录」填一个**容器内路径**，并让所有相关容器都把宿主机同一个目录挂到它上面：

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

- 一定要写**同一个绝对路径**：两个 compose 文件放在不同目录时，各自的 `./data` 指向不同位置，
  会出现「看着挂了其实不是同一份」
- 留空 = 用插件数据目录（默认行为不变）；填了但目录不可用 / 不可写会**打警告并回退**，
  不会把下载整个搞坏

### ② 媒体中转（链接方式）

配置「媒体发送 → 启用媒体中转」，把已下载的视频注册进 AstrBot 的 `file_token_service`，
拿到 `{回调地址}/api/file/<token>` 后用链接发送。协议端只要能访问到那个地址就行，**不需要共享挂载**。

- 「AstrBot 回调地址」留空时回退 AstrBot 全局 `callback_api_base`
- 同一 Docker 网络内可以直接用容器名：`http://astrbot:6185`
- 地址必须带 `http://` / `https://`，否则不启用（避免生成协议端无法识别的链接）
- 任何一步失败（没配地址、文件不存在、注册异常）都会**回退成本地文件发送**，不会把视频丢掉

> 「首页 → 总览」里有一张「媒体发送」卡片，会在「检测到在容器里但两条路都没铺」时给出 ⚠️ 提示。

## 平台细节

### B站：未登录也能拿到 720P

B站的清晰度上限由服务端按登录态决定，与用什么方式取流无关。但**同一匿名状态下，两条取流路径给到的结果不同**
（实测 2026-09-17，BV1uth56uEz3，5 分 09 秒）：

| 路径 | 服务端 `quality` | 实际给到的流 |
| --- | --- | --- |
| DASH（`fnval=4048`） | 64（720P） | 只有 **480P** 和 360P，没有 720P 流 |
| html5 / MP4 单文件 | 64（720P） | 单个合并好的 **720P** 文件，35.3 MB（360P 只有 11.2 MB） |

`accept_quality` 里列出的 1080P+ / 1080P 是「诱饵」，匿名下并不真的给流。

所以插件采用：**DASH 拿不到流、或拿到的流低于目标档位时，回退 html5 单文件 MP4**，
且只在 html5 档位确实更高时才替换（避免登录后目标设 4K、DASH 给 1080P 却被降到 720P）。

要 1080P 及以上仍然必须配置 Cookie（`/bili_login`）。

### 网页截图

两个后端，配置 `SCREENSHOT_BACKEND` 切换：

| 后端 | 凭据 | 实测 | 适用 |
| --- | --- | --- | --- |
| `thum`（默认） | 免 key | 连打 4/4 成功，900×900 | 一般资讯站、博客、商品页 |
| `cloudflare` | Account ID + API Token | 未实测（需账号） | 要截长图 / 等待 JS / 指定选择器 |

命令 `/shot <网址>`；也可开启 `SCREENSHOT_FALLBACK`，让匹配不到任何平台的链接自动截图
（默认关，避免群里刷图）。

局限：强反爬站点截不到内容（实测知乎只返回 16KB 空白图），thum.io 也拿不到 JS 渲染后的长图。

### Pixiv 内容过滤

Pixiv 的 `xRestrict` 字段 0 = 全年龄、1 = R18、2 = R18G，本插件对非 0 一律拦截，**不提供开关**。

实测未登录时搜索结果 `xRestrict` 恒为 0，但配置 Cookie 后收录更全的同时也会开始出现 R18 ——
过滤逻辑与是否登录无关，配 Cookie 不会放宽。这是为了不在群里发出违规内容。

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
- CheapShark 的 `cheapestPriceEver` 字段**实测三个游戏恒为 None**，已不可依赖
- 所以真史低只能用 ITAD，且必须申请 key（免费：<https://isthereanydeal.com/apps>）

**没填 key 时插件照常工作，只是不显示史低这一行。**

注意价格显示：国区价来自官方接口（¥），折扣率那行的美元价来自 CheapShark，两者不同源，
已标注「美元区」避免混淆。实测样例（2026-09-19，国区）：

```
艾尔登法环  FromSoftware, Inc.
价格: ¥ 298.00
当前折扣: -10%（美元区 $53.88 / 原价 $59.99）
Metacritic: 94
Steam 评价: Very Positive (94%)
```

### GitHub 速率限制

**建议填 `GITHUB_TOKEN`**：免 token 限额 60 次 / 小时且**按出口 IP 计算**，
共享出口（代理、容器 NAT）下几乎必被限流；填了提升到 5000 次 / 小时。

## 目录结构

```
astrbot_plugin_denia_share/
├── main.py                  插件入口：平台分发、命令、发送、WebUI 注册
├── metadata.yaml            插件元数据（AstrBot 读取）
├── _conf_schema.json        配置契约（与 core/config.py 的 CONFIG_META 一致）
├── logo.png                 插件 logo
├── requirements.txt
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
    ├── card_renderer.py     分享卡片渲染（约 2300 行 Pillow）
    ├── screenshot.py        网页截图（thum / Cloudflare 双后端）
    ├── webui.py             网页界面的后端接口层（18 个路由）
    ├── relay.py             媒体中转：本地文件 → 临时 HTTP 链接
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
        ├── bilibili.py  kuaishou.py  weibo.py  xiaohongshu.py
        ├── twitter.py  acfun.py  nga.py  github.py  pixiv.py  steam.py
        └── douyin/          抖音（唯一需要拆包的平台）
            ├── parser.py    解析主流程
            ├── sign.py      a_bogus 签名
            └── web.py       网页客户端（ttwid 会话）
```

命名约定：`models/` 下的子目录名与 `parsers/` 下的解析器名一一对应，
`parsers/bilibili.py` 对应 `models/bilibili/`，不用缩写。

## 开发状态

| 模块 | 状态 |
| --- | --- |
| 仓库骨架 / 插件注册 | ✅ |
| 解析层（`core/parsers/`） | ✅ 11 个平台 |
| 分享卡片渲染（`core/card_renderer.py`） | ✅ 4 布局 × 深浅双主题 |
| 下载器（`core/download.py`） | ✅ 含体积上限与代理 |
| 网页截图（`core/screenshot.py`） | ✅ 双后端，thum 免 key / Cloudflare 需账号 |
| 网页界面（`pages/` + `core/webui.py`） | ✅ 总览 / 解析 / 缓存 / 配置 四个标签 |
| 媒体发送（`core/relay.py`） | ✅ 共享缓存目录 + 媒体中转两套 |
| 解析记录（`core/history.py`） | ✅ JSONL 落盘，默认保留 500 条 |
| 配置（`_conf_schema.json` + `core/config.py`） | ✅ 8 组 30 项，页面内维护、保存即生效 |

## 许可与致谢

本项目以 MIT License 发布，详见 [LICENSE](LICENSE)。

- [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share)（MIT）——
  解析框架、分享卡片渲染器，以及 B站 / 微博 / 小红书 / AcFun / NGA 的解析实现
- [astrbot_plugin_media_parser](https://github.com/xiaoxi2760/astrbot_plugin_media_parser_yaya)
  （娅娅版）—— 抖音签名接口、快手新版页面兼容、Twitter 兜底链、媒体发送机制
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2)（**Apache-2.0**）——
  抖音 `a_bogus` 签名（`core/parsers/douyin/sign.py`）

其余部分（GitHub / Pixiv / Steam 解析、网页截图、网页界面等）为本项目实现。
以上项目的原始版权声明均保留在对应源文件内。
