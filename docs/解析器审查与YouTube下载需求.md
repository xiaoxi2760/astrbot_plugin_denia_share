# 解析器审查与 YouTube 视频下载需求

记录日期为 2026 年 11 月 20 日。

## YouTube 社区帖子验收

社区图文按帖子 ID 解析。分享入口使用 `youtube.com/post/<帖子 ID>`，旧分享链接使用频道 `community?lb=<帖子 ID>`。插件统一返回 `/post/` 原链接，沿用 YouTube 的分平台代理、输出模式和同会话去重设置。两种写法指向同一帖子时，共用去重键。

| 场景 | 预期行为 |
| --- | --- |
| 公开的纯文字帖子 | 文本和卡片展示频道、正文及原链接，正文保留换行。 |
| 公开单图或多图帖子 | 卡片显示帖子图片，开启富媒体后按原顺序发送图片。无正文的帖子也可解析。 |
| 帖内包含视频预览、头像或投票缩略图 | 可返回帖文正文，图片附件仅使用帖子上传的图。 |
| 已删除、限制访问或找不到匹配 ID | 网页解析返回公开预览失败原因，聊天按「解析失败时发送错误提示」设置反馈。 |
| 普通 watch、短链和 Shorts | 沿用已有 oEmbed 预览和按需有声视频下载。 |

