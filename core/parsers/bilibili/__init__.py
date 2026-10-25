"""B站解析器包。

目录职责：

- :mod:`.parser` —— 主力解析器（依赖 ``bilibili-api-python``，可选依赖）。
- :mod:`.fallback` —— 自建备用取流实现；主力依赖不可用时由
  ``core/parsers/__init__.py`` 直接改用 ``.fallback.parser``。
- :mod:`.access` —— 试看 / 受限片段判定的公共逻辑。
- :mod:`.models` —— B站接口的 msgspec 数据模型。

⚠️ 本文件只放文档、**不放 import**。``core/parsers/__init__.py`` 必须能在
``bilibili_api`` 缺失时退到 ``.fallback.parser``；若在这里 import ``.parser``，
那么连 ``.fallback`` 都还没机会加载就会先炸在父包上 —— 父包总是先于子模块执行。
"""
