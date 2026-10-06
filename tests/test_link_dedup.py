"""同群重复链接去重的行为测试（core/link_dedup.py）。

不联网、不碰 AstrBot：本模块只做「(会话, 链接) -> 怎么办」的纯判定，
网络与事件管线都在 main.py 那边另测。

钉住的设计决定（每条都在 :mod:`core.link_dedup` 的模块文档里有理由）：

1. **按会话隔离** —— A 群和 B 群同时发同一条链接，两边都要解析。混成全局的
   后果是「A 群发完，B 群就再也解析不到了」，那是误伤而不是去重。
2. **提示只发一次** —— 第 2 个人收到提示，第 3 个及之后静默。每个人都回一遍
   的话，5 个人发就是 1 张卡 + 4 句同样的话，等于没去重。
3. **窗口按首次出现时间算** —— 不因后来者而延长，否则一直有人发就永远锁死。
4. **forget() 撤销占位** —— 第一次解析挂了就该让下一个人重试。
5. **窗口 0 = 完全关闭**，且不登记任何东西（否则关了也会去重）。

运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.config import (  # noqa: E402
    CONFIG_META,
    get_config,
    init_config,
)
from astrbot_plugin_denia_share.core.link_dedup import (  # noqa: E402
    HINT,
    MAX_ENTRIES,
    PROCEED,
    SILENT,
    LinkDedup,
)

init_config({}, PLUGIN_DIR, PLUGIN_DIR)

GROUP_A = "OneBot:FriendMessage:group_a"
GROUP_B = "OneBot:FriendMessage:group_b"
PM = "OneBot:FriendMessage:private_c"
URL = "https://www.bilibili.com/video/BV1GJ411x7h7"
OTHER = "https://www.douyin.com/video/123"


def _dedup(window=60):
    return LinkDedup(lambda: window)


class TestBasicFlow(unittest.TestCase):
    def test_first_sender_proceeds(self):
        self.assertEqual(_dedup().check(GROUP_A, "k"), PROCEED)

    def test_second_sender_gets_hint(self):
        d = _dedup()
        d.check(GROUP_A, "k")
        self.assertEqual(d.check(GROUP_A, "k"), HINT)

    def test_third_and_later_are_silent(self):
        """「只回复一次」：第 3 个及之后连提示都不发，否则等于没去重。"""
        d = _dedup()
        d.check(GROUP_A, "k")
        self.assertEqual(d.check(GROUP_A, "k"), HINT)
        for i in range(5):
            self.assertEqual(d.check(GROUP_A, "k"), SILENT, f"第 {i + 3} 个应当静默")

    def test_window_expires(self):
        d = _dedup(window=0)
        d.check(GROUP_A, "k")
        # 窗口 0 时 check 不登记，天然每次都放行
        self.assertEqual(d.check(GROUP_A, "k"), PROCEED)

    def test_hint_does_not_extend_the_window(self):
        """窗口按**首次**出现算。延长的话一直有人发就永远锁死。"""
        d = _dedup(window=60)
        d.check(GROUP_A, "k")
        first_ts = d._entries[LinkDedup.key(GROUP_A, "k")][0]
        d.check(GROUP_A, "k")
        d.check(GROUP_A, "k")
        after = d._entries[LinkDedup.key(GROUP_A, "k")][0]
        self.assertEqual(
            first_ts, after,
            "提示/静默判定把首次出现时间往后推了 —— 窗口会被无限延长",
        )


class TestIsolation(unittest.TestCase):
    def test_different_group_is_independent(self):
        d = _dedup()
        self.assertEqual(d.check(GROUP_A, "k"), PROCEED)
        self.assertEqual(
            d.check(GROUP_B, "k"), PROCEED,
            "B 群应当照常解析 —— 去重是「一个群里不刷屏」，不是「全世界只解析一次」",
        )

    def test_different_url_is_independent(self):
        d = _dedup()
        self.assertEqual(d.check(GROUP_A, "k1"), PROCEED)
        self.assertEqual(d.check(GROUP_A, "k2"), PROCEED)

    def test_private_chat_is_its_own_scope(self):
        d = _dedup()
        self.assertEqual(d.check(PM, "k"), PROCEED)
        self.assertEqual(d.check(PM, "k"), HINT)
        self.assertEqual(d.check(GROUP_A, "k"), PROCEED)

    def test_key_contains_both_parts(self):
        self.assertNotEqual(
            LinkDedup.key(GROUP_A, "k"), LinkDedup.key(GROUP_B, "k"))
        self.assertNotEqual(
            LinkDedup.key(GROUP_A, "k1"), LinkDedup.key(GROUP_A, "k2"))


class TestForgetOnFailure(unittest.TestCase):
    """第一次解析失败必须撤销占位，否则整个窗口都被废掉。"""

    def test_forget_allows_retry(self):
        d = _dedup()
        d.check(GROUP_A, "k")
        self.assertEqual(d.check(GROUP_A, "k"), HINT)
        d.forget(GROUP_A, "k")
        self.assertEqual(
            d.check(GROUP_A, "k"), PROCEED,
            "占位没撤销：第一次解析挂了，后面所有人都被静默掉",
        )

    def test_forget_resets_the_hint_flag_too(self):
        """撤销后重新占位，提示应当还能再发一次（新一轮的第一人 + 第二人）。"""
        d = _dedup()
        d.check(GROUP_A, "k")
        d.check(GROUP_A, "k")          # 提示已发
        d.forget(GROUP_A, "k")
        d.check(GROUP_A, "k")          # 新一轮第一人
        self.assertEqual(d.check(GROUP_A, "k"), HINT)

    def test_forget_unknown_key_is_noop(self):
        d = _dedup()
        d.forget(GROUP_A, "never-seen")  # 不该抛
        self.assertEqual(len(d), 0)


class TestWindowDisabled(unittest.TestCase):
    def test_zero_window_never_dedups(self):
        d = _dedup(window=0)
        for _ in range(5):
            self.assertEqual(d.check(GROUP_A, "k"), PROCEED)

    def test_zero_window_registers_nothing(self):
        """关掉之后不该残留占位，否则把窗口改回来会莫名其妙地命中旧记录。"""
        d = _dedup(window=0)
        d.check(GROUP_A, "k")
        d.check(GROUP_A, "k")
        self.assertEqual(len(d), 0)

    def test_negative_window_treated_as_disabled(self):
        d = _dedup(window=-5)
        self.assertEqual(d.check(GROUP_A, "k"), PROCEED)

    def test_dirty_window_falls_back_instead_of_raising(self):
        """脏值不能裸抛：抛出去会一路冒到 _process_url 变成「处理出错」，
        那这条链接就永远解析不了了。"""
        for bad in ("abc", None, object()):
            with self.subTest(window=bad):
                d = LinkDedup(lambda b=bad: b)
                self.assertEqual(d.check(GROUP_A, "k"), PROCEED)


class TestMemoryBounds(unittest.TestCase):
    def test_expired_entries_are_pruned(self):
        d = _dedup(window=60)
        d.check(GROUP_A, "k")
        self.assertEqual(len(d), 1)
        # 把时刻拨到 10 分钟前：下次 check 会顺手清掉
        key = LinkDedup.key(GROUP_A, "k")
        ts, hint = d._entries[key]
        d._entries[key] = (ts - 600, hint)
        d.check(GROUP_B, "other")
        self.assertNotIn(key, d._entries)

    def test_capacity_cap(self):
        d = _dedup(window=3600)
        for i in range(MAX_ENTRIES + 20):
            d.check(GROUP_A, f"k{i}")
        self.assertLessEqual(len(d), MAX_ENTRIES)

    def test_clear(self):
        d = _dedup()
        d.check(GROUP_A, "k")
        d.clear()
        self.assertEqual(len(d), 0)
        self.assertEqual(d.check(GROUP_A, "k"), PROCEED)


class TestConfigWiring(unittest.TestCase):
    """配置项的四处一致 + WebUI 里露出的形态。"""

    def test_default_is_60_seconds(self):
        meta = next(m for m in CONFIG_META
                    if m["key"] == "DUPLICATE_LINK_WINDOW_SECONDS")
        self.assertEqual(meta["default"], 60)
        self.assertEqual(meta["min"], 0, "0 = 关闭，必须合法")
        self.assertEqual(meta["unit"], "秒")
        self.assertEqual(meta["group"], "维护")

    def test_property_reads_config(self):
        class _C:
            def __init__(self, v):
                self.v = v

            def _cfg_get(self, key, default=None):
                return self.v if key == "DUPLICATE_LINK_WINDOW_SECONDS" else default

        cfg = get_config()
        orig = cfg._cfg_get
        try:
            cfg._cfg_get = _C(30)._cfg_get
            self.assertEqual(cfg.DUPLICATE_LINK_WINDOW_SECONDS, 30)
        finally:
            cfg._cfg_get = orig

    def test_schema_invisible_and_aligned(self):
        schema = json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )
        item = schema["维护"]["items"]["DUPLICATE_LINK_WINDOW_SECONDS"]
        self.assertTrue(item["invisible"], "原生面板不该露出这个开关")
        self.assertEqual(item["default"], 60)
        self.assertEqual(item["min"], 0)

    def test_webui_payload_exposes_it(self):
        from astrbot_plugin_denia_share.core.config import config_meta_payload

        items = {
            i["key"]: i for i in config_meta_payload()["items"]
            if isinstance(i, dict) and "key" in i
        }
        item = items["DUPLICATE_LINK_WINDOW_SECONDS"]
        self.assertEqual(item["label"], "同群重复链接去重")
        self.assertTrue(item["hint"])


class TestMainWiring(unittest.TestCase):
    """main.py 侧的接线（AST 层面 —— 该文件在测试环境里导不进来）。

    顺序是最容易写错的一处：**去重必须排在缓存查询之前**。反了的话第二个人
    会命中结果缓存、照常收到卡片，整个去重形同虚设。
    """

    @classmethod
    def setUpClass(cls):
        import ast

        cls.src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(cls.src)
        cls.fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_url"
        )
        cls.body = ast.unparse(cls.fn)

    def test_dedup_check_precedes_cache_lookup(self):
        i_dedup = self.body.find("_link_dedup.check")
        i_cache = self.body.find("_get_cached_result")
        self.assertGreaterEqual(i_dedup, 0, "_process_url 里没有去重判定")
        self.assertGreaterEqual(i_cache, 0, "_process_url 里没有缓存查询")
        self.assertLess(
            i_dedup, i_cache,
            "去重必须排在缓存查询之前：反了的话第二个人命中缓存、照旧收到卡片，"
            "等于没去重",
        )

    def test_dedup_is_gated_to_group_chats(self):
        """需求原文是「只在同一个群」，私聊必须不去重。

        私聊里一个人连发两次同一链接，多半是「没看到回复再发一遍」，
        这时候拦下来只会让人以为插件坏了 —— 私聊本来也不存在刷屏问题。
        """
        self.assertIn("_dedup_applies(event)", self.body)
        self.assertLess(
            self.body.find("_dedup_applies(event)"),
            self.body.find("_link_dedup.check"),
            "群聊门禁要在去重判定之前生效",
        )

    def test_forget_called_in_every_failure_path(self):
        """三个 except 分支都要撤销占位，否则失败一次废掉整个窗口。"""
        import ast

        tree = ast.parse(self.src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_url"
        )
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        self.assertEqual(len(handlers), 3, "_process_url 应当正好三个 except 分支")
        missing = []
        for h in handlers:
            body_src = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
            if "_forget_dedup(" not in body_src:
                name = ast.unparse(h.type) if h.type else "?"
                missing.append(name)
        self.assertEqual(
            missing, [],
            f"这些 except 分支没有撤销去重占位：{missing}。"
            "（只统计出现次数不管位置的话，把三处挪进同一个分支也能骗过测试）",
        )

    def test_forget_is_guarded_by_delivered_flag(self):
        """**已经交付出去就别再撤销占位**。

        ``_record_history`` 这类交付之后的收尾步骤失败时撤销占位，会让下一个
        发同链接的人重新解析并再发一张卡片 —— 那正是这个功能要消灭的事。
        """
        self.assertIn("def _forget_dedup(", self.src)
        self.assertIn("not delivered", self.src)
        self.assertIn("delivered = False", self.body, "delivered 没有预置")
        self.assertIn("delivered = True", self.body, "交付后没有置位")

    def test_scope_and_cache_key_preassigned_before_try(self):
        """``scope`` / ``cache_key`` 必须在 try **之前**预置。

        它们在 try 里逐个赋值，而 except 分支要用它们撤销占位。若 try 第一行
        ``result_cache_key(url)`` 就抛了（url 含孤立代理字符时 ``url.encode()``
        会抛 UnicodeEncodeError），except 引用未赋值的 ``scope`` 会再抛
        UnboundLocalError —— 真实错误被掩盖，forget 不执行、报错提示也发不出去。
        """
        import ast

        tree = ast.parse(self.src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_url"
        )
        try_pos = next(
            i for i, n in enumerate(fn.body) if isinstance(n, ast.Try)
        )
        pre = ast.unparse(ast.Module(body=fn.body[:try_pos], type_ignores=[]))
        self.assertIn("cache_key: str | None = None", pre)
        self.assertIn("scope: str | None = None", pre)

    def test_url_read_inside_try(self):
        """``event.message_str.strip()`` 原来在 try 之外，非 str 时会静默逃逸、
        连日志都没有。"""
        import ast

        tree = ast.parse(self.src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_url"
        )
        try_pos = next(
            i for i, n in enumerate(fn.body) if isinstance(n, ast.Try)
        )
        pre = ast.unparse(ast.Module(body=fn.body[:try_pos], type_ignores=[]))
        self.assertNotIn(
            "message_str", pre,
            "取 url 还在 try 之外：message_str 不是 str 时会静默逃逸且无日志",
        )

    def test_cleared_alongside_every_cache_clear(self):
        """4 处清结果缓存的地方（后台清理 / 切目录 / 热更新 / 手动清空）
        都要连带清去重窗口。"""
        self.assertEqual(
            self.src.count("self._link_dedup.clear()"), 4,
            "清缓存时没连带清去重窗口：窗口里锁着后来者会被无故静默",
        )

    def test_hint_text_is_a_real_sentence(self):
        """提示语是用户唯一能看到的东西，不能是占位符或漏字。"""
        import re

        m = re.search(r'_DEDUP_HINT = "([^"]+)"', self.src)
        self.assertTrue(m, "main.py 里找不到 _DEDUP_HINT 的字面量")
        hint = m.group(1)
        self.assertIn("解析过", hint)
        self.assertNotIn("{}", hint)
        self.assertLessEqual(len(hint), 40, "群里发的提示不该是一长串")


if __name__ == "__main__":
    unittest.main()
