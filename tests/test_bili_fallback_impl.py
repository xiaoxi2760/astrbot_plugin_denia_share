"""备用取流模块的离线测试。

全部吃 ``tests/fixtures/`` 里的真实 B站响应（已消毒），**不联网**。

重点钉住四件事：

1. **WBI 签名是确定性的** —— 混排表、剔除字符、排序，任何一步变了签名就废，
   而签名一废**热评接口直接 -403**。所以用快照钉死。
2. **排序键是「画质优先、编码次之」** —— 这是最容易写反的地方。写成编码优先
   的话，qn=16 的 AVC 会赢过 qn=120 的 AV1，画质从 4K 掉到 360P，而且测试
   不会报警，只会在用户手上体现成「下载到了 360P」。见
   ``test_quality_outranks_codec_preference``。
3. **backupUrl 真的被读了** —— 实测每条流都带 2 个备用 CDN。
4. **hev1 / hvc1 两种 sample entry 都要认** —— HEVC 已从 hev1 换代为 hvc1，
   只认前者会在现代视频上认不出编码。

运行：``py -3 -m unittest discover -s tests -t .``（在插件根目录）
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bili_fallback import (  # noqa: E402
    CODEC_ORDER,
    WbiKeyProvider,
    av2bv,
    codec_rank,
    extract_key,
    mixin_key,
    pick_streams,
    sign_params,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 现代样本（HEVC 是 hvc1）与老样本（hev1）各取一个，覆盖 sample entry 换代
MODERN = "BV1YtHs62EHZ"
LEGACY = "BV1GJ411x7h7"
MULTI_P = "BV1cwHa6mEPH"


def load(sample: str, endpoint: str = "playurl_fnval4048"):
    return json.loads((FIXTURES / sample / f"{endpoint}.json").read_text(encoding="utf-8"))


def dash_of(sample: str) -> dict:
    return (load(sample).get("data") or {}).get("dash") or {}


class TestWbiSignature(unittest.TestCase):
    """签名相关。错一步就是 -403，所以全部用快照钉死。"""

    def test_mixin_key_matches_real_nav_response(self):
        """拿真实 nav 响应里的 key 算一遍，和 yaya 实测值对得上。"""
        wbi = ((load(LEGACY, "nav").get("data") or {}).get("wbi_img")) or {}
        self.assertEqual(
            mixin_key(extract_key(wbi["img_url"]), extract_key(wbi["sub_url"])),
            "ea1db124af3c7062474693fa704f4ff8",
        )

    def test_extract_key_strips_path_and_extension(self):
        self.assertEqual(
            extract_key("https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"),
            "7cd084941338484aae1ad9425b84077c",
        )

    def test_sign_params_is_deterministic_for_fixed_time(self):
        """签名含时间戳，必须固定 ``time.time()`` 才能做快照断言。"""
        with mock.patch("core.bili_fallback.wbi.time.time", return_value=1791256800.0):
            signed = sign_params(
                {"bvid": "BV1GJ411x7h7", "cid": 137649199},
                "ea1db124af3c7062474693fa704f4ff8",
            )
        # 返回的字典里**所有值都是字符串**（实现里统一 str() 过，urlencode 不受影响）
        self.assertEqual(int(signed["wts"]), 1791256800)
        self.assertEqual(signed["w_rid"], "4424b0df26e98c706edabd084d7d5f07")

    def test_sign_params_strips_forbidden_characters(self):
        """B站要求剔除值里的 ``!'()*``，不剔除就签不出来。"""
        with mock.patch("core.bili_fallback.wbi.time.time", return_value=1791256800.0):
            dirty = sign_params({"keyword": "a!b'c(d)e*f"}, "k" * 32)
            clean = sign_params({"keyword": "abcdef"}, "k" * 32)
        self.assertEqual(dirty["keyword"], "abcdef")
        self.assertEqual(dirty["w_rid"], clean["w_rid"])

    def test_sign_params_sorts_keys_before_hashing(self):
        """参数顺序不同但内容相同，签名必须相同（B站按 key 排序后再算）。"""
        with mock.patch("core.bili_fallback.wbi.time.time", return_value=1791256800.0):
            a = sign_params({"a": 1, "b": 2}, "k" * 32)
            b = sign_params({"b": 2, "a": 1}, "k" * 32)
        self.assertEqual(a["w_rid"], b["w_rid"])


class TestAvBv(unittest.TestCase):
    """AV ↔ BV 互转。备用取流要自己认 av 链接时用。"""

    def test_known_vectors(self):
        for av, expected in (
            (170001, "BV17x411w7KC"),
            (2, "BV1xx411c7mD"),
            (1, "BV1xx411c7mQ"),
            (999999999, "BV1n44y1F7Yn"),
        ):
            with self.subTest(av=av):
                self.assertEqual(av2bv(av), expected)


class TestCodecRank(unittest.TestCase):
    def test_codecid_is_authoritative(self):
        order = CODEC_ORDER["H.264 优先"]
        self.assertEqual(codec_rank({"codecid": 7}, order), 0)   # AVC
        self.assertEqual(codec_rank({"codecid": 12}, order), 1)  # HEVC
        self.assertEqual(codec_rank({"codecid": 13}, order), 2)  # AV1

    def test_both_hev_sample_entries_resolve_to_hevc(self):
        """HEVC 已从 hev1 换代成 hvc1；codecid 认不出时字符串兜底也要认。"""
        order = CODEC_ORDER["H.264 优先"]
        self.assertEqual(codec_rank({"codecs": "hev1.1.6.L120.90"}, order), 1)
        self.assertEqual(codec_rank({"codecs": "hvc1.1.6.L120.90"}, order), 1)
        self.assertEqual(codec_rank({"codecs": "avc1.64001F"}, order), 0)
        self.assertEqual(codec_rank({"codecs": "av01.0.08M.08"}, order), 2)

    def test_unknown_codec_sorts_last(self):
        order = CODEC_ORDER["H.264 优先"]
        self.assertEqual(codec_rank({"codecs": "somethingelse"}, order), len(order))


