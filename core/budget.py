"""单项媒体的绝对截止时间，跨取流、下载和重试传递。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

from astrbot.api import logger

from .exception import DownloadTotalTimeout, ShareDownloadLimitExceeded


T = TypeVar("T")
download_deadline: ContextVar[float | None] = ContextVar("denia_download_deadline", default=None)


@dataclass
class _ShareBudget:
    limit: int
    remaining: int


_share_budget: ContextVar[_ShareBudget | None] = ContextVar("denia_share_budget", default=None)


@contextmanager
def share_download_budget(maximum_mb: int):
    if _share_budget.get() is not None or not maximum_mb:
        yield
        return
    limit = maximum_mb * 1024 * 1024
    token = _share_budget.set(_ShareBudget(limit, limit))
    try:
        yield
    finally:
        _share_budget.reset(token)


def check_share_download_budget(size: int = 1) -> None:
    budget = _share_budget.get()
    if budget is not None and size > budget.remaining:
        raise ShareDownloadLimitExceeded(budget.limit)


def consume_share_download_bytes(size: int) -> None:
    budget = _share_budget.get()
    if budget is not None:
        remaining = budget.remaining
        budget.remaining = max(0, remaining - size)
        if size > remaining:
            raise ShareDownloadLimitExceeded(budget.limit)


def get_download_deadline() -> float:
    deadline = download_deadline.get()
    if deadline is not None:
        return deadline

    from .config import get_config

    try:
        config = get_config()
    except RuntimeError:
        config = None
    seconds = getattr(config, "DOWNLOAD_MEDIA_TIMEOUT_SECONDS", 120)
    return asyncio.get_running_loop().time() + seconds


def check_download_deadline() -> None:
    deadline = download_deadline.get()
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise DownloadTotalTimeout()


async def run_download(
    factory: Callable[[], Awaitable[T]], deadline: float, *, timer: asyncio.Timeout | None = None,
) -> T:
    outermost = download_deadline.get() is None
    token = download_deadline.set(deadline)
    if timer is None:
        timer = asyncio.timeout_at(deadline)
    try:
        try:
            async with timer:
                check_download_deadline()
                result = await factory()
                if timer.expired():
                    raise DownloadTotalTimeout()
                return result
        except TimeoutError:
            if not timer.expired():
                raise
            raise DownloadTotalTimeout() from None
    except DownloadTotalTimeout as exc:
        if outermost and not exc.logged:
            exc.logged = True
            logger.warning("[denia_share] 媒体下载超时 | reason=media_timeout")
        raise
    finally:
        download_deadline.reset(token)
