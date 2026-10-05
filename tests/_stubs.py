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


class _AnyMeta(type):
    """让占位类支持任意属性访问（``VideoCodecs.AVC``、``QrCodeLoginEvents.SCANNED``…）。"""

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = _AnyMeta(name, (), {})
        setattr(cls, name, value)
        return value

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        return super().__call__()


class _Any(metaclass=_AnyMeta):
    """占位类的实例，同样支持任意属性/方法调用。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _Any()

    def __iter__(self):
        return iter(())


class _StubModule(types.ModuleType):
    """桩模块。

    多数属性给一个「万能占位类」—— 能当类用、能实例化、能取任意属性、能当
    函数调。但**数据形状**必须给真的：解析器会把 ``bilibili_api.HEADERS``
    直接 ``.copy()`` 当请求头用，给个类会直接 AttributeError。
    """

    #: 必须是真数据的名字 → 值
    DATA: dict[str, Any] = {
        "HEADERS": {},
        "DEFAULT_HEADERS": {},
    }

    #: 必须带特定方法的名字
    _RequestSettings = type(
        "_RequestSettings",
        (),
        {
            "set": lambda self, *a, **k: None,
            "set_proxy": lambda self, *a, **k: None,
            "get": lambda self, *a, **k: None,
        },
    )

    SPECIAL: dict[str, Any] = {"request_settings": _RequestSettings}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in type(self).DATA:
            value = type(self).DATA[name]
        elif name in type(self).SPECIAL:
            value = type(self).SPECIAL[name]()
        else:
            value = _AnyMeta(name, (_Any,), {})
        setattr(self, name, value)
        return value


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
