"""B站备用取流实现。

**这是备用，不是主力。** 主力是 ``bilibili-api-python``（上游仍在维护，
能跟住 B站接口变化）。本包存在的唯一理由是那个依赖不可用时，B站仍能解析 ——
而不是像修复前那样让整个插件（11 个平台）一起加载失败。

分层刻意拆开，方便离线测试：

- :mod:`.wbi` —— WBI 签名纯函数 + 带正/负缓存的取 key。取 key 会发网络请求。
- :mod:`.streams` —— 从已有 DASH 数据里挑流。**纯函数，零网络。**
- :mod:`.fetch` —— 网络层，把上面两者串起来。

来源：从 ``yaya``（娅娅解析 v7.6.0，Apache-2.0）抽出，改为 httpx 实现。
"""

from .fetch import build_playurl_params, fetch_streams
from .streams import CODEC_ORDER, PLAYURL_API, StreamPair, codec_rank, pick_streams
from .wbi import (
    NAV_API,
    WBI_KEY_FAIL_TTL,
    WBI_KEY_TTL,
    WbiKeyProvider,
    av2bv,
    extract_key,
    mixin_key,
    sign_params,
)

__all__ = [
    "build_playurl_params",
    "fetch_streams",
    "CODEC_ORDER",
    "PLAYURL_API",
    "StreamPair",
    "codec_rank",
    "pick_streams",
    "NAV_API",
    "WBI_KEY_FAIL_TTL",
    "WBI_KEY_TTL",
    "WbiKeyProvider",
    "av2bv",
    "extract_key",
    "mixin_key",
    "sign_params",
]
