"""访问控制：白名单 / 黑名单。

判定顺序照搬 yaya（作者另一个插件）的模型：

    管理员 > 个人白名单 > 个人黑名单 > 群组白名单 > 群组黑名单

五种情况都不命中时：**白名单启用 → 拒绝**（白名单的语义是「只放行名单里的」），
否则放行。所以「启用白名单但名单为空」= 除管理员外谁都不能用。

管理员由 AstrBot 判定（``event.is_admin()``），这里只收结果 —— 与
``/denia_status`` 等命令的 ADMIN 门保持一致，不再单独配一个管理员 ID。

名单在配置里是**逗号分隔的字符串**（不是列表）：本插件的配置是扁平键值，
``_conf_schema.json`` 只支持 bool / int / select / string / text，
``DISABLED_PLATFORMS`` 已经是这个套路。
"""

from __future__ import annotations

import re
from typing import Any

# 分隔符同时收中文逗号与顿号：从聊天里复制群号时经常带上
_ID_SPLIT_RE = re.compile(r"[,\s、，]+")


def parse_id_list(raw: Any) -> frozenset[str]:
    """把逗号 / 空白分隔的 ID 串解析成集合。"""
    return frozenset(part for part in _ID_SPLIT_RE.split(str(raw or "")) if part)


def is_allowed(
    cfg: Any,
    *,
    is_admin: bool,
    is_private: bool,
    sender_id: Any,
    group_id: Any,
) -> bool:
    """这次请求该不该放行。

    Args:
        cfg: 配置对象，读 ``WHITELIST_*`` / ``BLACKLIST_*`` 六项。
        is_admin: 是否管理员。管理员始终放行（白名单也拦不住）。
        is_private: 是否私聊。私聊没有群号，**群组名单对它不生效** ——
            所以只配了群组白名单时，私聊会被拒。
        sender_id: 发送者 ID。
        group_id: 群号（私聊时会被忽略）。

    Returns:
        True 放行，False 拦下。
    """
    if is_admin:
        return True

    sender = str(sender_id or "").strip()
    group = "" if is_private else str(group_id or "").strip()

    whitelist_on = bool(cfg.WHITELIST_ENABLE)
    blacklist_on = bool(cfg.BLACKLIST_ENABLE)

    # 两个开关都关着是常态：直接放行，不做任何解析
    if not whitelist_on and not blacklist_on:
        return True

    if whitelist_on and sender in parse_id_list(cfg.WHITELIST_USER):
        return True
    if blacklist_on and sender in parse_id_list(cfg.BLACKLIST_USER):
        return False
    if whitelist_on and group and group in parse_id_list(cfg.WHITELIST_GROUP):
        return True
    if blacklist_on and group and group in parse_id_list(cfg.BLACKLIST_GROUP):
        return False

    # 都没命中：白名单启用时默认拒绝（它是「只放行名单里的」），否则放行
    return not whitelist_on
