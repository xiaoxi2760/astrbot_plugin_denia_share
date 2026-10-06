"""同群重复链接去重的**端到端行为**测试：真的跑一遍 main.py 的 _process_url。

## 为什么要有这个文件

``tests/test_link_dedup.py`` 有 31 条，但它证明不了运行时行为：

- 那 31 条里大部分直接测 :class:`core.link_dedup.LinkDedup` 这个纯判定类 —— 它不碰
  网络、不碰事件、也不知道 ``main.py`` 会不会把 ``HINT`` 真的发出去；
- ``main.py`` 那部分只有 **AST 断言**（去重判定排在缓存查询之前、三个 ``except``
  都调了撤销占位、``clear()`` 的四处位置）。AST 只能证明「代码写对了顺序」，
  证明不了「跑起来行为对」：``yield`` 出去的消息被谁消费？三个调用方能不能正确
  处理「只 yield 一条就 return」？第一张卡片真的发出来了吗？撤销占位是不是
  真的让下一个人能重试？这些都得跑。

## 怎么在不联网、不碰 AstrBot 的前提下跑真代码

``main.py`` 在测试环境里导不进来：第 29 行 ``from astrbot.api import logger,
AstrBotConfig``，而 ``tests/_stubs.py`` 装的 ``astrbot.api`` 桩没有
``AstrBotConfig``（也没有 ``astrbot.api.event`` / ``message_components``）。

约束是不许改 ``_stubs.py`` 也不许改 ``main.py``，所以这里在**导入 main.py 之前**
把桩补齐（见下面 ``_extend_astrbot_stub``），然后正常 import：

- **真代码**：``_process_url`` 本体、去重判定（``LinkDedup`` 实例 + ``HINT``/
  ``SILENT`` 分支）、``_dedup_applies`` / ``_event_scope`` / ``_dedup_window`` /
  ``_forget_dedup``、``_get_cached_result`` / ``_remember_result``、
  ``_deliver`` → ``_build_output`` → ``_send_plain_output`` → ``_try_send_media``
  的整条交付链、``_record_history``、``_access_denied``、以及三个调用方
  （``_dispatch`` / ``custom_parser_handler`` / ``json_card_handler``）。全部是
  ``main.py`` 与 ``core/*`` 里的原始实现，改了生产代码这里立刻会红。
- **替身**：只有 ``event``（消息事件）、``parser``（平台解析器）、卡片渲染器
  （关掉，免得拉 Pillow 画图）、解析记录落盘对象、``StarTools``。这些都是 IO 边界，
  换掉不改变「去重消息序列」这个被测行为。
- **模块只有一份**：走的是普通包 import（不是 exec/重新造命名空间），所以
  ``main.LinkDedup is core.link_dedup.LinkDedup`` 成立，不会出现两套
  ``astrbot_plugin_denia_share.*`` 实例。

不实例化整个插件（``DeniaSharePlugin.__init__`` 会建 downloader、读配置目录、
连 AstrBot 数据目录），而是用 ``object.__new__`` 造一个只填了 ``_process_url``
真正用到的那几个属性的实例 —— ``__init__`` 里 ``self._link_dedup`` 的接线方式
（``LinkDedup(lambda: self._dedup_window())``）在这里原样复刻。

## 读这些用例前必须知道的一件事

**结果缓存的键只有 URL、没有会话，是全局的**（``result_cache_key`` 是静态方法），
而去重的键是 ``会话|URL``。两者都开着时，「后面的人没触发解析」可能只是命中了缓存、
照旧拿到了卡片。所以：

- 想断言「真的重新解析了」，用例里必须把 ``RESULT_CACHE_TTL_SECONDS`` 设成 0；
- 想断言「同一个人又拿到一张卡片」（而不是被去重），用默认的 600。

``_cfg(window=..., cache_ttl=..., send_errors=...)`` 就是干这个的，每条用例都显式写明。

运行：``py -3 -m unittest tests.test_link_dedup_e2e -v``（在插件根目录）
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()


# ============ 补齐 astrbot.api 桩的缺口（只加不改，不动 _stubs.py） ============

def _identity(fn):
    """装饰器装饰器：原样返回被装饰的函数。

    真的 AstrBot 里 ``@filter.regex(...)`` 会把函数包成带元数据的 handler；
    这里必须保持「返回原函数」，否则 ``main.py`` 里那些 handler 就不是普通的
    async 生成器函数，后面的调用方用例（真调 ``_dispatch`` / 处理器）没法跑。
    """
    return fn


class _FilterStub:
    """``astrbot.api.event.filter`` 的最小替身。"""

    class PermissionType:
        ADMIN = "ADMIN"
        MEMBER = "MEMBER"

    regex = staticmethod(lambda *a, **k: _identity)
    command = staticmethod(lambda *a, **k: _identity)
    permission_type = staticmethod(lambda *a, **k: _identity)
    event_message_type = staticmethod(lambda *a, **k: _identity)


class _Plain:
    def __init__(self, text: str = ""):
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class _Image:
    def __init__(self, path: str | None = None, url: str | None = None):
        self.path = path
        self.url = url

    @classmethod
    def fromFileSystem(cls, path: str) -> "_Image":
        return cls(path=path)

    @classmethod
    def fromURL(cls, url: str) -> "_Image":
        return cls(url=url)

    def __repr__(self) -> str:
        return f"Image({self.path or self.url})"


class _Video(_Image):
    pass


class _Record:
    def __init__(self, file: str | None = None):
        self.file = file

    def __repr__(self) -> str:
        return f"Record({self.file})"


class _Json:
    def __init__(self, data: Any = None):
        self.data = data


class _Nodes:
    def __init__(self, nodes: list | None = None):
        self.nodes = list(nodes or [])


class _Node:
    def __init__(self, content=None, **_):
        self.content = content


class _MessageChain:
    def file_image(self, path: str):
        return ("file_image", path)

    def message(self, text: str):
        return ("message", text)


def _extend_astrbot_stub() -> None:
    """把 main.py 顶层 import 需要、但 ``_stubs.py`` 没装的几个名字补上。

    只往**已有的桩模块对象上加属性**，不替换模块本身 —— 别的测试模块也用同一份
    桩，替换掉会波及它们。
    """
    import astrbot.api as api
    import astrbot.api.star as star

    # main.py 第 29 行：``from astrbot.api import logger, AstrBotConfig``。
    # AstrBotConfig 只用在 __init__ 的类型注解上，本文件不构造插件实例，用不到。
    if not hasattr(api, "AstrBotConfig"):
        class AstrBotConfig:  # noqa: D401 - 桩
            """桩：main.py 只拿它当类型注解用。"""

        api.AstrBotConfig = AstrBotConfig

    # main.py 第 32 行：``from astrbot.api.star import Context, Star, register, StarTools``。
    # ``Star`` 是 ``DeniaSharePlugin`` 的基类，必须是真类（class 语句会拿它当基类求值）。
    if not hasattr(star, "Star"):
        class Star:
            def __init__(self, context: Any = None):
                self.context = context

        star.Star = Star

    if not hasattr(star, "register"):
        def register(*_args, **_kwargs):
            def _deco(cls):
                return cls

            return _deco

        star.register = register

    if not hasattr(star, "StarTools"):
        class StarTools:
            @staticmethod
            def get_data_dir(name: str):
                # 只在 __init__ 里用；本文件不跑 __init__，真被调到就说明测试写错了
                raise AssertionError("本测试不该走 StarTools（说明 __init__ 被跑了）")

        star.StarTools = StarTools

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _FilterStub
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.MessageEventResult = type("MessageEventResult", (), {})
    event_mod.MessageChain = _MessageChain
    sys.modules.setdefault("astrbot.api.event", event_mod)
    api.event = sys.modules["astrbot.api.event"]

    comp = types.ModuleType("astrbot.api.message_components")
    for _name, _obj in (
        ("Plain", _Plain), ("Image", _Image), ("Video", _Video),
        ("Record", _Record), ("Json", _Json), ("Nodes", _Nodes), ("Node", _Node),
    ):
        setattr(comp, _name, _obj)
    sys.modules.setdefault("astrbot.api.message_components", comp)
    api.message_components = sys.modules["astrbot.api.message_components"]


_extend_astrbot_stub()

# 配置单例：core.config 的 get_config() 依赖它（与其它测试模块同一个）
from astrbot_plugin_denia_share.core.config import get_config, init_config  # noqa: E402
from astrbot_plugin_denia_share.core.constants import (  # noqa: E402
    register_platform,
    unregister_platform,
)
from astrbot_plugin_denia_share.core.data import ParseResult, platform_of  # noqa: E402
from astrbot_plugin_denia_share.core.exception import (  # noqa: E402
    ParseException,
    SilentException,
)
from astrbot_plugin_denia_share.core.base_parser import PlatformEnum  # noqa: E402

init_config({}, PLUGIN_DIR, PLUGIN_DIR)

# 到这里才 import main.py —— 必须放在补桩之后
import astrbot_plugin_denia_share.main as plugin_main  # noqa: E402
from astrbot_plugin_denia_share.core import link_dedup as link_dedup_mod  # noqa: E402

DeniaSharePlugin = plugin_main.DeniaSharePlugin

PLATFORM = platform_of(PlatformEnum.BILIBILI)

GROUP_A = "OneBot:FriendMessage:group_a"
GROUP_B = "OneBot:FriendMessage:group_b"
URL = "https://www.bilibili.com/video/BV1GJ411x7h7"
OTHER_URL = "https://www.douyin.com/video/123"
TITLE = "端到端测试用标题"
HINT = DeniaSharePlugin._DEDUP_HINT


# ============ 配置临时改写（不碰 _conf_schema.json，也不写配置文件） ============

class _Cfg:
    """在用例期间覆盖几个配置项。

    ``ParserConfig`` 是普通属性读配置（没有 setter），所以只能换掉实例上的
    ``_cfg_get``。退出时还原，避免污染同进程里其它测试模块。
    """

    def __init__(self, **values: Any):
        self._values = values

    def __enter__(self) -> "_Cfg":
        self._cfg = get_config()
        self._orig = self._cfg._cfg_get
        orig, values = self._orig, self._values

        def _get(key: str, default: Any = None) -> Any:
            if key in values:
                return values[key]
            return orig(key, default)

        self._cfg._cfg_get = _get
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._cfg._cfg_get = self._orig
        return False


def _cfg(window: int = 60, cache_ttl: int = 600, send_errors: bool = False) -> _Cfg:
    """去重窗口 / 结果缓存 TTL / 是否发错误提示。

    默认窗口 60（生产默认），结果缓存 TTL 600（同样生产默认）。
    """
    return _Cfg(
        DUPLICATE_LINK_WINDOW_SECONDS=window,
        RESULT_CACHE_TTL_SECONDS=cache_ttl,
        SEND_ERROR_MESSAGES=send_errors,
    )


# ============ 替身：只在 IO 边界 ============

class _Out:
    """``event.plain_result()`` / ``event.chain_result()`` 的产物。"""

    def __init__(self, kind: str, payload: Any):
        self.kind = kind
        self.payload = payload

    def text(self) -> str:
        """把这个产物拍平成一段文本，方便断言「到底发出了什么」。"""
        if self.kind == "plain":
            return self.payload
        return "".join(
            part.text if isinstance(part, _Plain) else f"<{type(part).__name__}>"
            for part in self.payload
        )

    def __repr__(self) -> str:
        return f"_Out({self.kind}, {self.text()!r})"


class _FakeEvent:
    """只提供 _process_url 与三个调用方真正用到的事件接口。"""

    def __init__(self, origin: str, message: str, sender: str = "u1",
                 group: str = "g1", private: bool = False,
                 json_component: bool = False):
        self.unified_msg_origin = origin
        self.message_str = message
        self._sender = sender
        self._group = group
        self._private = private
        self.produced: list[_Out] = []
        if json_component:
            self.message_obj = types.SimpleNamespace(message=[_Json(data=json.dumps(
                {"meta": {"detail_1": {"qqdocurl": message}}}
            ))])

    # --- 消息产物（AstrBot 里最终发出去的东西就是这两个方法的返回值）---
    def plain_result(self, text: str) -> _Out:
        out = _Out("plain", text)
        self.produced.append(out)
        return out

    def chain_result(self, parts: list) -> _Out:
        out = _Out("chain", list(parts))
        self.produced.append(out)
        return out

    # --- 身份 / 会话 ---
    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str | None:
        return None if self._private else self._group

    def get_sender_name(self) -> str:
        return "tester"

    def is_admin(self) -> bool:
        return False

    def is_private_chat(self) -> bool:
        return self._private

    def get_platform_name(self) -> str:
        # 故意不是 aiocqhttp：走「非 OneBot」那条 _send_plain_output 分支
        return "fake_platform"


class _FakeParser:
    """平台解析器替身。记录 search_url / parse 的调用，用来断言「有没有真的去解析」。"""

    #: 给 ``search_url`` 造一个真 Match 用的 pattern，**必须像个平台 pattern**：
    #: 从整条消息里挑出**链接**那一段（而不是第一个非空白 token），边界还要在
    #: 中文标点处停住 —— 真实解析器的 pattern 都是 ASCII 字符类，例如
    #: ``bilibili\.com/video/BV[0-9A-Za-z]{10}``。
    #:
    #: 这不是随手写的：``_process_url`` 现在按 ``searched.group(0)`` 算缓存/去重键，
    #: 替身的边界直接决定被测行为。写成 ``\S+`` 的话「【】看看这个 <url>」
    #: 匹配到的是「【】看看这个」，键就错了 —— 而且错得很安静（异常会被
    #: ``except Exception`` 吞成「什么都没发生」）。
    _ANY_TOKEN = re.compile(r"https?://[^\s\"'<>，。！？、）】]+|BV[0-9A-Za-z]{10}")

    def __init__(self, accept: str | None = None, errors: list[BaseException] | None = None):
        # accept=None 表示什么都收；给了就只收包含该子串的链接，其余抛
        # SilentException（真解析器就是这么表示「这条链接不归我管」的）
        self._accept = accept
        self._errors = list(errors or [])
        self.search_calls: list[str] = []
        self.parse_calls: list[str] = []

    def search_url(self, url: str) -> tuple[str, Any]:
        """返回 ``(keyword, match)``。

        ⚠️ 第二个元素必须是**真正的正则 Match**，不能拿 bool 顶替 ——
        ``_process_url`` 现在按 ``searched.group(0)``（解析器实际认领的那一段）
        算缓存/去重键，返回 bool 会让每个用例都在 try 里抛 AttributeError、
        被 ``except Exception`` 吞成一条「处理出错」，表现为「什么都没发生」。
        """
        self.search_calls.append(url)
        if self._accept is not None and self._accept not in url:
            raise SilentException()
        return (url, self._ANY_TOKEN.search(url))

    async def parse(self, keyword: str, searched: Any = None) -> ParseResult:
        self.parse_calls.append(keyword)
        if self._errors:
            raise self._errors.pop(0)
        return ParseResult(platform=PLATFORM, title=TITLE, contents=[])


class _FakeRenderer:
    """卡片渲染器替身：关掉渲染。

    真渲染器要 PIL + 字体，而且渲染路径会把 _deliver 分支切到另一条
    （_send_image / 不发文字），那就不是「群里看到的那条消息」了。
    """

    enabled = False

    async def render(self, *_a, **_k):
        return None


class _FakeHistory:
    """解析记录落盘替身（_record_history 会通过 asyncio.to_thread 调它）。"""

    def __init__(self):
        self.records: list[Any] = []

    def add(self, record: Any) -> bool:
        self.records.append(record)
        return True


class _FakeContext:
    async def send_message(self, *_a, **_k) -> bool:
        return True


# ============ 插件实例（不跑 __init__） ============

def _make_plugin(send_errors: bool = False) -> Any:
    """造一个只填了必需属性的插件实例。

    ``object.__new__`` 绕开 ``__init__``：那里面会建 downloader、解析器、读配置
    目录、连 AstrBot 数据目录 —— 与「去重消息序列」这个被测行为无关。
    ``self._link_dedup`` 的接线方式与 ``__init__`` 第 285 行完全一致。
    """
    p = object.__new__(DeniaSharePlugin)
    p.context = _FakeContext()
    p.config = None
    p.parsers = {}
    p._result_cache = {}
    p._render_cache = {}
    p._renderer = _FakeRenderer()
    p._send_errors = send_errors
    p.history = _FakeHistory()
    p._link_dedup = link_dedup_mod.LinkDedup(lambda: p._dedup_window())
    return p


def _result() -> ParseResult:
    return ParseResult(platform=PLATFORM, title=TITLE, contents=[])


def _drive(agen: Any) -> list[_Out]:
    """把一个 async 生成器跑完，收集它 yield 出去的**全部**产物。"""

    async def _collect() -> list[_Out]:
        return [item async for item in agen]

    return asyncio.run(_collect())


def _send(plugin: Any, event: _FakeEvent, parser: _FakeParser) -> list[_Out]:
    """走真实 _process_url，返回这次 yield 出去的消息序列。"""
    return _drive(plugin._process_url(event, parser))


def _texts(items: list[_Out]) -> list[str]:
    return [i.text() for i in items]


# ============ 1~4：A 群第一个人 / 第二个人 / 第三四个人 / B 群 ============

class TestFirstSenderProceeds(unittest.TestCase):
    """A 群第 1 个人发链接 -> 正常解析并交付。"""

    def test_card_is_delivered_and_parser_is_called_once(self):
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            items = _send(plugin, _FakeEvent(GROUP_A, URL), parser)

            self.assertEqual(len(parser.parse_calls), 1, "第一个人应当被解析一次")
            self.assertEqual(parser.search_calls, [URL])
            self.assertEqual(len(items), 1, f"应当只发一条，实际 {items}")
            self.assertEqual(items[0].kind, "chain", "交付走的是消息链，不是提示")
            self.assertIn(TITLE, items[0].text())
            self.assertNotEqual(items[0].text(), HINT, "第一个人不该收到去重提示")

    def test_history_recorded_only_for_the_first_sender(self):
        """去重靠的是「不响应」，但第一个人的解析记录必须照常写。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(plugin.history.records), 1)


