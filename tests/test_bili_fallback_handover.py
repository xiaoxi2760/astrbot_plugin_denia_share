"""B站「主力 → 自建备用取流」接管点的行为测试。

## 为什么单独写这个文件

冒烟套件（``tests/smoke/``）里 ``bilibili/普通视频`` 与 ``bilibili/多P视频``
两条会挂在 ``TypeError: object _Any can't be used in 'await' expression`` ——
那是 ``tests/_stubs.py`` 里 ``bilibili_api`` 的**桩**不支持 await
（真库是 async 的），失败发生在 ``video.get_info()``，**压根到不了取流**。

也就是说：``core/parsers/bilibili.py`` 里新加的 ``_fallback_stream_urls`` 及其
两个触发点，在现有测试里是**零覆盖**的。而这一层最要紧 —— 它是「依赖还在但
B站改了接口」时的唯一救命通道。

本文件用替身把这层跑起来：假 ``video`` 对象 + 假 ``fetch_streams``，验证
「什么时候该接管、什么时候不该接管、返回格式对不对」。
``fetch_streams`` 本身（真正的 HTTP）已由联网端到端验证过，这里不重复。

运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.exception import IgnoreException  # noqa: E402
from astrbot_plugin_denia_share.core.config import init_config  # noqa: E402
from astrbot_plugin_denia_share.core import bili_fallback  # noqa: E402
from astrbot_plugin_denia_share.core.parsers import bilibili as bili_mod  # noqa: E402

#: etch_streams 是 _fallback_stream_urls **函数体内** import 的（rom ..bili_fallback），
#: 不是 bilibili 模块的属性 —— 得 patch 它的源模块，否则 patch.object 直接报
#: AttributeError。

# 单独跑本文件时配置还没初始化（extract_download_urls 内部要读
# BILI_CODEC / BILI_QUALITY / VIDEO_DURATION_MAXIMUM）。全量 discover 时
# 可能已被别的模块初始化过 —— init_config 只是给全局赋值，重复调用无害。
init_config({}, PLUGIN_DIR, PLUGIN_DIR)

BVID = "BV1GJ411x7h7"
CID = 137649199

#: 备用取流成功时返回的形状：(视频, 音频, raw)
FB_OK = (
    ("https://cdn.test/v.m4s", ["https://cdn.test/v2.m4s"]),
    ("https://cdn.test/a.m4s", ["https://cdn.test/a2.m4s"]),
    {"quality": 80},
)
FB_NONE = (None, None, {})


class _FakeVideo:
    """替身：可控 get_download_url 的成功/失败，以及 html5 回退。"""

    def __init__(self, *, error=None, data=None, html5=None):
        self._error = error
        self._data = data
        self._html5 = html5
        self.calls = 0

    async def get_download_url(self, page_index=0, html5=False):
        self.calls += 1
        if html5:
            if self._html5 is None:
                raise RuntimeError("html5 不可用")
            return self._html5
        if self._error is not None:
            raise self._error
        return self._data if self._data is not None else {"dash": {"video": []}}


def _parser():
    """造一个只够 ``extract_download_urls`` / ``_fallback_stream_urls`` 用的实例。

    用 ``__new__`` 跳过 ``__init__``（那会拉起 downloader / 配置 / 缓存目录），
    所以下面这些属性得手动补齐 —— 少一个就会在**方法内部**抛 AttributeError，
    而 ``_fallback_stream_urls`` 有自己的 ``except Exception`` 会把它吞成
    ``return None``，测试就变成「静默地什么都没发生」而不是明确报错。
    """
    p = bili_mod.BilibiliParser.__new__(bili_mod.BilibiliParser)
    p._bili_ck = ""
    p._bili_cookie = ""
    p._bili_login_tasks = {}
    # extract_download_urls 里 `credential = await self.credential`，而它读
    # `self._credential`；None 会触发 `_init_credential()`，那里还会读
    # `_cookies_file`。两个都给成「匿名、无持久化文件」。
    p._credential = None
    p._cookies_file = None
    # _fallback_stream_urls 里要读 self.headers 并调 self.new_client()
    p.headers = {"User-Agent": "test", "Referer": "https://www.bilibili.com"}
    p.new_client = lambda **kw: mock.MagicMock()
    return p


class _Cfg:
    """把 BILI_CODEC / BILI_QUALITY / VIDEO_DURATION_MAXIMUM 固定住。"""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        from astrbot_plugin_denia_share.core.config import get_config

        self.cfg = get_config()
        self.orig = self.cfg._cfg_get
        orig, values = self.orig, self.values

        def _get(key, default=None):
            if key in values:
                return values[key]
            return orig(key, default)

        self.cfg._cfg_get = _get
        return self

    def __exit__(self, *exc):
        self.cfg._cfg_get = self.orig
        return False


def _run(coro):
    return asyncio.run(coro)


class TestFallbackHandover(unittest.TestCase):
    """两个触发点：报错时接管、解析不出流时接管。"""

    def test_handover_on_primary_error(self):
        """主力 get_download_url 报错 → 走自建取流。"""
        parser = _parser()
        video = _FakeVideo(error=RuntimeError("接口返回形状变了"))
        with _Cfg(BILI_CODEC="H.264 优先", BILI_QUALITY="1080P",
                  VIDEO_DURATION_MAXIMUM=0), \
             mock.patch.object(bili_fallback, "fetch_streams", new=mock.AsyncMock(return_value=FB_OK)):
            got = _run(parser.extract_download_urls(
                video=video, bvid=BVID, cid=CID, warnings=[],
            ))
        self.assertEqual(
            got, ("https://cdn.test/v.m4s", ["https://cdn.test/v2.m4s"],
                  "https://cdn.test/a.m4s", ["https://cdn.test/a2.m4s"]),
            "备用取流成功了却没被返回 —— 接管点没生效",
        )

    def test_handover_returns_same_shape_as_primary(self):
        """备用返回的四元组形状必须与主路径**完全一致**，否则下游 CDN 重试
        循环的处理会不一样。"""
        parser = _parser()
        video = _FakeVideo(error=RuntimeError("boom"))
        with _Cfg(BILI_CODEC="H.264 优先", BILI_QUALITY="1080P",
                  VIDEO_DURATION_MAXIMUM=0), \
             mock.patch.object(bili_fallback, "fetch_streams", new=mock.AsyncMock(return_value=FB_OK)):
            got = _run(parser.extract_download_urls(
                video=video, bvid=BVID, cid=CID, warnings=[],
            ))
        self.assertIsInstance(got, tuple)
        self.assertEqual(len(got), 4)
        self.assertIsInstance(got[0], str)
        self.assertIsInstance(got[1], list)
        self.assertIsInstance(got[3], list)

    def test_handover_when_no_stream_parsed(self):
        """主力调用成功但没解析出视频流 → 也要走备用。"""
        parser = _parser()
        video = _FakeVideo(data={"dash": {"video": [], "audio": []}})
        with _Cfg(BILI_CODEC="H.264 优先", BIDI_QUALITY="1080P",
                  BILI_QUALITY="1080P", VIDEO_DURATION_MAXIMUM=0), \
             mock.patch.object(bili_fallback, "fetch_streams", new=mock.AsyncMock(return_value=FB_OK)):
            got = _run(parser.extract_download_urls(
                video=video, bvid=BVID, cid=CID, warnings=[],
            ))
        self.assertEqual(got[0], "https://cdn.test/v.m4s")

    def test_no_handover_when_fallback_also_fails(self):
        """备用也拿不到流 → 走原有的 DownloadException，不要静默。"""
        parser = _parser()
        video = _FakeVideo(data={"dash": {"video": [], "audio": []}})
        with _Cfg(BILI_CODEC="H.264 优先", BILI_QUALITY="1080P",
                  VIDEO_DURATION_MAXIMUM=0), \
             mock.patch.object(bili_fallback, "fetch_streams", new=mock.AsyncMock(return_value=FB_NONE)):
            with self.assertRaises(Exception):
                _run(parser.extract_download_urls(
                    video=video, bvid=BVID, cid=CID, warnings=[],
                ))

    def test_not_handover_without_bvid_or_cid(self):
        """没给 bvid/cid 时备用不可用，直接按原来的路失败 —— 不能瞎试。"""
        parser = _parser()
        video = _FakeVideo(data={"dash": {"video": [], "audio": []}})
        fetch = mock.AsyncMock(return_value=FB_OK)
        with _Cfg(BILI_CODEC="H.264 优先", BILI_QUALITY="1080P",
                  VIDEO_DURATION_MAXIMUM=0), \
             mock.patch.object(bili_fallback, "fetch_streams", fetch):
            with self.assertRaises(Exception):
                _run(parser.extract_download_urls(video=video, warnings=[]))
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
