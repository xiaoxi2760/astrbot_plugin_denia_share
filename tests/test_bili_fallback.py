"""bilibili-api-python 缺失时的隔离测试。

背景：``core/parsers/bilibili.py`` 顶层就 ``from bilibili_api import ...``。
原先这条链是**无保护**的：依赖一挂，异常顺着
``.bilibili → core/parsers/__init__.py → main.py`` 一路冒泡，
让抖音 / 快手 / 微博 / 小红书 / Twitter / NGA / AcFun / GitHub / Pixiv / Steam
这 10 个与 B站毫无关系的平台**一起起不来** —— 一个边缘平台的可选依赖
拥有整个插件的爆炸半径。

本组用例钉住修复后的**两级**契约：

1. ``core.parsers`` 在 bilibili_api 缺失时**仍能 import 成功**。
2. **顶替的是自建解析器**（``FallbackBilibiliParser``），不是 ``None`` ——
   依赖没了不等于 B站没了，视频仍应能解析下载。
3. 自建解析器**整条 import 链里不含 bilibili_api** —— 它存在的意义就是顶替那个
   依赖，要是自己又偷偷 import 回来，在真正的关键时刻会直接 import 失败。
4. ``BILIBILI_IMPORT_ERROR`` 留住真实异常，日志才说得清是「装不上」还是「版本不对」。
5. ``BILIBILI_IS_FALLBACK`` 标志让 main.py 能对用户说清「现在是什么模式」。
6. 其余 10 个解析器一个不少。
7. 依赖正常时走的是**主力**解析器（防止兜底写成「永远降级」）。

必须在**子进程**里跑：屏蔽一个 import 需要干净的 ``sys.modules`` 与
``meta_path`` 状态，进程内改完就污染了后续用例。
运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
PKG_PARENT = PLUGIN_ROOT.parent

# 被探测的解析器名（除 bilibili 外的十个）
OTHER_PARSERS = [
    "DouyinParser",
    "KuaiShouParser",
    "WeiBoParser",
    "XiaoHongShuParser",
    "TwitterParser",
    "NGAParser",
    "AcfunParser",
    "GitHubParser",
    "PixivParser",
    "SteamParser",
]

_CHILD_TEMPLATE = r'''
import sys

sys.path.insert(0, __PKG__)
sys.path.insert(0, __TESTS__)
from _stubs import install_host_stubs
install_host_stubs()

__BLOCK__

import astrbot_plugin_denia_share.core.parsers as P

others = [n for n in P.__all__ if not n.startswith("BILIBILI_") and n != "BilibiliParser"]
missing = [n for n in others if getattr(P, n, None) is None]

# main.py 的那行 import 必须照样成立
try:
    from astrbot_plugin_denia_share.core.parsers import BilibiliParser as B
    named_ok = "BilibiliParser" in P.__all__
except Exception as exc:
    named_ok = "IMPORT_FAIL:" + type(exc).__name__

# 自建替补整条 import 链里不允许 import bilibili_api。
# 用 AST 而不是字符串查找：模块文档字符串里会正常提到 ``bilibili_api`` 这个名字
# （说明「顶替的就是它」），那是散文不是依赖，字符串匹配会误报。
parser_mod = getattr(P.BilibiliParser, "__module__", "") if P.BilibiliParser else ""
chain_ok = None
chain_detail = ""
if P.BilibiliParser is not None:
    import ast
    import importlib
    import inspect

    mod = importlib.import_module(parser_mod)
    offenders = []
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "bilibili_api":
                    offenders.append("import " + alias.name)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "bilibili_api":
                offenders.append("from " + str(node.module) + " import ...")
    chain_ok = not offenders
    chain_detail = "; ".join(offenders)

print("RESULT " + repr({
    "bili_is_none": P.BilibiliParser is None,
    "is_fallback": bool(P.BILIBILI_IS_FALLBACK),
    "parser_name": getattr(P.BilibiliParser, "__name__", None),
    "parser_module": parser_mod,
    "chain_clean": chain_ok,
    "chain_detail": chain_detail,
    "err_type": type(P.BILIBILI_IMPORT_ERROR).__name__,
    "err_msg": str(P.BILIBILI_IMPORT_ERROR),
    "missing": missing,
    "named_ok": named_ok,
    "other_count": len(others),
}))
'''

# 用 meta_path 钩子把 bilibili_api 打成「没装」，比从 sys.modules 里删干净得多：
# sys.modules 里删条目挡不住子模块的再次导入，钩子则在源头拒绝。
_BLOCK_CODE = '''
import importlib.abc

class _BlockBilibili(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "bilibili_api" or fullname.startswith("bilibili_api."):
            raise ImportError("No module named %r (simulated)" % fullname)
        return None

for _name in [k for k in list(sys.modules)
              if k == "bilibili_api" or k.startswith("bilibili_api.")]:
    del sys.modules[_name]
sys.meta_path.insert(0, _BlockBilibili())
'''


def _run_child(block: bool) -> dict:
    """在子进程里导入 core.parsers，返回探测结果字典。"""
    # 用显式占位符而不是 str.format：子进程代码里有 dict / set 推导的花括号，
    # format 会把它们当占位符，KeyError 起来很难看。
    code = (
        _CHILD_TEMPLATE
        .replace("__PKG__", repr(str(PKG_PARENT)))
        .replace("__TESTS__", repr(str(TESTS_DIR)))
        .replace(
            "__BLOCK__",
            _BLOCK_CODE if block else "# 不屏蔽：依赖正常",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PLUGIN_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"子进程退出码 {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return eval(line[len("RESULT "):])  # noqa: S307 —— 自己拼的字面量
    raise AssertionError(f"子进程没有输出 RESULT 行\nstdout:\n{proc.stdout}")


class TestBilibiliDependencyIsolation(unittest.TestCase):
    """bilibili_api 的存在与否，不得影响其余十个平台。"""

    def test_missing_dependency_keeps_other_parsers_importable(self):
        result = _run_child(block=True)
        self.assertEqual(
            result["missing"],
            [],
            f"以下解析器在 bilibili_api 缺失时不可用：{result['missing']}",
        )
        self.assertEqual(result["other_count"], len(OTHER_PARSERS))

    def test_missing_dependency_swaps_in_selfbuilt_parser(self):
        """**依赖没了不等于 B站没了。** 顶上自建解析器，视频仍可下载。"""
        result = _run_child(block=True)
        self.assertFalse(
            result["bili_is_none"],
            "缺依赖时 BilibiliParser 应为自建替补，而不是 None（那样等于 B站整个没）",
        )
        self.assertTrue(result["is_fallback"], "BILIBILI_IS_FALLBACK 应为 True")
        self.assertEqual(result["parser_name"], "FallbackBilibiliParser")
        self.assertIn("bili_fallback", result["parser_module"])

    def test_selfbuilt_parser_does_not_import_bilibili_api(self):
        """替补的 import 语句里若出现 bilibili_api，在真正的关键时刻会失败。"""
        result = _run_child(block=True)
        self.assertTrue(
            result["chain_clean"],
            "自建替补存在对 bilibili_api 的 import："
            + result["chain_detail"]
            + " —— 它顶替的就是这个依赖，不可能自己又依赖回去",
        )

    def test_error_detail_is_preserved_for_logging(self):
        """日志要能区分「装不上」和「版本不兼容」，所以异常得留住。"""
        result = _run_child(block=True)
        self.assertEqual(result["err_type"], "ImportError")
        self.assertIn("bilibili_api", result["err_msg"])

    def test_exported_name_survives_so_main_py_import_works(self):
        """main.py:62 的 from ... import BilibiliParser 必须照样成立。"""
        result = _run_child(block=True)
        self.assertEqual(
            result["named_ok"],
            True,
            "导出名必须保留，否则 main.py 的 import 会连带失败",
        )

    def test_healthy_dependency_uses_primary_parser(self):
        """反向钉子：依赖正常时必须走主力，不能被兜底顶掉。"""
        result = _run_child(block=False)
        self.assertFalse(result["bili_is_none"])
        self.assertFalse(
            result["is_fallback"],
            "bilibili_api 可用时不该切自建备用路径 —— 兜底写死了",
        )
        self.assertEqual(result["parser_name"], "BilibiliParser")
        self.assertEqual(result["parser_module"].split(".")[-1], "bilibili")
        self.assertEqual(result["err_type"], "NoneType")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["other_count"], len(OTHER_PARSERS))


if __name__ == "__main__":
    unittest.main()