class TestSecondSenderGetsHint(unittest.TestCase):
    """A 群第 2 个人紧接着发同一条链接 -> 不解析，只回一句提示。"""

    def test_no_parse_and_single_hint(self):
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(parser.parse_calls), 1)

            items = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)

            self.assertEqual(
                len(parser.parse_calls), 1,
                "第 2 个人不该再触发解析（结果缓存命中也不能算解析，但更不该重发卡片）",
            )
            self.assertEqual(_texts(items), [HINT], f"实际产出 {items}")

    def test_second_sender_never_parses_again(self):
        """第 2 个人**绝对不能**再次 ``parse``（= 不再请求平台接口）。

        ``search_url`` 仍会被调一次，而且**必须**调：去重/缓存的键取自
        ``searched.group(0)``（解析器实际认领的那一段），不先问它就不知道
        这是哪条链接。它只是一次本地正则匹配，不产生任何网络请求。

        真正的行为证据是 ``parse_calls`` 不增长 —— 加上上面那条
        「第二个人只拿到提示、没拿到卡片」，就构成「去重排在缓存查询之前」
        的运行时证明（AST 只能验文本顺序）。
        """
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            parses_before = len(parser.parse_calls)
            searches_before = len(parser.search_calls)

            _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)

            self.assertEqual(
                len(parser.parse_calls), parses_before,
                "第 2 个人又触发了解析（= 又请求了一次平台接口）",
            )
            # search_url 会多一次（取键要用），但它不发网络请求
            self.assertEqual(
                len(parser.search_calls), searches_before + 1,
                "search_url 应恰好多调一次：键取自它匹配到的那一段",
            )

    def test_hint_is_emitted_even_when_error_messages_are_off(self):
        """去重提示与 SEND_ERROR_MESSAGES 无关：它是功能的一部分，不是报错。"""
        with _cfg(send_errors=False):
            plugin = _make_plugin(send_errors=False)
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(_texts(_send(plugin, _FakeEvent(GROUP_A, URL), parser)), [HINT])


