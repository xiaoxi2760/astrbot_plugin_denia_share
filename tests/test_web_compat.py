"""core/web_compat 的行为测试。

这个模块是「4.24.2 ~ 4.26.x 白屏」那类问题的唯一防线，值得钉住它的三条契约：

1. **import 期绝不抛**。它一旦抛，main.py 的 try/except 会把整个 WebUI 注册
   静默跳过，用户看到的是「页面打不开」而不是任何可读的原因。
2. **两条后端路径对外同形**。成功给裸业务对象、失败给 {status,message,data}
   信封；页面与 bridge 全靠这个判据，形状一变就是线上白屏。
3. **非法请求体不炸 handler**。空体/坏 JSON/顶层是 list 都收敛成 {}。

用 stdlib unittest 而非 pytest：本插件的 requirements 里没有测试框架，
而这组断言只依赖一个假 request 对象，不需要 Quart 真的跑起来。
运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import web_compat  # noqa: E402


class FakeQuery:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class _FakeRequestBase:
    """两种宿主的 request 取值方式其实**不兼容**，必须分开建模：

    - astrbot.api.web：``await request.json(default=X)`` —— 方法 + default 形参，
      且 ``default`` 的语义就是「解析失败/空体时返回它，而不是抛」
    - quart：          ``await request.json``           —— async property、无形参，
      坏 JSON 直接抛异常

    早先合成一个假对象时，``json`` 只能是其中一种形态，于是另一条路径会先撞上
    TypeError、再被兼容层里「老版本没有 default 形参」的兜底吞掉——测试照样绿，
    但真正要验的那段代码从未执行过。故此处按后端各给一个。
    """

    def __init__(self, *, query: dict[str, str] | None = None, body=None, raises: bool = False):
        self._query = query or {}
        self._body = body
        self._raises = raises

    def _value(self, default):
        if self._raises:
            raise ValueError("bad json")
        return self._body if self._body is not None else default


class FakeApiWebRequest(_FakeRequestBase):
    """astrbot.api.web 形态（4.27+）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.query = FakeQuery(self._query)

    async def json(self, default=None):
        try:
            return self._value(default)
        except ValueError:
            # default 形参的语义：解析不了就给默认值，而不是把异常抛给 handler
            return default


class FakeLegacyApiWebRequest(FakeApiWebRequest):
    """更早的 astrbot.api.web：``json()`` 没有 default 形参。

    兼容层对它必须靠 TypeError 兜底重试一次，否则读 body 直接 500。
    """

    async def json(self):  # type: ignore[override]
        return self._value(None)


