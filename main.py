"""达妮娅分享 - 链接分享自动解析插件。

解析核心移植自 astrbot_plugin_rika_share（MIT License），
底层基于 nonebot-plugin-parser 的解析思路。

当前为仓库骨架：插件可正常加载，解析层与卡片渲染尚未迁入。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import __version__

PLUGIN_ID = "astrbot_plugin_denia_share"


@register(
    PLUGIN_ID,
    "xiaoxi2760",
    "达妮娅分享 - 链接分享自动解析，渲染为分享卡片",
    __version__,
)
class DeniaSharePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.parsers: dict[str, object] = {}
        logger.info(f"[达妮娅分享] v{__version__} 已加载，解析层待迁移")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_status")
    async def denia_status(self, event: AstrMessageEvent):
        """查看达妮娅分享的运行状态"""
        platforms = "、".join(self.parsers) if self.parsers else "（尚未注册）"
        yield event.plain_result(
            f"达妮娅分享 v{__version__}\n"
            f"已注册平台：{platforms}"
        )

    async def terminate(self):
        logger.info("[达妮娅分享] 已卸载")
