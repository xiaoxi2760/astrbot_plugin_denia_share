# 开发与实现说明

面向改代码的人。用户向的简介、安装、命令、配置速查在 [README.md](README.md)。

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
│   ├── prefs.js             界面外观偏好（主题色 / 圆角 / 紧凑 / 动效的存取与 CSS 变量派生）
│   └── views/               overview / parse / cache / appearance / config 五个标签页
└── core/
    ├── base_parser.py       解析器基类：URL 注册、懒下载、媒体构建
    ├── data.py              解析结果数据模型（ParseResult 等）
    ├── task.py              PathTask：结果的懒加载包装
    ├── download.py          下载器：流式下载、体积上限、代理
    ├── card_renderer.py     分享卡片渲染（约 2300 行 Pillow）
    ├── screenshot.py        网页截图（thum / Cloudflare 双后端）
    ├── webui.py             网页界面的后端接口层（23 个路由）
    ├── custom_parsers.py    用户自定义解析器：目录扫描、动态加载、失败隔离
    ├── custom_parsers_guide.md  自定义解析器说明（运行时复制成数据目录里的 README.md）
    ├── relay.py             媒体中转：本地文件 → 临时 HTTP 链接
    ├── history.py           解析记录持久化（history.jsonl）
    ├── config.py            配置元数据与读写（CONFIG_META 是唯一来源）
    ├── constants.py         常量与平台枚举
    ├── exception.py         异常类型
    ├── media_utils.py       媒体与文件工具（ffmpeg、缓存清理、时长格式化）
    ├── cookie_utils.py      Cookie 工具
    ├── models/              平台接口数据模型（msgspec）
    └── parsers/             平台解析器，一个平台一个模块（douyin 是唯一拆包的）
```

命名约定：`models/` 下的子目录名与 `parsers/` 下的解析器名一一对应，
`parsers/bilibili.py` 对应 `models/bilibili/`，不用缩写。

## 配置项（全 38 项 / 8 组）

**这张表是「有哪些配置项」在文档侧的唯一完整清单**（README 只留一张常见项速查表，
不逐项复制）。总共 38 项里有一项（`HTTP_VERIFY_SSL`）标了 `invisible`，只存在于 json，
不出现在配置页 —— 所以用户在实际页面上看到的是 37 项。分组与网页界面里的一一对应。

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
| `RENDER_ACCENT_COLOR` | 空 | 自定义强调色，`#RRGGBB`；留空 = 跟随平台主色 |
| `RENDER_WATERMARK` | `希望解析` | 页脚水印文字，最多 12 字；留空 = 不显示水印 |
| `RENDER_DESC_MAX_LINES` | 0 | 正文最大行数，0 = 不限（按布局自适应），上限 12 |
| `RENDER_SHOW_AVATAR` | 开 | 是否显示作者头像；关闭后作者行紧凑居中 |
| `RENDER_SHOW_PLAY_BUTTON` | 开 | 视频封面中央的毛玻璃播放按钮；关掉后封面更干净 |
| `RENDER_GRADIENT_TOP` | 空 | 背景渐变顶部色，`#RRGGBB`；留空 = 用主题自带渐变 |
| `RENDER_GRADIENT_BOTTOM` | 空 | 背景渐变底部色，同上 |

### 媒体发送

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_DIR` | 空 | 共享缓存目录（容器内路径）。留空 = 插件数据目录 |
| `MEDIA_RELAY_ENABLED` | 关 | 把已下载的视频注册成临时 HTTP 链接再发送 |
| `MEDIA_RELAY_CALLBACK_URL` | 空 | 协议端可达的回调地址，如 `http://astrbot:6185`；留空回退全局 `callback_api_base` |
| `MEDIA_RELAY_TTL` | 300 秒 | 中转链接有效期，最小 30 |

