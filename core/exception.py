"""异常类定义"""


class ParseException(Exception):
    """解析失败。

    ``notify_prefix`` 是发给用户时的文案前缀，做成类属性而不是靠 ``except``
    的书写顺序区分 —— 子类关系决定捕获顺序，一旦顺序写反就会出现
    「永远不执行的分支」，而且读代码时看不出来。
    """

    notify_prefix: str = "❌ 解析失败:"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DownloadException(ParseException):
    """下载异常"""

    notify_prefix = "⚠️ 下载失败:"

    def __init__(self, message: str | None = None):
        super().__init__(message or "媒体下载失败")


class IgnoreException(ParseException):
    """可忽略异常"""

    notify_prefix = "ℹ️"

    def __init__(self, message: str | None = None):
        super().__init__(message or "可忽略异常")


class TipException(ParseException):
    """提示异常"""


class SilentException(ParseException):
    """静默异常 - 不发送通知，仅静默忽略"""

    notify_prefix = ""

