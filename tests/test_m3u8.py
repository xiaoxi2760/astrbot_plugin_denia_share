"""m3u8 清单解析与重封装行为测试。

1.2.x 的 ``download_m3u8`` 把「非 ``#`` 开头的行」一律当分片，于是有两个洞：

- 遇到**主播放列表**（master playlist）时，那些行其实是子清单地址，
  当分片下载会得到一堆「把播放列表当视频内容」的垃圾字节；
- 忽略 ``#EXT-X-MAP``，fMP4 流缺了 init segment（moov 头）就是空壳。

:func:`parse_m3u8_playlist` 是纯函数，不碰网络，所以这里可以穷举各种清单形态。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.media_utils import (  # noqa: E402
    MAX_M3U8_PLAYLIST_DEPTH,
    parse_m3u8_playlist,
)

BASE = "https://cdn.example.com/hls/index.m3u8"


class MediaPlaylist(unittest.TestCase):
    def test_plain_segment_list(self):
        text = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\n"
            "seg0.ts\n"
            "#EXTINF:9.5,\n"
            "seg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        init, segments, variants = parse_m3u8_playlist(text, BASE)
        self.assertIsNone(init)
        self.assertEqual(variants, [])
        self.assertEqual(
            segments,
            ["https://cdn.example.com/hls/seg0.ts", "https://cdn.example.com/hls/seg1.ts"],
        )

    def test_relative_segments_resolve_against_base(self):
        _, segments, _ = parse_m3u8_playlist("#EXTM3U\nseg0.ts\n", BASE)
        self.assertEqual(segments, ["https://cdn.example.com/hls/seg0.ts"])

    def test_master_playlist_lines_are_variants_not_segments(self):
        """回归点：主列表里的行是子清单地址，绝不能混进分片列表。"""
        text = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
            "low/index.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1280x720\n'
            "high/index.m3u8\n"
        )
        init, segments, variants = parse_m3u8_playlist(text, BASE)
        self.assertEqual(segments, [], "主列表不应产生分片")
        self.assertIsNone(init)
        # 分辨率优先于带宽：720P 虽然带宽高但分辨率也高，这里两者同向，
        # 再用下面那条「分辨率低但带宽高」的用例单独验证优先级
        self.assertEqual(
            variants,
            ["https://cdn.example.com/hls/high/index.m3u8", "https://cdn.example.com/hls/low/index.m3u8"],
        )

    def test_resolution_wins_over_bandwidth(self):
        """4K 的码流带宽数字不一定最大，按分辨率挑才不会把 4K 排在 720P 后面。"""
        text = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=640x360\n'
            "sd.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\n'
            "hd.m3u8\n"
        )
        _, _, variants = parse_m3u8_playlist(text, BASE)
        self.assertEqual(variants, ["https://cdn.example.com/hls/hd.m3u8", "https://cdn.example.com/hls/sd.m3u8"])

    def test_bandwidth_breaks_resolution_ties(self):
        text = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n'
            "low.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1280x720\n'
            "high.m3u8\n"
        )
        _, _, variants = parse_m3u8_playlist(text, BASE)
        self.assertEqual(variants, ["https://cdn.example.com/hls/high.m3u8", "https://cdn.example.com/hls/low.m3u8"])

    def test_average_bandwidth_also_accepted(self):
        text = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=5000000,RESOLUTION=1920x1080\n'
            "hd.m3u8\n"
        )
        _, _, variants = parse_m3u8_playlist(text, BASE)
        self.assertEqual(variants, ["https://cdn.example.com/hls/hd.m3u8"])

    def test_fmp4_init_segment_is_captured(self):
        """回归点：缺 init segment 的 fMP4 流播放器读不出索引，只能卡住。"""
        text = (
            "#EXTM3U\n"
            '#EXT-X-MAP:URI="init.mp4"\n'
            "#EXTINF:10.0,\n"
            "seg0.m4s\n"
        )
        init, segments, _ = parse_m3u8_playlist(text, BASE)
        self.assertEqual(init, "https://cdn.example.com/hls/init.mp4")
        self.assertEqual(segments, ["https://cdn.example.com/hls/seg0.m4s"])

    def test_init_segment_without_quotes(self):
        text = "#EXTM3U\n#EXT-X-MAP:URI=init.mp4\nseg0.m4s\n"
        init, _, _ = parse_m3u8_playlist(text, BASE)
        self.assertEqual(init, "https://cdn.example.com/hls/init.mp4")

    def test_absolute_init_segment_uri_is_left_alone(self):
        text = '#EXTM3U\n#EXT-X-MAP:URI="https://other.cdn/i.mp4"\nseg0.m4s\n'
        init, _, _ = parse_m3u8_playlist(text, BASE)
        self.assertEqual(init, "https://other.cdn/i.mp4")

    def test_empty_and_garbage_input_yields_nothing(self):
        for text in ("", "#EXTM3U\n", "\n\n\n", "#EXTINF:10.0,\n"):
            with self.subTest(text=text):
                init, segments, variants = parse_m3u8_playlist(text, BASE)
                self.assertIsNone(init)
                self.assertEqual(segments, [])
                self.assertEqual(variants, [])

    def test_stream_inf_without_following_uri_does_not_hang(self):
        """``#EXT-X-STREAM-INF`` 后面必须有 URI 行；缺了就当没有变体。"""
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n#EXT-X-ENDLIST\n"
        init, segments, variants = parse_m3u8_playlist(text, BASE)
        self.assertEqual((init, segments, variants), (None, [], []))

    def test_depth_limit_allows_nested_master(self):
        """深度上限至少要够走通「主列表 → 媒体列表」两层。"""
        self.assertGreaterEqual(MAX_M3U8_PLAYLIST_DEPTH, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
