"""惰性卡片附加信息加载（B站在线人数 / AI 摘要）。

设计约束（对应产品要求）：
- 解析阶段**不**发请求；只有真正要渲染卡片时才按需加载。
- 同一个 ``ParseResult`` 上每类信息**只取一次**：先只取 online，
  之后再打开 AI 开关能补取 AI，但不会重新取 online。
- 并发渲染同一结果时合并到同一批 task，不重复请求。
- 总等待上限默认 5 秒；超时或调用方取消都会取消子任务并等待收尾，
  不留下后台悬空 task。
- 失败绝不抛出到调用方：只写入空串 + 状态，不阻断卡片。

``extra`` 约定（不复用 ``extra['info']``，别的平台在那里放价格等）：
- ``extra['online']``: str
- ``extra['ai_summary']``: str，仅有效正文
- ``extra['ai_summary_status']``: str，ok / no_credential / unavailable / error / timeout
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

#: 单个结果上 online + AI 的总等待上限（秒）。
DEFAULT_TIMEOUT = 5.0


class CardInfoLoader:
    """挂在 ``ParseResult.card_info_loader`` 上的惰性加载器。"""

    def __init__(
        self,
        result: Any,
        *,
        fetch_online: Callable[[], Awaitable[str]] | None = None,
        fetch_ai: Callable[[], Awaitable[tuple[str, str]]] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._result = result
        self._timeout = max(0.0, float(timeout))
        self._fetchers: dict[str, Callable[[], Awaitable[Any]]] = {}
        if fetch_online is not None:
            self._fetchers["online"] = fetch_online
        if fetch_ai is not None:
            self._fetchers["ai"] = fetch_ai
        self._tasks: dict[str, "asyncio.Future[Any]"] = {}

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    async def load(self, include_ai: bool = False) -> None:
        """按需加载；``include_ai=False`` 时只保证 online（若配置了）。

        本方法永不抛出业务异常。调用方被取消时子任务会先取消收尾，
        再把 ``CancelledError`` 原样传播。
        """
        names = ["online"]
        if include_ai:
            names.append("ai")
        tasks = [self._task(name) for name in names if name in self._fetchers]
        if not tasks:
            return

        try:
            _done, pending = await asyncio.wait(tasks, timeout=self._timeout)
        except asyncio.CancelledError:
            self._cancel(tasks)
            await self._drain(tasks)
            raise

        if pending:
            # 达到总等待上限：取消剩余子任务，收尾后标记超时。
            self._cancel(pending)
            await self._drain(pending)
            for name, task in self._tasks.items():
                if task in pending:
                    self._store_timeout(name)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _task(self, name: str) -> "asyncio.Future[Any]":
        task = self._tasks.get(name)
        if task is None:
            task = asyncio.ensure_future(self._run(name))
            self._tasks[name] = task
        return task

    async def _run(self, name: str) -> None:
        try:
            value = await self._fetchers[name]()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 失败不阻断卡片：online 退化为空串，AI 记为 error。
            value = None
        self._store(name, value)

    def _store(self, name: str, value: Any) -> None:
        extra = self._result.extra
        if name == "online":
            extra["online"] = value if isinstance(value, str) else ""
            return
        if isinstance(value, tuple) and len(value) == 2:
            text, status = value
        elif isinstance(value, str):
            text, status = value, ("ok" if value else "unavailable")
        else:
            text, status = "", "error"
        text = text or ""
        extra["ai_summary"] = text
        extra["ai_summary_status"] = status or ("ok" if text else "unavailable")

    def _store_timeout(self, name: str) -> None:
        extra = self._result.extra
        if name == "online":
            extra["online"] = ""
            return
        if not extra.get("ai_summary_status"):
            extra["ai_summary"] = ""
            extra["ai_summary_status"] = "timeout"

    @staticmethod
    def _cancel(tasks: list["asyncio.Future[Any]"]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()

    @staticmethod
    async def _drain(tasks: list["asyncio.Future[Any]"]) -> None:
        """等待被取消/超时的子任务真正收尾，避免后台悬空 task。"""
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
