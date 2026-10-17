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


def _format_mb(byte_count: int) -> str:
    """把字节数写成人类看得懂的体积（不足 1MB 时退化成 KB）。"""
    if byte_count >= 1024 * 1024:
        return f"{byte_count / 1024 / 1024:.1f}MB"
    if byte_count >= 1024:
        return f"{byte_count // 1024}KB"
    return f"{byte_count}B"


class MediaSizeExceeded(IgnoreException):
    """媒体体积超过上限 —— 是「**故意不下载**」，但用户有权知道是哪一条。

    为什么不直接用普通 ``IgnoreException``：那在本仓是「按策略跳过」的通用信号，
    而审计（``ParseResult.audit_missing_media``）正是靠**异常类型**区分「跳过」
    与「真失败」—— 靠匹配异常字符串认体积超限，等于把这条判定钉死在文案上，
    改一次措辞就静默失效。所以单独成类：**判定靠类型，展示才靠文案**。

    时长超限、0 字节响应、m3u8 分片超限不属于体积超限，不能归入本类型。
    时长等已有提示由原路径保留，避免重复或错误归类。

    ``received_bytes`` 在流式中断时是「已经下到的字节数」（不是文件真实大小），
    ``limit_bytes`` 是生效上限（``min(用户配置, 硬上限)``）。
    """

    def __init__(self, *, received_bytes: int, limit_bytes: int):
        self.received_bytes = received_bytes
        self.limit_bytes = limit_bytes
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:
        """给用户看的一句话（进 ``limit_warnings``，卡片与纯文字链路共用）。"""
        return (
            f"⚠️ 媒体大小({_format_mb(self.received_bytes)})"
            f"超过上限({_format_mb(self.limit_bytes)})，跳过下载"
        )


class MediaProcessException(ParseException):
    """本机媒体处理失败 —— 是本机环境问题，不是网络 / CDN 故障。

    目前只有一类来源：本机没装 ffmpeg（或它不在进程的 ``PATH`` 里）。B站高清视频是
    音视频分离流，只能靠 ffmpeg 合并 —— 所以这类错误**换 CDN 不会有任何改善**。
    B站的 CDN 重试循环据此豁免它：不再逐个备用地址重试（每轮都会把整段视频重下一遍），
    而是直接打断。
    """

    notify_prefix = "⚠️ 媒体处理失败:"


class TipException(ParseException):
    """提示异常"""


class SilentException(ParseException):
    """静默异常 - 不发送通知，仅静默忽略"""

    notify_prefix = ""

