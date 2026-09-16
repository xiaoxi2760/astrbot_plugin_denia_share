# 达妮娅分享

AstrBot 链接分享自动解析插件：解析分享链接，渲染成分享卡片发送。

支持 **B站 / 抖音 / 快手 / 微博 / 小红书 / Twitter / AcFun / NGA** 八个平台。

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

## 与 yaya（astrbot_plugin_media_parser）的差异

娅娅版功能面很大（13 平台 + LLM 翻译 + 热评 + 归档 + 媒体中转 + 权限/限流），
本插件是有意做减法的精简版：

- ❌ **LLM 文本翻译**（10 语言 × 10 厂商接口）—— 与链接解析无关
- ❌ **视频仅发送封面** —— 边缘功能
- ❌ Cloudflare 网页截图兜底（20+ 配置项）
- ❌ B站 Cookie 定时监控与失效通知（保留扫码登录 + 持久化）
- ❌ 热评、ZIP 归档、媒体中转、权限白黑名单、频率限制
- ✅ 保留：8 平台解析、卡片渲染、OneBot 合并转发 / 其他平台直发、JSON 卡片（QQ 小程序）提取、B站扫码登录

配置项从娅娅版的几十个压到 **14 项**（上游 rika 同期为 40+ 项且仍在增加）。

## 配置项

WebUI 里只有 4 组，常用在前、折腾在后。

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
| `PROXY` | 空 | 媒体下载代理，如 `http://127.0.0.1:7897` |
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
| 解析层（`core/parsers/`） | ✅ 8 平台（3 个为合并改写） |
| 分享卡片渲染（`core/render.py`） | ✅ 来自 rika |
| 下载器（`core/download.py`） | ✅ 来自 rika，加了体积上限与代理 |
| 配置（`_conf_schema.json`） | ✅ 5 组 15 项 |

## 命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
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
