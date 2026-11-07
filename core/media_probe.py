"""媒体描述与有预算探测 —— 确认下载两路实现共用的**公开契约**。

本模块是「先发卡片、确认后下载」流程里唯一的元数据入口：解析器只负责把
可选的 ``info_loader`` 挂到 :class:`~core.data.MediaContent` 的专门字段上，
控制器只依赖这里导出的函数，**不窥探 PathTask 闭包**。

公开接口
--------
- :func:`describe_result` —— 聚合整条解析结果（contents / graphics / repost）
  的媒体描述，去重、有总等待预算。
- :func:`probe_url` —— 对单个 URL 做**只读响应头**的有预算 HEAD 探测，
  不读取媒体正文。

字典契约（每条媒体 / 整条结果共用同一形状）
--------------------------------------------
``{"quality": str, "size_bytes": int | None, "estimated": bool, "media_count": int}``

- ``quality``：实际清晰度/音质，拿不到就空串，**不把配置目标档当真值**；
- ``size_bytes``：整数或 ``None``。**未知必须是 ``None``，不能编 0**；
- ``estimated``：该大小是否为估算（如 B站 DASH 音视频合计）；
- ``media_count``：媒体数量。

聚合规则
--------
- 遍历 ``contents`` / ``graphics``（仅 ``ImageContent``，纯字符串跳过）/
  ``repost``（递归），同一来源只算一次（按 PathTask 与 loader 的身份去重）；
- 只有**全部**媒体的大小都已知才给合计，否则 ``None``（部分未知 = 总大小
  未知）；
- 单条描述失败 ⇒ 该条视为未知，**不吞外层取消**（``CancelledError`` 原样
  向上抛）；
- 全部 loader 的总等待有预算（:data:`DESCRIBE_TIMEOUT_SECONDS`），超时未完成
  的条目按未知处理并取消，已完成的照样参与聚合。

安全约束
--------
本模块**永不**把 URL / 请求头 / 凭据写进描述字典或 ``extra``；loader 只存在
:class:`~core.data.MediaContent` 的 ``info_loader`` 专门字段里。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from urllib.parse import urlsplit

#: 单次 URL HEAD 的等待预算（秒）。HEAD 只读响应头，不读媒体正文。
PROBE_TIMEOUT_SECONDS = 10.0

#: ``describe_result`` 聚合全部 info_loader 的总等待预算（秒）。
DESCRIBE_TIMEOUT_SECONDS = 20.0

#: ``describe_result`` 内同时运行的 loader 上限。
#:
#: 不能把 50 张图的 HEAD 一次全放出去：确认预览是在解析返回后紧接着做的，
#: 同一瞬间几十个 HEAD 会让平台侧限流、也会让总预算被队头拖死。4 与下载器
#: 的媒体并发闸门同量级，既够快又不会把用户带宽/平台配额打满。
DESCRIBE_CONCURRENCY = 4

#: 描述字典的公开键。
DESCRIPTION_KEYS: tuple[str, ...] = (
    "quality", "size_bytes", "estimated", "media_count",
)


def empty_description(media_count: int = 0) -> dict[str, Any]:
    """一条「全是未知」的描述：quality 空串、size ``None``（不能编 0）。

    媒体数量只做结构计数，未知也不并入这个字段。
    """
    return {
        "quality": "",
        "size_bytes": None,
        "estimated": False,
        "media_count": max(0, int(media_count)),
        "unavailable_reason": "",
    }


def _coerce_positive_int(value: Any) -> int | None:
    """把可能为字符串/浮点的计数收成非负整数；非法值一律 ``None``。

    这里**不能**用 ``int(value)`` 后直接比较：``int("abc")`` 会抛错，
    而早先的写法把它当成 ``None`` 后再执行 ``size <= 0``，结果变成
    ``TypeError: '<=' not supported between NoneType and int`` —— 一条脏
    loader 值就能让整段描述失败，连已经解析出的 quality 也一起丢掉。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _normalize(loader_result: Any) -> dict[str, Any] | None:
    """把 loader 的任意返回值收窄成内部规范字典；非法形状返回 ``None``。

    只认公开契约的键，其余字段一律丢弃（URL / 凭据即使误传也进不来）。
    ``size_bytes`` 必须是正整数，0 / 负数 / 非数字一律当未知（不能编 0）。
    **大小解析失败不丢质量**：质量与大小是两条独立的证据，大小是脏值只代表
    大小未知。
    """
    if not isinstance(loader_result, dict):
        return None
    size = _coerce_positive_int(loader_result.get("size_bytes"))
    if size == 0:
        size = None
    count = _coerce_positive_int(loader_result.get("media_count"))
    return {
        "quality": str(loader_result.get("quality") or "").strip(),
        "size_bytes": size,
        "estimated": bool(loader_result.get("estimated")),
        "media_count": count or 0,
        "unavailable_reason": str(loader_result.get("unavailable_reason") or "").strip(),
    }


