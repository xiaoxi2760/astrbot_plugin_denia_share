# 达妮娅分享

AstrBot 链接分享自动解析插件：解析分享链接，渲染成分享卡片发送。

> 当前处于**仓库骨架阶段**，解析层尚未迁入。

## 与 rika_share 的关系

解析核心移植自 [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share)（MIT License），
后者移植自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)。

## 开发状态

| 模块 | 状态 |
| --- | --- |
| 仓库骨架 / 插件注册 | ✅ 已完成 |
| 解析层（`core/parsers/`） | ⏳ 待从 rika_share 迁入 |
| 分享卡片渲染（`core/render.py`） | ⏳ 待迁入 |
| 下载器（`core/download.py`） | ⏳ 待迁入 |
| 配置与依赖清单 | ⏳ 待精简确认 |

## 安装

1. 将 `astrbot_plugin_denia_share` 放入 AstrBot 的 `data/plugins/` 目录
2. 安装依赖：`pip install -r requirements.txt`
3. 重启 AstrBot，在 WebUI 插件管理中启用

## 许可

MIT License。详见 [LICENSE](LICENSE)。

本项目包含移植自 `astrbot_plugin_rika_share`（MIT）的代码，保留其原始版权声明。
