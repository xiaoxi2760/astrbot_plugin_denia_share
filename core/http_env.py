"""环境代理的已知地雷：``NO_PROXY`` 里的方括号 IPv6 会让 httpx 构造客户端时直接抛异常。

**现象**：httpx 0.28 在 ``AsyncClient()`` 构造阶段就把 ``NO_PROXY`` 的每一项当成
模式处理（``httpx/_utils.py`` 的 ``get_environment_proxies`` → ``URLPattern``），
``[::1]`` 最终炸在 ``httpx/_urlparse.py`` 的 ``normalize_port``，报
``httpx.InvalidURL: Invalid port: ':1]'``。

**为什么会炸**（双重误读，实测链路）：

1. ``httpx/_utils.py`` 的 ``is_ipv6_hostname('[::1]')`` 返回 **False** ——
   它内部用 ``ipaddress.IPv6Address()``，而 stdlib **拒绝带方括号**的 IPv6 字面量；
2. 于是 ``[::1]`` 掉进最后的「主机名模式」分支，变成 ``all://*[::1]``；
3. 解析这个模式时 host 吃掉 ``*``，剩下的 ``:1]`` 被当成端口 → ``int(":1]")`` 抛异常。

也就是说 **httpx 想要的输入本来就是裸写法** ``::1``：走 ``is_ipv6_hostname``
为真的分支后，httpx 自己会把括号加回去（``all://[::1]``）。

**为什么危险**：这不是网络问题，是**构造客户端时就炸**，一个请求都没发出去。
表现是「所有平台解析全挂、下载也挂」，堆栈却指向 httpx 内部，怎么看都不像
配置问题。插件里所有客户端都走 ``trust_env=True``（环境变量代理与插件代理是两条
独立来源，见 ``BaseParser.client_kwargs``），所以这条路径是常态而非边角。

**为什么值得兜底**：``NO_PROXY`` 里写 ``[::1]`` 是 Windows 上的常见写法
（Clash Verge 等代理客户端写的就是这个形式）。实测 httpx 0.28.1 的接受度：

===========================  ==========  =========================================
``NO_PROXY`` 条目             httpx       本兜底能否修
===========================  ==========  =========================================
``::1`` / ``fe80::1``        OK         —（httpx 想要的就是这种）
``127.0.0.1`` / ``localhost`` OK         —
``.local`` / ``*``           OK         —
``example.com:8080``         OK         —
``10.0.0.0/8``               OK         —
``[::1]`` / ``[::]``         **RAISE**  ✅
``[fe80::1]``                **RAISE**  ✅
``http://[::1]:8080``        OK         —（scheme URL 路径支持）
``https://[2001:db8::1]``    OK         —（scheme URL 路径支持）
``[2001:db8::1]:8080``       **RAISE**  ❌ 不做子串改写
``[127.0.0.1]``              OK         ✅（绕过键写错，顺手修对）
``127.0.0.1:abc``            **RAISE**  ❌ 端口不是数字，与方括号无关
``[::1`` / ``::1]``          **RAISE**  ❌ 括号不配对，无法安全猜测原意
``[::1]:``                   **RAISE**  ❌
===========================  ==========  =========================================

**带端口条目的边界**：``[2001:db8::1]:8080`` 不是本兜底支持的形式。
若只去括号，httpx 会把 ``2001:db8::1:8080`` 当成另一个 IPv6 主机、静默吞掉端口；
所以函数会原样保留此条目。当前 httpx 可能因此在构造客户端时抛 InvalidURL，且本兜底
不提供这种条目的精确绕过能力。不要把去括号说成语义等价。

**上游状态**：httpx 0.27.2 / 0.28.0 / 0.28.1 / master 的相关代码逐字相同，
**未修**。所以这个兜底不能靠升级 httpx 撤掉；等哪天上游修了，
``tests/test_http_env.py`` 里的对照组用例会自己 ``skipTest`` 提醒退役。

**对策**：只把完整的无 scheme 方括号 IP 条目去括号（``[::1]`` → ``::1``）。
用 ``ipaddress`` 验证 IP 字面量；scheme URL 与其他条目原样保留。幂等，重复调用无副作用。
"""

import ipaddress
import os
import re

#: 只认一个完整、无 scheme、无端口的方括号 IP 条目。
#: scheme URL（如 ``http://[::1]:8080``）和含尾缀的条目都不能做子串替换。
_BRACKETED_IP_ITEM = re.compile(r"^\[([^\[\]]+)\]$")

#: Windows 上 os.environ 大小写不敏感，两个名字指向同一个变量；
#: 这里都试一遍，POSIX 下各自独立 —— 而 httpx 走 ``urllib.request.getproxies()``，
#: 在 POSIX 上**小写 no_proxy 优先于大写 NO_PROXY**，两个键都必须处理。
_ENV_KEYS = ("NO_PROXY", "no_proxy")


def sanitize_no_proxy_env() -> bool:
    """把独立的方括号 IP 条目改写成 httpx 认得的裸写法。

    只处理完整的 ``[IPv6]`` / ``[IPv4]`` 条目。scheme URL（如
    ``http://[::1]:8080``）保留原样，因为 httpx 的 ``://`` 解析路径本身支持它。
    带端口的无 scheme IPv6（如 ``[::1]:8080``）不在此修复范围内：不能通过
    去括号来保留其 host/port 语义，因此本函数不会对子串改写或声称支持精确绕过。

    在**导入** :mod:`base_parser` / :mod:`download` / :mod:`screenshot` 时自动执行
    一次（那三处是插件里所有 httpx 客户端的必经之路），也可以被调用方显式再调一次。

    Returns:
        是否真的改写了环境变量（False = 本来就没有需要修的写法）。
    """
    changed = False
    for key in _ENV_KEYS:
        raw = os.environ.get(key)
        if not raw or "[" not in raw:
            continue
        entries = raw.split(",")
        fixed_entries: list[str] = []
        for entry in entries:
            stripped = entry.strip()
            match = _BRACKETED_IP_ITEM.fullmatch(stripped)
            fixed_entry = entry
            if match:
                try:
                    ipaddress.ip_address(match.group(1))
                except ValueError:
                    pass
                else:
                    # 保留条目前后的空白，只移除 IP 字面量的方括号。
                    left = entry[: len(entry) - len(entry.lstrip())]
                    right = entry[len(entry.rstrip()):]
                    fixed_entry = f"{left}{match.group(1)}{right}"
            fixed_entries.append(fixed_entry)
        fixed = ",".join(fixed_entries)
        if fixed != raw:
            os.environ[key] = fixed
            changed = True
    if changed:
        try:
            from astrbot.api import logger

            logger.warning(
                "已把 NO_PROXY 里的独立方括号 IP（如 [::1]）改写成裸写法："
                "httpx 0.28 无法解析该写法；scheme URL 与带端口条目不会被改写"
            )
        except Exception:  # 日志不可用不该影响主流程
            pass
    return changed


# 导入即修：base_parser / download / screenshot 在模块顶部 import 本模块，
# 那三处是插件里所有 httpx 客户端的必经之路。
sanitize_no_proxy_env()
