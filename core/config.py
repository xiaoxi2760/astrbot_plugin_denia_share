"""配置管理模块。

精简版：只保留「平台 / B站 / 缓存 / 渲染 / 调试」五组，
相比 rika 原版移除了 Cloudflare 截图 Fallback（20+ 配置项）与 B站 Cookie 监控轮询。
相比娅娅版移除了 LLM 文本翻译（10 语言 × 10 厂商接口）与「视频仅发送封面」。

明确**不包含**的能力（有意不做，勿再引入）：
- LLM 文本翻译
- 视频仅发送封面 / 跳过视频本体
"""

from pathlib import Path
from typing import Any

_config = None

CONFIG_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "平台设置": (
        "DISABLED_PLATFORMS",
        "VIDEO_DURATION_MAXIMUM",
        "VIDEO_SIZE_MAXIMUM_MB",
        "PROXY",
        "XHS_CK",
    ),
    "B站设置": ("BILI_CK", "BILI_QUALITY"),
    "缓存设置": (
        "CACHE_TTL_HOURS",
        "CACHE_CLEANUP_INTERVAL_MINUTES",
    ),
    "解析图片渲染": (
        "RENDER_ENABLED",
        "RENDER_THEME",
        "RENDER_LAYOUT",
        "RENDER_WIDTH",
        "RENDER_FONT_PATH",
        "RENDER_COVER_FULL_SIZE",
    ),
    "调试设置": ("DEBUG_LOG_ENABLED",),
}

_KEY_GROUP_MAP: dict[str, str] = {
    key: group for group, keys in CONFIG_GROUP_KEYS.items() for key in keys
}

_DEFAULTS: dict[str, Any] = {
    "DISABLED_PLATFORMS": "",
    "VIDEO_DURATION_MAXIMUM": 480,
    "VIDEO_SIZE_MAXIMUM_MB": 100,
    "PROXY": "",
    "XHS_CK": "",
    "BILI_CK": "",
    "BILI_QUALITY": "1080P",
    "CACHE_TTL_HOURS": 24,
    "CACHE_CLEANUP_INTERVAL_MINUTES": 60,
    "RENDER_ENABLED": True,
    "RENDER_THEME": "dark",
    "RENDER_LAYOUT": "standard",
    "RENDER_WIDTH": 800,
    "RENDER_FONT_PATH": "",
    "RENDER_COVER_FULL_SIZE": False,
    "DEBUG_LOG_ENABLED": True,
}


def migrate_grouped_config(config: Any) -> bool:
    """旧版扁平配置 → 分组配置，幂等。"""
    changed = False
    for group, keys in CONFIG_GROUP_KEYS.items():
        group_cfg = config.get(group)
        if not isinstance(group_cfg, dict):
            group_cfg = {}
            config[group] = group_cfg
        for key in keys:
            default = _DEFAULTS.get(key)
            flat_val = config.get(key, default)
            nested_val = group_cfg.get(key, default)
            if nested_val == default and flat_val != default:
                group_cfg[key] = flat_val
                config[key] = default
                changed = True
    return changed


class ParserConfig:
    """解析器配置 - 由 main.py 初始化"""

    def __init__(self, astrbot_config: Any, cache_dir: Path, config_dir: Path):
        self._cfg = astrbot_config
        self.cache_dir = cache_dir
        self.config_dir = config_dir

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        """优先读分组配置，回退到扁平旧配置。"""
        group = _KEY_GROUP_MAP.get(key)
        if group is not None:
            group_cfg = self._cfg.get(group)
            if isinstance(group_cfg, dict) and key in group_cfg:
                default_val = _DEFAULTS.get(key, default)
                flat_val = self._cfg.get(key, default_val)
                nested_val = group_cfg[key]
                if nested_val == default_val and flat_val != default_val:
                    return flat_val
                return nested_val
        return self._cfg.get(key, default)

    # ---------------- 平台 ---------------- #

    @property
    def DISABLED_PLATFORMS(self) -> list[str]:
        raw = self._cfg_get("DISABLED_PLATFORMS", "")
        if not raw:
            return []
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    @property
    def VIDEO_DURATION_MAXIMUM(self) -> int:
        return int(self._cfg_get("VIDEO_DURATION_MAXIMUM", 480))

    @property
    def VIDEO_SIZE_MAXIMUM_MB(self) -> int:
        """单个视频体积上限（MB），超限不下载。"""
        return max(1, int(self._cfg_get("VIDEO_SIZE_MAXIMUM_MB", 100)))

    @property
    def PROXY(self) -> str:
        """全局代理地址，形如 http://127.0.0.1:7890，留空不使用。"""
        return str(self._cfg_get("PROXY", "") or "").strip()

    @property
    def XHS_CK(self) -> str | None:
        return self._cfg_get("XHS_CK", None)

    # ---------------- B站 ---------------- #

    @property
    def BILI_CK(self) -> str | None:
        return self._cfg_get("BILI_CK", None)

    @property
    def BILI_QUALITY(self) -> str:
        return str(self._cfg_get("BILI_QUALITY", "1080P"))

    # ---------------- 缓存 ---------------- #

    @property
    def CACHE_TTL_HOURS(self) -> int:
        return int(self._cfg_get("CACHE_TTL_HOURS", 24))

    @property
    def CACHE_CLEANUP_INTERVAL_MINUTES(self) -> int:
        return int(self._cfg_get("CACHE_CLEANUP_INTERVAL_MINUTES", 60))

    # ---------------- 渲染 ---------------- #

    @property
    def RENDER_ENABLED(self) -> bool:
        return bool(self._cfg_get("RENDER_ENABLED", True))

    @property
    def RENDER_THEME(self) -> str:
        val = str(self._cfg_get("RENDER_THEME", "dark")).strip().lower()
        return val if val in {"dark", "light"} else "dark"

    @property
    def RENDER_LAYOUT(self) -> str:
        val = str(self._cfg_get("RENDER_LAYOUT", "standard")).strip().lower()
        return val if val in {"standard", "magazine", "immersive", "feed"} else "standard"

    @property
    def RENDER_WIDTH(self) -> int:
        return max(520, min(1080, int(self._cfg_get("RENDER_WIDTH", 800))))

    @property
    def RENDER_FONT_PATH(self) -> str:
        return str(self._cfg_get("RENDER_FONT_PATH", "") or "").strip()

    @property
    def RENDER_COVER_FULL_SIZE(self) -> bool:
        return bool(self._cfg_get("RENDER_COVER_FULL_SIZE", False))

    # ---------------- 调试 ---------------- #

    @property
    def DEBUG_LOG_ENABLED(self) -> bool:
        return bool(self._cfg_get("DEBUG_LOG_ENABLED", True))


def init_config(astrbot_config: Any, cache_dir: Path, config_dir: Path) -> ParserConfig:
    global _config
    _config = ParserConfig(astrbot_config, cache_dir, config_dir)
    return _config


def get_config() -> ParserConfig:
    global _config
    if _config is None:
        raise RuntimeError("ParserConfig not initialized yet")
    return _config
