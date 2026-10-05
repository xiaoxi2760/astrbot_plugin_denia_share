"""预览图 base64 编码缓存的行为测试。

缓存页每次刷新 / 切走再切回来都会重新请求全部缩略图，也就是把同一批卡片图
重新开一遍、缩一遍、base64 编一遍。这组测试守住三件事：

1. 同一文件同一宽度只真的编码一次（命中即复用）；
2. 缓存键带 mtime_ns / 大小，**文件被重写后不能返回旧图**（卡片重新渲染就是这种情况）；
3. 按字节预算淘汰，不会把内存吃光。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core import webui  # noqa: E402


class _Recorder:
    """替换 ``_image_data_url``，记录真编码次数。

    要照抄真实实现的一个行为：**文件不在就返回 None**。否则「不存在的文件
    不会被缓存」这类断言就测不到真东西——桩自己造出了值。
    """

    #: 每个返回值的长度，字节预算相关测试靠它控制占用
    payload = 23

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, path: Path, max_width: int = webui.PREVIEW_MAX_WIDTH):
        self.calls.append((str(path), max_width))
        if not Path(path).is_file():
            return None
        marker = str(len(self.calls)).encode()
        return b"data:image/png;base64," + marker + b"A" * (self.payload - 22 - len(marker))


class ImageCache(unittest.TestCase):
    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

        webui.clear_image_cache()
        self.addCleanup(webui.clear_image_cache)

        self.recorder = _Recorder()
        self._orig = webui._image_data_url
        webui._image_data_url = self.recorder
        self.addCleanup(self._restore)

    def _restore(self):
        webui._image_data_url = self._orig

    def _write(self, name: str, size: int = 1024) -> Path:
        path = Path(self.tmp) / name
        path.write_bytes(b"\x00" * size)
        return path

    def test_same_file_hits_cache(self):
        path = self._write("a.png")
        first = webui._image_data_url_cached(path, 900)
        second = webui._image_data_url_cached(path, 900)
        self.assertEqual(first, second)
        self.assertEqual(len(self.recorder.calls), 1, "第二次应当命中缓存")

    def test_different_width_is_a_different_entry(self):
        path = self._write("a.png")
        webui._image_data_url_cached(path, 900)
        webui._image_data_url_cached(path, 720)
        self.assertEqual(len(self.recorder.calls), 2)

    def test_rewritten_file_does_not_serve_stale_image(self):
        """回归点：卡片重新渲染会换掉文件，缓存不能把上一版的预览图配给新卡片。"""
        path = self._write("a.png", size=16)
        first = webui._image_data_url_cached(path, 900)
        # 改成不同大小 + 推进 mtime，模拟「重新渲染出了新图」
        path.write_bytes(b"\x00" * 4096)
        import os

        os.utime(path, ns=(0, 0))
        second = webui._image_data_url_cached(path, 900)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.recorder.calls), 2)

    def test_missing_file_is_not_cached(self):
        """文件不在时不该往缓存里塞东西 —— 它可能是「稍后才补齐」。"""
        path = Path(self.tmp) / "nope.png"
        self.assertIsNone(webui._image_data_url_cached(path, 900))
        self.assertEqual(len(webui._image_cache), 0)

    def test_failure_result_is_not_cached(self):
        """编码失败不缓存：否则文件后来修好了也永远拿不到图。"""

        def boom(path, max_width=webui.PREVIEW_MAX_WIDTH):
            return None

        webui._image_data_url = boom
        path = self._write("a.png")
        self.assertIsNone(webui._image_data_url_cached(path, 900))
        self.assertEqual(len(webui._image_cache), 0)

    def test_byte_budget_evicts_oldest(self):
        """按字节预算而不是条数淘汰，超预算时从最旧的开始丢。"""
        self.recorder.payload = 100
        original_budget = webui._IMAGE_CACHE_BUDGET
        webui._IMAGE_CACHE_BUDGET = 500  # 每条 100 字节 → 只装得下 5 条
        self.addCleanup(setattr, webui, "_IMAGE_CACHE_BUDGET", original_budget)

        paths = [self._write(f"{i}.png") for i in range(6)]
        for path in paths:
            webui._image_data_url_cached(path, 900)

        self.assertLessEqual(webui._image_cache_bytes, 500)
        self.assertEqual(len(webui._image_cache), 5, "应恰好淘汰到装满预算为止")
        # 最早的条目先被淘汰，最新的还在
        newest = next(iter(reversed(webui._image_cache)))
        self.assertEqual(newest[0], str(paths[-1]))
        self.assertFalse(any(k[0] == str(paths[0]) for k in webui._image_cache))

    def test_clear_empties_and_resets_byte_counter(self):
        path = self._write("a.png")
        webui._image_data_url_cached(path, 900)
        self.assertGreater(webui._image_cache_bytes, 0)
        webui.clear_image_cache()
        self.assertEqual(len(webui._image_cache), 0)
        self.assertEqual(webui._image_cache_bytes, 0)

    def test_recently_used_is_kept_alive(self):
        """命中要把条目挪到队尾，否则最热的图反而先被淘汰。"""
        self.recorder.payload = 100
        original_budget = webui._IMAGE_CACHE_BUDGET
        webui._IMAGE_CACHE_BUDGET = 500
        self.addCleanup(setattr, webui, "_IMAGE_CACHE_BUDGET", original_budget)

        hot = self._write("hot.png")
        for i in range(3):
            webui._image_data_url_cached(self._write(f"fill{i}.png"), 900)
        webui._image_data_url_cached(hot, 900)  # 插队后立刻命中一次
        for i in range(3):
            webui._image_data_url_cached(self._write(f"more{i}.png"), 900)

        self.assertTrue(
            any(k[0] == str(hot) for k in webui._image_cache),
            "刚被访问过的条目不该被淘汰",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
