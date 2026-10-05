"""Twitter 镜像 API 地址拼接的回归测试。

**背景**：原先两级取数都是这么拼地址的::

    url.replace("x.com", host).replace("twitter.com", host)

第一段产出的 ``api.fxtwitter.com`` 本身就含 ``twitter.com``（``f-x`` + ``twitter.com``），
第二段会对着它**再替换一次**，拼出 ``api.fxapi.fxtwitter.com`` 这种不存在的域名。
于是两级兜底同时失效，而报错是 ``SSLV3_ALERT_HANDSHAKE_FAILURE`` 或空的
``ConnectError`` —— 全都指向"网络问题"，排查方向会被彻底带偏
（实测就为此绕了一大圈：先怀疑国内 TLS 重置、再怀疑代理、最后才拼出 URL 一看就穿）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.parsers.twitter import to_api_url  # noqa: E402

VX = "api.vxtwitter.com"
FX = "api.fxtwitter.com"

# 用户提供的真实链接
REAL = "https://x.com/gosari542/status/2106766026847522972"


def old_broken(url: str, host: str) -> str:
    """修复前的写法，留着做对照。"""
    return url.replace("x.com", host).replace("twitter.com", host)


class NoDoubleReplace(unittest.TestCase):
    def test_regression_host_is_not_replaced_twice(self):
        """回归钉子：host 里含 twitter.com 也不该被二次替换。"""
        self.assertEqual(
            to_api_url(REAL, FX), f"https://{FX}/gosari542/status/2106766026847522972"
        )
        self.assertEqual(
            to_api_url(REAL, VX), f"https://{VX}/gosari542/status/2106766026847522972"
        )

    def test_old_way_was_actually_broken(self):
        """证明这不是我臆想的风险 —— 旧写法确实产出不存在的域名。"""
        for host in (VX, FX):
            with self.subTest(host=host):
                broken = old_broken(REAL, host)
                self.assertNotEqual(broken, to_api_url(REAL, host))
                self.assertIn("api.", broken.split("//")[1].split("/")[0][3:])

    def test_twitter_com_input(self):
        url = "https://twitter.com/someone/status/1234567890"
        self.assertEqual(to_api_url(url, FX), f"https://{FX}/someone/status/1234567890")

    def test_query_string_is_preserved(self):
        url = "https://x.com/u/status/1?s=20&t=abc"
        self.assertEqual(to_api_url(url, FX), f"https://{FX}/u/status/1?s=20&t=abc")

    def test_fragment_is_dropped(self):
        """fragment 不会发给服务器，带过去只会让签名/缓存键对不上。"""
        url = "https://x.com/u/status/1#media"
        self.assertEqual(to_api_url(url, FX), f"https://{FX}/u/status/1")

    def test_scheme_is_forced_to_https(self):
        self.assertTrue(to_api_url("http://x.com/u/status/1", FX).startswith("https://"))

    def test_hosts_that_contain_the_keyword_stay_intact(self):
        for host in (VX, FX):
            self.assertEqual(host.count("twitter.com"), 1, "这个用例的前提是 host 含该子串")


class ParserUsesTheHelper(unittest.TestCase):
    def test_both_tiers_call_to_api_url(self):
        """两级都必须走这个函数，不能有人「顺手」改回 replace 链。"""
        import inspect

        from astrbot_plugin_denia_share.core.parsers.twitter import TwitterParser

        for name in ("parse_by_vxapi", "parse_by_fxapi"):
            with self.subTest(method=name):
                src = inspect.getsource(getattr(TwitterParser, name))
                self.assertIn("to_api_url(", src)
                self.assertNotIn(
                    '.replace("x.com"', src, "又用回 replace 链了"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
