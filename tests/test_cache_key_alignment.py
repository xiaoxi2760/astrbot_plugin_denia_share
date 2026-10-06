"""缓存/去重的键必须等于「解析器实际认领的那一段」。

## 背景：这个键算错过两次

键取错对象过一次、两次，症状都是「同一条链接被处理了两遍」或更糟
「**用户收到的是另一个链接的内容**」：

1. 键用**整条消息**。而 11 个内置平台处理器走 ``_dispatch`` 传进来的正是整条事件
   （``【】看看这个 https://…``），于是「带前缀的转发」与「裸链接」成了两个键 ——
   不去重、也不共用结果缓存，同一链接解析两遍、群里两张一样的卡片。
2. 改成用 ``URL_PATTERN.search()`` 抽「第一个 URL」。方向对了，但那是**独立**的抽取，
   和解析器自己的 pattern **可以是不同的链接**：
   - 一条消息里两个平台链接 → 第二个平台的处理器拿到第一个链接的键，
     抖音解析器一次没被调用，**却发出一张 B站卡片**；
   - 前面挂一个非本平台链接 + 两个不同视频 → 两个不同视频共用一个键，
     用户收到的是**第一个视频的卡片**；
   - ``URL_PATTERN`` 的 ``[^\\s'"<>]+`` 不排除中文与标点，
     「…/xxx，挺有意思」把 8 个中文字一起吞进键。

正确做法：**先让解析器认领（``search_url``），再按它认领的那段（``searched.group(0)``）
算键**。这样「键 = 这次解析处理的到底是哪个链接」恒等，多链接、跨平台、带前缀
各种形态都自动对。

本文件的用例都跑**真 main.py**（复用 ``test_link_dedup_e2e`` 的进程内补桩搭法），
断言的是实际 yield 出去的消息与解析器调用次数。

运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import re
import unittest
from typing import Any

from tests.test_link_dedup_e2e import (  # noqa: E402
    GROUP_A,
    HINT,
    PLATFORM,
    URL,
    _FakeEvent,
    _cfg,
    _make_plugin,
    _send,
    _texts,
)
from astrbot_plugin_denia_share.core.data import ParseResult  # noqa: E402
from astrbot_plugin_denia_share.core.exception import SilentException  # noqa: E402

BILI = "https://www.bilibili.com/video/BV1GJ411x7h7"
BILI2 = "https://www.bilibili.com/video/BV1YtHs62EHZ"
DOUYIN = "https://v.douyin.com/iRNBho6/"
FOREIGN = "https://example.com/article/123"


class PlatformParser:
    """照抄 ``core/base_parser.py:189`` ``search_url`` 的真实语义：
    用**本平台自己的 pattern** 去搜**整条字符串**，返回 ``(keyword, match)``。
    """

    def __init__(self, keyword: str, pattern: str, title: str):
        self._keyword = keyword
        self._pattern = re.compile(pattern)
        self._title = title
        self.search_calls: list = []
        self.parse_calls: list = []

    def search_url(self, url: str):
        self.search_calls.append(url)
        if self._keyword not in url:
            raise SilentException("unmatched")
        m = self._pattern.search(url)
        if m is None:
            raise SilentException("unmatched")
        return m.group(0), m

    async def parse(self, keyword: str, searched: Any = None) -> ParseResult:
        self.parse_calls.append(keyword)
        # 标题里带上**这次实际认领的那一段**，否则两个不同链接产出的卡片长得
        # 一模一样，测试就分不出「拿到了正确的内容」还是「拿到了缓存里的旧内容」。
        claimed = searched.group(0) if searched is not None else keyword
        tail = claimed.rsplit("/", 1)[-1] or claimed
        return ParseResult(
            platform=PLATFORM, title=f"{self._title}:{tail[-12:]}", contents=[]
        )

    async def search(self, keyword: str) -> ParseResult:
        return await self.parse(keyword)


def _bili(title: str = "BILI-CARD") -> PlatformParser:
    """同时覆盖「带域名的 URL」和「整条消息就是一个裸 BV 号」两种 pattern ——
    主力那边是两���独立的 ``@handle``，这里合成一条以免测试替身比真实实现更宽松。"""
    return PlatformParser(
        "bilibili",
        r"bilibili\.com/video/BV[0-9A-Za-z]{10}|^(?P<bvid>BV[0-9A-Za-z]{10})",
        title,
    )


def _douyin(title: str = "DOUYIN-CARD") -> PlatformParser:
    return PlatformParser("douyin", r"douyin\.com/[0-9A-Za-z]+", title)


class TestKeyFollowsParserClaim(unittest.TestCase):
    """键 = 解析器认领的那一段。"""

    def test_two_platform_links_get_two_keys(self):
        """一条消息带两个平台链接：两个处理器各自解析各自的链接。"""
        with _cfg():
            p = _make_plugin()
            b, d = _bili(), _douyin()
            ev = _FakeEvent(GROUP_A, f"{BILI} and also {DOUYIN}")

            first = _send(p, ev, b)
            second = _send(p, ev, d)

            self.assertIn("BILI-CARD", first[0].text())
            self.assertIn("DOUYIN-CARD", second[0].text(),
                          "第二个平台应发自己的卡片，不能重放第一个的")
            self.assertEqual(len(p._result_cache), 2,
                             "两个不同平台的链接是两个键")
            self.assertEqual(len(d.parse_calls), 1, "抖音解析器应当被调用")

    def test_two_different_videos_never_share_a_key(self):
        """两个不同视频共用一个键 = 用户收到第一个视频的内容。"""
        with _cfg():
            p = _make_plugin()
            b = _bili()
            ev = _FakeEvent(GROUP_A, f"前导 {FOREIGN} 然后 {BILI} 还有 {BILI2}")

            first = _send(p, ev, b)
            second = _send(p, _FakeEvent(GROUP_A, BILI2, sender="u2"), b)

            self.assertEqual(
                len(p._result_cache), 2,
                "两个**不同**的视频被存进了同一个键 —— 第二个会拿到第一个的结果",
            )
            self.assertNotEqual(
                _texts(first), _texts(second),
                "两个不同链接发出了同样的卡片",
            )

    def test_key_does_not_depend_on_link_order(self):
        """同一条链接换位置/换措辞，必须是同一个键。"""
        with _cfg():
            p = _make_plugin()
            b = _bili()
            _send(p, _FakeEvent(GROUP_A, BILI), b)
            _send(p, _FakeEvent(GROUP_A, f"再看看 {BILI}", sender="u2"), b)
            _send(p, _FakeEvent(GROUP_A, f"{BILI} 挺好", sender="u3"), b)
            self.assertEqual(len(p._result_cache), 1, "同一条链接存了多份缓存")
            self.assertEqual(len(b.parse_calls), 1, "同一条链接被重新解析了")

    def test_chinese_punctuation_after_link_does_not_split_the_key(self):
        """「…/xxx，挺有意思」与「…/xxx」必须是同一个键。

        这是被换掉的那版 ``URL_PATTERN`` 的残留面：``[^\\s'"<>]+`` 不排除中文，
        会把 8 个中文字一起吞进键。
        """
        with _cfg():
            p = _make_plugin()
            d = _douyin()
            _send(p, _FakeEvent(GROUP_A, DOUYIN), d)
            _send(p, _FakeEvent(
                GROUP_A, f"看看这个 {DOUYIN}，挺有意思", sender="u2"), d)
            self.assertEqual(len(p._result_cache), 1,
                             "中文标点尾巴把同一条链接切成了两个键")
            self.assertEqual(len(d.parse_calls), 1)

    def test_private_chat_does_not_replay_first_handler_card(self):
        """私聊不去重（设计如此），但**也不能**把第一个处理器的卡片重放给第二个。"""
        with _cfg():
            p = _make_plugin()
            b, d = _bili(), _douyin()
            ev = _FakeEvent("OneBot:FriendMessage:private_x",
                            f"{BILI} and also {DOUYIN}", private=True)
            _send(p, ev, b)
            second = _send(p, ev, d)
            self.assertIn("DOUYIN-CARD", second[0].text(),
                          "第二个处理器发的是第一个链接的卡片")
            self.assertEqual(len(d.parse_calls), 1)

    def test_bare_bv_form_still_works(self):
        """不能把「整条消息就是一个裸 BV 号」这条路修坏。

        注意裸形态的 keyword 是 ``BV`` 而不是 ``bilibili`` —— 真实
        ``search_url`` 先用 keyword 过滤（``if keyword not in url: continue``），
        主力那边是 ``@handle("BV", ...)`` 与 ``@handle("/BV", ...)`` 两条独立注册。
        """
        with _cfg():
            p = _make_plugin()
            bare = PlatformParser("BV", r"^(?P<bvid>BV[0-9A-Za-z]{10})", "BARE-CARD")
            out = _send(p, _FakeEvent(GROUP_A, "BV1GJ411x7h7"), bare)
            self.assertTrue(out, "整条消息就是裸 BV 号时应当能解析")
            self.assertIn("BARE-CARD", out[0].text())
            self.assertEqual(len(p._result_cache), 1)

    def test_dedup_still_fires_for_group_chat(self):
        """键换成「认领的那段」之后，去重必须照常生效。"""
        with _cfg():
            p = _make_plugin()
            b = _bili()
            _send(p, _FakeEvent(GROUP_A, f"【】看看这个 {BILI}"), b)
            second = _send(p, _FakeEvent(GROUP_A, BILI, sender="u2"), b)
            self.assertEqual(
                _texts(second), [HINT],
                f"带前缀的转发与裸链接没认出是同一条：{second}",
            )
            self.assertEqual(len(b.parse_calls), 1)


if __name__ == "__main__":
    unittest.main()
