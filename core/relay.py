"""媒体中转：把已下载到本地的媒体注册成 AstrBot 的临时 HTTP 链接。

为什么需要它
------------
AstrBot 把插件发出去的媒体交给协议端时，各类消息段处理方式并不一样
（见 ``astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py``）：

- **图片 / 语音** 会先 ``convert_to_base64()`` 转成 ``base64://``，不碰文件系统，
  所以协议端在另一个容器里也能收到；
- **视频** 走 ``to_dict()`` 原样发出去，字段就是 ``file:///AstrBot/data/...``
  这种**容器内绝对路径**。协议端读不到这个路径，视频就发不出去。

两条解法：

1. **共享目录**（配置 ``CACHE_DIR``）：让 astrbot 与协议端两个容器把宿主机同一目录
   挂成同一个容器内路径，路径对双方都成立。
2. **媒体中转**（本模块）：把文件注册进 AstrBot 的 ``file_token_service``，
   拿到 ``{回调地址}/api/file/<token>``，用 ``Video.fromURL`` 发送 —— 协议端只要能
   访问到那个地址就行，不需要共享挂载。

本模块移植自作者自己的 yaya（astrbot_plugin_media_parser）里的
``core/storage/file_token.py``，去掉了元数据回填，改成直接返回 URL 列表。
"""

from __future__ import annotations

import os

from astrbot.api import logger


def _astrbot_module():
    """取 astrbot 的 file_token_service 与全局配置；不可用时返回 (None, None)。"""
    try:
        from astrbot.core import astrbot_config, file_token_service
    except ImportError:
        logger.warning(
            "[denia_share] 无法导入 astrbot.core 的 file_token_service，"
            "媒体中转不可用，将回退为本地文件发送"
        )
        return None, None
    return file_token_service, astrbot_config


def resolve_callback_base(configured: str) -> str:
    """确定中转链接的根地址：插件配置优先，其次 AstrBot 全局 callback_api_base。"""
    base = str(configured or "").strip().rstrip("/")
    if base:
        return base

    _, astrbot_config = _astrbot_module()
    if astrbot_config is None:
        return ""
    try:
        return str(astrbot_config.get("callback_api_base") or "").strip().rstrip("/")
    except Exception:
        logger.debug("读取 AstrBot callback_api_base 失败", exc_info=True)
        return ""


async def register_file(
    file_path: str | os.PathLike,
    callback_base: str,
    ttl_seconds: int,
) -> str | None:
    """把单个本地文件注册成可回调的临时 URL。

    Args:
        file_path: 本地文件路径。
        callback_base: 回调根地址，形如 ``http://astrbot:6185``（不带尾斜杠）。
        ttl_seconds: 链接有效期（秒）。

    Returns:
        可访问的 URL；任何一步失败都返回 None，由调用方回退成本地文件发送。
    """
    path = str(file_path or "")
    if not path or not os.path.isfile(path):
        return None

    base = resolve_callback_base(callback_base)
    if not base:
        logger.warning(
            "[denia_share] 媒体中转已启用但没有可用的回调地址，将回退为本地文件发送；"
            "请配置「AstrBot 回调地址」或 AstrBot 全局 callback_api_base"
        )
        return None
    if not base.startswith(("http://", "https://")):
        logger.warning(
            f"[denia_share] 回调地址缺少 http(s) 协议头（{base}），回退为本地文件发送"
        )
        return None

    file_token_service, _ = _astrbot_module()
    if file_token_service is None:
        return None

    try:
        token = await file_token_service.register_file(path, timeout=ttl_seconds)
    except Exception as exc:
        logger.warning(f"[denia_share] 注册文件到 Token 服务失败: {path}, 错误: {exc}")
        return None

    logger.debug(f"[denia_share] 已注册媒体中转: {path} -> /api/file/{token}")
    return f"{base}/api/file/{token}"
