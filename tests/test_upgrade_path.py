"""升级路径测试：新加的配置项在老用户升级后必须真的生效。

**为什么单独测这个**：新配置项的行为变更（「B站默认改用 H.264」「截图默认整页」）
完全依赖「AstrBot 加载配置时把这个键填成 schema 里的 default」。这条链路上有两处
可能悄悄失效，而且**都不会报错**：

1. ``_conf_schema.json`` 的 ``default`` 与 ``core/config.py`` 里 ``CONFIG_META``
   的 ``default`` 不一致 —— AstrBot 按前者填、代码按后者读，两边各说各话，
   表现是「配了没用」或「行为不是我以为的那样」。
2. 老用户的配置文件里没有这个键，而读侧的回退逻辑把它兜成了别的值。

所以这里不测「新键存在」，测的是**升级后用户实际拿到的值**。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

from astrbot_plugin_denia_share.core.config import (  # noqa: E402
    CONFIG_META,
    ParserConfig,
    verify_schema_alignment,
)

SCHEMA = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))

#: 1.2.1 新增的两个配置项
NEW_KEYS = ("BILI_CODEC", "SCREENSHOT_FULL_PAGE")


def schema_defaults() -> dict[str, tuple[str, object]]:
    """把 schema 摊平成 key -> (分组, default)。"""
    out = {}
    for group, node in SCHEMA.items():
        if isinstance(node, dict) and node.get("type") == "object":
            for key, value in (node.get("items") or {}).items():
                out[key] = (group, value.get("default"))
    return out


# 老用户在 1.2.0 时的配置长这样：有旧键，没有 1.2.1 新增的键
OLD_USER_CONFIG = {
    "B站设置": {"BILI_CK": "some-cookie", "BILI_QUALITY": "1080P"},
    "卡片外观": {"RENDER_ENABLED": True, "RENDER_THEME": "dark", "RENDER_LAYOUT": "standard"},
    "网页截图": {"SCREENSHOT_BACKEND": "thum", "SCREENSHOT_FALLBACK": False},
}


class SchemaMetaDefaultsAgree(unittest.TestCase):
    """schema 与 CONFIG_META 的默认值必须逐项一致。"""

    def setUp(self):
        self.flat = schema_defaults()

    def test_no_key_missing_from_schema(self):
        missing = [i["key"] for i in CONFIG_META if i["key"] not in self.flat]
        self.assertEqual(missing, [], f"这些键在 _conf_schema.json 里没有：AstrBot 填不了默认值")

    def test_defaults_match(self):
        mismatched = [
            (i["key"], i.get("default"), self.flat[i["key"]][1])
            for i in CONFIG_META
            if i["key"] in self.flat and self.flat[i["key"]][1] != i.get("default")
        ]
        self.assertEqual(
            mismatched, [],
            "schema 与 CONFIG_META 的默认值不一致 —— AstrBot 填一个、代码读另一个，"
            "表现为「配了不生效」",
        )

    def test_groups_match(self):
        mismatched = [
            (i["key"], i["group"], self.flat[i["key"]][0])
            for i in CONFIG_META
            if i["key"] in self.flat and self.flat[i["key"]][0] != i["group"]
        ]
        self.assertEqual(mismatched, [], "schema 分组与 CONFIG_META 不一致")

    def test_schema_alignment_self_check_passes(self):
        self.assertEqual(verify_schema_alignment(SCHEMA), [])


class OldUserUpgrade(unittest.TestCase):
    """老用户升级后：旧值不动，新键拿到新默认。"""

    def setUp(self):
        self.cfg = ParserConfig(OLD_USER_CONFIG, PLUGIN_DIR, PLUGIN_DIR)

    def test_existing_values_are_preserved(self):
        """升级**不能**动用户已经设过的值。"""
        self.assertEqual(self.cfg.BILI_QUALITY, "1080P")
        self.assertEqual(self.cfg.BILI_CK, "some-cookie")
        self.assertEqual(self.cfg.SCREENSHOT_BACKEND, "thum")
        self.assertIs(self.cfg.SCREENSHOT_FALLBACK, False)

    def test_new_keys_take_new_defaults(self):
        """这才是行为变更的落点：没有这个键 → 取 schema/代码的默认 → 新行为生效。"""
        self.assertEqual(self.cfg.BILI_CODEC, "H.264 优先")
        self.assertIs(self.cfg.SCREENSHOT_FULL_PAGE, True)

    def test_user_explicit_choice_survives_upgrade(self):
        """用户要是自己设过新键，后续升级不能覆盖回去。"""
        cfg = ParserConfig(
            {
                **OLD_USER_CONFIG,
                "B站设置": {**OLD_USER_CONFIG["B站设置"], "BILI_CODEC": "AV1 优先"},
                "网页截图": {**OLD_USER_CONFIG["网页截图"], "SCREENSHOT_FULL_PAGE": False},
            },
            PLUGIN_DIR,
            PLUGIN_DIR,
        )
        self.assertEqual(cfg.BILI_CODEC, "AV1 优先")
        self.assertIs(cfg.SCREENSHOT_FULL_PAGE, False)

    def test_legacy_flat_config_still_works(self):
        """老版本可能是扁平的（不在分组里），读侧的回退要能兜住。"""
        flat_cfg = ParserConfig(
            {"BILI_QUALITY": "4K", "SCREENSHOT_BACKEND": "cloudflare"},
            PLUGIN_DIR,
            PLUGIN_DIR,
        )
        self.assertEqual(flat_cfg.BILI_QUALITY, "4K")
        self.assertEqual(flat_cfg.SCREENSHOT_BACKEND, "cloudflare")
        # 扁平配置里没有新键 → 仍要拿到新默认
        self.assertEqual(flat_cfg.BILI_CODEC, "H.264 优先")
        self.assertIs(flat_cfg.SCREENSHOT_FULL_PAGE, True)

    def test_fresh_install_gets_defaults(self):
        cfg = ParserConfig({}, PLUGIN_DIR, PLUGIN_DIR)
        for key in NEW_KEYS:
            with self.subTest(key=key):
                meta = next(i for i in CONFIG_META if i["key"] == key)
                self.assertEqual(getattr(cfg, key), meta["default"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
