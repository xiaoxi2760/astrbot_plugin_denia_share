"""自建替补解析器的离线测试（识别 / 归一化 / 装配）。

全部吃 ``tests/fixtures/`` 与构造数据，**不联网**。

## 最重要的一条

:class:`FallbackBilibiliParser` 的 ``@handle`` pattern 必须与主力
:class:`BilibiliParser` **逐条对齐**。对不齐会出现最难受的一种 bug：
「依赖正常时能解析、依赖一缺就匹配不上」，而且只在依赖缺失时复现 ——
也就是最难排查的那种。对照用例
:meth:`TestPatternParity.matches_exactly_what_primary_matches` 钉住它。

## 另外两条

- 替补的 import 链里不能有 ``bilibili_api``（由 ``test_bili_fallback.py``
  在子进程里用 AST 检查）。
- ``parse_video`` 的装配（extra 字段、统计行、封面、分P、时长上限）不能随
  改��丢字段 —— 那会让备用路径解析成功但结果空空如也。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
# 导入用**真实包名**而不是直接 ``core.*``：``core/parsers/github.py`` 里有
# ``from ... import __version__``，以 core 为顶层包 import 会因「相对导入超出
# 顶层包」而失败。理由见 test_bili_codec.py 的模块文档。
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.bili_fallback.parser import (  # noqa: E402
    FallbackBilibiliParser,
)
from astrbot_plugin_denia_share.core.bili_fallback.video import normalize  # noqa: E402
from astrbot_plugin_denia_share.core.parsers.bilibili import BilibiliParser  # noqa: E402
from astrbot_plugin_denia_share.core.config import get_config, init_config  # noqa: E402

init_config({}, PLUGIN_DIR, PLUGIN_DIR)

FIXTURES = PLUGIN_DIR / "tests" / "fixtures"

# 主力与替补都必须认的链接
URL_SAMPLES = [
    "https://www.bilibili.com/video/BV1GJ411x7h7",
    "https://www.bilibili.com/video/BV1GJ411x7h7?p=2",
    "bilibili.com/video/BV1GJ411x7h7",
    "https://www.bilibili.com/video/av170001",
    "https://www.bilibili.com/video/av170001?p=3",
    "https://b23.tv/xxxxxxx",
    "https://bili2233.cn/xxxxxxx",
    "BV1GJ411x7h7",
    "BV1GJ411x7h7 2",
    "av170001",
    "av170001 3",
    # 下面这些两边都**不该**认
    "https://www.douyin.com/video/123",
    "https://twitter.com/x/status/123",
    "随便一段没有链接的文字",
    "",
]

# 归一化与识别要分开测的样本
LEGACY = "BV1GJ411x7h7"
MULTI_P = "BV1cwHa6mEPH"


def load(sample: str, endpoint: str) -> dict:
    return json.loads((FIXTURES / sample / f"{endpoint}.json").read_text(encoding="utf-8"))


class TestUrlRecognition(unittest.TestCase):
    """链接识别 —— 走 ``search_url()``，也就是生产路径真正用的那条。

    **这里曾经有个 ``identify.py``**，把 ``@handle`` 的 pattern 又抄了一份成
    纯正则函数。问题是它是死代码：生产路径一次都不调它（``parser.py`` 里只有
    一行 import，全文无调用）。而副本必然漂移 —— 它的 av 正则多带了一个
    ``re.IGNORECASE``，于是 ``identify("AV170001")`` 认得、``@handle`` 不认，
    两边倒挂，而且当时的测试只断言「identify 认不认」，不去比对 handler，
    所以**全套测试照样绿**。

    根因是重复实现：denia_share 继承 ``BaseParser``，``search_url()`` 遍历
    ``_key_patterns`` 干的就是识别这件事。yaya 需要自己写一套是因为它有自己
    的派发框架，不是这里。

    所以这组用例统一走 ``search_url()``，顺带保证 ``@handle`` 是唯一事实源。
    """

    @staticmethod
    def _match(url: str):
        """返回 (keyword, 正则匹配对象)；不认则抛 SilentException。"""
        return FallbackBilibiliParser.search_url(url)

    def test_bv_url_with_page(self):
        keyword, m = self._match("https://www.bilibili.com/video/BV1GJ411x7h7?p=2")
        self.assertEqual(keyword, "/BV")
        self.assertEqual(m.group("bvid"), "BV1GJ411x7h7")
        self.assertEqual(m.group("page_num"), "2")

    def test_av_url_with_page(self):
        keyword, m = self._match("https://www.bilibili.com/video/av170001?p=3")
        self.assertEqual(keyword, "/av")
        self.assertEqual(m.group("avid"), "170001")
        self.assertEqual(m.group("page_num"), "3")

    def test_bare_forms_with_trailing_page_number(self):
        _, m = self._match("BV1GJ411x7h7 2")
        self.assertEqual(m.group("bvid"), "BV1GJ411x7h7")
        self.assertEqual(m.group("page_num"), "2")
        _, m = self._match("av170001 3")
        self.assertEqual(m.group("avid"), "170001")
        self.assertEqual(m.group("page_num"), "3")

    def test_short_links(self):
        """短链走 ``parse_with_redirect``，@handle 只负责认出来。"""
        self.assertEqual(self._match("https://b23.tv/fYdEc25")[0], "b23.tv")
        self.assertEqual(self._match("https://bili2233.cn/abc")[0], "bili2233")

    def test_page_defaults_to_one(self):
        """没带 ``?p=`` 时 group 是 None，调用方按 1 处理。"""
        _, m = self._match("https://www.bilibili.com/video/BV1GJ411x7h7")
        self.assertIsNone(m.group("page_num"))
        _, m = self._match("BV1GJ411x7h7")
        self.assertIsNone(m.group("page_num"))

    def test_non_bilibili_is_rejected(self):
        for text in ("https://www.douyin.com/video/123", "没有链接", ""):
            with self.subTest(text=text):
                with self.assertRaises(Exception):
                    self._match(text)

    def test_lowercase_bv_prefix_not_recognised(self):
        """主力要求大写 ``BV`` 前缀，替补保持一致 —— 不在这里放宽。

        放宽的后果是「依赖正常时匹配不上、依赖缺失时反而匹配上了」：
        用户会觉得「怎么装了那个库反而解析不了了」。
        """
        for text in ("bv1gj411x7h7", "https://www.bilibili.com/video/bv1gj411x7h7",
                     "AV170001", "/video/AV170001"):
            with self.subTest(text=text):
                with self.assertRaises(Exception):
                    self._match(text)

    def test_no_dead_recogniser_left_behind(self):
        """防止再有人写一份「@handle 的副本」当识别器。

        判据很硬：``core/bili_fallback/`` 下不许存在**只被 import、从未被调用**
        的模块内符号 —— 那种东西测试全绿却一行都不执行（``identify`` 就是
        这样活下来的）。
        """
        import ast

        pkg = PLUGIN_DIR / "core" / "bili_fallback"
        self.assertFalse(
            (pkg / "identify.py").exists(),
            "identify.py 应当已删除：@handle 已是唯一事实源",
        )
        dead = []
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: dict[str, int] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if getattr(node, "col_offset", 0) != 0:
                        continue  # 函数体内的局部 import 另算
                    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                        continue  # 编译指令，不是真的导入
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        imported[alias.asname or alias.name.split(".")[0]] = node.lineno
            # 只认**裸标识符**（Name + Load）。**不能把属性名也算进去** ——
            # `resp.json()` 的 attr 是 "json"，与 `import json` 这个模块毫无关系，
            # 混进来会让真死导入逃过检查（第一版就是这么漏掉 wbi.py 的 json 的）。
            # 名字在 `__all__` 里算「有意再导出」。
            used = {
                n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
                ):
                    for elt in getattr(node.value, "elts", []):
                        if isinstance(elt, ast.Constant):
                            used.add(elt.value)
            for name, lineno in imported.items():
                if name not in used:
                    dead.append(f"{path.name}:{lineno} 导入了 {name} 但从未使用")
        self.assertEqual(dead, [], "死导入：\n" + "\n".join(dead))


class TestPatternParity(unittest.TestCase):
    """替补与主力必须认**完全相同**的链接。"""

    @staticmethod
    def _matches(parser_cls, url: str) -> bool:
        try:
            parser_cls.search_url(url)
            return True
        except Exception:
            return False

    def test_matches_exactly_what_primary_matches(self):
        mismatched = []
        for url in URL_SAMPLES:
            primary = self._matches(BilibiliParser, url)
            fallback = self._matches(FallbackBilibiliParser, url)
            if primary != fallback:
                mismatched.append((url, primary, fallback))
        self.assertEqual(
            mismatched, [],
            "替补与主力对链接的认定不一致 —— 会出现「依赖正常能解析、"
            f"依赖一缺就匹配不上」：{mismatched}",
        )

    def test_primary_recognises_the_positive_samples(self):
        """反向钉子：上面对齐了还不够，主力本身得真能认出那些正向样本。"""
        positive = [u for u in URL_SAMPLES if u and "douyin" not in u and "twitter" not in u
                    and "没有链接" not in u]
        for url in positive:
            with self.subTest(url=url):
                self.assertTrue(
                    self._matches(BilibiliParser, url),
                    "对照用例失去意义：主力都认不出这个样本",
                )


class TestVideoNormalize(unittest.TestCase):
    def test_from_real_view_response(self):
        info = normalize(load(LEGACY, "view"))
        self.assertEqual(info.bvid, "BV1GJ411x7h7")
        self.assertTrue(info.title)
        self.assertTrue(info.author_name)
        self.assertTrue(info.cover)
        self.assertGreater(info.duration, 0)
        self.assertTrue(info.pages)
        self.assertIn("view", info.stat)

    def test_pages_are_indexed_from_zero(self):
        info = normalize(load(MULTI_P, "view"))
        self.assertGreaterEqual(len(info.pages), 2)
        self.assertEqual(info.pages[0].index, 0)
        self.assertEqual(info.pages[1].index, 1)
        self.assertTrue(all(p.cid for p in info.pages))

    def test_page_lookup_and_out_of_range(self):
        info = normalize(load(MULTI_P, "view"))
        self.assertEqual(info.page(0).index, 0)
        self.assertIsNone(info.page(999))
        self.assertIsNone(info.page(-1))

    def test_bvid_derived_from_aid_when_missing(self):
        """view 只给 aid 时要能自己算出 bvid —— playurl 已不接受 aid 参数。"""
        info = normalize({"code": 0, "data": {"aid": 170001, "title": "t"}})
        self.assertEqual(info.bvid, "BV17x411w7KC")

    def test_malformed_payload_does_not_crash(self):
        for payload in ({}, {"data": None}, {"data": {"pages": [None, 42]}},
                        {"data": {"owner": None, "stat": None, "pages": None}}):
            with self.subTest(payload=payload):
                info = normalize(payload)
                self.assertEqual(info.pages, [])


class _FakeDownloader:
    """``download_img`` 必须返回**协程** —— ``PathTask`` 内部会 ``asyncio.create_task``，
    传普通对象会直接 TypeError。"""

    def __init__(self):
        self._media_slots = object()

    @staticmethod
    def _coro(name: str):
        async def _c():
            return PLUGIN_DIR / f"fake-{name}"

        return _c()

    def download_img(self, *a, **k):
        return self._coro("img.jpg")

    def download_video(self, *a, **k):
        return self._coro("video.mp4")

    def download_audio(self, *a, **k):
        return self._coro("audio.m4a")

    async def download_av_and_merge(self, *a, **k):
        return PLUGIN_DIR / "fake-merged.mp4"

    async def _download_file(self, *a, **k):
        return PLUGIN_DIR / "fake-file.mp4"


class _FakeParser(FallbackBilibiliParser):
    """把下载换掉，只测装配逻辑。"""

    def __init__(self, info):
        self._info = info
        super().__init__(downloader=_FakeDownloader())

    async def _download(self, info, page, pconfig, limit_warnings):
        return PLUGIN_DIR / "fake.mp4"


class TestParseVideoAssembly(unittest.TestCase):
    """``parse_video`` 的装配不能随改动丢字段。"""

    def _run(self, sample: str, page_num: int = 1):
        info = normalize(load(sample, "view"))
        from astrbot_plugin_denia_share.core.bili_fallback import video as video_mod

        parser = _FakeParser(info)
        with mock.patch.object(video_mod, "fetch_video_info", new=mock.AsyncMock(return_value=info)):
            return asyncio.run(parser.parse_video(bvid=info.bvid, page_num=page_num))

    def test_result_has_expected_fields(self):
        result = self._run(LEGACY)
        self.assertEqual(result.title, normalize(load(LEGACY, "view")).title)
        self.assertTrue(result.text is not None)
        self.assertIsNotNone(result.author)
        self.assertTrue(result.author.name)
        self.assertTrue(result.contents)
        self.assertEqual(result.extra["content_type"], "视频")
        self.assertIn("备用", result.extra["info"])

    def test_stats_line_is_populated(self):
        result = self._run(LEGACY)
        stats = result.extra["stats_line"]
        self.assertTrue(stats, "统计行不应为空")
        self.assertTrue(any(icon in stats for icon in ("👀", "👍", "💬")))

    def test_multi_page_title_and_url(self):
        result = self._run(MULTI_P, page_num=2)
        self.assertIn("?p=2", result.url)
        self.assertTrue(result.title)

    def test_duration_limit_warning(self):
        """超过时长上限要给出警告，而不是静默。"""
        result = self._run(LEGACY)
        pconfig = get_config()
        page = normalize(load(LEGACY, "view")).resolve_page(1)
        warnings = result.extra["limit_warnings"]
        # 备用模式告知必须在最前面（limit_warnings 是卡片上唯一被渲染的提示通道，
        # extra["info"] 在出图模式下不显示 —— 放进 limit_warnings 才真的能被看到）
        self.assertEqual(warnings[0], FallbackBilibiliParser._NOTICE)
        if page.duration > pconfig.VIDEO_DURATION_MAXIMUM:
            self.assertEqual(len(warnings), 2, "超长视频应当是「备用告知 + 限长」两条")
            self.assertIn("超过限制", warnings[1])
        else:
            self.assertEqual(len(warnings), 1)


class TestLoginStateContract(unittest.TestCase):
    """替补必须实现 ``update_cookie`` / ``clear_cookie``。

    main.py 是用 ``hasattr(parser, "update_cookie")`` 决定要不要把扫码登录的
    cookie 塞给解析器的（``_bili_apply_cookie_to_parser``），登出同理用
    ``clear_cookie``。替补**原先两个都没有** → ``hasattr`` 为 False →
    扫码登录拿到的 cookie 被**静默丢弃**：不报错、不留痕，WebUI 还照样
    显示「已配置」，而解析器一直是匿名，清晰度默默降档。
    登出更糟：登出后替补内存里仍留着 cookie，继续带着它取流。
    """

    def _parser(self):
        return FallbackBilibiliParser(_FakeDownloader())

    def test_implements_both_hooks(self):
        parser = self._parser()
        self.assertTrue(hasattr(parser, "update_cookie"))
        self.assertTrue(hasattr(parser, "clear_cookie"))

    def test_update_cookie_then_clear(self):
        parser = self._parser()
        self.assertEqual(parser._bili_ck, "")
        parser.update_cookie("SESSDATA=abc; bili_jct=def")
        self.assertIn("SESSDATA=abc", parser._bili_ck)
        self.assertTrue(parser.has_cookie)
        parser.clear_cookie()
        self.assertEqual(parser._bili_ck, "")
        self.assertFalse(parser.has_cookie)

    def test_cookie_reaches_view_and_playurl_headers(self):
        """cookie 要贯穿 nav / view / playurl 三处，不是只给 playurl。"""
        parser = self._parser()
        parser.update_cookie("SESSDATA=abc")
        headers = parser._headers_with_cookie()
        self.assertIn("Cookie", headers)
        self.assertIn("SESSDATA=abc", headers["Cookie"])
        # 没 cookie 时不该凭空造一个 Cookie 头
        self.assertNotIn("Cookie", self._parser()._headers_with_cookie())


class TestPageResolutionParity(unittest.TestCase):
    """``resolve_page`` 必须与主力的 ``extract_info_with_page`` 给出同样的展示信息。

    同一份 fixture 喂给两边，逐字段比对。这条对不齐的症状是「同一个链接，
    主力显示视频标题、替补显示分P 名」—— 用户看不出哪个是「对的」，
    只会觉得「有时候标题不一样」。
    """

    @staticmethod
    def _primary(sample: str, page_num: int):
        from msgspec import convert

        from astrbot_plugin_denia_share.core.models.bilibili.video import VideoInfo

        model = convert(load(sample, "view")["data"], VideoInfo)
        return model.extract_info_with_page(page_num)

    def _compare(self, sample: str, page_num: int):
        mine = normalize(load(sample, "view")).resolve_page(page_num)
        theirs = self._primary(sample, page_num)
        self.assertIsNotNone(mine, f"{sample} p{page_num} 没解析出分P")
        self.assertEqual(mine.cid, theirs.cid, "cid 不一致")
        self.assertEqual(mine.index, theirs.index, "index 不一致")
        self.assertEqual(mine.title, theirs.title, "标题不一致")
        self.assertEqual(mine.duration, theirs.duration, "时长不一致")
        self.assertEqual(mine.cover, theirs.cover or "", "封面不一致")
        self.assertEqual(mine.timestamp, theirs.timestamp, "时间戳不一致")

    def test_single_page(self):
        self._compare(LEGACY, 1)

    def test_multi_page_first(self):
        self._compare(MULTI_P, 1)

    def test_multi_page_second(self):
        self._compare(MULTI_P, 2)

    def test_out_of_range_wraps_like_primary(self):
        """多 P 越界是**取模回绕**（99 % 2 → 第 2 个），主力就是这么做的。"""
        self._compare(MULTI_P, 99)

    def test_single_page_out_of_range_matches_primary(self):
        """**回归**：单 P 越界（如 ``?p=3`` 打在一个只有 1P 的视频上），
        主力给 ``index = page_num - 1``（**不取模**）。

        替补原先硬编码 ``index=0``，导致两模式的缓存文件名（``{bvid}-{index+1}``）
        和 URL 里的 ``?p=`` 都不互认 —— 同一个链接在两种模式下各下一份。
        """
        self._compare(LEGACY, 3)
        mine = normalize(load(LEGACY, "view")).resolve_page(3)
        self.assertEqual(
            mine.index, 2,
            "单 P 越界时 index 必须等于 page_num-1，与主力一致（不取模、不归零）",
        )

    def test_no_second_copy_of_handler_patterns(self):
        """``@handle`` 的 pattern 只能有一处定义。

        历史上多出过一份 ``identify.py`` 副本，它给 av 正则多带了
        ``re.IGNORECASE``，于是「手动识别认大写 AV / 自动路由不认」，而测试
        因为只断言副本自己、从不比对 handler，全程绿灯。副本活着一天，
        就多一天「两边不一致」的可能入口。
        """
        import ast

        pkg = PLUGIN_DIR / "core" / "bili_fallback"
        offenders = []
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "re"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                pattern = node.args[0].value
                if isinstance(pattern, str) and any(
                    frag in pattern for frag in ("BV[0-9", "av(?P", "b23.tv", "bili2233")
                ):
                    offenders.append(f"{path.name}:{node.lineno} 手写了 @handle 的 pattern")
        self.assertEqual(
            offenders, [],
            "链接识别的 pattern 只有 @handle 一处事实源，"
            "再写一份副本必然漂移：\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
