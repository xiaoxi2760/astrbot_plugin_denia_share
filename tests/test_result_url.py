"""解析结果 ``url`` 缺失的兜底与规范链接补全。

**背景**：卡片页脚左侧画的是 ``short_url(result.url)``（core/card_renderer.py 的
``_footer_block``），而 ``result.url`` 完全由每个 handler 自己在
``self.result(...)`` 里传 —— 漏传**没有任何报错**，卡片只是安安静静地少一行。

实测（AST 扫全部 ``self.result()`` 调用）有 10 处没传 url：
抖音视频/图文、小红书两条、AcFun、Pixiv 收藏、微博视频、B站动态/专栏/收藏夹。
于是这些平台的卡片左下角一直空着，只有 B站视频、GitHub、快手、微博图文、
Twitter、Pixiv 插画、NGA 有内容。

分两层修：

1. ``BaseParser.fill_result_url`` —— 所有平台、所有 handler（含自定义解析器、
   ``parse_with_redirect``、WebUI 手动解析）的**唯一收口点**，url 为空时用
   ``searched.group(0)`` 补上。
2. 抖音 / 小红书 / AcFun 各自补**规范链接** —— 用户发短链时第 1 层只能补出
   ``v.douyin.com/xxx/`` 这种无意义短码，展开成 ``/video/<作品号>`` 才是用户
   认得的「号码」。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.base_parser import BaseParser, handle  # noqa: E402
from astrbot_plugin_denia_share.core.card_renderer import short_url  # noqa: E402
from astrbot_plugin_denia_share.core.data import (  # noqa: E402
    ParseResult, platform_of,
)
from astrbot_plugin_denia_share.core.constants import PlatformEnum  # noqa: E402

# 唯一域名，避免与任何真实解析器的 pattern 撞车
_FAKE_DOMAIN = "urlprobe.invalid"


class _ProbeParser(BaseParser):
    """测试用解析器：两个 handler，一个不传 url，一个传。"""

    platform = platform_of("bilibili")  # 类型上无所谓，这里只走 url 逻辑

    @handle(_FAKE_DOMAIN, r"urlprobe\.invalid/plain-(?P<sid>\w+)")
    async def _no_url(self, searched: re.Match[str]):
        return ParseResult(platform=self.platform, title=searched.group("sid"))

    @handle(_FAKE_DOMAIN, r"urlprobe\.invalid/full-(?P<sid>\w+)")
    async def _with_url(self, searched: re.Match[str]):
        return ParseResult(
            platform=self.platform,
            title=searched.group("sid"),
            url="https://example.com/canonical",
        )


class FillResultUrl(unittest.TestCase):
    """收口点本身的语义。"""

    def setUp(self):
        self.parser = _ProbeParser(None)
        self.addCleanup(_unregister_probe)

    def test_none_url_is_filled_with_matched_text(self):
        result = self.parser.result(title="x")
        self.assertIsNone(result.url)
        _ProbeParser.fill_result_url(result, re.search(r"(urlprobe\.invalid/plain-\w+)", "urlprobe.invalid/plain-abc"))
        self.assertEqual(result.url, "urlprobe.invalid/plain-abc")

    def test_existing_url_is_never_overwritten(self):
        """解析器给的规范链接优先 —— 它可能展开过短链、去掉过追踪参数。"""
        result = self.parser.result(url="https://example.com/canonical")
        _ProbeParser.fill_result_url(result, re.search(r"(urlprobe\.invalid/plain-\w+)", "urlprobe.invalid/plain-abc"))
        self.assertEqual(result.url, "https://example.com/canonical")

    def test_empty_match_writes_nothing(self):
        """pattern 匹配到空串时不能把 url 写成空 —— 那会让页脚静默消失。"""
        result = self.parser.result(title="x")
        empty = re.match(r"(x*)", "")  # group(0) == ""
        self.assertEqual(empty.group(0), "")
        _ProbeParser.fill_result_url(result, empty)
        self.assertIsNone(result.url)


class ParseFillsUrl(unittest.TestCase):
    """经由 ``parse()`` 的端到端行为 —— 这是线上真实走的入口。"""

    def setUp(self):
        self.parser = _ProbeParser(None)
        self.addCleanup(_unregister_probe)

    def test_parse_fills_missing_url(self):
        keyword, searched = self.parser.search_url("前缀 urlprobe.invalid/plain-abc 后缀")
        result = _run(self.parser.parse(keyword, searched))
        self.assertEqual(result.url, "urlprobe.invalid/plain-abc")

    def test_parse_keeps_parser_supplied_url(self):
        keyword, searched = self.parser.search_url("urlprobe.invalid/full-xyz")
        result = _run(self.parser.parse(keyword, searched))
        self.assertEqual(result.url, "https://example.com/canonical")

    def test_footer_text_is_non_empty_after_fill(self):
        """页脚真正要画的东西：补完之后 short_url 不能是空串。"""
        keyword, searched = self.parser.search_url("urlprobe.invalid/plain-abc")
        result = _run(self.parser.parse(keyword, searched))
        self.assertTrue(short_url(result.url))


class DouyinCanonicalUrl(unittest.IsolatedAsyncioTestCase):
    """短链也要展开成带作品号的永久链接。"""

    async def test_short_link_becomes_canonical(self):
        from astrbot_plugin_denia_share.core.parsers.douyin.parser import DouyinParser

        parser = DouyinParser(None)
        probed = {}

        async def fake_fetch(ty: str, vid: str):
            probed["ty"], probed["vid"] = ty, vid
            return parser.result(title="作品")

        parser._fetch_douyin = fake_fetch
        searched = re.search(
            r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)",
            "https://www.douyin.com/video/7301234567890123456",
        )
        result = await parser._parse_douyin(searched)
        self.assertEqual(probed, {"ty": "video", "vid": "7301234567890123456"})
        self.assertEqual(result.url, "https://www.douyin.com/video/7301234567890123456")

    async def test_slides_also_canonical(self):
        from astrbot_plugin_denia_share.core.parsers.douyin.parser import DouyinParser

        parser = DouyinParser(None)

        async def fake_fetch(ty: str, vid: str):
            return parser.result(title="图集")

        parser._fetch_douyin = fake_fetch
        searched = re.search(
            r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)",
            "https://www.douyin.com/slides/730999",
        )
        result = await parser._parse_douyin(searched)
        self.assertEqual(result.url, "https://www.douyin.com/slides/730999")


class XiaohongshuCanonicalUrl(unittest.IsolatedAsyncioTestCase):
    async def test_explore_path(self):
        from astrbot_plugin_denia_share.core.parsers.xiaohongshu import XiaoHongShuParser

        parser = XiaoHongShuParser(None)

        async def fake_explore(url, xhs_id):
            return parser.result(title="笔记")

        parser.parse_explore = fake_explore
        searched = parser.search_url(
            "https://www.xiaohongshu.com/explore/65abc123?xsec_token=AAA"
        )[1]
        result = await parser._parse_common(searched)
        # 只留作品号：query 连着 xsec_token 一长串，页脚放不下
        self.assertEqual(result.url, "https://www.xiaohongshu.com/explore/65abc123")

    async def test_discovery_fallback_path(self):
        from astrbot_plugin_denia_share.core.parsers.xiaohongshu import XiaoHongShuParser

        parser = XiaoHongShuParser(None)

        async def boom(url, xhs_id):
            raise RuntimeError("explore 挂了")

        async def fake_discovery(url):
            return parser.result(title="笔记")

        parser.parse_explore = boom
        parser.parse_discovery = fake_discovery
        searched = parser.search_url(
            "https://www.xiaohongshu.com/discovery/item/65def456"
        )[1]
        result = await parser._parse_common(searched)
        self.assertEqual(result.url, "https://www.xiaohongshu.com/explore/65def456")


class AcfunHandlerContract(unittest.TestCase):
    """AcFun 的 url 就在 handler 里现成（L26 拼的 ``/v/ac<acid>``），不值得绕。

    它的 handler 直接发网络请求，离线测不了；这里用 AST 钉住「result() 必须带
    url=」这条契约 —— 哪天重构又漏了，这里会红。
    """

    def test_handler_passes_url(self):
        src = (PLUGIN_DIR / "core" / "parsers" / "acfun.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "result"
        ]
        self.assertEqual(len(calls), 1, "AcFun 应只有一处 self.result()")
        self.assertIn("url", [kw.arg for kw in calls[0].keywords])


class AllPlatformParsersStillDispatch(unittest.TestCase):
    """改 url 不能顺手把平台派发改断。

    抖音的 ``_parse_douyin`` 被拆成了「wrapper + ``_fetch_douyin``」，
    而 ``__init_subclass__`` 是按 ``dir(cls)`` 收集带 ``@handle`` 的方法、
    再按 **pattern 索引** 派发的（见 base_parser 的说明）。拆分时若把装饰器
    留在错的方法上，这里就会发现「pattern 在册、handler 取不到」。
    """

    def test_every_registered_pattern_has_a_handler(self):
        for parser_cls in BaseParser._registry:
            for keyword, pattern in getattr(parser_cls, "_key_patterns", []):
                self.assertIn(
                    pattern,
                    parser_cls._handlers,
                    f"{parser_cls.__name__} 的 pattern（{keyword}）没有对应 handler",
                )

    def test_douyin_canonical_pattern_still_dispatches_to_wrapper(self):
        from astrbot_plugin_denia_share.core.parsers.douyin.parser import DouyinParser

        pattern = re.compile(r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)")
        self.assertIs(DouyinParser._handlers[pattern], DouyinParser._parse_douyin)

    def test_refactored_inner_method_is_not_registered_as_handler(self):
        """``_fetch_douyin`` 没有 ``@handle``，不该出现在派发表里。"""
        from astrbot_plugin_denia_share.core.parsers.douyin.parser import DouyinParser

        for _keyword, pattern in DouyinParser._key_patterns:
            self.assertIsNot(DouyinParser._handlers[pattern], DouyinParser._fetch_douyin)


def _unregister_probe() -> None:
    """测试用的子类不要留在全局注册表里污染后续用例。"""
    BaseParser._registry = [
        p for p in BaseParser._registry if p is not _ProbeParser
    ]


def _run(coro):
    """在同步用例里跑协程。"""
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()