async def _run_loader(loader: Any, gate: asyncio.Semaphore | None = None) -> Any:
    """await 一条 loader：可调用对象（返回 dict / awaitable）或裸 awaitable。"""
    if gate is not None:
        async with gate:
            return await _run_loader(loader)
    if callable(loader):
        value = loader()
        if inspect.isawaitable(value):
            return await value
        return value
    return await loader


def _collect_media(result: Any) -> list[Any]:
    """收集 contents / graphics / repost 里的媒体条目，**按来源去重**。

    去重身份 = ``id(path_task)``：同一个 PathTask（同一份媒体、或同一张图被
    contents 与 graphics 各引用一次）只算一次。纯字符串的 graphics（NGA/微博
    正文那种）没有任务也没有描述，跳过。

    同一个 PathTask 被两个 MediaContent 引用、其中一个还带着 loader 时，保留
    带 loader 的那条（更具体的证据优先）。``MediaContent`` 在这里**函数内
    导入**：模块顶层导入会牵进 ``core.data → task/constants``，而 media_probe
    又是多个解析器/控制器的底层依赖，保持导入链最短更稳。
    """
    from .data import MediaContent

    by_task: dict[int, Any] = {}
    visited: set[int] = set()

    def add(cont: Any) -> None:
        key = id(cont.path_task)
        previous = by_task.get(key)
        if previous is None:
            by_task[key] = cont
        elif (
            getattr(previous, "info_loader", None) is None
            and getattr(cont, "info_loader", None) is not None
        ):
            by_task[key] = cont

    def visit(node: Any) -> None:
        if node is None:
            return
        # 缓存/解析器可能把 repost 指回自己；不设访问集会无限递归。
        marker = id(node)
        if marker in visited:
            return
        visited.add(marker)
        for cont in getattr(node, "contents", None) or []:
            if isinstance(cont, MediaContent):
                add(cont)
        for gra in getattr(node, "graphics", None) or []:
            if isinstance(gra, MediaContent):
                add(gra)
        visit(getattr(node, "repost", None))

    visit(result)
    return list(by_task.values())


async def describe_result(
    result: Any,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """聚合一条解析结果的媒体描述（确认下载预览用）。

    遍历 ``contents`` / ``graphics`` / ``repost``（递归、去重）里的
    ``MediaContent.info_loader``，串行/并发地 await 它们，最后聚合出：

    - ``quality``：按遍历顺序收集的**非空**质量去重后以 ``" / "`` 连接；
      **只要还有媒体质量未知，就追加「未知」** —— 不能把「部分已知」显示成
      一个统一规格，让人误以为全部媒体都是那一档；
    - ``size_bytes``：**全部**媒体大小已知才给合计，否则 ``None``；
    - ``estimated``：任一 loader 报了估算，整条结果就算估算；
    - ``media_count``：去重后的媒体条目数（结构计数，不依赖 loader 存在）；
    - ``unavailable_reason``（可选项，默认空串）：任一 loader 明确上报的
      平台侧不可下载原因，按出现顺序去重后用 ``"；"`` 连接。只在平台业务
      明确无权限/不可下载时才有值；网络 / HEAD 失败不会产生它。

    单条 loader 失败（抛普通 ``Exception``）⇒ 该条未知，不影响其它条目；
    **外层取消（``CancelledError``）原样向上抛，绝不吞**。总等待受
    ``timeout``（缺省 :data:`DESCRIBE_TIMEOUT_SECONDS`）限制，超时未完成的
    条目取消并按未知处理。同时运行的 loader 数受
    :data:`DESCRIBE_CONCURRENCY` 限制，避免整页图集同时 HEAD。
    """
    media = _collect_media(result)
    if not media:
        return empty_description(0)

    # 每个条目一个结果槽（与 media 同序）；不能按 id(loader) 归并 —— 多个
    # 媒体共用同一个可调用对象时会把它们的结果互相覆盖。
    results: list[dict[str, Any] | None] = [None] * len(media)
    entries = [
        (index, m)
        for index, m in enumerate(media)
        if getattr(m, "info_loader", None) is not None
    ]
    if entries:
        gate = asyncio.Semaphore(DESCRIBE_CONCURRENCY)
        paired = [
            (asyncio.ensure_future(_run_loader(m.info_loader, gate)), index)
            for index, m in entries
        ]
        tasks = [task for task, _ in paired]
        budget = DESCRIBE_TIMEOUT_SECONDS if timeout is None else float(timeout)
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=budget, return_when=asyncio.ALL_COMPLETED,
            )
        except asyncio.CancelledError:
            # 外层取消必须原样传播：先收尾各 loader 任务，避免泄漏。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task, index in paired:
            if task in done:
                try:
                    results[index] = _normalize(task.result())
                except asyncio.CancelledError:  # pragma: no cover —— 防御
                    results[index] = None
                except Exception:
                    # 单条描述失败 ⇒ 该条未知；不吞外层取消（取消已在上方处理）
                    results[index] = None
            else:
                # 超时未完成：取消并按未知处理
                results[index] = None
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return _aggregate(media, results)


