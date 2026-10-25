# 本文件参考 MusicUID（MIT License）的搜索结果暂存思路实现，
# 上游项目：https://github.com/Xbaiyz12/MusicUID
# 本仓库为其改编版本，完整归属与许可证见项目根目录 README「许可与致谢」。

"""搜索结果会话暂存（SearchSessions）。

用户在群里搜索歌曲后，会拿到一张带序号的候选卡片；随后发送「点歌 2」
之类的指令时，需要把刚才那批搜索结果取回来。这里用一个纯内存的
``(scope, sender) -> songs`` 暂存表实现：

- **单调时钟 TTL**（``time.monotonic``）：到期自动失效，避免用户十分钟前
  的搜索结果被误用于点歌；使用单调钟也免疫系统时间被调整。
- **FIFO 容量上限**：超出 ``max_entries`` 时淘汰最早写入的一组，防止
  长时间运行的机器人内存无限增长。
- **按 scope + sender 双重隔离**：群聊里 A 的搜索结果不会被 B 读到，
  私聊（scope 不同）之间也互相看不到。

约束：不写任何文件、不落盘、不保存任何凭据；不持有调用方的列表引用，
put/get 都做浅拷贝，避免调用方后续修改列表导致会话内容被篡改。
"""

from __future__ import annotations

import time
import hashlib
from itertools import islice
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Hashable, Iterable

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查时需要，避免运行时循环导入
    from .models import Song

__all__ = ["SearchSessions"]

# 单组会话允许保存的最大歌曲数量；上游搜索接口本身也只返回少量结果，
# 这里再兜一层，避免异常输入把内存撑爆。
_MAX_SONGS_PER_SESSION = 64
# 单个 key 的最大字符长度，防止异常超长 sender/group id 占内存。
_MAX_KEY_LENGTH = 128


def _norm_key(value: Any) -> Hashable:
    """把 scope/sender 规整成稳定字符串 key，并限制长度。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > _MAX_KEY_LENGTH:
        return ('long', len(text), hashlib.sha256(text.encode('utf-8','surrogatepass')).hexdigest())
    return text


class SearchSessions:
    """带 TTL 与容量上限的搜索结果暂存表。

    :param ttl: 会话有效期（秒），默认 300 秒；<=0 表示永不过期。
    :param max_entries: 最多保留多少组会话，超出按写入顺序淘汰最早的。
    :param clock: 单调时钟函数，默认为 ``time.monotonic``；测试可注入假时钟。
    """

    def __init__(
        self,
        ttl: float = 300,
        max_entries: int = 128,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            self._ttl = float(ttl)
        except (TypeError, ValueError):
            self._ttl = 300.0
        if self._ttl < 0:
            self._ttl = 0.0
        try:
            self._max_entries = int(max_entries)
        except (TypeError, ValueError):
            self._max_entries = 128
        if self._max_entries < 1:
            self._max_entries = 1
        self._clock = clock
        # key -> (timestamp, songs)。OrderedDict 保证 FIFO 淘汰顺序。
        self._entries: "OrderedDict[Hashable, tuple[float, list[Any]]]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    # ---------------- 内部工具 ---------------- #

    @staticmethod
    def _make_key(scope: Any, sender: Any) -> Hashable:
        return (_norm_key(scope), _norm_key(sender))

    def _expired(self, ts: float) -> bool:
        if self._ttl <= 0:
            return False
        return (self._clock() - ts) >= self._ttl

    def _prune(self) -> None:
        """清理过期会话，并按 FIFO 施加容量上限。"""
        if self._ttl > 0:
            now = self._clock()
            stale = [k for k, (ts, _) in self._entries.items() if (now - ts) >= self._ttl]
            for k in stale:
                self._entries.pop(k, None)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # FIFO：淘汰最早写入的一组

    @staticmethod
    def _copy_songs(songs: Iterable[Any] | None) -> list[Any]:
        if not songs:
            return []
        # 只做浅拷贝：Song 是 dataclass，复制列表即可避免调用方改动列表结构；
        # 不做 deepcopy，避免把 Path / Task / 大对象整棵复制（性能与安全都更差）。
        copied = list(islice(songs, _MAX_SONGS_PER_SESSION))
        return copied

    # ---------------- 公开 API ---------------- #

    @property
    def ttl(self) -> float:
        return self._ttl

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        self._prune()
        return len(self._entries)

    def put(self, scope: Any, sender: Any, songs: Iterable[Any] | None) -> None:
        """写入一组搜索结果；同一 ``(scope, sender)`` 会被整体覆盖（重新搜索）。"""
        key = self._make_key(scope, sender)
        payload = self._copy_songs(songs)
        now = self._clock()
        # 先删后插，保证刷新后的 key 排到队尾，淘汰顺序按「最近写入」计算。
        self._entries.pop(key, None)
        self._entries[key] = (now, payload)
        self._prune()

    def get(self, scope: Any, sender: Any) -> list[Any] | None:
        """取出一组搜索结果；不存在或已过期返回 ``None``。返回的是列表副本。"""
        key = self._make_key(scope, sender)
        item = self._entries.get(key)
        if item is None:
            self._misses += 1
            self._prune()
            return None
        ts, songs = item
        if self._expired(ts):
            self._entries.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return list(songs)

    def peek(self, scope: Any, sender: Any) -> list[Any] | None:
        """同 :meth:`get`，但命中后不刷新写入顺序（不用于点歌主流程）。"""
        item = self._entries.get(self._make_key(scope, sender))
        if item is None or self._expired(item[0]):
            return None
        return list(item[1])

    def clear(self) -> None:
        """清空所有会话。"""
        self._entries.clear()

    def stats(self) -> dict[str, int]:
        """简单的运行期计数，便于排查命中率问题（不含任何歌曲内容）。"""
        return {
            "entries": len(self),
            "hits": self._hits,
            "misses": self._misses,
            "max_entries": self._max_entries,
        }
