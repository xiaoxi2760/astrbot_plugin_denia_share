"""平台真实图标加载（纯本地、离线、可安全叠加）。

**为什么要有这个模块**：卡片左上角原先画的是一枚「彩色圆点」——它只靠
``card_renderer.PLATFORM_COLORS`` 里的一个颜色来代表平台。QQ 音乐是黄绿色的
音符、网易云音乐是红色的音符、酷狗是蓝色的 K，光看一个圆点，用户根本认不出
这是哪个平台；颜色还可能和卡片主题撞色。

这里改成加载**平台官方 favicon**（打包在 ``resources/platforms/`` 下的 PNG），
由卡片渲染层用 ``alpha_composite`` 贴上去。

设计约束（这几条都是踩过坑才定下来的）：

- **纯本地**：图标是随插件打包的静态资源，读取路径绝不联网。插件在离线环境、
  内网环境运行都必须是零请求。
- **资源路径相对 ``__file__`` 构造**：插件被安装到 AstrBot 的
  ``data/plugins/`` 下，运行时 CWD 是别的目录。用 ``__file__`` 反推才能在任何
  工作目录下都找得到资源；同时保留一条 ``importlib.resources`` 兜底路径，
  打包成 zip（zipimport）时也能读到。
- **不认识就返回 ``None``**：调用方拿到 ``None`` 就退回原来的彩色圆点。
  新增平台、自定义平台、拼错的平台名都不该在这里抛异常 —— 一个装饰性图标
  崩掉整张卡片是不可接受的。
- **返回的是独立副本**：调用方要拿它去 ``alpha_composite``、可能要再缩放、
  可能要改 alpha。缓存里的对象一旦被交出去就会被就地改坏，下一个调用方拿到的
  就是被污染的图标。所以缓存里存的是「基准图」，每次返回前先 ``copy()``。
- **缓存有上限**：图标是有限几种平台，用 ``lru_cache`` 限流即可，避免长期运行
  的进程把一堆 Pillow 对象留在内存里。

对外只暴露一个入口 :func:`load_platform_icon`；:func:`get_platform_icon` 是
同义别名，方便调用方按习惯取名。
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

__all__ = [
    "load_platform_icon",
    "get_platform_icon",
    "has_platform_icon",
    "available_platforms",
    "PLATFORM_ICONS",
]

# ---------------------------------------------------------------------------
# 资源定位
# ---------------------------------------------------------------------------

#: 图标资源目录。``core/platform_icons.py`` 的上一级就是插件根目录，
#: 所以 ``resources/platforms/`` 一定在插件包内。
_ICON_DIR: Path = Path(__file__).resolve().parent.parent / "resources" / "platforms"

#: 平台键 → 资源文件名。
PLATFORM_ICONS: dict[str, str] = {
    "qqmusic": "qqmusic.png",
    "netease": "netease.png",
    "kugou": "kugou.png",
}

#: 别名 → 平台键。键全部小写；值里同时收了英文缩写、中文正式名、
#: 常见口语名，以及 NetEase 的两种拼写（netease / netase）。
_ALIASES: dict[str, str] = {
    # QQ 音乐
    "qq": "qqmusic",
    "qqmusic": "qqmusic",
    "qq music": "qqmusic",
    "qq_music": "qqmusic",
    "qq音乐": "qqmusic",
    "腾讯音乐": "qqmusic",
    # 网易云音乐
    "netease": "netease",
    "netase": "netease",
    "netease music": "netease",
    "netease_cloud": "netease",
    "neteasecloudmusic": "netease",
    "163": "netease",
    "网易云": "netease",
    "网易云音乐": "netease",
    "网易": "netease",
    "云音乐": "netease",
    # 酷狗
    "kugou": "kugou",
    "kg": "kugou",
    "酷狗": "kugou",
    "酷狗音乐": "kugou",
}

#: 单张图标的最大边长上限，防止调用方传入离谱的尺寸把内存撑爆。
_MAX_SIZE = 256

#: 基准图缓存上限。平台数量是固定的十几种，这里给 32 绰绰有余。
_CACHE_SIZE = 32


def _normalize(platform: str) -> str:
    """把用户/调用方给的各种平台写法归一成平台键。

    归一化步骤：去空白 → 小写 → 查别名表。查不到就原样（小写化）返回，
    由调用方判断是不是认识的平台。
    """
    if platform is None:
        return ""
    text = str(platform).strip().lower()
    if not text:
        return ""
    return _ALIASES.get(text, text)


def _read_asset_bytes(filename: str) -> bytes | None:
    """读取图标资源的原始字节，失败返回 ``None``（不抛异常）。

    主路径是文件系统读取（插件是普通目录，解压即用）。如果插件被打包成
    zip（zipimport），文件系统路径不存在，退到 ``importlib.resources``。
    """
    try:
        return (_ICON_DIR / filename).read_bytes()
    except OSError:
        pass

    # zipimport 兜底：按插件包名逐级尝试。
    try:
        from importlib import resources as _resources

        candidates: list[str] = []
        pkg = __package__ or ""
        if pkg:
            parts = pkg.split(".")
            for i in range(len(parts), 0, -1):
                candidates.append(".".join(parts[:i]))
        for name in candidates:
            try:
                root = _resources.files(name)
            except (ImportError, TypeError, ValueError):
                continue
            try:
                return (root / "resources" / "platforms" / filename).read_bytes()
            except (FileNotFoundError, NotADirectoryError, OSError, TypeError):
                continue
    except Exception:  # pragma: no cover - 兜底路径本身不该影响主流程
        return None
    return None


@lru_cache(maxsize=_CACHE_SIZE)
def _load_base_image(platform: str) -> Image.Image | None:
    """加载并缓存平台基准图（RGBA，原始分辨率）。

    缓存的是**不会被交出去**的基准图，:func:`load_platform_icon` 每次都会
    ``copy()`` 一份再返回，所以缓存不会被调用方污染。

    Args:
        platform: 已归一化的平台键。

    Returns:
        RGBA 图像；平台未知或资源缺失时返回 ``None``。
    """
    if Image is None:
        return None
    filename = PLATFORM_ICONS.get(platform)
    if not filename:
        return None
    data = _read_asset_bytes(filename)
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return im.convert("RGBA")
    except Exception:
        return None


def load_platform_icon(platform: str, size: int = 24) -> Image.Image | None:
    """加载平台官方图标。

    Args:
        platform: 平台名。接受平台键（``qqmusic`` / ``netease`` / ``kugou``）、
            常见别名（``qq`` / ``netase``）、中文名（``QQ音乐`` / ``网易云音乐`` /
            ``酷狗``），以及 :class:`core.constants.PlatformEnum` 的值。
        size: 目标边长（正方形），默认 24。

    Returns:
        一张**独立的** RGBA 图像（每次调用都是新对象，调用方可以随意
        ``alpha_composite`` / 缩放 / 改 alpha，不会影响后续调用）。
        平台不认识、图标资源缺失、或 ``size`` 非法时返回 ``None``，
        由调用方退回彩色圆点兜底。
    """
    key = _normalize(platform)
    if not key:
        return None

    try:
        target = int(size)
    except (TypeError, ValueError):
        return None
    if target <= 0 or target > _MAX_SIZE:
        return None

    base = _load_base_image(key)
    if base is None:
        return None

    # copy() 保证缓存里的基准图不会被调用方就地改坏；
    # 尺寸相同时也照样 copy，返回的永远是独立对象。
    icon = base.copy()
    if icon.size != (target, target):
        icon = icon.resize((target, target), Image.LANCZOS)
    if icon.mode != "RGBA":
        icon = icon.convert("RGBA")
    return icon


#: :func:`load_platform_icon` 的别名，方便调用方按习惯取名。
get_platform_icon = load_platform_icon


def has_platform_icon(platform: str) -> bool:
    """该平台是否有可用的官方图标（不读像素，只查资源是否存在）。"""
    key = _normalize(platform)
    if not key:
        return False
    return _load_base_image(key) is not None


def available_platforms() -> list[str]:
    """列出当前**实际可用**图标的平台键（资源缺失的会被剔除）。"""
    return [key for key in PLATFORM_ICONS if _load_base_image(key) is not None]