YouTube 帮助记录了纯文字、图片和投票等帖子类型，以及单帖最多十张图的限制。帖子详情的公开页面数据结构可对照 yt-dlp 维护者在问题记录中给出的 `backstagePostRenderer`。 [YouTube 帮助](https://support.google.com/youtube/answer/7124474) · [yt-dlp 帖子数据样本](https://github.com/yt-dlp/yt-dlp/issues/15664)

本地 YouTube 专项回归共通过 48 项。使用三个公开帖子的真实页面和图片响应生成了 WebUI 卡片，单图与十图分别解码并构建了 1 个和 10 个 AstrBot 图片组件。测试时系统代理有间歇性 TLS 中断，图文验收通过带重试的测试传输完成。真实 QQ 群和 Telegram 会话的收件效果待部署验证。样本为 [NASA 纯文字帖](https://www.youtube.com/post/Ugkx0X33g-nAj6FyciU8R9pJUCrPERrtPBJE)、[NASA 单图帖](https://www.youtube.com/post/Ugkxvw3nKUJFYnzgAx5YBGKZ5wuaHdkelGlO)和 [MrBeast 十图帖](https://www.youtube.com/post/Ugkx5QH9-Xr0EWbVJ1riARjVrDcXRN1LfJS7)。

## YouTube 视频下载实现与验收

开启富媒体后，解析器把视频登记为惰性下载任务，由现有发送链路在需要视频时执行。yt-dlp 只负责获取公开播放格式。单文件交给现有下载器，分离的 AVC 视频和 AAC 音频交给现有 ffmpeg 合并流程，最后通过 AstrBot 视频组件投递。仅发文本或卡片时不下载视频本体。

### 验收场景

| 场景 | 预期行为 |
| --- | --- |
| 开启富媒体输出，发送一条在时长和大小限制以内的公开点播视频 | 插件下载视频，聊天端收到可播放、有原声的视频附件。用普通链接和 Shorts 各验证一次。 |
| 同一视频分别使用 watch 链接和 youtu.be 短链 | 沿用现有视频 ID 去重，在已启用的去重窗口内避免重复下载和投递。 |
| 只开启文本或卡片 | 返回所选内容，跳过视频文件下载。 |
| 视频超过配置限制 | 遵守现有时长和大小限制，给出跳过原因。默认值为 480 秒和 100 MB，以部署配置为准。 |
| 私密、已删除的视频，或下载中断 | 明确反馈原因或失败阶段，避免把缩略图计为视频下载成功。 |

已从实际 YouTube 地址下载普通点播《Me at the zoo》（629,172 字节）及 NASA Shorts《Become a NASA Astronaut!》（19,644,792 字节），两者均生成本地有声 MP4，ffmpeg 完整解码音视频后，插件构建了 AstrBot 视频消息组件。本地样片分别验证单文件、双轨合并和 Windows 大小写敏感视频 ID 的缓存隔离。正在直播的内容不录制。真实聊天账号的上传、收取和客户端播放仍待部署实测。

## YouTube 下载实现参考

已核对 drdon1234 的流媒体聚合解析器 v1.13.3，固定提交为 `0ec4ad6`。以下为实现前的源码审查记录。

- **获取媒体**。它从播放器响应读取视频时长和短时有效的媒体地址。可用格式分为带声音的单文件、纯视频和纯音频。两种单轨都可用时，它把配对的音视频排在首位，随后最多保留四个自带声音的单文件候选。只有纯视频轨时无法满足有声附件的要求。[YouTube 解析器](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/0ec4ad6f67a9b4618f8effd995ca74023dddaa22/core/parser/platform/youtube.py)
- **下载与合并**。下载路由把双轨任务交给 DASH 处理器，后者分别下载两轨，再调用 ffmpeg 合并，并处理超时和取消。希望解析器已有 `StreamDownloader.download_video` 和 `download_av_and_merge`，可以独立实现 YouTube 选流并沿用现有下载、缓存和投递路径。参考仓库标注 AGPL-3.0，本次仅记录设计和来源。[下载路由](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/0ec4ad6f67a9b4618f8effd995ca74023dddaa22/core/downloader/router.py) · [DASH 处理器](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/0ec4ad6f67a9b4618f8effd995ca74023dddaa22/core/downloader/handler/dash.py) · [上游许可](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/0ec4ad6f67a9b4618f8effd995ca74023dddaa22/LICENSE)
- **适配现有设置**。保留 oEmbed 作为卡片预览入口，富媒体发送时再获取播放地址，避免缓存中的临时直链过期。选流应计入两轨合计大小；遇到超限或缺少 ffmpeg，优先尝试限额内的有声单文件。验收时检查附件确实包含音轨和视频轨，并在聊天客户端播放。
- **X 的交叉对照**。上游同样按帖文 ID 请求 FxTwitter。本插件已有这条取数路径，之前复现的网页截图问题出在链接识别阶段，可分别测试链接识别和服务端取数。[X 解析器](https://github.com/drdon1234/astrbot_plugin_media_parser/blob/0ec4ad6f67a9b4618f8effd995ca74023dddaa22/core/parser/platform/twitter.py)

## 本轮已验证

- X 的移动分享路径 `/i/web/status/编号` 在旧规则下会漏匹配，开启截图兜底时可复现网页截图。修复后，聊天和 WebUI 共用帖文匹配规则，媒体接口失败时尝试官方 oEmbed 正文。实际解析了公开帖文 20，并用模拟响应验证了两个镜像失效后的正文回退。
- YouTube 已接入聊天、WebUI、平台停用和分平台代理。19 项回归测试通过，实际解析了《Me at the zoo》，另一条公开样例验证了缩略图为 JPEG。此前测试在本机进行，真实 QQ 群投递仍待验证。

## 上游核查

| 来源 | 本轮发现 |
| --- | --- |
| [莉卡解析 v3.0.1 至 v3.4.0 源码对比](https://github.com/iris1598/astrbot_plugin_rika_share/compare/4cc965f...b5726d671915c8536c34347db1cdf5b92cbb6749) | 两版 X 适配器相同。B 站新增按标题搜索，小红书调整页面数据提取及图片地址回退。后续优先对照小红书。 |
| [MusicUID](https://github.com/Xbaiyz12/MusicUID/commits/main/) | 11 月更新涉及 QQ 音乐会员账号处理、音乐卡片解析和签名卡片发送。与本插件直接相关的是账号状态和取流条件。 |
| [F2 抖音签名](https://github.com/Johnserf-Seed/f2/commits/master/f2/utils/abogus.py) | 查到的签名文件最近修改在 2025 年 4 月。 |
| [heybox-url](https://github.com/huandu/heybox-url/commits/master/) 与 [RSSHub 小黑盒模块](https://github.com/DIYgod/RSSHub/commits/master/lib/routes/xiaoheihe/util.ts) | 本轮未发现需要同步的近期签名算法变更。 |
| [B 站依赖发布记录](https://pypi.org/project/bilibili-api-python/17.4.2/) | 核查时 PyPI 最新版本为 17.4.2，上游 GitHub 仓库已归档。本插件声明的下限为 16.0.0，部署实例的实际版本需另行核对。 |
