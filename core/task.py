# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License），
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""异步路径包装 - 用于延迟获取下载结果"""

import asyncio
from pathlib import Path
from collections.abc import Callable, Coroutine
from typing import Any

from astrbot.api import logger


#: 惰性工厂的返回值类型：协程、已调度的 Task，或直接给出的路径。
_LazySource = Coroutine[Any, Any, Path] | asyncio.Task[Path] | Path

#: 临时失败时**最多重建几次**下载（不含首次，即最多共 3 次尝试）。
#:
#: 必须是有限次数：重试是为了盖住「一次网络抖动」，不是常驻重试循环。
MAX_TRANSIENT_ATTEMPTS = 2


class PathTask:
    """包装一次「拿到本地路径」的动作。

    两种构造方式，语义完全不同：

    1. **急**（``PathTask(coro_or_task)``，历史行为）—— 传入协程时立刻
       ``create_task``，也就是**构造那一刻就开始下载**，与后续是否真的会用到
       它无关。外部自定义解析器普遍走这条路，必须保留。
    2. **惰**（``PathTask.lazy(factory)``）—— 只记住一个工厂，直到第一次
       ``get()`` / ``safe_get()`` 才真正创建任务。从没被 get 过就**一次字节都
       不会下载**。这是「仅文本 / 仅卡片」两种模式不产生多余下载的前提：
       ``__init__`` 里就 ``create_task`` 的话，跳过发送端完全拦不住已经在跑
       的下载（``_deliver`` 之前的解析阶段就已经把流量发出去了）。

    ``needs_media=True`` 标记的是「**为了拿到它必须先下载本体**」的派生任务
    （典型是用 ffmpeg 从视频抽第一帧当封面）。仅卡片模式要跳过它们：为了一个
    封面去下整段视频不划算，宁可让卡片用占位背景。
    """

    __slots__ = (
        "_path", "_task", "_factory", "_needs_media", "_started", "_lock", "_attempts",
    )

    def __init__(
        self,
        task: "asyncio.Task[Path] | Coroutine[Any, Any, Path] | Callable[[], _LazySource] | Path",
    ):
        # **可调用对象按惰性工厂处理**。B站主力与备用解析已经用
        # ``PathTask(lambda: download_video())`` 这个写法传下载工厂；若把 callable
        # 当协程丢给 create_task 会直接 TypeError（create_task 只收 coroutine），
        # 而老的 Task/协程仍保持急式。
        if callable(task) and not isinstance(task, asyncio.Task):
            self._factory = task
            self._task = None
            self._path = None
            self._needs_media = False
            self._started = False
            self._lock = None
            self._attempts = 0
            return
        if isinstance(task, Path):
            self._task = None
            self._factory = None
            self._path = task
            self._needs_media = False
            self._started = True
            self._lock = None
            self._attempts = 0
            return
        if isinstance(task, asyncio.Task):
            self._task: asyncio.Task[Path] | None = task
        else:
            self._task = asyncio.create_task(task)
        self._factory = None
        self._path = None
        self._needs_media = False
        self._started = True
        self._lock = None
        self._attempts = 0

    @classmethod
    def lazy(
        cls,
        factory: Callable[[], _LazySource],
        *,
        needs_media: bool = False,
    ) -> "PathTask":
        """构造一个**惰性**路径任务：``factory`` 到第一次 ``get()`` 才被调用。

        Args:
            factory: 无参可调用，返回协程 / Task / Path。**不要**在这里就把
                下载协程创建出来 —— 那样等于急式，等于没做惰化。
            needs_media: 该任务是否必须先下载本体才能完成（见类文档）。
        """
        self = cls.__new__(cls)
        self._task = None
        self._factory = factory
        self._path = None
        self._needs_media = needs_media
        self._started = False
        self._lock = None
        self._attempts = 0
        return self

    def _ensure_started(self) -> asyncio.Task[Path]:
        """确保底层任务已创建，返回它。**只在本事件循环内同步调用**。

        并发安全：``_lock`` 的创建与「检查-置位」之间没有任何 await 点，而
        asyncio 是单线程协作调度，因此两个并发 ``get()`` 不可能都走进创建分支
        —— 后到的那个一定在 await 锁之后看到已经建好的任务。
        """
        if self._task is not None:
            return self._task
        factory = self._factory
        if factory is None:  # pragma: no cover —— 防御：__init__/lazy 不会走到
            raise RuntimeError("PathTask 既没有预置任务也没有工厂")
        source = factory()
        if isinstance(source, Path):
            # 同步就能给出结果的（如示例图），无需包一层任务
            self._path = source
            self._started = True
            return _completed_task(source)
        if isinstance(source, asyncio.Task):
            self._task = source
        else:
            self._task = asyncio.create_task(source)
        self._started = True
        return self._task

    async def get(self) -> Path:
        if self._path is not None:
            return self._path
        while True:
            if self._task is None:
                # 惰性任务：并发 get() 只建一次任务、只 await 一次下载。
                # 直接 `await factory()` 会在并发下重复下载（每次调用都新建协程），
                # 所以必须先在锁内建好任务再解锁 await。
                if self._lock is None:
                    # 同步构造，无 await 点 ⇒ 不会与并发调用交错
                    self._lock = asyncio.Lock()
                async with self._lock:
                    task = self._ensure_started()
            else:
                task = self._task
            try:
                self._path = await asyncio.shield(task)
                return self._path
            except asyncio.CancelledError:
                # 取消必须原样传播：当成下载失败去重试会让外层取消语义丢失。
                raise
            except Exception as exc:
                if self._lock is None:
                    self._lock = asyncio.Lock()
                async with self._lock:
                    # 同一个失败可能唤醒多个等待者。只有仍持有当前任务的
                    # 等待者负责重建，其他等待者共享已经安排的下一次尝试。
                    if self._task is not task:
                        continue
                    if not self._should_retry(exc):
                        from .exception import IgnoreException
                        # 策略跳过保留结果；改限额时配置热更新会统一清缓存。
                        task._denia_failed = not isinstance(exc, IgnoreException)
                        raise
                    self._task = None
                    self._started = False
                    self._attempts += 1
                    logger.info(
                        f"[denia_share] 临时失败，重建下载任务"
                        f"（第 {self._attempts}/{MAX_TRANSIENT_ATTEMPTS} 次）| "
                        f"{type(exc).__name__}",
                    )

    @staticmethod
    def _is_transient(exc: Exception, _depth: int = 0) -> bool:
        """这次失败是不是「临时」的，值得再试一次。

        用**白名单**而不是「非永久即临时」：黑名单只能挡住已知类型，而下载链
        上抛出的异常五花八门（httpx、aiohttp、asyncio、第三方解析器），
        黑名单必然漏。反过来白名单漏掉的代价只是「这次没重试」—— 交给上层
        淘汰缓存重解析，用户体验略差但不会错；而黑名单漏掉的代价是「对着一
        个根本不会自愈的错误反复下载」。

        **永久失败绝不重试**：体积/时长超限（``IgnoreException`` 家族，含
        ``MediaSizeExceeded``）是策略跳过；内容已删除、鉴权失败同理。
        """
        from .exception import DownloadException, IgnoreException, ParseException

        # 下载器、平台 CDN 会保留原始原因；只沿明确 cause 解包，
        # 不把任意业务错误的上下文当成可重试故障。
        if isinstance(exc, DownloadException) and _depth < 8 and exc.__cause__ is not None:
            return PathTask._is_transient(exc.__cause__, _depth + 1)
        if isinstance(exc, (IgnoreException, ParseException)):
            return False
        # 网络类：TimeoutError / ConnectionError 在 Python 3 里是 OSError 子类，
        # 所以必须在 OSError 判定**之前**认，否则最该重试的反而一次都重试不到。
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        # httpx / aiohttp 的传输层故障（连接被重置、临时 DNS 失败）。
        try:
            import httpx

            if isinstance(exc, httpx.TransportError):
                return True
            if isinstance(exc, httpx.HTTPStatusError):
                return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
        except Exception:  # pragma: no cover —— httpx 不可用时按下面走
            pass
        try:
            import aiohttp

            if isinstance(exc, aiohttp.ClientResponseError):
                return exc.status == 429 or 500 <= exc.status < 600
            if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError)):
                return True
        except Exception:  # pragma: no cover
            pass
        try:
            from curl_cffi.requests import exceptions as curl_errors
            if isinstance(exc, (curl_errors.Timeout, curl_errors.ConnectionError)):
                return not isinstance(exc, curl_errors.CertificateVerifyError)
        except ImportError:  # pragma: no cover
            pass
        # 其余一律按永久处理：权限、磁盘满、只读文件系统、文件不存在，
        # 以及**业务性错误**（RuntimeError/ValueError 等）。这些再试一遍
        # 还是同样的错，只会白白多下载几次还拖慢用户拿到提示的时间。
        return False

    def _should_retry(self, exc: Exception) -> bool:
        """临时失败 + 有工厂 + 未超次数 ⇒ 可以重建一次。

        ``_factory is None`` 覆盖的是急式构造的**一次性**协程 / Task：它们只能
        await 一次，await 完就是终态，重建只会凭空造一个新协程（等于重复下载
        一次还可能下出半个文件）。这类只能把失败如实抛出去，由调用方淘汰缓存。
        """
        if self._factory is None:
            return False
        if self._attempts >= MAX_TRANSIENT_ATTEMPTS:
            return False
        return self._is_transient(exc)

    async def safe_get(
        self,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Path | None:
        try:
            return await self.get()
        except Exception as e:
            # 这里**不能**被 DEBUG_LOG_ENABLED 门控：取不到路径意味着最终产物会缺料，
            # 而缺料必须留痕。开关只用来决定要不要附堆栈（排障时才需要）。
            try:
                from .config import get_config
                verbose = get_config().DEBUG_LOG_ENABLED
            except Exception:
                verbose = False
            logger.warning(
                f"PathTask 获取失败 | task={self._describe()} | "
                f"{type(e).__name__}: {e}",
                exc_info=verbose,
            )
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

    @property
    def started(self) -> bool:
        """是否已经开始过下载/计算。

        「仅文本」「全关闭」模式下必须用它确认**一个字节都没下载**：
        ``resolved is None`` 只说明「还没落盘」，区分不出「正在下载」和
        「压根没启动」—— 而后者才是我们要保证的性质。
        """
        return self._started

    @property
    def needs_media(self) -> bool:
        """是否必须先下载本体才能完成（仅卡片模式据此跳过派生任务）。"""
        return self._needs_media

    @property
    def failed(self) -> bool:
        """这次取路径是否**已经彻底失败**（重试用尽或永久失败）。

        供解析结果缓存判断「这份结果里已经有下载不了的媒体」：留着它只会
        让后续同链接一直命中一份缺料的结果，而且**永远不会自己好起来**。
        """
        if self._path is not None:
            return False
        task = self._task
        if task is None:
            return False
        return bool(getattr(task, "_denia_failed", False))

    def _describe(self) -> str:
        if self._task is not None:
            try:
                return self._task.get_name()
            except Exception:  # pragma: no cover
                return "task"
        return "lazy" if self._factory is not None else "unknown"

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
        if self._task is not None:
            return f"PathTask(task={self._describe()}, done={self._task.done()})"
        return f"PathTask(lazy, started={self._started})"


def _completed_task(value: Path) -> asyncio.Task[Path]:
    """把一个已知的 ``Path`` 包成已完成的 Task，让调用方统一走 await 路径。

    只在协程内被调用（``get`` / ``safe_get`` 内部），因此一定拿得到运行中的
    事件循环 —— 工厂同步给出 ``Path`` 的场景（示例图）走这里。
    """

    async def _done() -> Path:
        return value

    return asyncio.create_task(_done())