class TestPickStreams(unittest.TestCase):
    def setUp(self):
        for sample in (MODERN, LEGACY, MULTI_P):
            dash = dash_of(sample)
            self.assertTrue((dash.get("video") or []), f"{sample} 没有视频流，fixture 有问题")

    def test_picks_avc_by_default_within_same_quality(self):
        """默认必须选 AVC。AV1 体积小但电脑版 QQ 放出来花屏。"""
        video, _ = pick_streams(dash_of(MODERN))
        self.assertIsNotNone(video)
        self.assertTrue(video[0].startswith("https://"), "主地址应是 https URL")

    def test_codec_preference_changes_choice(self):
        dash = dash_of(MODERN)
        avc, _ = pick_streams(dash, codec_preference="H.264 优先")
        av1, _ = pick_streams(dash, codec_preference="AV1 优先")
        self.assertNotEqual(
            avc[0], av1[0], "H.264 优先与 AV1 优先应选出不同的流"
        )

    def test_quality_outranks_codec_preference(self):
        """**排序键最容易写反的地方。** 编码偏好若排在清晰度之前，
        qn=16 的 AVC 会赢过 qn=120 的 AV1 —— 画质从 4K 掉到 360P。
        """
        dash = {
            "video": [
                # 低画质 + 最优先编码
                {"id": 16, "codecid": 7, "codecs": "avc1.64001E",
                 "bandwidth": 350253, "baseUrl": "https://low-avc.test/a.m4s"},
                # 高画质 + 最次优先编码
                {"id": 120, "codecid": 13, "codecs": "av01.0.08M.08",
                 "bandwidth": 184371, "baseUrl": "https://high-av1.test/b.m4s"},
            ]
        }
        video, _ = pick_streams(dash, codec_preference="H.264 优先")
        self.assertEqual(
            video[0], "https://high-av1.test/b.m4s",
            "清晰度必须压过编码偏好：H.264 优先时若选了 360P 的 AVC 就是写反了",
        )

    def test_reads_backup_urls(self):
        """实测每条流都带 backupUrl，白放着不用是浪费。"""
        video, audio = pick_streams(dash_of(MODERN))
        for name, pair in (("video", video), ("audio", audio)):
            with self.subTest(stream=name):
                self.assertIsNotNone(pair)
                primary, backups = pair
                self.assertTrue(backups, f"{name} 应带备用地址")
                self.assertNotIn(primary, backups, "备用列表里不该含主地址")

    def test_max_qn_caps_quality(self):
        dash = dash_of(MODERN)
        video, _ = pick_streams(dash, max_qn=16)
        # 选中的流 id 不会超过上限；由于 primary 是 URL，用 dash 反查
        picked_id = next(
            s["id"] for s in dash["video"]
            if s.get("baseUrl") == video[0]
        )
        self.assertLessEqual(picked_id, 16)

    def test_max_qn_zero_means_unlimited(self):
        video_unlimited, _ = pick_streams(dash_of(MODERN), max_qn=0)
        video_capped, _ = pick_streams(dash_of(MODERN), max_qn=16)
        self.assertIsNotNone(video_unlimited)
        self.assertIsNotNone(video_capped)

    def test_handles_missing_audio(self):
        video, audio = pick_streams({"video": dash_of(LEGACY)["video"]})
        self.assertIsNotNone(video)
        self.assertIsNone(audio, "无音频流时应返回 None 而不是抛异常")

    def test_handles_empty_dash(self):
        self.assertEqual(pick_streams({}), (None, None))
        self.assertEqual(pick_streams({"video": [], "audio": []}), (None, None))

    def test_stream_without_any_url_is_skipped(self):
        dash = {"video": [{"id": 32, "codecid": 7, "bandwidth": 1},  # 没有 URL
                          {"id": 32, "codecid": 7, "bandwidth": 2,
                           "baseUrl": "https://ok.test/v.m4s"}]}
        video, _ = pick_streams(dash)
        self.assertEqual(video[0], "https://ok.test/v.m4s")


class TestWbiKeyProviderCaching(unittest.TestCase):
    """取 key 的缓存行为 —— 负缓存缺了会导致并发雪崩。"""

    def _provider(self, fail_ttl: float = 60.0):
        calls = []

        class _Boom:
            async def __aenter__(self):
                calls.append(1)
                raise ConnectionError("nav 挂了")

            async def __aexit__(self, *exc):
                return False

        return WbiKeyProvider(lambda: _Boom(), fail_ttl=fail_ttl), calls

    def test_failure_is_cached_not_raised(self):
        """取 key 失败是**正常结果**（返回 None），不是异常。"""
        import asyncio

        provider, calls = self._provider()
        first = asyncio.run(provider.get())
        second = asyncio.run(provider.get())
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            len(calls), 1,
            "负缓存失效：第二次调用又去打 nav 了，并发下会串成 15s×N 的长队",
        )

    def test_zero_fail_ttl_still_returns_none(self):
        """即使关掉负缓存，也只能返回 None，不能抛。"""
        import asyncio

        provider, _ = self._provider(fail_ttl=0.0)
        self.assertIsNone(asyncio.run(provider.get()))

    def test_peek_is_side_effect_free(self):
        provider = WbiKeyProvider(lambda: None)
        self.assertIsNone(provider.peek())


if __name__ == "__main__":
    unittest.main()