### 高级设置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `XHS_CK` | 空 | 小红书 Cookie |
| `PIXIV_CK` | 空 | Pixiv Cookie，留空也能搜；填了收录更全（R18 仍会被过滤） |
| `GITHUB_TOKEN` | 空 | 建议填，免 token 时 60 次 / 小时且按出口 IP 计 |
| `PROXY` | 空 | 全局代理，如 `http://127.0.0.1:7897`，作用于媒体下载与自建请求 |
| `HTTP_VERIFY_SSL` | 开 | HTTPS 证书校验。**`invisible`，不出现在配置页**，只能改 json；只有自建反代 / 内网镜像的证书链有问题时才临时关掉 |
| `TWITTER_MEDIA_PROXY_BASE` | 空 | twimg 反代根地址，服务器连不上 X 的 CDN 时填 |

### 维护

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `CACHE_TTL_HOURS` | 24 小时 | 缓存保留时长，0 = 不自动清理 |
| `CACHE_CLEANUP_INTERVAL_MINUTES` | 60 分钟 | 后台清理任务间隔，改动需重载插件 |
| `DEBUG_LOG_ENABLED` | 开 | 关闭后只保留警告与错误日志 |

`_conf_schema.json` 必须与 `core/config.py` 里的 `CONFIG_META` 保持一致：它是配置项的
唯一定义来源，`_conf_schema.json` 由它生成（`tools/gen_conf_schema.py`）。AstrBot 加载
配置时会**剔除 schema 之外的键**，两边一旦不一致，用户保存的值会在下次重载时静默丢失 ——
插件启动时会自检并在不一致时打警告。

## 插件详情页显示什么（改文案前先看这里）

AstrBot 插件管理里的详情页直接吃这几个来源，改错一处用户一眼就看到：

| 页面位置 | 来源 | 约束 |
| --- | --- | --- |
| 插件名 | `metadata.yaml` 的 `display_name`（i18n 可覆盖） | 中文名的四处清单见 `MAINTENANCE.md`「品牌字串都在哪」 |
| 插件名下面的说明 | `metadata.yaml` 的 `desc`（i18n `metadata.desc` 可覆盖） | **20 行以内、不套 `##` 标题** —— 它是「插件名下面那段说明」，不是文档 |
| 卡片短描述 | `short_desc` | 一句话，列清支持的平台 |
| 信息栏 | `version` / `author` / `astrbot_version` / `repo` / `social_link` / `support_platforms` | `support_platforms` 目前填 `aiocqhttp`：合并转发走 OneBot，其他适配器未实测 |
| 页面卡片 | `.astrbot-plugin/i18n/*.json` 的 `pages.<目录名>.title` / `description` | 两个语言包都要有 |
| 指令列表 | **每个 handler 的 docstring** | AstrBot 的 `get_handler_or_create` 取 `handler.__doc__` 当描述，没有就显示「无描述」（`desc=` 可覆盖）。所以每个 `@filter.*` 处理器都要有一句话 docstring，**长说明写在 `#` 注释里** |

自检的 `check_plugin_detail_texts()` 验四件事：`desc` ≤ 20 行、`desc` 里没有标题、
`support_platforms` 非空、每个 `@filter.*` 处理器都有 docstring 且首行 ≤ 80 字
（这两处都真出过问题：`desc` 一度写到 70 行把页面撑满；12 条 handler 没有 docstring，
详情页上是一片「无描述」）。

`desc` 这类「插件名下面」的文案要短，长内容留给 `README.md`。另外 `en-US.json` 缺
`metadata.desc` 时英文界面会落回中文，所以两个语言包都要写。

已知缺口：配置项文案（i18n 的 `config.*`）目前只有中文，英文界面下配置页仍是中文。

## 网页界面的实现机制

用的是 AstrBot 官方的 **Plugin Pages**：页面放在插件目录 `pages/` 下，由 Dashboard 以受限
iframe 加载，通过 `window.AstrBotPluginPage` bridge 调用插件注册的 Web API。因此：

- 插件**不需要自己起 HTTP 服务**，也不引入任何前端框架或 CDN 资源，离线可用
- 页面跟随 Dashboard 的亮 / 暗主题
- **配置保存后即时生效**，不需要重载插件：解析器、渲染器、截图服务、下载器参数都会按新
  配置重建（只有「缓存清理间隔」要重载才生效）
