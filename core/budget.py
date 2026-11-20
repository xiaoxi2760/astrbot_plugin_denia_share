"""单项媒体的绝对截止时间，跨取流、下载和重试传递。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypeVar

from astrbot.api import logger

from .exception import DownloadTotalTimeout


T = TypeVar("T")
download_deadline: ContextVar[float | None] = ContextVar("denia_download_deadline", default=None)


def get_download_deadline() -> float:
    deadline = download_deadline.get()
    if deadline is not None:
        return deadline

    from .config import get_config

    try:
        config = get_config()
    except RuntimeError:
        config = None
    seconds = getattr(config, "DOWNLOAD_MEDIA_TIMEOUT_SECONDS", 180)
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
