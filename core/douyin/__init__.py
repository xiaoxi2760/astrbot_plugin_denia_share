"""抖音取数层。

- :mod:`sign` —— a_bogus 签名（纯 Python，移植自 Johnserf-Seed/f2，Apache-2.0）
- :mod:`web`  —— Web 详情接口客户端（ttwid 会话 + 签名请求）
"""

from .web import DouyinWebClient

__all__ = ["DouyinWebClient"]