- 卡片图片不通过 URL 暴露（受限 iframe 带不上鉴权头），预览是把图片缩成 base64 随 JSON
  返回；要看原图走页面上的「下载」按钮
- **页面左上角的品牌位直接指向 `pages/denia/logo.png`**（`index.html` 里的 `<img class="rail-logo">`）。
  AstrBot 的 Page 服务把资源解析到**页目录之内**、越界一律拒绝，所以插件根目录那张
  `logo.png`（插件列表用的）不能被 `../` 引用 —— 页目录里放的是一份**副本**。
  **换 logo 时两处都要换**，自检有一条断言校验两份逐字节相同（只换一处会 FAIL，
  否则页面会一直显示旧图）

原生插件配置面板已隐藏，避免两处入口写同一份配置。

两套「外观」是刻意分开的：

| | 界面自定义 | 卡片设计器 |
| --- | --- | --- |
| 影响范围 | 只有这个网页 | 真正发出去的卡片 |
| 存放位置 | `plugin_data/astrbot_plugin_denia_share/webui_appearance.json` | 插件配置（`RENDER_*`） |
| 保存链路 | 独立接口，白名单键 + 回读二次校验 | 走配置页同一条保存链路，保存即生效 |

外观页的预览用的是内置示例数据，**不联网、不写解析记录**，改参数不会污染缓存。

## 媒体发送：两条链路

README 里讲了「怎么配」，这里记的是实现上的取舍：

- 共享缓存目录留空 = 用插件数据目录（默认行为不变）；填了但目录不可用 / 不可写会**打警告
  并回退**，不会把下载整个搞坏
- 一定要写**同一个绝对路径**：两个 compose 文件放在不同目录时，各自的 `./data` 指向不同
  位置，会出现「看着挂了其实不是同一份」
- 媒体中转的地址必须带 `http://` / `https://`，否则不启用（避免生成协议端无法识别的链接）
- 中转的任何一步失败（没配地址、文件不存在、注册异常）都会**回退成本地文件发送**，
  不会把视频丢掉

## 平台实现细节与实测结论

### B站：未登录也能拿到 720P

B站的清晰度上限由服务端按登录态决定，与用什么方式取流无关。但**同一匿名状态下，两条取流
路径给到的结果不同**（实测 2026-09-17，BV1uth56uEz3，5 分 09 秒）：

| 路径 | 服务端 `quality` | 实际给到的流 |
| --- | --- | --- |
| DASH（`fnval=4048`） | 64（720P） | 只有 **480P** 和 360P，没有 720P 流 |
| html5 / MP4 单文件 | 64（720P） | 单个合并好的 **720P** 文件，35.3 MB（360P 只有 11.2 MB） |

`accept_quality` 里列出的 1080P+ / 1080P 是「诱饵」，匿名下并不真的给流。

所以插件采用：**DASH 拿不到流、或拿到的流低于目标档位时，回退 html5 单文件 MP4**，
且只在 html5 档位确实更高时才替换（避免登录后目标设 4K、DASH 给 1080P 却被降到 720P）。
要 1080P 及以上仍然必须配置 Cookie。

### B站：受限视频会说明原因

充电专属 / 大会员专享 / 付费专享 / 登录后可看的视频拿不到完整流。插件会**在卡片与消息里
说明是哪一种**，而不是笼统地报一句「视频下载失败」——后者分不清「该去登录开会员」还是
「过一会儿重试」。只拿到试看片段时，提示里还会带上「可解析时长 / 全长」（如 `3:12 / 16:24`），
判定依据是播放接口返回的分段时长与全长之差，**不需要会员账号就能看出来**。

### 网页截图：两个后端

| 后端 | 凭据 | 实测 | 适用 |
| --- | --- | --- | --- |
| `thum`（默认） | 免 key | 连打 4/4 成功，900×900 | 一般资讯站、博客、商品页 |
| `cloudflare` | Account ID + API Token | 未实测（需账号） | 要截长图 / 等待 JS / 指定选择器 |

局限：强反爬站点截不到内容（实测知乎只返回 16KB 空白图），thum.io 也拿不到 JS 渲染后的长图。

