"""快手解析器包。

- :mod:`.parser` —— 解析主流程（分享页解析，兼容 photo 为 JSON 字符串的新版页面）。
- :mod:`.models` —— 预留的数据模型目录。

本文件只放文档、不放 import：解析器由 ``core/parsers/__init__.py`` 直接从
``.parser`` 导入，避免导入子模块时先触发父包副作用。
"""
