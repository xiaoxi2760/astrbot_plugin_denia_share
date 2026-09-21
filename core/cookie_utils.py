"""Cookie 工具函数"""

from pathlib import Path


def ck2dict(cookies_str: str) -> dict[str, str]:
    """将 cookies 字符串转换为字典"""
    res = {}
    for cookie in cookies_str.split(";"):
        cookie = cookie.strip()
        if not cookie or "=" not in cookie:
            continue
        name, value = cookie.split("=", 1)
        res[name] = value
    return res