### Pixiv 内容过滤

Pixiv 的 `xRestrict` 字段 0 = 全年龄、1 = R18、2 = R18G，本插件对非 0 一律拦截，**不提供
开关**。实测未登录时搜索结果 `xRestrict` 恒为 0，但配置 Cookie 后收录更全的同时也会开始
出现 R18 —— 过滤逻辑与是否登录无关，配 Cookie 不会放宽。这是为了不在群里发出违规内容。

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

价格显示要注意：国区价来自官方接口（¥），折扣率那行的美元价来自 CheapShark，两者不同源，
已标注「美元区」避免混淆。

### GitHub 速率限制

**建议填 `GITHUB_TOKEN`**：免 token 限额 60 次 / 小时且**按出口 IP 计算**，共享出口
（代理、容器 NAT）下几乎必被限流；填了提升到 5000 次 / 小时。

## 自定义解析器

用户向的完整说明在 [`core/custom_parsers_guide.md`](core/custom_parsers_guide.md)
（运行时会被复制成数据目录里的 `custom_parsers/README.md`）。改这里的接口时记得：

- **改 `BaseParser` 的对外接口就要把 `core/custom_parsers.py` 的 `API_VERSION` +1**，
  并在这里说明改了什么，让用户知道要跟着改哪里
- 新增/删除构建件（`create_*` / `client_kwargs` 等）都要同步更新那份说明与 `TEMPLATE`

## 开发状态

| 模块 | 状态 |
| --- | --- |
| 仓库骨架 / 插件注册 | ✅ |
| 解析层（`core/parsers/`） | ✅ 11 个平台 |
| 分享卡片渲染（`core/card_renderer.py`） | ✅ 4 布局 × 深浅双主题，可自定义强调色 / 渐变 / 水印 / 正文行数 / 头像 / 播放按钮 |
| 下载器（`core/download.py`） | ✅ 含体积上限与代理 |
| 网页截图（`core/screenshot.py`） | ✅ 双后端，thum 免 key / Cloudflare 需账号 |
| 网页界面（`pages/` + `core/webui.py`） | ✅ 总览 / 解析 / 缓存 / 外观 / 配置 五个标签 |
| 媒体发送（`core/relay.py`） | ✅ 共享缓存目录 + 媒体中转两套 |
| 解析记录（`core/history.py`） | ✅ JSONL 落盘，默认保留 500 条 |
| 配置（`_conf_schema.json` + `core/config.py`） | ✅ 8 组 38 项，页面内维护、保存即生效 |
| 自定义解析器（`core/custom_parsers.py`） | ✅ 动态加载 `.py`，失败隔离并显示在页面 |

## 许可与出处

本项目以 MIT License 发布，详见 [LICENSE](LICENSE)。引用了第三方代码的部分只有这两处：

| 来源 | 许可 | 用到哪里 |
| --- | --- | --- |
| [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share) | MIT | 解析框架（`base_parser` / `data` / `task` / `models`）、分享卡片渲染器 `core/card_renderer.py`、B站 / 微博 / 小红书 / AcFun / NGA 的解析实现、抖音的 HTML 取流路径、快手的页面结构、Twitter 的 vxtwitter 兜底、网页截图的 Cloudflare 后端 |
| [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | Apache-2.0 | 抖音 `a_bogus` 签名（`core/parsers/douyin/sign.py`，文件头保留了 `SPDX-License-Identifier: Apache-2.0`） |

其余部分（GitHub / Pixiv / Steam 解析、网页界面与外观自定义、媒体发送的两套机制、卡片外观
配置项等）为本项目实现。

- Apache-2.0 全文随包附在 `LICENSES/Apache-2.0.txt`
- 上表里列到的源文件，文件头都带一行来源说明
  （`# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码`）；
  README 与 `core/__init__.py` / `core/parsers/__init__.py` 只做汇总，不替代它们
- 注意：rika_share 上游本身没有 per-file 版权头，声明只在其仓库的 LICENSE 里；
  这里的文件头是本项目为满足 MIT「在副本中保留声明」而额外补的
