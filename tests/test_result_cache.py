"""解析结果缓存的行为测试（重复解析间隔 + 媒体文件失效检测）。

## 为什么这两件事要一起测

改之前解析结果缓存**没有时间维度**：只靠 FIFO 128 条 + 三处 clear。
于是和磁盘上的媒体文件脱节了 —— ``CACHE_TTL_HOURS``（默认 24 小时）把视频
文件清掉了，内存里的结果还在，于是下一次发同一条链接会：

1. 命中缓存，跳过解析（省了一次请求，看起来「缓存生效了」）；
2. 拿一个**已经不存在的路径**去发视频（``PathTask`` 记着首次下载的路径，
   之后一律直接返回，不查文件在不在）；
3. 卡片正常发出去，**视频静默消失**，日志里什么都没有。

所以「重复解析间隔」这个配置项如果只加不加检测，等于把一个静默丢媒体的坑
做成了用户可配置的。这里把它钉死。

不实例化整个插件：``_remember_result`` / ``_get_cached_result`` /
``_cache_media_gone`` 只用 ``self._result_cache`` 与 ``self._render_cache``，
用一个只带这两个属性的轻量对象就能测，也就不用拉起 downloader / 截图后端。

运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import ast
import asyncio
import sys
import time
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
from astrbot_plugin_denia_share.core.data import (  # noqa: E402
    AudioContent,
    ParseResult,
    VideoContent,
    platform_of,
)
from astrbot_plugin_denia_share.core.task import PathTask  # noqa: E402
from astrbot_plugin_denia_share.core.base_parser import PlatformEnum  # noqa: E402

CFG = init_config({}, PLUGIN_DIR, PLUGIN_DIR)
PLATFORM = platform_of(PlatformEnum.BILIBILI)

MAIN_PY = PLUGIN_DIR / "main.py"
MAIN_SRC = MAIN_PY.read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SRC)

# main.py 本身**导不进来**（tests/_stubs.py 的 astrbot.api 没有 AstrBotConfig），
# 同一原因 test_main_plumbing.py 也只能做 AST 检查。但纯 AST 检查验不了行为 ——
# 「TTL 到了会不会真的重新解析」这种问题，只有把真代码跑一遍才算数。
#
# 所以这里用 AST 定位到那三个方法的源码，编译后放进一个带齐依赖的命名空间里执行。
# 测的是 main.py 里的**真实实现**，不是复制品；改了 main.py 立刻会测出来。
_METHODS = (
    "_remember_result",
    "_cache_media_gone",
    "_get_cached_result",
    "_drop_cache_entry",
)


MAIN_CLASS_NAME = "DeniaSharePlugin"


def _load_cache_methods() -> dict:
    cls = next(
        n for n in MAIN_TREE.body
        if isinstance(n, ast.ClassDef) and n.name == MAIN_CLASS_NAME
    )
    wanted = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in _METHODS
    ]
    found = {n.name for n in wanted}
    missing = set(_METHODS) - found
    if missing:
        raise AssertionError(f"main.py 里找不到这些方法：{missing}")

    module = ast.Module(body=wanted, type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, str(MAIN_PY), "exec")

    from astrbot.api import logger

    ns = {
        "time": time,
        "logger": logger,
        "get_config": get_config,
        "VideoContent": VideoContent,
        "AudioContent": AudioContent,
        "MAX_RESULT_CACHE_ENTRIES": main_max_result_cache_entries(),
    }
    exec(code, ns)  # noqa: S102 —— 目的就是执行 main.py 里的真源码
    return ns


def main_max_result_cache_entries() -> int:
    for node in MAIN_TREE.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "MAX_RESULT_CACHE_ENTRIES":
                    return int(node.value.value)
    return 128


_NS = _load_cache_methods()


class _Fake:
    """只提供被测方法真正用到的那两个属性。

    方法本体是 main.py 里的**真源码**（上面 exec 出来的），挂回这个类当实例
    方法用 —— 这样 ``self._cache_media_gone(...)`` 那种内部调用也是真的在跑。
    """

    def __init__(self):
        self._result_cache = {}
        self._render_cache = {}


for _name in _METHODS:
    setattr(_Fake, _name, _NS[_name])


class _Config:
    """临时改配置用。"""

    def __init__(self, ttl: int):
        self.ttl = ttl

    def __enter__(self):
        self._orig = get_config()._cfg_get
        ttl = self.ttl
        orig = self._orig
        get_config()._cfg_get = lambda key, default=None: (
            ttl if key == "RESULT_CACHE_TTL_SECONDS" else orig(key, default)
        )
        return self

    def __exit__(self, *exc):
        get_config()._cfg_get = self._orig
        return False


def _task_at(path: Path) -> PathTask:
    """造一个「已解析出路径」的 PathTask。

    ``PathTask`` 构造时会 ``asyncio.create_task``，所以只能在事件循环里调
    （本文件的媒体用例都包了 ``asyncio.run``）。之后直接把 ``_path`` 填上，
    模拟「下载已完成」的状态。
    """

    async def _noop():
        return path

    task = PathTask(_noop())
    task._path = path
    return task


def _result_with_video(path: Path | None) -> ParseResult:
    """造一个带视频内容的结果；``path`` 为 None 表示「还没下载出路径」。"""

    async def _noop():
        return path or PLUGIN_DIR / "never-exists.mp4"

    task = PathTask(_noop())
    if path is not None:
        task._path = path
    return ParseResult(
        platform=PLATFORM,
        title="t",
        contents=[VideoContent(task)],
    )


def _result_no_media() -> ParseResult:
    return ParseResult(platform=PLATFORM, title="t", contents=[])


class TestResultCacheTtl(unittest.TestCase):
    def test_fresh_entry_hits(self):
        with _Config(ttl=600):
            fake = _Fake()
            result = _result_no_media()
            fake._remember_result("k", result)
            self.assertIs(fake._get_cached_result("k"), result)

    def test_ttl_zero_always_misses(self):
        """0 = 每次都重新解析（用户主动关掉这个优化）。"""
        with _Config(ttl=0):
            fake = _Fake()
            result = _result_no_media()
            fake._remember_result("k", result)
            self.assertIsNone(fake._get_cached_result("k"))
            # 顺带钉住：失效的条目要**删掉**，不能留着每次都白查一遍
            self.assertNotIn("k", fake._result_cache)

    def test_expired_entry_misses_and_is_dropped(self):
        with _Config(ttl=1):
            fake = _Fake()
            result = _result_no_media()
            fake._remember_result("k", result)
            # 手工把写入时刻拨到 10 秒前，跳过真的 sleep
            fake._result_cache["k"] = (time.monotonic() - 10, result)
            self.assertIsNone(fake._get_cached_result("k"))
            self.assertNotIn("k", fake._result_cache)

    def test_miss_on_absent_key(self):
        with _Config(ttl=600):
            self.assertIsNone(_Fake()._get_cached_result("nope"))


class TestResultCacheMediaGone(unittest.TestCase):
    """缓存命中前必须确认磁盘上的媒体还在。

    ``PathTask`` 构造时就会 ``asyncio.create_task()``，所以这些用例得跑在事件
    循环里 —— 用例本身是同步的，包一层 ``asyncio.run`` 最省事。
    """

    @staticmethod
    def _run(body):
        async def _main():
            return body()

        return asyncio.run(_main())

    def test_existing_media_hits(self):
        with _Config(ttl=600):
            real = PLUGIN_DIR / "requirements.txt"  # 拿一个确实存在的文件当媒体
            self.assertTrue(real.exists())

            def body():
                fake = _Fake()
                result = _result_with_video(real)
                fake._remember_result("k", result)
                return fake._get_cached_result("k") is result

            self.assertTrue(self._run(body))

    def test_deleted_media_forces_reparse(self):
        """**核心回归**：文件被 CACHE_TTL_HOURS 清掉后必须重新解析，
        不能拿一个不存在的路径去发视频。"""
        with _Config(ttl=600):
            gone = PLUGIN_DIR / "definitely-not-here.mp4"
            self.assertFalse(gone.exists())

            def body():
                fake = _Fake()
                fake._remember_result("k", _result_with_video(gone))
                return fake._get_cached_result("k"), "k" in fake._result_cache

            result, still_there = self._run(body)
            self.assertIsNone(result, "文件已清理却还返回缓存 → 视频会发不出来")
            self.assertFalse(still_there, "失效条目要删掉，不能留着每次白查磁盘")

    def test_dropping_result_also_drops_render(self):
        """结果重解析了，卡片也得重画，否则标题/统计可能对不上。"""
        with _Config(ttl=600):
            gone = PLUGIN_DIR / "definitely-not-here.mp4"

            def body():
                fake = _Fake()
                fake._render_cache["k"] = PLUGIN_DIR / "card.png"
                fake._remember_result("k", _result_with_video(gone))
                fake._get_cached_result("k")
                return "k" in fake._render_cache

            self.assertFalse(self._run(body))

    def test_unresolved_task_is_not_treated_as_gone(self):
        """还在下载中的（``resolved`` 为 None）不能据此判定失效。"""
        with _Config(ttl=600):

            def body():
                fake = _Fake()
                result = _result_with_video(None)
                fake._remember_result("k", result)
                return fake._get_cached_result("k") is result

            self.assertTrue(self._run(body))

    def test_audio_content_also_checked(self):
        with _Config(ttl=600):
            gone = PLUGIN_DIR / "definitely-not-here.m4a"

            def body():
                fake = _Fake()
                result = ParseResult(
                    platform=PLATFORM, title="t", contents=[AudioContent(_task_at(gone))]
                )
                fake._remember_result("k", result)
                return fake._get_cached_result("k")

            self.assertIsNone(self._run(body))


class TestResultCacheCapacity(unittest.TestCase):
    def test_fifo_cap(self):
        fake = _Fake()
        cap = main_max_result_cache_entries()
        with _Config(ttl=600):
            for i in range(cap + 10):
                fake._remember_result(f"k{i}", _result_no_media())
        self.assertEqual(len(fake._result_cache), cap)
        # 最早写的那些被挤掉了，最新的还在
        self.assertNotIn("k0", fake._result_cache)
        self.assertIn(f"k{cap + 9}", fake._result_cache)


class TestConfigWiring(unittest.TestCase):
    def test_key_exists_in_config_meta(self):
        self.assertIn("RESULT_CACHE_TTL_SECONDS", [m["key"] for m in CONFIG_META])

    def test_property_reads_config(self):
        with _Config(ttl=123):
            self.assertEqual(get_config().RESULT_CACHE_TTL_SECONDS, 123)

    def test_default_is_ten_minutes(self):
        meta = next(m for m in CONFIG_META if m["key"] == "RESULT_CACHE_TTL_SECONDS")
        self.assertEqual(meta["default"], 600)
        self.assertEqual(meta["unit"], "秒")
        # 0 必须合法（关掉这个优化）
        self.assertEqual(meta["min"], 0)

    def test_schema_and_meta_agree(self):
        """schema 与 CONFIG_META 必须逐项一致（test_upgrade_path 也会查，这里再钉一次）。"""
        import json

        schema = json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )
        for meta in CONFIG_META:
            if meta["key"] != "RESULT_CACHE_TTL_SECONDS":
                continue
            item = schema["维护"]["items"][meta["key"]]
            self.assertEqual(item["type"], meta["type"])
            self.assertEqual(item["default"], meta["default"])
            return
        self.fail("schema 里没有 RESULT_CACHE_TTL_SECONDS")


class TestMediaSendGuardWiring(unittest.TestCase):
    """``_try_send_media`` 里的 path.exists() 兜底（AST 层面）。

    正常情况下 ``_cache_media_gone`` 已经拦住了，这里是**双保险**：清理任务
    可能正好在缓存检查之后、发送之前把文件删掉。
    """

    @classmethod
    def setUpClass(cls):
        tree = ast.parse((PLUGIN_DIR / "main.py").read_text(encoding="utf-8"))
        cls.func = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_try_send_media"
        )

    def test_checks_path_exists_before_sending(self):
        src = ast.unparse(self.func)
        self.assertIn(
            "path.exists()",
            src,
            "发送前必须检查 path.exists()，否则清理任务抢跑时会发不出媒体且无日志",
        )

    def test_result_cache_cleared_on_runtime_config_change(self):
        """改 B站编码/清晰度等配置后必须清解析结果缓存。

        不清的话用户在配置页把编码从 AV1 换成 H.264，再发同一条链接拿到的
        还是旧结果 —— 配置改了看不出效果。
        """
        tree = ast.parse((PLUGIN_DIR / "main.py").read_text(encoding="utf-8"))
        apply_fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "apply_runtime_config"
        )
        src = ast.unparse(apply_fn)
        self.assertIn("_result_cache.clear()", src)
        self.assertIn("_render_cache.clear()", src)


class TestAllCacheReadersGoThroughAccessor(unittest.TestCase):
    """**回归钉子**：缓存值形状是 ``(写入时刻, ParseResult)`` 元组。

    任何直接 ``_result_cache.get(key)`` 的地方都会拿到元组并当成 ParseResult
    往下传 —— 在 WebUI「按当前外观重渲染」那条路上就是崩。这个漏过一次
    （改了 main.py 的缓存形状，忘了 webui.py 也直接读），169 条测试全绿
    也没抓到，因为那条路径没有测试覆盖。

    所以这里用 AST 静态扫：**除了 main.py 里那一个 accessor 自身，
    全仓不允许再有直接读 ``_result_cache`` 的地方。**
    """

    def _accessor_lines(self) -> set[int]:
        """accessor 与其辅助方法的行号集合 —— 这些行里的直接读取是合法的。"""
        cls = next(
            n for n in MAIN_TREE.body
            if isinstance(n, ast.ClassDef) and n.name == MAIN_CLASS_NAME
        )
        lines: set[int] = set()
        for n in cls.body:
            if isinstance(n, ast.FunctionDef) and n.name in _METHODS:
                lines |= set(range(n.lineno, n.end_lineno + 1))
        return lines

    #: ``x._result_cache.<方法>()`` 里，这些方法**不把元素交出去**，可以放行
    #: （.get 拿元素、.pop 删元素，所以它们归 accessor 用，不归这里）
    _SAFE_METHODS = {"clear", "keys", "values", "items"}
    #: ``len(x)`` / ``list(x)`` 之类：只取长度或视图，同样不把元素交出去
    _SAFE_FUNCS = {"len", "bool", "list", "dict", "set", "iter", "any", "all"}

    @classmethod
    def _classify(cls, node, parent) -> str:
        """把一个 ``_result_cache`` 属性访问归类。

        AST 形态速查（写错过一次，记下来）::

            self._result_cache.get(k)   -> Call(func=Attribute(value=.._result_cache, attr='get'))
            len(self._result_cache)     -> Call(func=Name('len'), args=[.._result_cache])
            self._result_cache[k]        -> Subscript(value=.._result_cache, slice=Name('k'))
            self._result_cache: T = {}   -> AnnAssign(target=.._result_cache)

        也就是说 ``.get``/``.setdefault`` 的**父节点是 Attribute**，
        而 ``len()`` 的父节点是 Call(func=Name) —— 两种形状都得认。
        """
        # 写：AnnAssign / Assign 的目标，或任何 Store 上下文
        if isinstance(getattr(node, "ctx", None), ast.Store):
            return "assign"
        if parent is None:
            return "unknown"
        if isinstance(parent, ast.Subscript):
            # x._result_cache[k] = v 是写；x._result_cache[k] 是读
            return "assign" if isinstance(parent.ctx, ast.Store) else "unsafe-subscript"
        if isinstance(parent, ast.Attribute):
            return "safe" if parent.attr in cls._SAFE_METHODS else "unsafe-method"
        if isinstance(parent, ast.Call):
            if isinstance(parent.func, ast.Name) and parent.func.id in cls._SAFE_FUNCS:
                return "safe"
            return "unsafe-bare"
        return "unsafe-bare"

    def _scan(self) -> list[str]:
        accessor_lines = self._accessor_lines()
        this_file = Path(__file__).name
        offenders: list[str] = []
        for path in PLUGIN_DIR.rglob("*.py"):
            if "fixtures" in path.parts or "__pycache__" in path.parts:
                continue
            # 本文件自身要排除：它构造假的 _result_cache 来驱动 accessor，
            # 那些 `fake._result_cache` 是测试夹具而不是「绕过 accessor 的读取」。
            # 排除前先确认除它之外没有别的 offenders，否则会把真问题一起藏掉。
            if path.name == this_file:
                continue
            rel = path.relative_to(PLUGIN_DIR).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                # 解析失败就跳过等于整文件免检 —— 宁可报出来
                offenders.append(f"{rel}: 解析失败，检查被跳过（{exc.msg}）")
                continue
            parents: dict[int, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[id(child)] = node
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr != "_result_cache":
                    continue
                if rel == "main.py" and node.lineno in accessor_lines:
                    continue
                kind = self._classify(node, parents.get(id(node)))
                if kind in ("safe", "assign"):
                    continue
                offenders.append(f"{rel}:{node.lineno} {kind}")
        return offenders

    def test_no_direct_dict_reading_outside_accessor(self):
        """B 复核实测：原谓词只拦 ``.get(`` 和「slice 是 Attribute」的下标，
        于是 ``x[cache_key]``（slice 是 ``Name``）、``.setdefault``、``.values()``
        全漏。而这条扫描是**唯一的跨文件回归防线**（调用点本身零测试覆盖），
        漏了就等于防线不存在。

        改成**白名单式**：任何 ``_result_cache`` 属性访问，只要不在 accessor 行
        范围内、又不是明确的读方法、也不是赋值，就报错。
        """
        self.assertEqual(
            self._scan(), [],
            "缓存值是 (时刻, 结果) 元组，这些地方会拿到元组当 ParseResult 用",
        )

    def test_scanner_catches_the_natural_rewrite_forms(self):
        """**反向验证扫描本身有效**。

        原实现对下面第一、四种写法实测漏报 —— 也就是说「加了扫描」这件事
        本身从来没被验证过，扫描漏了也没人知道。
        """
        cases = {
            "return self._result_cache[k]": "unsafe-subscript",
            "return self._result_cache.setdefault(k, v)": "unsafe-method",
            "return self._result_cache.values()": "safe",
            "return self._result_cache": "unsafe-bare",
            "return self._result_cache.get(k)": "unsafe-method",
            "return len(self._result_cache)": "safe",
            "self._result_cache.clear()\n    return None": "safe",
            "self._result_cache: dict = {}\n    return None": "assign",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                tree = ast.parse(f"def f(self):\n    {source}\n")
                node = next(
                    n for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute) and n.attr == "_result_cache"
                )
                parents = {}
                for n in ast.walk(tree):
                    for child in ast.iter_child_nodes(n):
                        parents[id(child)] = n
                self.assertEqual(
                    self._classify(node, parents.get(id(node))), expected,
                    f"{source!r} 被判成了 {self._classify(node, parents.get(id(node)))}",
                )

    def test_scan_is_not_vacuous_on_this_file(self):
        """排除自身后扫描不能变成「什么都不看」—— 拿本文件的夹具验证一下。

        上一版扫描器实测对 ``x[cache_key]``、``.setdefault``、``.values()``
        全部漏报，却一直「通过」。一个从没被证伪过的检查等于没有检查。
        """
        self_file = Path(__file__).name
        raw = self_file and (PLUGIN_DIR / "tests" / self_file).read_text(encoding="utf-8")
        tree = ast.parse(raw)
        parents = {}
        for n in ast.walk(tree):
            for child in ast.iter_child_nodes(n):
                parents[id(child)] = n
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_result_cache":
                found.append(self._classify(node, parents.get(id(node))))
        # 本文件里既有夹具访问（unsafe-*），也有长度断言（safe）
        self.assertTrue(
            any(k.startswith("unsafe") for k in found),
            "本文件自己都扫不出 unsafe 用例，扫描逻辑肯定坏了",
        )
        self.assertTrue(
            any(k == "safe" for k in found),
            "本文件里应当存在被放行的安全用法（len/clear），否则白名单形同虚设",
        )

    def test_webui_render_cached_uses_accessor(self):
        src = (PLUGIN_DIR / "core" / "webui.py").read_text(encoding="utf-8")
        # 不用 assertIn：它失败时会把整个 webui.py 打进输出（几万字），淹掉真正的错
        self.assertTrue(
            "_get_cached_result(cache_key)" in src,
            "webui.render_cached 必须走 _get_cached_result —— 直接读 _result_cache "
            "会拿到元组，且「已过期」的 410 永远不触发",
        )


if __name__ == "__main__":
    unittest.main()
