"""小黑盒解析器包。

本包只放小黑盒平台自己的实现：

- ``parser.py`` —— ``XiaoheiheParser``：链接识别、BBS 帖子 / 游戏数据取回、
  正文与媒体映射、``ParseResult`` 组装。
- ``sign.py`` —— 网页端 ``hkey`` 路径签名；纯标准库，不涉及登录态。

``__init__.py`` 保持为纯文档：解析器由 ``core/parsers/__init__.py`` 或宿主
按需从 ``.parser`` 导入，避免本包在导入阶段引入网络或可选的宿主依赖。
"""
