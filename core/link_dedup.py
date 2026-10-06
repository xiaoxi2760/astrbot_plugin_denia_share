"""同群重复链接去重。

## 它解决什么

群里一条热链被连着转发时，机器人会连发好几张一模一样的卡片。这个模块让
**第一个人发完之后的窗口内，后来者不再解析、也不再收到卡片**。

## 与「重复解析间隔」的区别（别混）

- ``RESULT_CACHE_TTL_SECONDS``（重复解析间隔）：命中缓存后**照旧发卡片**，
  省的是「再请求一次平台接口」
- 本模块：**不响应**，治的是「刷屏」

两个可以同时开，语义正交。

## 几个刻意的决定

1. **按会话隔离**（键含 ``unified_msg_origin``）。A 群和 B 群同时发同一条链接，
   应当各自都解析一次 —— 去重的目的是「一个群里不刷屏」，不是「全世界只解析一次」。
   私聊**不去重**（见 ``main.DeniaSharePlugin._dedup_applies``）：一个人私聊里连发
   两次同一链接，多半是「没看到回复再发一遍」，拦下来只会像插件坏了。

2. **提示只发一次**。第 2 个人收到「刚刚已经解析过了哦」，第 3 个及之后**静默**。
   每个人都回一遍的话，5 个人发就是 1 张卡 + 4 句同样的话，等于没去重。

3. **解析失败要撤销占位**。否则第一次解析挂了、后面所有人都被静默掉，用户只会
   觉得「插件坏了」。占位是在**开始处理时**记的（防并发重复劳动），但失败时
   必须撤掉，让下一个人能重试。

4. **窗口按首次出现时间算，不因后来者而延长**。否则一直有人发就永远锁死。

5. **有容量上限**。纯内存字典，没人发链接时不会长，但要防「一直发不同链接」
   把它撑大 —— 每次 check 顺手清掉过期的，并按 FIFO 兜底。
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple

__all__ = ["LinkDedup", "PROCEED", "HINT", "SILENT"]

#: 正常处理
PROCEED = "proceed"
#: 窗口内重复，且提示还没发过 -> 回一句提示
HINT = "hint"
#: 窗口内重复，且提示已发过 -> 完全静默
SILENT = "silent"

#: 条数上限。纯内存字典，实际很难到（有窗口淘汰），这里是防「一直发不同链接」
#: 把字典撑大的兜底。
MAX_ENTRIES = 512


class LinkDedup:
    """按 (会话, 链接) 维度做去重判定。

    :param window_provider: 无参可调用，返回当前窗口秒数。传函数而不是读配置，
        是为了让热更新能立即生效，也方便测试注入。
    """

    def __init__(self, window_provider: Callable[[], int]) -> None:
        self._window = window_provider
        # (会话, 链接) -> (首次出现时刻, 提示是否已发)
        self._entries: Dict[str, Tuple[float, bool]] = {}

    @staticmethod
    def key(scope: str, cache_key: str) -> str:
        """``scope`` 是 ``unified_msg_origin``（平台:类型:会话ID）。"""
        return f"{scope}|{cache_key}"

    def check(self, scope: str, cache_key: str) -> str:
        """判定这条链接现在该怎么处理，并登记占位。

        返回 :data:`PROCEED` / :data:`HINT` / :data:`SILENT`。窗口为 0 时永远
        ``PROCEED``，且不登记任何东西（等于功能关闭）。
        """
        try:
            window = int(self._window())
        except (TypeError, ValueError):
            window = 60
        if window <= 0:
            return PROCEED

        self._prune(window)
        key = self.key(scope, cache_key)
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and (now - entry[0]) < window:
            if entry[1]:
                return SILENT
            self._entries[key] = (entry[0], True)
            return HINT

        self._entries[key] = (now, False)
        self._enforce_cap()
        return PROCEED

    def forget(self, scope: str, cache_key: str) -> None:
        """撤销占位 —— 首次处理失败时调用，好让下一个人能重试。"""
        self._entries.pop(self.key(scope, cache_key), None)

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self, window: int) -> None:
        now = time.monotonic()
        stale = [k for k, (t, _) in self._entries.items() if (now - t) >= window]
        for k in stale:
            self._entries.pop(k, None)

    def _enforce_cap(self) -> None:
        while len(self._entries) > MAX_ENTRIES:
            self._entries.pop(next(iter(self._entries)), None)

    def __len__(self) -> int:
        return len(self._entries)
