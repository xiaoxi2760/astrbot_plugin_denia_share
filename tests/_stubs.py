"""import 宿主依赖的最小桩，供 tests/ 复用。

denia_share 的模块顶层会 ``from astrbot.api import logger``、``from bilibili_api import ...``，
而这两样在离线单测环境里都不存在。这里只做「让 import 过」，不实现任何真实行为——
被测的是纯函数与配置契约，不该依赖网络库。

``bilibili_api`` 将来是要从 requirements 里摘掉的（见 1.3.0 计划），届时本文件里
对应的桩可以整段删掉，这也算是给那次迁移留的一个检查点。
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any


class _StubModule(types.ModuleType):
    """属性按需生成的桩模块：``from x import 任意名字`` 都能过。"""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        placeholder = type(name, (), {"__init__": lambda self, *a, **k: None})
        placeholder.__name__ = name
        setattr(self, name, placeholder)
        return placeholder


def install_host_stubs() -> list[str]:
    """装好 astrbot / bilibili_api 的桩。返回被注入的模块名，便于 teardown。"""
    injected: list[str] = []

    def put(name: str, module: types.ModuleType) -> None:
        sys.modules[name] = module
        injected.append(name)

    # ---- astrbot ----
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot.stub")
    star = types.ModuleType("astrbot.api.star")

    class Context:  # noqa: D401 - 桩
        pass

    star.Context = Context
    astrbot.api = api
    put("astrbot", astrbot)
    put("astrbot.api", api)
    put("astrbot.api.star", star)

    # ---- bilibili_api（离线环境没装，插件顶层却会 import）----
    for name in (
        "bilibili_api",
        "bilibili_api.opus",
        "bilibili_api.video",
        "bilibili_api.login_v2",
    ):
        put(name, _StubModule(name))

    return injected


def remove_host_stubs(names: list[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)
