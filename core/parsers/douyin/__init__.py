"""抖音解析器

拆成三个文件，避免单个模块过大且职责混杂：
- `parser.py` 解析主流程（HTML 直取 + 签名接口双路径）
- `sign.py`  a_bogus 签名（移植自 Johnserf-Seed/f2，Apache-2.0）
- `web.py`   网页客户端（ttwid 会话管理 + 接口调用）
"""

from .parser import DouyinParser
from .web import DouyinWebClient

__all__ = ["DouyinParser", "DouyinWebClient"]
