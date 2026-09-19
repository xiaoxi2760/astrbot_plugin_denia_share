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

# 只保留「会改变可见行为」的配置。 housekeeping 类的（缓存清理间隔、卡片宽度、
# 封面裁剪、调试日志开关）不再暴露到 WebUI，改用代码内默认值。
CONFIG_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "解析设置": (
        "DISABLED_PLATFORMS",
        "VIDEO_DURATION_MAXIMUM",
        "VIDEO_SIZE_MAXIMUM_MB",
        "SEND_ERROR_MESSAGES",
    ),
    "B站设置": ("BILI_CK", "BILI_QUALITY"),
    "网页截图": (
        "SCREENSHOT_BACKEND",
        "SCREENSHOT_FALLBACK",
        "CF_ACCOUNT_ID",
        "CF_API_TOKEN",
    ),
    "卡片外观": (
        "RENDER_ENABLED",
        "RENDER_THEME",
        "RENDER_LAYOUT",
    ),
    "高级设置": (
        "XHS_CK",
        "PIXIV_CK",
        "GITHUB_TOKEN",
        "PROXY",
        "CACHE_TTL_HOURS",
        "RENDER_FONT_PATH",
        "TWITTER_MEDIA_PROXY_BASE",
    ),
}

_KEY_GROUP_MAP: dict[str, str] = {
    key: group for group, keys in CONFIG_GROUP_KEYS.items() for key in keys
}

_DEFAULTS: dict[str, Any] = {
    "DISABLED_PLATFORMS": "",
    "VIDEO_DURATION_MAXIMUM": 480,
    "VIDEO_SIZE_MAXIMUM_MB": 100,
    "SEND_ERROR_MESSAGES": False,
    "XHS_CK": "",
    "PIXIV_CK": "",
    "GITHUB_TOKEN": "",
    "PROXY": "",
    "SCREENSHOT_BACKEND": "thum",
    "SCREENSHOT_FALLBACK": False,
    "CF_ACCOUNT_ID": "",
    "CF_API_TOKEN": "",
    "BILI_CK": "",
    "BILI_QUALITY": "1080P",
    "CACHE_TTL_HOURS": 24,
    "RENDER_ENABLED": True,
    "RENDER_THEME": "dark",
    "RENDER_LAYOUT": "standard",
    "RENDER_FONT_PATH": "",
    "TWITTER_MEDIA_PROXY_BASE": "",
    # 不再暴露到 WebUI，保留默认值以便旧配置与代码内部引用仍可读
    "DEBUG_LOG_ENABLED": True,
    "CACHE_CLEANUP_INTERVAL_MINUTES": 60,
    "RENDER_WIDTH": 800,
    "RENDER_COVER_FULL_SIZE": False,
}

# 从 WebUI 移除、但仍按固定值生效的配置
CACHE_CLEANUP_INTERVAL_MINUTES_FIXED = 60
RENDER_WIDTH_FIXED = 800


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

    # ---------------- Pixiv / GitHub ---------------- #

    @property
    def PIXIV_CK(self) -> str:
        """Pixiv Cookie（可选）。

        配了 Cookie 搜索收录会更全，但也会让搜索结果开始出现 R18 作品。
        解析器侧对 xRestrict != 0 有硬性过滤，Cookie 不会放宽这条限制。
        """
        return str(self._cfg_get("PIXIV_CK", "") or "").strip()

    @property
    def GITHUB_TOKEN(self) -> str:
        """GitHub Personal Access Token（可选，但强烈建议）。

        免 token 时限额 60 次/小时且按**出口 IP**计，共享 IP 下几乎必被限流。
        填了 token 提升到 5000 次/小时（按账号计）。
        """
        return str(self._cfg_get("GITHUB_TOKEN", "") or "").strip()

    # ---------------- 网页截图 ---------------- #

    @property
    def SCREENSHOT_BACKEND(self) -> str:
        val = str(self._cfg_get("SCREENSHOT_BACKEND", "thum")).strip().lower()
        return val if val in {"thum", "cloudflare"} else "thum"

    @property
    def SCREENSHOT_FALLBACK(self) -> bool:
        """链接匹配不到任何平台时，是否自动截图（默认关，避免群里刷图）。"""
        return bool(self._cfg_get("SCREENSHOT_FALLBACK", False))

    @property
    def CF_ACCOUNT_ID(self) -> str:
        return str(self._cfg_get("CF_ACCOUNT_ID", "") or "").strip()

    @property
    def CF_API_TOKEN(self) -> str:
        return str(self._cfg_get("CF_API_TOKEN", "") or "").strip()

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
        # 已不在 WebUI 暴露，保留读取以便旧配置兼容
        return int(self._cfg_get("CACHE_CLEANUP_INTERVAL_MINUTES", CACHE_CLEANUP_INTERVAL_MINUTES_FIXED))

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
        # 已不在 WebUI 暴露，保留读取以便旧配置兼容
        return max(520, min(1080, int(self._cfg_get("RENDER_WIDTH", RENDER_WIDTH_FIXED))))

    @property
    def RENDER_FONT_PATH(self) -> str:
        return str(self._cfg_get("RENDER_FONT_PATH", "") or "").strip()

    @property
    def RENDER_COVER_FULL_SIZE(self) -> bool:
        # 已不在 WebUI 暴露，保留读取以便旧配置兼容
        return bool(self._cfg_get("RENDER_COVER_FULL_SIZE", False))

    # ---------------- 行为与调试 ---------------- #

    @property
    def SEND_ERROR_MESSAGES(self) -> bool:
        """解析失败时是否向对话发送错误提示（关闭则只记日志，群里更安静）。"""
        return bool(self._cfg_get("SEND_ERROR_MESSAGES", False))

    @property
    def TWITTER_MEDIA_PROXY_BASE(self) -> str:
        """Twitter/X 媒体反代根地址（结尾斜杠会被去掉），留空表示不启用。"""
        return str(self._cfg_get("TWITTER_MEDIA_PROXY_BASE", "") or "").strip().rstrip("/")

    @property
    def DEBUG_LOG_ENABLED(self) -> bool:
        # 已不在 WebUI 暴露，默认开启
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