def _aggregate(
    media: list[Any],
    results: list[dict[str, Any] | None],
) -> dict[str, Any]:
    total = 0
    all_known = True
    any_size = False
    estimated = False
    qualities: list[str] = []
    any_quality_unknown = False
    reasons: list[str] = []
    for index, item in enumerate(media):
        loader = getattr(item, "info_loader", None)
        output = results[index] if loader is not None else None
        if output is None:
            # 没有 loader 或描述失败/超时：这条的大小未知 ⇒ 总大小未知；
            # 质量同样属于未知，不能只展示其他条目的已知档位。
            all_known = False
            any_quality_unknown = True
            continue
        size = output.get("size_bytes")
        if size is None:
            all_known = False
        else:
            total += size
            any_size = True
        estimated = estimated or bool(output.get("estimated"))
        quality = output.get("quality") or ""
        if quality:
            if quality not in qualities:
                qualities.append(quality)
        else:
            any_quality_unknown = True
        reason = output.get("unavailable_reason") or ""
        if reason and reason not in reasons:
            reasons.append(reason)
    if not qualities:
        quality_text = ""
    elif any_quality_unknown:
        quality_text = " / ".join([*qualities, "未知"])
    else:
        quality_text = " / ".join(qualities)
    return {
        "quality": quality_text,
        "size_bytes": total if (any_size and all_known) else None,
        "estimated": estimated,
        "media_count": len(media),
        "unavailable_reason": "；".join(reasons),
    }


async def probe_url(
    downloader: Any,
    url: str,
    headers: dict[str, str] | None = None,
    quality: str = "",
) -> dict[str, Any]:
    """对单个 URL 做**有预算的 HEAD** 探测，只读响应头，绝不读取媒体正文。

    返回值与 :func:`describe_result` 同一形状（``media_count=1``）：

    - ``quality``：透传调用方提供（且**确系实际档位**）的 ``quality``，
      空串保持空串；本函数自己不会猜任何画质；
    - ``size_bytes``：HEAD 响应 ``Content-Length`` 为正整数、且确认是
      最终媒体文件本身时才已知。以下情况一律 ``None``（不是 0）：
      缺失 / chunked / 非 2xx / 超时 / 网络失败 / **HLS(m3u8) 清单** /
      **Content-Encoding 压缩后的长度** / 分段响应（206）；
    - ``estimated``：Content-Length 是所下文件的精确大小，恒 ``False``；
    - ``media_count``：1。

    HLS 的 m3u8 是播放清单，``Content-Length`` 只是清单文本的大小，不是
    最终视频体积；压缩响应（gzip/br/…）的 Content-Length 同理是压缩后
    长度。这两种都必须报未知，不能拿去和单文件上限比较。

    安全：不把 ``url`` / ``headers`` / 凭据放进任何返回值。
    """
    desc = empty_description(1)
    if quality:
        desc["quality"] = str(quality).strip()
    client = getattr(downloader, "client", None)
    if client is None or not callable(getattr(client, "head", None)):
        return desc
    try:
        response = await asyncio.wait_for(
            client.head(url, headers=headers or {}, follow_redirects=True),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return desc
    except asyncio.CancelledError:
        # 外层取消必须原样传播
        raise
    except Exception:
        return desc

    def _header(name: str) -> str:
        try:
            value = response.headers.get(name)
        except Exception:  # pragma: no cover —— 异常响应头（桩/畸形）
            return ""
        return str(value or "").strip().lower()

    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    # 非 2xx 不可能是「要下载的完整媒体」；206 的 Content-Length 只是本次
    # Range 的长度，也不是最终文件大小。
    if not (200 <= status < 300) or status == 206:
        return desc

    content_type = _header("content-type")
    content_encoding = _header("content-encoding")
    transfer_encoding = _header("transfer-encoding")
    content_range = _header("content-range")
    final_url = ""
    try:
        final_url = str(getattr(response, "url", "") or "")
    except Exception:  # pragma: no cover —— 异常响应对象
        final_url = ""

    def _looks_like_hls(candidate: str) -> bool:
        if not candidate:
            return False
        try:
            path = urlsplit(candidate).path.lower()
        except (TypeError, ValueError):
            return False
        return path.endswith(".m3u8") or ".m3u8" in path

    # m3u8 清单的 Content-Length 是清单文本大小，不是视频体积。
    if (
        _looks_like_hls(url)
        or _looks_like_hls(final_url)
        or "mpegurl" in content_type
        or "m3u8" in content_type
    ):
        return desc
    # 压缩后长度不是解压后的媒体大小，不能当精确值。
    if content_encoding and content_encoding not in {"identity"}:
        return desc
    # chunked 明确没有可信总长度；Content-Range 表示这是分段响应。
    if "chunked" in transfer_encoding or content_range:
        return desc

    raw_length = None
    try:
        raw_length = response.headers.get("content-length")
    except Exception:  # pragma: no cover —— 异常响应头（桩/畸形）
        raw_length = None
    if raw_length is None or str(raw_length).strip() == "":
        return desc
    try:
        length = int(str(raw_length).strip())
    except (TypeError, ValueError):
        return desc
    if length > 0:
        desc["size_bytes"] = length
        desc["estimated"] = False
    return desc