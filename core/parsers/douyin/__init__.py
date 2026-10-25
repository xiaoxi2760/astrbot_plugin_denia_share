"""抖音解析器包。

拆成多个文件，避免单个模块过大且职责混杂：

- :mod:`.parser` —— 解析主流程（HTML 直取 + 签名接口双路径）
- :mod:`.sign`   —— a_bogus 签名（移植自 Johnserf-Seed/f2，Apache-2.0）
- :mod:`.web`    —— 网页客户端（ttwid 会话管理 + 接口调用）
- :mod:`.session` —— 分享页会话复用
- :mod:`.models` —— 接口数据模型（video / slides）

"""

from .parser import DouyinParser
from .web import DouyinWebClient

__all__ = ["DouyinParser", "DouyinWebClient"]
