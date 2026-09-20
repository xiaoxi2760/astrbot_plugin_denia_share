"""配置管理模块。

配置项的**唯一来源**是 :data:`CONFIG_META`（分组 + 键 + 类型 + 默认值 + 文案）。
插件的 WebUI 页面直接从它渲染表单；``_conf_schema.json`` 是同一次定义的**存储契约**
（AstrBot 加载插件配置时会剔除 schema 之外的键，两者必须保持一致，
由 :func:`verify_schema_alignment` 在启动时自检并告警）。

相比 rika 原版移除了 Cloudflare 截图 Fallback（20+ 配置项）与 B站 Cookie 监控轮询。
相比娅娅版移除了 LLM 文本翻译（10 语言 × 10 厂商接口）与「视频仅发送封面」。

明确**不包含**的能力（有意不做，勿再引入）：
- LLM 文本翻译
- 视频仅发送封面 / 跳过视频本体
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_config = None

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 配置分组：展示顺序即此处的顺序，常用在前、折腾在后
CONFIG_GROUPS: tuple[tuple[str, str], ...] = (
    ("解析设置", "日常最常用到的几项"),
    ("B站设置", "B站 Cookie 与下载清晰度"),
    ("Steam 设置", "价格地区与史低数据源"),
    ("网页截图", "把任意网页截成图片发送"),
    ("卡片外观", "把解析结果渲染成分享卡片图片"),
    ("媒体发送", "视频怎么送到消息平台：共享目录或中转链接"),
    ("高级设置", "一般用不到，按需开启"),
    ("维护", "缓存与调试，改动后立即生效"),
)

# 配置项元数据。字段说明：
#   key/group/label/type/default/hint 必填；type 取值 string | text | int | bool | select
#   secret=True 表示前端以密码框展示（仅遮罩显示，配置文件里仍是明文，与原生面板一致）
#   options/labels 仅 select 使用；min/max/unit 仅 int 使用；placeholder 仅输入框使用
CONFIG_META: tuple[dict[str, Any], ...] = (
    # ---------------- 解析设置 ---------------- #
    {
        "key": "DISABLED_PLATFORMS",
        "group": "解析设置",
        "label": "禁用的平台",
        "type": "string",
        "default": "",
        "placeholder": "nga,acfun",
        "hint": "逗号分隔，留空=全部启用。可用平台见本页下方列表，填错的名字会被忽略",
    },
    {
        "key": "VIDEO_DURATION_MAXIMUM",
        "group": "解析设置",
        "label": "视频最大时长",
        "type": "int",
        "default": 480,
        "min": 0,
        "max": 86400,
        "unit": "秒",
        "hint": "超过此时长不下载视频，但仍会返回标题、作者和封面",
    },
    {
        "key": "VIDEO_SIZE_MAXIMUM_MB",
        "group": "解析设置",
        "label": "单个视频体积上限",
        "type": "int",
        "default": 100,
        "min": 1,
        "max": 4096,
        "unit": "MB",
        "hint": "下载前按 Content-Length 判断，边下边判。QQ 大文件上传容易失败，建议不超过 60",
    },
    {
        "key": "SEND_ERROR_MESSAGES",
        "group": "解析设置",
        "label": "解析失败时发送错误提示",
        "type": "bool",
        "default": False,
        "hint": "关闭则只写日志、不在群里刷报错。排查问题时可临时打开",
    },
    # ---------------- B站设置 ---------------- #
    {
        "key": "BILI_CK",
        "group": "B站设置",
        "label": "哔哩哔哩 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "推荐用「总览」页的扫码登录自动写入；配了才能下 1080P 及以上",
    },
    {
        "key": "BILI_QUALITY",
        "group": "B站设置",
        "label": "B站下载清晰度",
        "type": "select",
        "default": "1080P",
        "options": ["360P", "480P", "720P", "1080P", "1080P+", "4K", "8K"],
        "hint": "高画质需要对应账号权限；未登录时实际只能拿到 720P",
    },
    # ---------------- Steam 设置 ---------------- #
    {
        "key": "STEAM_REGION",
        "group": "Steam 设置",
        "label": "价格地区代码",
        "type": "string",
        "default": "cn",
        "placeholder": "cn",
        "hint": "决定官方价格的货币（cn/us/jp 等），同时用作 ITAD 的 country 参数",
    },
    {
        "key": "ITAD_API_KEY",
        "group": "Steam 设置",
        "label": "IsThereAnyDeal API Key",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "只有想看「历史最低价」才需要，免费申请 https://isthereanydeal.com/apps 。留空时 Steam 解析照常工作，只是不显示史低",
    },
    # ---------------- 网页截图 ---------------- #
    {
        "key": "SCREENSHOT_BACKEND",
        "group": "网页截图",
        "label": "截图后端",
        "type": "select",
        "default": "thum",
        "options": ["thum", "cloudflare"],
        "labels": ["thum（免 key）", "cloudflare（需账号）"],
        "hint": "thum=image.thum.io，900×900 视窗图开箱即用；cloudflare 可截长图/等待 JS，但必须有账号",
    },
    {
        "key": "SCREENSHOT_FALLBACK",
        "group": "网页截图",
        "label": "匹配不到平台的链接自动截图",
        "type": "bool",
        "default": False,
        "hint": "默认关闭，避免群里刷图。开启后任何解析不了的 http 链接都会尝试截图",
    },
    {
        "key": "CF_ACCOUNT_ID",
        "group": "网页截图",
        "label": "Cloudflare Account ID",
        "type": "string",
        "default": "",
        "hint": "仅 cloudflare 后端需要，在 CF 控制台右侧栏可复制",
    },
    {
        "key": "CF_API_TOKEN",
        "group": "网页截图",
        "label": "Cloudflare API Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "仅 cloudflare 后端需要，需带 Browser Rendering 写权限",
    },
    # ---------------- 卡片外观 ---------------- #
    {
        "key": "RENDER_ENABLED",
        "group": "卡片外观",
        "label": "启用卡片渲染",
        "type": "bool",
        "default": True,
        "hint": "关闭则回退为纯文本输出",
    },
    {
        "key": "RENDER_THEME",
        "group": "卡片外观",
        "label": "卡片主题",
        "type": "select",
        "default": "dark",
        "options": ["dark", "light"],
        "labels": ["深色", "浅色"],
        "hint": "只影响发送出去的卡片图片，与网页界面主题无关",
    },
    {
        "key": "RENDER_LAYOUT",
        "group": "卡片外观",
        "label": "卡片布局",
        "type": "select",
        "default": "standard",
        "options": ["standard", "magazine", "immersive", "feed"],
        "labels": ["standard 标准", "magazine 杂志", "immersive 沉浸", "feed 信息流"],
        "hint": "切换后可在「解析」页立刻预览效果",
    },
    {
        "key": "RENDER_WIDTH",
        "group": "卡片外观",
        "label": "卡片宽度",
        "type": "int",
        "default": 800,
        "min": 520,
        "max": 1080,
        "unit": "px",
        "hint": "520~1080，越宽信息越舒展，但在聊天软件里显示会更小",
    },
    {
        "key": "RENDER_COVER_FULL_SIZE",
        "group": "卡片外观",
        "label": "封面按原始尺寸展示",
        "type": "bool",
        "default": False,
        "hint": "开启后不裁切封面，长图会完整铺在卡片顶部",
    },
    {
        "key": "RENDER_FONT_PATH",
        "group": "卡片外观",
        "label": "自定义字体文件",
        "type": "string",
        "default": "",
        "hint": "留空自动探测系统字体；卡片出现方块字时才需要指定绝对路径",
    },
    {
        "key": "RENDER_ACCENT_COLOR",
        "group": "卡片外观",
        "label": "自定义强调色",
        "type": "string",
        "default": "",
        "placeholder": "#FB7299",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=每个平台用自己的品牌色。填 #RRGGBB 后所有卡片统一用这个强调色（徽章/光晕/水印圆点）",
    },
    {
        "key": "RENDER_WATERMARK",
        "group": "卡片外观",
        "label": "卡片水印文字",
        "type": "string",
        "default": "希望解析",
        "max_length": 12,
        "hint": "卡片右下角的水印文字，留空则不显示水印，最长 12 个字符",
    },
    {
        "key": "RENDER_DESC_MAX_LINES",
        "group": "卡片外观",
        "label": "正文最大行数",
        "type": "int",
        "default": 0,
        "min": 0,
        "max": 12,
        "unit": "行",
        "hint": "0=按布局默认（4~6 行）。简介超长时按此行数截断，卡片更紧凑",
    },
    {
        "key": "RENDER_SHOW_AVATAR",
        "group": "卡片外观",
        "label": "显示作者头像",
        "type": "bool",
        "default": True,
        "hint": "关闭后作者行只保留昵称与签名，不绘制头像圆（头像也不会再被加载）",
    },
    {
        "key": "RENDER_GRADIENT_TOP",
        "group": "卡片外观",
        "label": "背景渐变·顶部色",
        "type": "string",
        "default": "",
        "placeholder": "#242B3F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的顶部颜色",
    },
    {
        "key": "RENDER_GRADIENT_BOTTOM",
        "group": "卡片外观",
        "label": "背景渐变·底部色",
        "type": "string",
        "default": "",
        "placeholder": "#12161F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的底部颜色",
    },
    # ---------------- 媒体发送 ---------------- #
    {
        "key": "CACHE_DIR",
        "group": "媒体发送",
        "label": "共享缓存目录",
        "type": "string",
        "default": "",
        "placeholder": "/app/sharedFolder/denia_share/cache",
        "hint": (
            "① 本地文件方式：填一个「所有容器都把宿主机同一目录挂到它上面」的容器内路径，"
            "协议端（NapCat 等）就能直接读走视频。留空=用插件数据目录 —— 分容器部署时协议端读不到，"
            "视频会发不出去（图片语音不受影响，它们会转成 base64）"
        ),
    },
    {
        "key": "MEDIA_RELAY_ENABLED",
        "group": "媒体发送",
        "label": "启用媒体中转",
        "type": "bool",
        "default": False,
        "hint": (
            "② 链接方式：把已下载的视频注册成 AstrBot 的临时 HTTP 链接（/api/file/<token>）再发送。"
            "协议端只要能访问到下面的回调地址即可，不需要共享挂载；注册失败会自动回退本地文件"
        ),
    },
    {
        "key": "MEDIA_RELAY_CALLBACK_URL",
        "group": "媒体发送",
        "label": "AstrBot 回调地址",
        "type": "string",
        "default": "",
        "placeholder": "http://astrbot:6185",
        "hint": (
            "留空时回退 AstrBot 全局 callback_api_base。必须填「消息平台所在容器能访问到」的地址，"
            "同一 Docker 网络内可直接用容器名，如 http://astrbot:6185"
        ),
    },
    {
        "key": "MEDIA_RELAY_TTL",
        "group": "媒体发送",
        "label": "中转链接有效期",
        "type": "int",
        "default": 300,
        "min": 30,
        "max": 86400,
        "unit": "秒",
        "hint": "到期后链接失效。视频越大、链路越慢越要留足时间，建议不低于 120 秒",
    },
    # ---------------- 高级设置 ---------------- #
    {
        "key": "XHS_CK",
        "group": "高级设置",
        "label": "小红书 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "部分笔记需要登录态才能完整解析，从浏览器复制 xiaohongshu.com 的 cookie",
    },
    {
        "key": "PIXIV_CK",
        "group": "高级设置",
        "label": "Pixiv Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "留空也能搜索。填了收录更全，但搜索结果会出现 R18 —— 插件对非全年龄内容有硬性过滤，不会放宽",
    },
    {
        "key": "GITHUB_TOKEN",
        "group": "高级设置",
        "label": "GitHub Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "强烈建议填。免 token 限额 60 次/小时且按出口 IP 计算，共享 IP 下极易被限流；填后 5000 次/小时",
    },
    {
        "key": "PROXY",
        "group": "高级设置",
        "label": "全局代理",
        "type": "string",
        "default": "",
        "placeholder": "http://127.0.0.1:7897",
        "hint": "留空不使用。作用于下载与自建请求（GitHub/Pixiv/截图）。填运行 AstrBot 那台机器上的地址",
    },
    {
        "key": "TWITTER_MEDIA_PROXY_BASE",
        "group": "高级设置",
        "label": "Twitter 图片/视频反代根地址",
        "type": "string",
        "default": "",
        "placeholder": "https://your-proxy.example",
        "hint": "留空=不启用。服务器连不上 twimg 时填写",
    },
    # ---------------- 维护 ---------------- #
    {
        "key": "CACHE_TTL_HOURS",
        "group": "维护",
        "label": "缓存保留时长",
        "type": "int",
        "default": 24,
        "min": 0,
        "max": 8760,
        "unit": "小时",
        "hint": "超过此时间未使用的缓存会被自动清理，设为 0 禁用自动清理",
    },
    {
        "key": "CACHE_CLEANUP_INTERVAL_MINUTES",
        "group": "维护",
        "label": "缓存清理间隔",
        "type": "int",
        "default": 60,
        "min": 1,
        "max": 1440,
        "unit": "分钟",
        "hint": "后台清理任务多久跑一次，改动需重载插件后才生效",
    },
    {
        "key": "DEBUG_LOG_ENABLED",
        "group": "维护",
        "label": "输出调试日志",
        "type": "bool",
        "default": True,
        "hint": "关闭后只保留警告与错误日志，排查问题时可打开",
    },
)

_ITEM_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in CONFIG_META}

CONFIG_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    group: tuple(item["key"] for item in CONFIG_META if item["group"] == group)
    for group, _ in CONFIG_GROUPS
}

_KEY_GROUP_MAP: dict[str, str] = {
    key: group for group, keys in CONFIG_GROUP_KEYS.items() for key in keys
}

_DEFAULTS: dict[str, Any] = {item["key"]: item["default"] for item in CONFIG_META}


def config_meta_payload() -> dict[str, Any]:
    """返回供 WebUI 渲染的配置元数据（分组 + 全部配置项定义）。"""
    return {
        "groups": [
            {
                "name": name,
                "description": description,
                "keys": list(CONFIG_GROUP_KEYS.get(name, ())),
            }
            for name, description in CONFIG_GROUPS
        ],
        "items": [dict(item) for item in CONFIG_META],
    }


def coerce_value(item: dict[str, Any], raw: Any) -> tuple[Any, str | None]:
    """把前端传来的值转成配置要求的类型。

    Returns:
        (转换后的值, 错误描述)。错误描述非 None 时调用方应丢弃该值。
    """
    key = item["key"]
    label = item.get("label", key)
    kind = item["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True, None
            if lowered in {"false", "0", "no", "off", ""}:
                return False, None
        return None, f"{label}：需要是开关值"

    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, f"{label}：需要是整数"
        low, high = item.get("min"), item.get("max")
        if low is not None and value < low:
            return None, f"{label}：不能小于 {low}"
        if high is not None and value > high:
            return None, f"{label}：不能大于 {high}"
        return value, None

    text = "" if raw is None else str(raw).strip()
    if kind == "select":
        options = item.get("options") or []
        if text and text not in options:
            return None, f"{label}：只能是 {' / '.join(options)} 之一"
    if kind == "text" and len(text) > 8000:
        return None, f"{label}：内容过长（超过 8000 字）"
    max_length = item.get("max_length")
    if max_length is not None and len(text) > int(max_length):
        return None, f"{label}：不能超过 {max_length} 个字符"
    pattern = item.get("pattern")
    if pattern and text:
        import re

        if not re.match(pattern, text):
            return None, f"{label}：格式不正确（需要 {pattern}）"
    return text, None


def verify_schema_alignment(schema: dict[str, Any]) -> list[str]:
    """自检 ``_conf_schema.json`` 与 :data:`CONFIG_META` 的键集合是否一致。

    AstrBot 加载插件配置时会剔除 schema 中不存在的键（``check_config_integrity``），
    两者一旦不一致，用户保存的值会在下次重载时静默丢失，因此必须显式比对。

    Returns:
        描述差异的字符串列表，空列表表示一致。
    """
    schema_keys: set[str] = set()
    schema_group_of: dict[str, str] = {}
    problems: list[str] = []

    for group, node in schema.items():
        if not isinstance(node, dict):
            problems.append(f"schema 分组 {group} 不是对象")
            continue
        items = node.get("items")
        if not isinstance(items, dict):
            problems.append(f"schema 分组 {group} 缺少 items")
            continue
        for key in items:
            schema_keys.add(key)
            schema_group_of[key] = group

    meta_keys = set(_ITEM_BY_KEY)
    for key in sorted(schema_keys - meta_keys):
        problems.append(f"schema 有而 CONFIG_META 缺失的键：{key}")
    for key in sorted(meta_keys - schema_keys):
        problems.append(f"CONFIG_META 有而 schema 缺失的键：{key}")
    for key in sorted(schema_keys & meta_keys):
        if schema_group_of[key] != _KEY_GROUP_MAP.get(key):
            problems.append(
                f"{key} 在 schema 里属于 {schema_group_of[key]} 组，"
                f"但 CONFIG_META 归到 {_KEY_GROUP_MAP.get(key)} 组"
            )
    for group in schema:
        if group not in CONFIG_GROUP_KEYS:
            problems.append(f"schema 里存在 CONFIG_GROUPS 未声明的分组：{group}")
    return problems


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

    # ---------------- WebUI 读写 ---------------- #

    def current_values(self) -> dict[str, Any]:
        """返回所有配置项的当前生效值（键 → 值）。"""
        return {
            item["key"]: self._cfg_get(item["key"], item["default"])
            for item in CONFIG_META
        }

    def apply_updates(self, payload: Any) -> tuple[list[str], list[str]]:
        """按白名单把 WebUI 提交的值写回配置。

        只接受 :data:`CONFIG_META` 里声明的键；写入分组位置的同时把扁平旧键重置为
        默认值，避免遗留的旧值把新值盖掉（与 :func:`migrate_grouped_config` 同源）。

        Args:
            payload: 前端提交的 ``{配置键: 新值}``。

        Returns:
            (实际变更的键列表, 错误信息列表)。
        """
        if not isinstance(payload, dict):
            return [], ["提交内容不是对象"]

        changed: list[str] = []
        errors: list[str] = []

        for key, raw in payload.items():
            item = _ITEM_BY_KEY.get(key)
            if item is None:
                errors.append(f"未知配置项：{key}")
                continue
            value, error = coerce_value(item, raw)
            if error is not None:
                errors.append(error)
                continue
            if self._cfg_get(key, item["default"]) == value:
                continue
            group_cfg = self._cfg.get(_KEY_GROUP_MAP[key])
            if not isinstance(group_cfg, dict):
                group_cfg = {}
                self._cfg[_KEY_GROUP_MAP[key]] = group_cfg
            group_cfg[key] = value
            self._cfg[key] = item["default"]
            changed.append(key)

        if changed:
            self.save()
        return changed, errors

    def reset_to_defaults(self) -> list[str]:
        """把所有配置项恢复为默认值，返回被改动的键列表。"""
        changed, _ = self.apply_updates(
            {item["key"]: item["default"] for item in CONFIG_META}
        )
        return changed

    def save(self) -> bool:
        """持久化配置（AstrBotConfig 提供 save_config，缺失时静默跳过）。"""
        save = getattr(self._cfg, "save_config", None)
        if not callable(save):
            return False
        try:
            save()
            return True
        except Exception:
            return False

    # ---------------- 平台 ---------------- #

    @property
    def DISABLED_PLATFORMS(self) -> list[str]:
        raw = self._cfg_get("DISABLED_PLATFORMS", "")
        if not raw:
            return []
        return [p.strip().lower() for p in str(raw).split(",") if p.strip()]

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

    # ---------------- Steam ---------------- #

    @property
    def STEAM_REGION(self) -> str:
        """Steam 价格地区代码（同时用于 ITAD 的 country 参数）。"""
        return str(self._cfg_get("STEAM_REGION", "cn") or "cn").strip().lower()

    @property
    def ITAD_API_KEY(self) -> str:
        """IsThereAnyDeal API key（可选）。

        只有想要「历史最低价」才需要。免费申请：https://isthereanydeal.com/apps
        留空时 Steam 解析照常工作，只是不显示史低。
        """
        return str(self._cfg_get("ITAD_API_KEY", "") or "").strip()

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

    @property
    def RENDER_ACCENT_COLOR(self) -> str:
        """自定义强调色（#RRGGBB），留空表示按平台品牌色。"""
        value = str(self._cfg_get("RENDER_ACCENT_COLOR", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_WATERMARK(self) -> str:
        """卡片右下角水印文字，留空表示不显示。"""
        return str(self._cfg_get("RENDER_WATERMARK", "希望解析") or "").strip()[:12]

    @property
    def RENDER_DESC_MAX_LINES(self) -> int:
        """正文最大行数，0 表示按布局默认。"""
        return max(0, min(12, int(self._cfg_get("RENDER_DESC_MAX_LINES", 0))))

    @property
    def RENDER_SHOW_AVATAR(self) -> bool:
        return bool(self._cfg_get("RENDER_SHOW_AVATAR", True))

    @property
    def RENDER_GRADIENT_TOP(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_TOP", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_GRADIENT_BOTTOM(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_BOTTOM", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    def renderer_options(self) -> dict[str, Any]:
        """返回构造 ShareCardRenderer 的关键字参数（配置的唯一装配点）。

        main.py 的常驻渲染器与 webui.py 的临时预览渲染器都用它，
        新增外观项只需要在这里和 ``_build_renderer`` 的覆盖层各加一行。
        """
        return {
            "enabled": self.RENDER_ENABLED,
            "width": self.RENDER_WIDTH,
            "theme": self.RENDER_THEME,
            "font_path": self.RENDER_FONT_PATH or None,
            "layout": self.RENDER_LAYOUT,
            "cover_full_size": self.RENDER_COVER_FULL_SIZE,
            "accent_color": self.RENDER_ACCENT_COLOR or None,
            "watermark": self.RENDER_WATERMARK,
            "desc_max_lines": self.RENDER_DESC_MAX_LINES,
            "show_avatar": self.RENDER_SHOW_AVATAR,
            "gradient_top": self.RENDER_GRADIENT_TOP or None,
            "gradient_bottom": self.RENDER_GRADIENT_BOTTOM or None,
        }

    # ---------------- 媒体发送 ---------------- #

    @property
    def CACHE_DIR(self) -> str:
        """Docker 共享缓存目录，留空表示用插件数据目录（默认行为）。

        与 yaya 的差别：那边在容器里会把默认值切成自己约定的
        ``/app/sharedFolder/...``；这里保守一些 —— 留空始终用插件数据目录，
        免得用户没挂载却被切到一个不可写路径上，把下载整个搞坏。
        """
        return str(self._cfg_get("CACHE_DIR", "") or "").strip()

    @property
    def MEDIA_RELAY_ENABLED(self) -> bool:
        """是否把已下载的视频注册成 AstrBot 的临时 HTTP 链接再发送。"""
        return bool(self._cfg_get("MEDIA_RELAY_ENABLED", False))

    @property
    def MEDIA_RELAY_CALLBACK_URL(self) -> str:
        """中转链接用的回调地址；留空时回退 AstrBot 全局 callback_api_base。"""
        return str(self._cfg_get("MEDIA_RELAY_CALLBACK_URL", "") or "").strip().rstrip("/")

    @property
    def MEDIA_RELAY_TTL(self) -> int:
        """中转链接有效期（秒），最小 30。"""
        return max(30, int(self._cfg_get("MEDIA_RELAY_TTL", 300)))

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