class FakeQuartRequest(_FakeRequestBase):
    """quart 形态。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.args = FakeQuery(self._query)

    @property
    def json(self):
        return _coro(self._value(None))


async def _coro(value):
    return value


#: 后端 → 该后端真实形态的 request 类
REQUEST_SHAPE = {
    "astrbot.api.web": FakeApiWebRequest,
    "quart": FakeQuartRequest,
}


def make_request(backend: str, **kwargs):
    return REQUEST_SHAPE[backend](**kwargs)


def run(coro):
    return asyncio.run(coro)


class ImportContract(unittest.TestCase):
    def test_module_imports_without_any_http_backend(self):
        """当前环境没有 astrbot 也没有 quart，import 仍必须成功。"""
        importlib.reload(web_compat)
        self.assertIsInstance(web_compat.HAS_WEB_API, bool)

    def test_reports_backend(self):
        """断言**具体**后端，而不是"在三个出口里"。

        原先写的是 ``assertIn(_BACKEND, {"astrbot.api.web", "quart", None})`` ——
        这个集合就是代码自己 except 分支的三个出口，实际不可能失败，恒真。

        判定宿主是否可用不能靠 ``importlib.util.find_spec``：测试里的 astrbot 是
        ``types.ModuleType`` 造的桩，没有 ``__spec__``，find_spec 会抛
        ValueError。直接看真实包能不能 import 才准。
        """
        import importlib
        import importlib.util

        def installed(name: str) -> bool:
            if name in sys.modules:
                spec = getattr(sys.modules[name], "__spec__", None)
                return spec is not None  # 桩没有 __spec__，不算真装了
            try:
                return importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                return False

        module = importlib.reload(web_compat)
        has_astrbot, has_quart = installed("astrbot"), installed("quart")
        expected = "astrbot.api.web" if has_astrbot else ("quart" if has_quart else None)
        self.assertEqual(
            module._BACKEND, expected,
            f"环境(astrbot={has_astrbot}, quart={has_quart})对应的后端不对",
        )


class _RequestOverride:
    """临时替换模块级 request 对象与后端标识，退出时精确还原。

    注意 ``_api_request`` 未必存在：astrbot 与 quart 都没有时，两级 import 全部
    落空、该名字根本没被绑定（这正是 ``_BACKEND is None`` 的含义）。
    """

    def __init__(self) -> None:
        self._had_request = hasattr(web_compat, "_api_request")
        self._orig_request = getattr(web_compat, "_api_request", None)
        self._orig_backend = web_compat._BACKEND

    def use(self, request, backend: str | None) -> None:
        web_compat._api_request = request
        web_compat._BACKEND = backend

    def restore(self) -> None:
        if self._had_request:
            web_compat._api_request = self._orig_request
        elif hasattr(web_compat, "_api_request"):
            del web_compat._api_request
        web_compat._BACKEND = self._orig_backend


class JsonBody(unittest.TestCase):
    def setUp(self):
        self._override = _RequestOverride()

    def tearDown(self):
        self._override.restore()

    def _use(self, request, backend):
        self._override.use(request, backend)

    def test_object_body_passes_through(self):
        for backend in REQUEST_SHAPE:
            with self.subTest(backend=backend):
                self._use(make_request(backend, body={"a": 1}), backend)
                self.assertEqual(run(web_compat.get_json_body()), {"a": 1})

    def test_empty_body_becomes_empty_dict(self):
        for backend in REQUEST_SHAPE:
            with self.subTest(backend=backend):
                self._use(make_request(backend, body=None), backend)
                self.assertEqual(run(web_compat.get_json_body()), {})

    def test_non_object_body_is_coerced(self):
        """顶层是 list/标量时必须收敛成 {}：调用点直接 body.get(...)。"""
        for body in ([1, 2], "text", 5, True):
            for backend in REQUEST_SHAPE:
                with self.subTest(body=body, backend=backend):
                    self._use(make_request(backend, body=body), backend)
                    self.assertEqual(run(web_compat.get_json_body()), {})

    def test_broken_json_is_swallowed(self):
        for backend in REQUEST_SHAPE:
            with self.subTest(backend=backend):
                self._use(make_request(backend, raises=True), backend)
                self.assertEqual(run(web_compat.get_json_body()), {})

    def test_missing_backend_raises_clear_error(self):
        self._use(make_request("quart", body={}), None)
        with self.assertRaises(RuntimeError) as ctx:
            run(web_compat.get_json_body())
        self.assertIn("astrbot.api.web", str(ctx.exception))

    def test_legacy_backend_without_default_kwarg(self):
        """老版 astrbot.api.web 的 json() 没有 default 形参：靠 TypeError 兜底重试。"""
        self._use(FakeLegacyApiWebRequest(body={"a": 1}), "astrbot.api.web")
        self.assertEqual(run(web_compat.get_json_body()), {"a": 1})


class QueryArg(unittest.TestCase):
    def setUp(self):
        self._override = _RequestOverride()

    def tearDown(self):
        self._override.restore()

    def _use(self, request, backend):
        self._override.use(request, backend)

    def test_reads_from_query_on_new_backend_and_args_on_quart(self):
        for backend in REQUEST_SHAPE:
            with self.subTest(backend=backend):
                self._use(make_request(backend, query={"keyword": "abc", "limit": "20"}), backend)
                self.assertEqual(web_compat.query_arg("keyword"), "abc")
                self.assertEqual(web_compat.query_arg("limit"), "20")

    def test_missing_key_uses_default(self):
        self._use(make_request("quart", query={}), "quart")
        self.assertEqual(web_compat.query_arg("nope"), "")
        self.assertEqual(web_compat.query_arg("nope", "x"), "x")

    def test_non_string_value_is_stringified(self):
        """astrbot 的 query 值可能是数字，统一 str() 让调用方只管 strip()。"""
        self._use(make_request("astrbot.api.web", query={"offset": 40}), "astrbot.api.web")
        self.assertEqual(web_compat.query_arg("offset"), "40")

    def test_missing_backend_raises_clear_error(self):
        self._use(make_request("quart", query={}), None)
        with self.assertRaises(RuntimeError) as ctx:
            web_compat.query_arg("x")
        self.assertIn("astrbot.api.web", str(ctx.exception))


class ResponseShape(unittest.TestCase):
    """错误信封的形状是前端 catch 分支的判据，必须锁死。"""

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

    def setUp(self):
        self._orig_backend = web_compat._BACKEND
        self._had_jsonify = hasattr(web_compat, "_quart_jsonify")
        self._orig_jsonify = getattr(web_compat, "_quart_jsonify", None)
        self.captured: dict = {}
        web_compat._quart_jsonify = lambda payload: (
            self.captured.update(payload=payload),
            self.FakeResponse(payload),
        )[1]
        web_compat._BACKEND = "quart"

    def tearDown(self):
        web_compat._BACKEND = self._orig_backend
        if self._had_jsonify:
            web_compat._quart_jsonify = self._orig_jsonify
        elif hasattr(web_compat, "_quart_jsonify"):
            del web_compat._quart_jsonify

    def test_error_envelope_shape_on_quart_path(self):
        resp = web_compat.error_response("未知平台", status_code=400)
        self.assertEqual(
            self.captured["payload"],
            {"status": "error", "message": "未知平台", "data": {}},
        )
        self.assertEqual(resp.status_code, 400)

    def test_json_response_passes_bare_payload_on_quart_path(self):
        """成功响应不套信封 —— 页面直接读业务字段，套上信封前端就全拿到 undefined。"""
        payload = {"version": "1.0.0", "platforms": []}
        resp = web_compat.json_response(payload, status_code=200)
        self.assertEqual(self.captured["payload"], payload)
        self.assertNotIn("data", self.captured["payload"])
        self.assertNotIn("status", self.captured["payload"])
        self.assertEqual(resp.status_code, 200)

    def test_missing_backend_raises_instead_of_nameerror(self):
        web_compat._BACKEND = None
        with self.assertRaises(RuntimeError):
            web_compat.json_response({})
        with self.assertRaises(RuntimeError):
            web_compat.error_response("x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