class TestThirdAndFourthAreSilent(unittest.TestCase):
    """第 3、第 4 个人 -> 一条都不发（提示只发一次）。"""

    def test_nothing_is_yielded(self):
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(_texts(_send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)),
                             [HINT])
            for i, sender in enumerate(("u3", "u4"), start=3):
                with self.subTest(sender=sender):
                    items = _send(plugin, _FakeEvent(GROUP_A, URL, sender=sender), parser)
                    self.assertEqual(items, [], f"第 {i} 个人应当完全静默，实际 {items}")
            self.assertEqual(len(parser.parse_calls), 1, "四个人总共只该解析一次")

    def test_many_senders_stay_silent(self):
        """连着 8 个人发：1 张卡 + 1 句提示 + 6 条静默。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            delivered = [
                _texts(_send(plugin, _FakeEvent(GROUP_A, URL, sender=f"u{i}"), parser))
                for i in range(8)
            ]
            self.assertEqual(len(parser.parse_calls), 1)
            self.assertEqual(sum(1 for d in delivered if d and d[0] == HINT), 1)
            self.assertEqual(sum(1 for d in delivered if d == []), 6)
            self.assertEqual(sum(1 for d in delivered if d and d[0] != HINT), 1)


class TestSessionIsolation(unittest.TestCase):
    """去重按会话隔离：A 群发过了，B 群照样解析。

    注意这里必须把**结果缓存 TTL 设成 0** 才能观察到第二次 ``parse``：结果缓存的键
    只有 URL、没有会话，是全局的（``result_cache_key`` 是静态方法）。TTL 默认 600 时
    B 群会命中它、直接拿 A 群解析好的结果 —— 那不是去重挡的，是缓存省的请求。
    两条路径各测一次，别把「缓存命中」误读成「去重没生效」。
    """

    def test_other_group_reparses_independently(self):
        with _cfg(cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            other = _send(plugin, _FakeEvent(GROUP_B, URL, sender="u9"), parser)

            self.assertEqual(len(parser.parse_calls), 2, "B 群应当独立解析一次")
            self.assertEqual(len(first), 1)
            self.assertEqual(len(other), 1)
            self.assertIn(TITLE, other[0].text())
            self.assertNotEqual(other[0].text(), HINT)

    def test_other_group_still_gets_a_card_with_default_cache(self):
        """生产默认（缓存 TTL=600）下 B 群的行为：命中全局缓存拿卡片，不重复请求。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            other = _send(plugin, _FakeEvent(GROUP_B, URL, sender="u9"), parser)
            self.assertEqual(len(parser.parse_calls), 1, "命中缓存就不该再请求平台")
            self.assertEqual(len(other), 1, "B 群必须拿到自己的卡片")
            self.assertNotEqual(other[0].text(), HINT, "去重是按会话的，不该波及 B 群")

    def test_other_url_in_same_group_is_independent(self):
        with _cfg(cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            other = _send(plugin, _FakeEvent(GROUP_A, OTHER_URL), parser)
            self.assertEqual(len(parser.parse_calls), 2)
            self.assertIn(TITLE, other[0].text())

    def test_private_chat_is_not_deduped(self):
        """现状（263e07b 起）：**只在群聊里去重**。

        私聊里一个人连发两次同一链接，第二次多半是「没看到回复再发一遍」，拦下来
        只会让人以为插件坏了 —— 私聊本来就不存在「刷屏」问题。判据是
        ``_dedup_applies``（``not event.is_private_chat()``）。
        """
        with _cfg(cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            ev = _FakeEvent("OneBot:FriendMessage:private_c", URL, private=True)
            first = _send(plugin, ev, parser)
            again = _send(plugin, ev, parser)
            self.assertEqual(len(parser.parse_calls), 2, "私聊不去重，两次都该解析")
            self.assertEqual(_texts(first), _texts(again))
            self.assertNotEqual(_texts(again), [HINT])

    def test_scope_falls_back_to_sender_when_origin_missing(self):
        """unified_msg_origin 取不到时退回发送者 id（宁可少去重，不误伤）。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            ev = _FakeEvent("", URL)
            _send(plugin, ev, parser)
            items = _send(plugin, ev, parser)
            self.assertEqual(_texts(items), [HINT])


# ============ 5：窗口过期 ============

class TestWindowExpiry(unittest.TestCase):
    def test_reparses_after_the_window_passes(self):
        """窗口设 1 秒并真的等过它 -> 重新解析。

        结果缓存 TTL 同时设 0：不这样设的话第二个人会命中结果缓存、照旧拿到卡片，
        ``parse`` 不会被调用 —— 那是「重复解析间隔」在起作用，和去重窗口是两回事。
        """
        with _cfg(window=1, cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(parser.parse_calls), 1)

            # 窗口内：仍然被去重
            self.assertEqual(
                _texts(_send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)), [HINT])
            self.assertEqual(len(parser.parse_calls), 1)

            time.sleep(1.05)  # 真的等过窗口（不靠改私有字典作弊）

            after = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u3"), parser)
            self.assertEqual(len(parser.parse_calls), 2, "窗口过了应当重新解析一次")
            self.assertEqual(len(after), 1)
            self.assertIn(TITLE, after[0].text())

    def test_hint_does_not_extend_the_window(self):
        """窗口按首次出现算：提示/静默判定不能把锁定期往后推。

        否则一直有人发就永远锁死（core/link_dedup 的设计决定 4）。
        """
        with _cfg(window=1, cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            for i in range(4):
                _send(plugin, _FakeEvent(GROUP_A, URL, sender=f"u{i}"), parser)
            time.sleep(1.05)
            _send(plugin, _FakeEvent(GROUP_A, URL, sender="u5"), parser)
            self.assertEqual(len(parser.parse_calls), 2, "窗口被后来者延长了")

    def test_expired_window_with_live_result_cache_serves_from_cache(self):
        """窗口过期但结果缓存还在 -> 第二个人仍拿到卡片，只是不再请求平台。

        这是与「重复解析间隔」的分工，不是 bug：去重治刷屏、缓存治重复请求。
        把这条钉住，免得有人以后把「窗口过期」误改成「必须重新解析」。
        """
        with _cfg(window=1, cache_ttl=600):
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            time.sleep(1.05)
            items = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            self.assertEqual(len(parser.parse_calls), 1, "命中缓存就不该再请求平台")
            self.assertEqual(len(items), 1)
            self.assertNotEqual(items[0].text(), HINT)


# ============ 6：失败可重试 ============

class TestFailureAllowsRetry(unittest.TestCase):
    """第一次解析失败必须撤销占位，否则整个窗口被废掉。"""

    def test_parse_failure_then_second_sender_retries(self):
        with _cfg(send_errors=False):
            plugin = _make_plugin(send_errors=False)
            parser = _FakeParser(errors=[ParseException("模拟解析失败")])

            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(first, [], f"失败且不报错时应当一条都不发，实际 {first}")
            self.assertEqual(len(parser.parse_calls), 1)

            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)

            self.assertEqual(
                len(parser.parse_calls), 2,
                "第 2 个人应当能重新尝试 —— 占位没撤销的话他只会收到「刚刚解析过了」",
            )
            self.assertNotEqual(_texts(second), [HINT], "失败后不许说「刚刚解析过了」")
            self.assertIn(TITLE, _texts(second)[0], "重试应当真的拿到卡片")

    def test_failure_hint_flag_is_reset_so_the_next_hint_still_fires(self):
        """失败 -> 重试成功 -> 再来一个人：提示还能再发一次。"""
        with _cfg(send_errors=False):
            plugin = _make_plugin(send_errors=False)
            parser = _FakeParser(errors=[ParseException("模拟解析失败")])
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            third = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u3"), parser)
            self.assertEqual(_texts(third), [HINT])

    def test_silent_exception_also_releases_the_slot(self):
        """「这条链接不归我管」不等于处理过，必须撤销占位。"""
        with _cfg(send_errors=False):
            plugin = _make_plugin(send_errors=False)
            parser = _FakeParser(errors=[SilentException("模拟不归我管")])
            self.assertEqual(_send(plugin, _FakeEvent(GROUP_A, URL), parser), [],
                             "静默异常不产出任何消息")
            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            self.assertEqual(len(parser.parse_calls), 2, "静默异常也必须撤销占位")
            self.assertNotEqual(_texts(second), [HINT])
            self.assertIn(TITLE, _texts(second)[0])

    def test_unexpected_exception_also_releases_the_slot(self):
        """第三级 except（普通 Exception）同样要撤销 —— AST 只数了 forget 的次数，
        这里验的是它真的在运行时被调用到了。"""
        with _cfg(send_errors=True):
            plugin = _make_plugin(send_errors=True)
            parser = _FakeParser(errors=[RuntimeError("模拟未预期异常")])
            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(first), 1, "开了 SEND_ERROR_MESSAGES 就该回一句")
            self.assertNotIn("刚刚解析过了", first[0].text())
            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            self.assertEqual(len(parser.parse_calls), 2, "普通异常也必须能重试")

    def test_error_message_does_not_leak_the_raw_exception(self):
        """普通异常的回显里不能带 URL / 本机路径。"""
        with _cfg(send_errors=True):
            plugin = _make_plugin(send_errors=True)
            parser = _FakeParser(errors=[RuntimeError("/var/lib/astrbot/secret " + URL)])
            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(first), 1)
            self.assertNotIn("/var/lib/astrbot/secret", first[0].text())
            self.assertNotIn(URL, first[0].text())


# ============ 7：窗口 0 = 关闭 ============

class TestWindowDisabled(unittest.TestCase):
    def test_everyone_parses_when_window_is_zero(self):
        """窗口 0 = 关闭：一律按「没这条功能」处理。

        结果缓存 TTL 设 0：不这样设的话后面的人会命中缓存拿到卡片，
        ``parse`` 只会调一次 —— 看着像「去重还在」，其实那是缓存在省请求。
        """
        with _cfg(window=0, cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            texts = []
            for i in range(4):
                items = _send(plugin, _FakeEvent(GROUP_A, URL, sender=f"u{i}"), parser)
                texts.extend(_texts(items))
            self.assertEqual(len(parser.parse_calls), 4, "窗口 0 时每个人都应当正常解析")
            self.assertNotIn(HINT, texts)
            self.assertEqual(len(texts), 4, "每个人都要拿到自己的卡片")
            # 注册表保持为空：关掉之后不该留残留占位，把窗口改回来时莫名命中旧记录
            self.assertEqual(len(plugin._link_dedup), 0)

    def test_zero_window_still_delivers_a_card_to_everyone(self):
        """生产默认（缓存 TTL=600）下：每个人都被解析/命中缓存并拿到卡片，无提示。"""
        with _cfg(window=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            delivered = [
                _texts(_send(plugin, _FakeEvent(GROUP_A, URL, sender=f"u{i}"), parser))
                for i in range(3)
            ]
            self.assertEqual(len(delivered), 3)
            for i, d in enumerate(delivered):
                self.assertTrue(d, f"第 {i + 1} 个人应当收到卡片")
                self.assertNotEqual(d[0], HINT)

    def test_negative_window_is_treated_as_disabled(self):
        with _cfg(window=-5, cache_ttl=0):
            plugin = _make_plugin()
            parser = _FakeParser()
            for i in range(3):
                _send(plugin, _FakeEvent(GROUP_A, URL, sender=f"u{i}"), parser)
            self.assertEqual(len(parser.parse_calls), 3)

    def test_dirty_window_config_does_not_break_parsing(self):
        """脏值不能裸抛：抛出去会被 except Exception 吞成「处理出错」，链接永远解析不了。"""
        with _cfg(window="abc"):
            plugin = _make_plugin()
            parser = _FakeParser()
            items = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(items), 1)
            self.assertIn(TITLE, items[0].text())
            self.assertEqual(_texts(_send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)),
                             [HINT], "脏值按 60 秒处理，所以第 2 个人还是被去重")


# ============ 调用方：hint 一路透传，且「只 yield 一条就 return」没人踩坑 ============

class TestCallersForwardTheHint(unittest.TestCase):
    """三个调用方（内置平台 / 自定义解析器 / JSON 卡片）都要把提示原样透出去。

    这是 AST 断言完全覆盖不到的一段：``_process_url`` 现在是「可能只 yield 一条
    就 return」的生成器，调用方那句 ``async for ... yield r`` 之后的行为没人验过。
    """

    def test_dispatch_forwards_hint_and_stops(self):
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            plugin.parsers["bilibili"] = parser

            first = _drive(plugin._dispatch(_FakeEvent(GROUP_A, URL), "bilibili"))
            second = _drive(plugin._dispatch(_FakeEvent(GROUP_A, URL, sender="u2"), "bilibili"))

            self.assertEqual(len(first), 1)
            self.assertIn(TITLE, first[0].text())
            self.assertEqual(_texts(second), [HINT], f"实际 {second}")
            self.assertEqual(len(parser.parse_calls), 1)

    def test_platform_handler_forwards_hint(self):
        """直接调 11 个内置平台处理器之一（装饰器是恒等替换，处理器还是原函数）。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            plugin.parsers["bilibili"] = parser

            first = _drive(plugin.bilibili_handler(_FakeEvent(GROUP_A, URL), None))
            second = _drive(plugin.bilibili_handler(_FakeEvent(GROUP_A, URL, sender="u2"), None))

            self.assertIn(TITLE, first[0].text())
            self.assertEqual(_texts(second), [HINT])

    def test_custom_parser_handler_forwards_hint(self):
        """自定义解析器入口用 ``_EventUrlWrapper``（message_str 只剩链接），
        所以它的去重键是「链接本身」，与内置处理器不同 —— 这条只验提示能透出去。"""
        register_platform("e2e_custom", "端到端测试平台")
        self.addCleanup(unregister_platform, "e2e_custom")
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            plugin.parsers["e2e_custom"] = parser

            first = _drive(plugin.custom_parser_handler(_FakeEvent(GROUP_A, URL), None))
            second = _drive(
                plugin.custom_parser_handler(_FakeEvent(GROUP_A, URL, sender="u2"), None))

            self.assertIn(TITLE, first[0].text())
            self.assertEqual(_texts(second), [HINT], f"实际 {second}")
            self.assertEqual(len(parser.parse_calls), 1)

    def test_json_card_handler_forwards_hint(self):
        """JSON 卡片入口：循环里 ``_process_url`` 正常返回（提示分支不抛异常），
        调用方的 ``return`` 必须真的生效，否则会拿下一个解析器再解析一遍。"""
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser(accept="bilibili")
            other = _FakeParser(accept="douyin")
            plugin.parsers["bilibili"] = parser
            plugin.parsers["douyin"] = other

            def _event(sender: str) -> _FakeEvent:
                return _FakeEvent(GROUP_A, URL, sender=sender, json_component=True)

            first = _drive(plugin.json_card_handler(_event("u1"), None))
            second = _drive(plugin.json_card_handler(_event("u2"), None))

            self.assertEqual(len(first), 1)
            self.assertIn(TITLE, first[0].text())
            self.assertEqual(_texts(second), [HINT], f"实际 {second}")
            self.assertEqual(len(parser.parse_calls), 1, "不该换下一个解析器重试")
            self.assertEqual(other.parse_calls, [])


# ============ 交付之后失败：不能撤销占位 ============

class TestPostDeliveryFailureKeepsTheSlot(unittest.TestCase):
    """卡片已经发出去之后，收尾步骤失败**不能**撤销去重占位。

    ``_record_history`` 这类交付之后的步骤失败时，如果照旧 ``forget()``，下一个人会
    重新解析并**再发一张一模一样的卡片** —— 那正是这个功能要消灭的事。
    ``_forget_dedup(scope, cache_key, delivered)`` 里的 ``delivered`` 就是为此存在的，
    而它**只能靠运行时验**：AST 只能数出「三个 except 都调了 forget」。
    """

    @staticmethod
    def _break_history(plugin: Any) -> None:
        """把交付之后的收尾步骤换成会抛的版本（只在测试里换这一个方法）。"""

        async def _boom(*_a, **_k):
            raise RuntimeError("模拟写记录失败")

        plugin._record_history = _boom

    def test_history_failure_after_card_does_not_release_the_slot(self):
        with _cfg(send_errors=True):
            plugin = _make_plugin(send_errors=True)
            parser = _FakeParser()
            self._break_history(plugin)

            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            # 卡片已经发到群里了，收尾失败**不该**再补一条「处理出错」——
            # 补了只会让人以为这条链接没解析成功，而内容其实已经拿到了。
            self.assertEqual(
                len(first), 1,
                f"卡片已交付后还多发了一条错误提示，实际 {first}",
            )
            self.assertIn(TITLE, first[0].text())

            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            self.assertEqual(
                _texts(second), [HINT],
                f"交付后撤销了占位 -> 第 2 个人会再收一张卡，实际 {second}",
            )
            self.assertEqual(len(parser.parse_calls), 1)

    def test_failure_before_delivery_still_releases_the_slot(self):
        """对照组：还没交付就失败（解析本身挂了）必须撤销，否则整个窗口被废掉。"""
        with _cfg(send_errors=True):
            plugin = _make_plugin(send_errors=True)
            parser = _FakeParser(errors=[ParseException("模拟解析失败")])
            self._break_history(plugin)  # 交付前就挂，_record_history 根本走不到

            first = _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            self.assertEqual(len(first), 1, "开了 SEND_ERROR_MESSAGES 才有一条错误提示")
            self.assertNotEqual(first[0].text(), HINT)

            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)
            self.assertEqual(len(parser.parse_calls), 2, "没交付过就该让下一个人重试")
            self.assertIn(TITLE, _texts(second)[0])


# ============ 两个值得报告的现状 ============

class TestKnownBehaviours(unittest.TestCase):
    """**现状钉住**，不是断言「这就是想要的行为」。两条都已在报告里写明。"""

    def test_dedup_key_is_the_extracted_link_not_the_whole_message(self):
        """去重/缓存键必须是**抽出来的链接**，不是整条消息。

        **这里曾经是本功能的最大缺口**：``_process_url`` 拿
        ``event.message_str.strip()`` 整条去算 ``cache_key``，而 11 个内置平台
        处理器（``_dispatch`` → ``_process_url(event, ...)``）传进来的正是
        **整条消息**。于是「【】看看这个 <链接>」与「<链接>」算两条不同的键：
        既不去重，也**不共用结果缓存** —— 同一个链接被完整解析两遍、群里收到
        两张一模一样的卡片，恰好是这个功能要消灭的刷屏。
        只有自定义解析器 / JSON 卡片入口用了 ``_EventUrlWrapper``（message_str
        只剩链接）才真的按链接去重。

        修法是键改用 ``URL_PATTERN`` 抽出来的链接，``search_url`` 仍收到整条
        消息（内置 pattern 里有 ``^BV…$`` 这种锚定形式，也有
        ``bilibili.com/video/BV…`` 这种内嵌形式，两种都要能匹配上）。
        """
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            first = _send(plugin, _FakeEvent(GROUP_A, f"【】看看这个 {URL}"), parser)
            second = _send(plugin, _FakeEvent(GROUP_A, URL, sender="u2"), parser)

            self.assertEqual(
                len(parser.parse_calls), 1,
                "同一条链接被解析了两遍 —— 去重/缓存键又退回整条消息了",
            )
            self.assertEqual(
                _texts(second), [HINT],
                "带前缀的转发与裸链接应当认出是同一条，第二个人只收提示",
            )
            self.assertIn(TITLE, first[0].text(), "第一个人照常拿到卡片")

    def test_result_cache_also_shares_across_different_message_text(self):
        """结果缓存同样按链接共用 —— 换个说法转发不该重新请求平台接口。

        出去重窗口、但仍在「重复解析间隔」里时，第二条消息应该直接复用第一次的
        结果缓存，**连提示都不发**（提示是去重逻辑发的，缓存命中时压根不走到那）。
        """
        with _cfg():
            plugin = _make_plugin()
            parser = _FakeParser()
            _send(plugin, _FakeEvent(GROUP_A, URL), parser)
            # 把去重窗口关掉，只留结果缓存，才能单独看缓存这一层
            with _cfg(window=0):
                again = _send(
                    plugin, _FakeEvent(GROUP_A, f"再看看 {URL}", sender="u2"), parser
                )
            self.assertEqual(len(parser.parse_calls), 1, "换了说法就重新解析 —— 缓存键不对")
            self.assertIn(TITLE, _texts(again)[0], "应当直接复用上次的卡片")

    def test_known_gap_dirty_message_no_longer_crashes_the_handler(self):
        """回归：消息串里带孤立代理字符时，``url.encode()`` 抛 UnicodeEncodeError。

        这是**真实**的触发方式（不用打桩）：某些协议端解码出的消息串可能带孤立
        代理项。以前这条异常会在三个 ``except`` 里二次抛出 UnboundLocalError 并
        **逃出生成器**，真实错误被掩盖、「处理出错」提示也发不出去。
        现在（``cache_key`` / ``scope`` 预置为 None）应当被正常兜住。
        """
        with _cfg(send_errors=True):
            plugin = _make_plugin(send_errors=True)
            parser = _FakeParser()
            items = _send(plugin, _FakeEvent(GROUP_A, URL + "\udcff"), parser)
            self.assertEqual(
                _texts(items),
                ["❌ 处理出错（UnicodeEncodeError），详情见 AstrBot 日志"],
                "脏消息串应当被 except Exception 兜住并如实提示，不能逃出生成器",
            )
            self.assertEqual(parser.parse_calls, [], "算不出缓存键就不该去解析")

    def test_dirty_message_with_error_messages_off_yields_nothing(self):
        with _cfg(send_errors=False):
            plugin = _make_plugin(send_errors=False)
            parser = _FakeParser()
            items = _send(plugin, _FakeEvent(GROUP_A, URL + "\udcff"), parser)
            self.assertEqual(items, [], "关掉错误提示时应当彻底安静，也不该抛异常")


# ============ 替身方案自身的自检 ============

class TestHarnessIsNotAFake(unittest.TestCase):
    """钉住「测的是真代码、只有一份模块实例」。

    这套替身方案最大的风险是「看着在跑 main.py，其实跑的是一份复制品或第二个模块
    实例」—— 那样全部用例都会绿，但生产代码改了它不会红。所以把这两件事本身写成
    断言：将来有人把导入方式改成 exec/造新命名空间，这里立刻会红。
    """

    def test_process_url_comes_from_main_py(self):
        fn = DeniaSharePlugin._process_url
        self.assertEqual(fn.__module__, "astrbot_plugin_denia_share.main")
        self.assertEqual(fn.__qualname__, "DeniaSharePlugin._process_url")
        import inspect

        self.assertEqual(
            Path(inspect.getsourcefile(fn)).resolve(), (PLUGIN_DIR / "main.py"),
            "_process_url 不是从 main.py 加载的 —— 本文件测的就不是生产代码",
        )

    def test_core_modules_are_shared_not_duplicated(self):
        """main.py 与本测试必须看到**同一个** core.link_dedup 模块对象。"""
        self.assertIs(
            plugin_main.LinkDedup, link_dedup_mod.LinkDedup,
            "出现了两套 astrbot_plugin_denia_share.core.link_dedup 实例",
        )
        self.assertIs(sys.modules["astrbot_plugin_denia_share.main"], plugin_main)
        self.assertIs(
            sys.modules["astrbot_plugin_denia_share.core.link_dedup"], link_dedup_mod
        )

    def test_delivery_chain_is_the_real_one(self):
        """交付链上的方法也都来自 main.py，而不是本文件的替身。"""
        for name in ("_deliver", "_build_output", "_send_plain_output",
                     "_try_send_media", "_get_cached_result", "_remember_result",
                     "_event_scope", "_dedup_window", "_dedup_applies",
                     "_forget_dedup", "_access_denied", "_record_history",
                     "_dispatch"):
            fn = getattr(DeniaSharePlugin, name)
            self.assertEqual(
                getattr(fn, "__module__", None), "astrbot_plugin_denia_share.main",
                f"{name} 不是 main.py 的实现",
            )


if __name__ == "__main__":
    unittest.main()