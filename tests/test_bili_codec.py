"""B站视频编码偏好（``BILI_CODEC``）的配置契约与映射测试。

背景：1.2.x 把 codec 顺序写死成 ``[AV1, AVC, HEV]``，即同清晰度下**优先挑兼容性
最差的 AV1**。AV1 体积最小，但老客户端（尤其电脑版 QQ）硬解不了，发过去就是花屏。
1.3.0 把它变成可配置项，默认改成 H.264 优先。

这里守三件事：
1. 加配置项只改了一处会立刻被发现（schema ↔ CONFIG_META 的键集合/分组必须一致）；
2. 读侧对脏值有兜底，不会把非法值透给解析器；
3. **默认顺序必须是 AVC 打头**——这是本次修复的本体，钉死以防将来又被改回去。

注意导入用的是**真实包名**（``astrbot_plugin_denia_share.*``）而不是直接 ``core.*``：
``core/parsers/github.py`` 里有 ``from ... import __version__``，以 ``core`` 为顶层包
import 会因「相对导入超出顶层包」而失败。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

# 必须在 import 插件模块之前装：插件顶层会 from astrbot.api / bilibili_api 拿东西
_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.config import (  # noqa: E402
    CONFIG_META,
    ParserConfig,
    verify_schema_alignment,
)
from astrbot_plugin_denia_share.core.parsers.bilibili import BilibiliParser  # noqa: E402


def _schema() -> dict:
    return json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))


def _meta(key: str) -> dict:
    return next(item for item in CONFIG_META if item["key"] == key)


def _cfg(stored=..., missing: bool = False) -> ParserConfig:
    # 注意只包一层：ParserConfig._cfg_get 取的是 config[分组名][键名]
    config = {} if missing else {"B站设置": {"BILI_CODEC": stored}}
    return ParserConfig(config, PLUGIN_DIR, PLUGIN_DIR)


class FakeCodecs:
    """替身 codec 枚举——``_codec_order`` 按名字取属性，不依赖真实类型。"""

    AVC = "avc"
    HEV = "hev"
    AV1 = "av1"


class SchemaAlignment(unittest.TestCase):
    def test_schema_and_config_meta_agree(self):
        """``BILI_CODEC`` 必须同时出现在 _conf_schema.json 与 CONFIG_META。"""
        self.assertEqual(verify_schema_alignment(_schema()), [])

    def test_meta_entry_shape(self):
        item = _meta("BILI_CODEC")
        self.assertEqual(item["group"], "B站设置")
        self.assertEqual(item["type"], "select")
        self.assertEqual(item["default"], "H.264 优先")
        self.assertEqual(
            item["options"], ["H.264 优先", "H.265 优先", "AV1 优先"], item["options"]
        )

    def test_schema_options_match_meta_options(self):
        """两处的候选值必须逐字一致，否则页面能选出一个后端不认的值。"""
        node = _schema()["B站设置"]["items"]["BILI_CODEC"]
        meta = _meta("BILI_CODEC")
        self.assertEqual(node["options"], meta["options"])
        self.assertEqual(node["default"], meta["default"])


class ReadSideFallback(unittest.TestCase):
    def test_valid_values_pass_through(self):
        for value in ("H.264 优先", "H.265 优先", "AV1 优先"):
            with self.subTest(value=value):
                self.assertEqual(_cfg(value).BILI_CODEC, value)

    def test_dirty_values_fall_back_to_default(self):
        """手工改过的配置 / 旧版本残留值不能透给解析器，静默回默认。"""
        for value in ("", "   ", None, "av1优先", "AVC", 123, ["H.264 优先"]):
            with self.subTest(value=value):
                self.assertEqual(_cfg(value).BILI_CODEC, "H.264 优先")

    def test_absent_key_uses_default(self):
        self.assertEqual(_cfg(missing=True).BILI_CODEC, "H.264 优先")


class CodecOrder(unittest.TestCase):
    def test_each_option_maps_to_its_order(self):
        cases = {
            "H.264 优先": ["avc", "hev", "av1"],
            "H.265 优先": ["hev", "avc", "av1"],
            "AV1 优先": ["av1", "avc", "hev"],
        }
        for preference, expected in cases.items():
            with self.subTest(preference=preference):
                self.assertEqual(
                    BilibiliParser._codec_order(FakeCodecs, preference), expected
                )

    def test_default_is_avc_first_not_av1(self):
        """回归钉子：1.2.x 把 AV1 排第一，电脑版 QQ 放出来花屏。默认必须是 AVC。"""
        for unknown in ("", None, "乱填", "av1"):
            with self.subTest(value=unknown):
                self.assertEqual(
                    BilibiliParser._codec_order(FakeCodecs, unknown)[0], "avc"
                )

    def test_every_order_keeps_all_three_codecs(self):
        """顺序可以换，但不能少给一种——少了等于把某些画质档直接排除掉。"""
        for preference in ("H.264 优先", "H.265 优先", "AV1 优先", "未知值"):
            with self.subTest(preference=preference):
                order = BilibiliParser._codec_order(FakeCodecs, preference)
                self.assertEqual(len(order), 3)
                self.assertEqual(set(order), {"avc", "hev", "av1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
