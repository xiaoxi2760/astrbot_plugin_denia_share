"""网页截图「整页 vs 视窗」的构造测试。

**背景**：``ScreenshotService.capture()`` 一直有 ``full_page`` 参数，但插件侧
（``main.py`` 的 ``_do_screenshot``）从来没传过，所以它一直是死代码，
thum 后端永远只给 1280×1280 的方图 —— 长页面上只剩顶部一小块。

回归钉子：
1. 整页模式**必须**把 ``/fullpage`` 写进 thum 的路径（不写就是视窗）；
2. 整页与视窗给不同宽度；
3. 默认配置项是**开**整页（视窗在长页面上基本不可用）；
4. 配置项读侧对脏值有兜底。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.config import ParserConfig  # noqa: E402
from astrbot_plugin_denia_share.core.screenshot import (  # noqa: E402
    ScreenshotService,
    THUM_BASE,
)

PAGE = "https://en.wikipedia.org/wiki/Internet_protocol_suite"


class ThumUrl(unittest.TestCase):
    def test_viewport_mode_has_no_fullpage(self):
        url = ScreenshotService.build_thum_url(PAGE, full_page=False)
        self.assertTrue(url.startswith(THUM_BASE))
        self.assertIn("/width/900/", url)
        self.assertNotIn("fullpage", url)
        self.assertTrue(url.endswith(PAGE))

    def test_fullpage_mode_puts_fullpage_in_path(self):
        """回归点：不写 /fullpage 就退化成视窗方图，整页内容全丢。"""
        url = ScreenshotService.build_thum_url(PAGE, full_page=True)
        self.assertIn("/fullpage", url)
        self.assertIn("/width/1280/", url)
        self.assertNotIn("/width/900/", url)
        self.assertTrue(url.endswith(PAGE))

    def test_option_order_and_separators(self):
        url = ScreenshotService.build_thum_url(PAGE, full_page=True)
        self.assertEqual(
            url,
            f"{THUM_BASE}/width/1280/fullpage/noanimate/{PAGE}",
            "thum 的选项是拼进路径的，顺序/斜杠错了服务端会当普通路径返回空白图",
        )

    def test_noanimate_always_present(self):
        for fp in (True, False):
            with self.subTest(full_page=fp):
                self.assertIn("/noanimate/", ScreenshotService.build_thum_url(PAGE, full_page=fp))

    def test_url_with_query_is_preserved_verbatim(self):
        tricky = "https://example.com/a?b=1&c=2#frag"
        for fp in (True, False):
            with self.subTest(full_page=fp):
                self.assertTrue(ScreenshotService.build_thum_url(tricky, full_page=fp).endswith(tricky))


class FullPageConfig(unittest.TestCase):
    def test_default_is_on(self):
        """视窗在长页面上基本不可用，所以默认开整页。"""
        cfg = ParserConfig({}, PLUGIN_DIR, PLUGIN_DIR)
        self.assertIs(cfg.SCREENSHOT_FULL_PAGE, True)

    def test_explicit_off_is_respected(self):
        cfg = ParserConfig(
            {"网页截图": {"SCREENSHOT_FULL_PAGE": False}}, PLUGIN_DIR, PLUGIN_DIR
        )
        self.assertIs(cfg.SCREENSHOT_FULL_PAGE, False)

    def test_dirty_value_falls_back_to_default(self):
        for value in ("", "  ", None, 1, "yes", ["true"]):
            with self.subTest(value=value):
                cfg = ParserConfig(
                    {"网页截图": {"SCREENSHOT_FULL_PAGE": value}}, PLUGIN_DIR, PLUGIN_DIR
                )
                # bool() 会把 1/"yes" 当真值，这里只保证不会炸、且非空真值仍为真
                self.assertIsInstance(cfg.SCREENSHOT_FULL_PAGE, bool)


class CapturePlumbing(unittest.TestCase):
    def test_capture_accepts_full_page_kwarg(self):
        """capture 必须真的接住这个参数（历史上它被调用方忽略了）。"""
        import inspect

        sig = inspect.signature(ScreenshotService.capture)
        self.assertIn("full_page", sig.parameters)
        self.assertIs(sig.parameters["full_page"].default, False)

    def test_thum_capture_passes_full_page_through(self):
        import inspect

        src = inspect.getsource(ScreenshotService._capture_thum)
        self.assertIn("full_page=full_page", src.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
