"""main.py 的接线测试（AST 层面）。

**为什么单独给 main.py 写测试**：这批修复里有两处的「本体」在 main.py 里，
而它此前**零覆盖**：

1. 整页截图的 ``full_page=`` 传参 —— 删掉它，``capture()`` 收回默认 False，
   整页功能立刻退回 1.2.x 的死代码状态，而 tests/ 下 80 多个用例**全绿**。
   tests/test_screenshot.py 只能验 ``capture()`` 的签名和 ``_capture_thum``
   的内部，验不到调用方。
2. ``_screenshot_caption`` —— 整条说明文本链路，一次都没被测过。

**为什么用 AST 而不是 ``inspect.getsource`` + 字符串匹配**：字符串匹配会被注释
和文档字符串里的示例骗过去（``to_api_url`` 的警告文档里就写着反面示例，
字符串搜曾因此误报过一次）。AST 看的是真正会被执行的 Call 节点。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

MAIN_PY = PLUGIN_DIR / "main.py"
TREE = ast.parse(MAIN_PY.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str | None:
    """取出被调用者的名字，``foo()`` 和 ``self.foo()`` 都算。"""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _calls(name: str) -> list[ast.Call]:
    """所有真正会被执行的 ``name(...)`` 调用。

    同时匹配 ``foo()`` 与 ``self.foo()`` / ``p.capture()`` —— 解析器里绝大多数
    是后者，只认 Name 会把「找不到调用」误报成「接线缺失」。
    """
    return [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and _call_name(n) == name]


def _method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


class ScreenshotPlumbing(unittest.TestCase):
    def test_capture_is_called_with_full_page_keyword(self):
        """回归钉子：``capture()`` 必须带 ``full_page=`` 关键字。

        没有它 = 整页截图退回死代码，而其它测试察觉不到。
        """
        calls = _calls("capture")
        self.assertTrue(calls, "main.py 里找不到 capture(...) 调用")
        for call in calls:
            kws = {kw.arg for kw in call.keywords if kw.arg}
            self.assertIn(
                "full_page",
                kws,
                f"main.py:{call.lineno} 的 capture() 没传 full_page —— 整页功能会退回死代码",
            )

    def test_full_page_flag_comes_from_config(self):
        """``full_page`` 的值必须来自配置项，而不是硬编码 True/False。"""
        src = MAIN_PY.read_text(encoding="utf-8")
        self.assertIn("SCREENSHOT_FULL_PAGE", src, "调用点没读配置项")
        for call in _calls("capture"):
            kw = next(k for k in call.keywords if k.arg == "full_page")
            self.assertIsInstance(
                kw.value, ast.Name, "full_page 的实参应是一个变量（由配置读出）"
            )

    def test_caption_is_called_from_do_screenshot(self):
        body = _method("_do_screenshot")
        self.assertIsNotNone(body, "找不到 _do_screenshot")
        called = {_call_name(n) for n in ast.walk(body) if isinstance(n, ast.Call)}
        called.discard(None)
        self.assertIn(
            "_screenshot_caption", called,
            "_do_screenshot 没调 _screenshot_caption —— 截图没有文字说明",
        )
        self.assertIn("_send_image", called, "_do_screenshot 没调 _send_image")

    def test_screenshot_never_goes_through_card_renderer(self):
        """截图必须走 ``_send_image`` 发原图，**不能**进卡片渲染器。

        整页长图塞进 16:9 的封面槽会被裁到只剩约 15%（等于废掉），开「封面不裁切」
        又会撑出一张两米高的卡片 —— 两种都不可用。这条钉住这个约束。
        """
        body = _method("_do_screenshot")
        self.assertIsNotNone(body)
        called = {_call_name(n) for n in ast.walk(body) if isinstance(n, ast.Call)}
        called.discard(None)
        self.assertNotIn("render", called, "_do_screenshot 里出现了 render，截图被塞进卡片了")
        self.assertNotIn(
            "_build_nodes_result", called, "_do_screenshot 走了卡片节点渲染路径"
        )


class CodecPlumbing(unittest.TestCase):
    """B站编码开关的接线在 ``core/parsers/bilibili.py``，不在 main.py。"""

    BILI = PLUGIN_DIR / "core" / "parsers" / "bilibili.py"
    BILI_TREE = ast.parse(BILI.read_text(encoding="utf-8"))

    def test_bilibili_calls_codec_order(self):
        """取流必须走 ``_codec_order``，不能回退到硬编码列表。"""
        calls = [
            n
            for n in ast.walk(self.BILI_TREE)
            if isinstance(n, ast.Call) and _call_name(n) == "_codec_order"
        ]
        self.assertTrue(calls, "bilibili.py 里找不到 _codec_order(...) 调用")
        for call in calls:
            # 第二个位置参数应是配置项读出来的偏好
            self.assertGreaterEqual(
                len(call.args), 2, f"L{call.lineno} _codec_order 少传了配置项参数"
            )

    def test_no_hardcoded_codec_list_in_bilibili(self):
        """bilibili.py 里不应再出现裸的 [VideoCodecs.…] 列表。"""
        for node in ast.walk(self.BILI_TREE):
            if not isinstance(node, ast.List):
                continue
            elements = [e for e in node.elts if isinstance(e, ast.Attribute)]
            if any(e.attr in {"AV1", "AVC", "HEV"} for e in elements):
                self.fail(
                    f"bilibili.py:{node.lineno} 仍有硬编码 codec 列表，"
                    "应改用 _codec_order()"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
