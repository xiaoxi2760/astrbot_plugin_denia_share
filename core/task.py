# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""异步路径包装 - 用于延迟获取下载结果"""

import asyncio
from pathlib import Path
from collections.abc import Callable, Coroutine
from typing import Any

from astrbot.api import logger


class PathTask:
    __slots__ = ("_path", "_task")

    def __init__(
        self,
        task: asyncio.Task[Path] | Coroutine[Any, Any, Path],
    ):
        if isinstance(task, asyncio.Task):
            self._task: asyncio.Task[Path] = task
        else:
            self._task = asyncio.create_task(task)
        self._path: Path | None = None

    async def get(self) -> Path:
        if self._path is not None:
            return self._path
        self._path = await self._task
        return self._path

    async def safe_get(
        self,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Path | None:
        try:
            return await self.get()
        except Exception as e:
            from .config import get_config
            if get_config().DEBUG_LOG_ENABLED:
                logger.exception(f"PathTask 获取失败 | task={self._task.get_name()}")
            if on_error is not None:
                on_error(e)
            return None

    @property
    def resolved(self) -> Path | None:
        """已完成的本地路径；未完成时返回 None（不阻塞、不触发下载）。

        供「解析记录」等旁路功能使用：看一眼已经落盘的结果，
        不需要为了记录文件名而把没下载的媒体也拖下来。
        """
        return self._path

    async def get_uri(self) -> str | None:
        """获取已下载文件的 file:// URI；下载失败返回 None。

        注意是 async 方法而非 property：属性访问无法 await，
        写成 property 会诱导调用方拿到未执行的协程对象。
        """
        path = await self.safe_get()
        return path.as_uri() if path else None

    def __repr__(self) -> str:
        if self._path is not None:
            return f"PathTask(path={self._path.name})"
        else:
            return f"PathTask(task={self._task.get_name()}, done={self._task.done()})"
