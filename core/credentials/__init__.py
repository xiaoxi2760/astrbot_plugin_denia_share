"""统一凭据后端（第一版：B站 / 小红书 / Pixiv / GitHub）。

对外只暴露三样东西：

* :class:`CredentialManager` —— 快照 / 导入保存 / 主动检查 / 清除；
* :class:`CredentialError` —— 面向用户的安全错误（**不含原文、不含请求头**）；
* :func:`public_config_values` —— 配置接口脱敏（secret 只回 ``configured``）。

本包不修改主模块路由，也不碰前端；薄接路由由集成方完成。
"""

from __future__ import annotations

from .importers import CredentialImportError
from .service import (
    CHECK_TARGETS,
    PLATFORMS,
    STATUS_ERROR,
    STATUS_VALID,
    CredentialError,
    CredentialManager,
    public_config_values,
)

__all__ = [
    "CHECK_TARGETS",
    "CredentialError",
    "CredentialImportError",
    "CredentialManager",
    "PLATFORMS",
    "STATUS_ERROR",
    "STATUS_VALID",
    "public_config_values",
]
