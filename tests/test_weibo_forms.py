"""微博 URL 形态与解码错误可读性的测试。

两处修复，各有对应的坑：

1. ``m.weibo.cn/statuses/show?id=``（微博 App 分享的默认形态）原先匹配不到；
   而 ``status/show?id=`` 又被通用 pattern 误当成 ``detail/<id>``、把 ``show``
   当成作品 id 抓走。两条都要钉住。
2. 接口返回错误 JSON 时 msgspec 的 ``ValidationError`` 原本直接穿给用户，
   变成 ``Object missing required field `data``` 这种天书。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.parsers.weibo import WeiBoParser  # noqa: E402
from astrbot_plugin_denia_share.core.exception import ParseException  # noqa: E402


def match(url: str):
    return WeiBoParser.search_url(url)


class UrlForms(unittest.TestCase):
    """微博实际会被粘进群里的几种形态。"""

    def test_statuses_show_is_matched(self):
        """回归点：App「分享 → 复制链接」的默认形态原先匹配不到。"""
        keyword, m = match("https://m.weibo.cn/statuses/show?id=P9M8meR0O")
        self.assertEqual(keyword, "m.weibo.cn")
        self.assertEqual(m.group("wid"), "P9M8meR0O")

    def test_status_show_is_matched(self):
        keyword, m = match("https://m.weibo.cn/status/show?id=5054181788092183")
        self.assertEqual(m.group("wid"), "5054181788092183")

    def test_show_is_never_taken_as_the_id(self):
        """回归点：``status/show?id=X`` 曾被当成 ``detail/<id>``，wid 抓成 "show"，
        于是去请求一条叫 show 的作品，报错完全指不到真因。"""
        for url in (
            "https://m.weibo.cn/status/show?id=5054181788092183",
            "https://m.weibo.cn/statuses/show?id=P9M8meR0O",
        ):
            with self.subTest(url=url):
                self.assertNotEqual(match(url)[1].group("wid"), "show")

    def test_existing_forms_still_work(self):
        cases = {
            "https://m.weibo.cn/detail/5054181788092183": "5054181788092183",
            "https://m.weibo.cn/status/5054181788092183": "5054181788092183",
            "https://weibo.com/1234567890/P9M8meR0O": "P9M8meR0O",
        }
        for url, wid in cases.items():
            with self.subTest(url=url):
                self.assertEqual(match(url)[1].group("wid"), wid)

    def test_other_weibo_forms_unaffected(self):
        for url, name in (
            ("https://weibo.com/tv/show/1234:5678?mid=5054181788092183", "weibo.com/tv"),
            ("https://video.weibo.com/show?fid=1034:50678901234567", "video.weibo"),
            ("https://weibo.com/article/id/1234567890", "weibo.com/article"),
        ):
            with self.subTest(url=url):
                self.assertEqual(match(url)[0], name)


class DecodeErrorMessage(unittest.TestCase):
    """接口返回错误 JSON 时，报错必须是给用户看的中文，不是 msgspec 的字段名。"""

    def _parser(self):
        return WeiBoParser.__new__(WeiBoParser)  # 不需要构造依赖

    def test_missing_data_field_becomes_readable(self):
        parser = self._parser()
        with self.assertRaises(ParseException) as ctx:
            parser._decode_status(json.dumps({"ok": 0, "msg": "访客系统"}).encode(), "123")
        message = str(ctx.exception)
        self.assertNotIn("required field", message, "msgspec 的原始报错漏给用户了")
        self.assertNotIn("ValidationError", message)
        self.assertIn("微博接口返回了非预期数据", message)

    def test_message_mentions_cookie_and_fengkong(self):
        """用户要知道下一步做什么：配 Cookie / 等一下，而不是看到字段名。"""
        parser = self._parser()
        with self.assertRaises(ParseException) as ctx:
            parser._decode_status(b"{}", "123")
        message = str(ctx.exception)
        self.assertTrue("Cookie" in message or "风控" in message, message)

    def test_truncated_json_also_wrapped(self):
        parser = self._parser()
        with self.assertRaises(ParseException):
            parser._decode_status(b'{"data": {"id"', "123")

    def test_non_json_bytes_wrapped(self):
        parser = self._parser()
        with self.assertRaises(ParseException):
            parser._decode_status(b"<html>Sina Visitor System</html>", "123")

    def test_wrong_work_assertion_is_not_masked(self):
        """「接口返回了别的作品」的消息更有用，不能被笼统的解码提示吞掉。

        走真实 decoder 造不出合法 payload（缺字段会先在解码阶段挂掉），
        所以这里直接替换 decoder 的 decode，让它返回一个结构完整但 id 不符的
        对象 —— 这样测的正是「解码成功后，校验阶段的异常能不能原样冒出去」。
        """
        from astrbot_plugin_denia_share.core.models.weibo import common as weibo_common

        # msgspec.Decoder 的 decode 是 C 扩展只读方法，替换实例上的属性会报
        # AttributeError；但 _decode_status 每次调用都重新
        # ``from ..models.weibo.common import decoder``，所以换模块属性即可。
        original = weibo_common.decoder

        # 注意用属性而不是 dict：_assert_status_matches_requested 走的是
        # getattr(data, key)，真实的 data 是 msgspec Struct。
        fake_data = types.SimpleNamespace(id=999, idstr="999", mid="999", mblogid="999")
        weibo_common.decoder = types.SimpleNamespace(
            decode=lambda content: types.SimpleNamespace(data=fake_data)
        )
        try:
            parser = self._parser()
            with self.assertRaises(ParseException) as ctx:
                parser._decode_status(b'{"data":{}}', "5054181788092183")
        finally:
            weibo_common.decoder = original

        message = str(ctx.exception)
        self.assertIn("5054181788092183", message, "被笼统的解码提示吞掉了")
        self.assertNotIn("非预期数据", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
