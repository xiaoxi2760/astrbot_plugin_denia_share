"""解析结果 / 渲染卡片的内存缓存（组合服务）。

从 ``main.py`` 抽出的职责：

- 两本字典的所有权与维护：``_result_cache``（写入时刻 + ParseResult）、
  ``_render_cache``（cache_key -> 卡片路径）；
- 条数上限（实时读配置，FIFO 兜底，TTL 过期判定在读取侧做）；
- 命中缓存前的「媒体文件还在不在」检查（``_cache_media_gone`` /
  ``_cache_media_failed``）—— 文件被 ``CACHE_TTL_HOURS`` 清掉后必须先淘汰
  旧结果，不能拿一个不存在的路径去发媒体；
- 结果条目与渲染条目**成对摘除**（``_drop_cache_entry`` / ``result_cache_key``）。

对外契约保持不变：插件实例仍暴露 ``_result_cache`` / ``_render_cache`` 两本字典
（指向本服务持有的同一对象），``_get_cached_result`` / ``_remember_result`` /
``_remember_render`` / ``result_cache_key`` 等仍是插件方法（薄委托），
``core/webui.py`` 与既有测试无需改指向。

不 import ``main``，避免循环依赖；稳定依赖 ``core/data`` / ``core/config``。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from astrbot.api import logger

from .config import get_config
from .data import ParseResult

# 解析结果内存缓存的条数上限（兜底常量；实际上限实时读配置，见 _result_cache_limit）。
# `_result_cache` 原先只在「清理循环 / 切换缓存目录 / 手动清空缓存」三处 clear，
# 而清理循环在 CACHE_TTL_HOURS=0 时根本不启动 —— 那种部署下它永不回收。
# 加个条数上限兜底，与 TTL 无关。
MAX_RESULT_CACHE_ENTRIES = 128

# 渲染索引（cache_key -> 卡片路径）的条数上限。
# `_result_cache` 有 TTL、有条数上限，而 `_render_cache` 原先两者都没有：
# 磁盘卡片按 CACHE_TTL_HOURS 清理时，渲染索引不会被回收（CACHE_TTL_HOURS=0
# 时清理循环根本不启动），于是处理过的每个不同链接都会在索引里留一条，
# 无界增长。它只是「哪张卡片图在磁盘哪个位置」的记忆，条数少了的代价只是
# 重新渲染一次，不会丢内容 —— 与磁盘保留策略分开管。
MAX_RENDER_CACHE_ENTRIES = 128


class ResultCache:
    """解析结果缓存 + 渲染索引的组合服务。

    实例同时持有两本字典；插件通过薄委托方法使用，字典对象本身以
    ``plugin._result_cache`` / ``plugin._render_cache`` 暴露给既有调用方。
    """

    def __init__(self) -> None:
        # 存 (写入时刻, 结果) —— 时刻是给「重复解析间隔」用的，见 _get_cached_result
        self._result_cache: dict[str, tuple[float, ParseResult]] = {}
        self._render_cache: dict[str, Path] = {}

    @staticmethod
    def result_cache_key(url: str) -> str:
        """解析结果缓存键：全量 URL 的短哈希。

        直接截断 URL 前 64 字符会让不同链接撞成同一个 key（小红书 / 微博的
        分享链常带长参数），撞上就会把 A 链接的解析结果当成 B 链接的返回。
        WebUI 的手动解析也必须用同一个键，否则同一链接在两条链路上各缓存一份。
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]

    def _result_cache_limit(self) -> int:
        """当前解析结果缓存条数上限；config 未初始化（旧测试桩）时回退 128。"""
        try:
            return max(1, int(get_config().MAX_RESULT_CACHE_ENTRIES))
        except Exception:
            return MAX_RESULT_CACHE_ENTRIES

    def _render_cache_limit(self) -> int:
        """当前渲染索引条数上限；config 未初始化（旧测试桩）时回退 128。"""
        try:
            return max(1, int(get_config().MAX_RENDER_CACHE_ENTRIES))
        except Exception:
            return MAX_RENDER_CACHE_ENTRIES

    def _trim_result_cache(self) -> None:
        """把 ``_result_cache`` 立即收敛到当前上限（FIFO，连带摘渲染索引）。

        配置把条数上限调小时调用：不动 TTL（过期判定仍在读取侧做），
        只是把超出新上限的旧条目当场淘汰，避免「改小了却不生效」。
        """
        cache = self._result_cache
        limit = self._result_cache_limit()
        while len(cache) > limit:
            evicted = next(iter(cache))
            cache.pop(evicted, None)
            # 结果被淘汰时**连带摘掉渲染索引**：留着的话，
            # ``renderer.render`` 会拿 existing 直接返回旧图，而那条图对应的
            # 结果已经不存在了，索引就成了无主条目。
            self._render_cache.pop(evicted, None)

    def _trim_render_cache(self) -> None:
        """把 ``_render_cache`` 立即收敛到当前上限（FIFO）。"""
        cache = self._render_cache
        limit = self._render_cache_limit()
        while len(cache) > limit:
            cache.pop(next(iter(cache)), None)

    def _remember_result(self, cache_key: str, result: ParseResult) -> None:
        """写入解析结果内存缓存，并维持条数上限（实时读 MAX_RESULT_CACHE_ENTRIES）。"""
        cache = self._result_cache
        cache[cache_key] = (time.monotonic(), result)
        # dict 保持插入序：超限就丢最早写入的（FIFO 够用，这里只是防无界增长）。
        # 上限实时读配置：调小后下一次写入就按新上限收敛，无需重启。
        limit = self._result_cache_limit()
        while len(cache) > limit:
            evicted = next(iter(cache))
            cache.pop(evicted, None)
            # 结果被淘汰时**连带摘掉渲染索引**：留着的话，
            # ``renderer.render`` 会拿 existing 直接返回旧图，而那条图对应的
            # 结果已经不存在了，索引就成了无主条目。
            self._render_cache.pop(evicted, None)

    def _remember_render(self, cache_key: str, path: Path) -> None:
        """登记渲染产物并维持索引条数上限（实时读 MAX_RENDER_CACHE_ENTRIES）。

        WebUI 的独立预览也走这里：那条入口**不经** ``_remember_result``，
        曾经绕过上限直接写 ``_render_cache``（只受结果缓存淘汰牵动）。
        """
        self._render_cache[cache_key] = path
        limit = self._render_cache_limit()
        while len(self._render_cache) > limit:
            self._render_cache.pop(next(iter(self._render_cache)), None)

    @staticmethod
    def _cache_media_gone(result: ParseResult) -> bool:
        """缓存里的结果，其媒体文件是否**已经被磁盘清理掉了**。

        为什么必须查：``PathTask`` 首次下载后把路径存进 ``_path``，之后再取
        一律直接返回，**不检查文件还在不在**；发送侧也不一定查
        ``path.exists()``。于是只要解析结果还在内存里、而磁盘文件已按
        ``CACHE_TTL_HOURS`` 被清掉，就会拿着一个不存在的路径去发媒体 ——
        卡片正常发出去、**视频/图片/封面静默消失**，日志里什么都没有。

        覆盖范围用 ``result._iter_media_tasks()``（视频 / 封面 / 音频 / 图片 /
        graphics / 转发），那是 ``core/data.py`` 里已有的媒体清单，收口在一处
        免得以后加了新内容类型这里漏掉。头像另算 —— 它只是装饰，但它同样会
        画在卡片上，文件没了会出现裂图。

        只查已解析出路径的（``resolved`` 非 None）：还在下载中的、或下载失败
        的不据此判定，那两种情况另有分支管。
        """
        tasks = list(result._iter_media_tasks())
        author = getattr(result, "author", None)
        avatar = getattr(author, "avatar", None) if author is not None else None
        if avatar is not None:
            tasks.append(("头像", avatar))

        for _label, task in tasks:
            path = task.resolved
            if path is not None and not path.exists():
                return True
        return False

    def _get_cached_result(self, cache_key: str) -> ParseResult | None:
        """取可用的缓存结果；不可用（过期 / 媒体已清理）时顺手删掉并返回 None。

        两种失效都**删掉条目**而不是留着等下次 —— 留着等于每次都白查一遍
        磁盘，而且会让「清空缓存」之外的路径继续返回一份发不出媒体的结果。
        """
        entry = self._result_cache.get(cache_key)
        if entry is None:
            return None
        stored_at, result = entry

        # 读侧兑底：手改过 JSON / 配置项是脏值时不能让整条链路跟着报错。
        # 用 int() 包一层 —— 裸 int("abc") 抛 ValueError，会被 _process_url 的
        # except Exception 吞成「处理出错」，而且异常发生在 pop 之前，
        # 坏条目永远摘不掉，那一个 key 从此每次都报错、无法自愈。
        try:
            ttl = int(get_config().RESULT_CACHE_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = 600
            logger.warning(
                "[denia_share] 重复解析间隔配置不是整数，按 600 秒处理",
            )
        if ttl <= 0:
            # 0 = 关掉这个优化，每次都重新解析。**不能写成「跳过过期检查」**：
            # 那样 TTL=0 反而变成永不过期，与配置项文案正好相反。
            self._drop_cache_entry(cache_key)
            return None
        if (time.monotonic() - stored_at) >= ttl:
            self._drop_cache_entry(cache_key)
            logger.debug(
                f"[denia_share] 解析结果已过重复解析间隔（{ttl}s），重新解析"
            )
            return None

        if self._cache_media_gone(result):
            self._drop_cache_entry(cache_key)
            logger.info(
                "[denia_share] 缓存里的媒体文件已被清理，重新解析一次（只是白费一次请求）"
            )
            return None

        # 轻量测试桩（只实现几个方法的替身）没有这个方法，getattr 兜一层，
        # 免得缓存读取在桩上直接 AttributeError。
        failed_check = getattr(self, "_cache_media_failed", None)
        if failed_check is not None and failed_check(result):
            # 缓存里这份结果已经**确定拿不到**媒体：留着它等于让后续每一次
            # 同链接都命中一份缺料的结果，而且它自己永远不会自愈。摘掉，
            # 让下一次真正重新解析（PathTask 侧只对临时失败有限重试，
            # 这里处理的是重试也没救回来的那部分）。
            self._drop_cache_entry(cache_key)
            logger.info(
                "[denia_share] 缓存里的媒体下载已失败，淘汰后重新解析一次"
            )
            return None

        return result

    @staticmethod
    def _cache_media_failed(result: ParseResult) -> bool:
        """缓存结果里是否已有**确定失败**的媒体任务（见 PathTask.failed）。"""
        for _label, task in result._iter_media_tasks():
            if task.failed:
                return True
        return False

    def _drop_cache_entry(self, cache_key: str) -> None:
        """摘掉一条解析结果，并连带摘掉它的渲染卡片。

        两者必须**同步**摘：卡片是从这份结果画出来的，结果都重解析了，卡片还
        留着的话，``renderer.render`` 见到 ``existing.exists()`` 就直接返回旧图，
        用户看到的标题/点赞数是上一次的。只做一半会出现「视频是新的、卡片是旧的」。
        """
        self._result_cache.pop(cache_key, None)
        self._render_cache.pop(cache_key, None)