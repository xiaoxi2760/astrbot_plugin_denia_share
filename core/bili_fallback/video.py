"""B站备用取流：视频信息（``view`` 接口）。

**只取视频，刻意不碰登录态。** 备用路径的价值是「依赖没了还能用」，
不是「替代主力」——AI 总结、在线人数、清晰度协商这些主力才有的东西，
本模块一概不提供。少即是多：备用路径一旦开始复刻主力，就会跟着主力一起腐化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .wbi import av2bv

__all__ = ["VIEW_API", "VideoInfo", "PageInfo", "ResolvedPage", "normalize", "fetch_video_info"]

VIEW_API = "https://api.bilibili.com/x/web-interface/view"


@dataclass(frozen=True)
class PageInfo:
    cid: int
    index: int          # 0 起，与 bilibili-api-python 的 page_index 对齐
    title: str
    duration: float     # 秒
    first_frame: str = ""
    ctime: int = 0


@dataclass(frozen=True)
class ResolvedPage:
    """按 ``?p=`` 解析出来的最终展示信息。

    字段语义与主力的 ``VideoInfo.extract_info_with_page``（``core/models/
    bilibili/video.py``）**逐条对齐** —— 替补要能顶班，展示出来的东西就不能
    变。对不齐的典型症状是「同一个链接，主力显示视频标题、替补显示分P 名」。

    规则（照抄主力）：

    - **单 P**：标题 / 时长 / 封面 / 时间全部用**视频级**的，``cid`` 取 P1
    - **多 P**：标题拼成 ``视频标题 | 分集 - 分P名``，时长 / 封面 / 时间改用该
      分 P 自己的
    - ``page_num`` 越界时**取模回绕**（``99 % 2 == 1`` → 第 2 个），
      不是报错 —— 主力就是这个行为，改了就是行为漂移
    """

    cid: int
    index: int
    title: str
    duration: float
    cover: str
    timestamp: int
    part: str = ""


@dataclass(frozen=True)
class VideoInfo:
    bvid: str
    aid: int
    title: str
    description: str
    cover: str
    author_name: str
    author_face: str
    published_at: int
    duration: float
    pages: List[PageInfo] = field(default_factory=list)
    stat: Dict[str, int] = field(default_factory=dict)
    rights: Dict[str, Any] = field(default_factory=dict)

    def page(self, index: int) -> Optional[PageInfo]:
        """按 0 起的 ``index`` 取分P；越界返回 ``None``。"""
        if 0 <= index < len(self.pages):
            return self.pages[index]
        return None

    def resolve_page(self, page_num: int = 1) -> Optional[ResolvedPage]:
        """把 ``?p=`` 解析成最终展示信息，语义对齐主力的 ``extract_info_with_page``。"""
        if not self.pages:
            return None
        index = page_num - 1
        if len(self.pages) > 1:
            index %= len(self.pages)
            page = self.pages[index]
            return ResolvedPage(
                cid=page.cid,
                index=index,
                title=f"{self.title} | 分集 - {page.title}",
                duration=page.duration,
                # 主力在这里给的是 ``page.first_frame``，空就是空。
                # 替补多一步回落视频封面：主力那条渲染出的是缺图，替补给出能看的
                # 封面。这是有意偏离，不是漏抄 —— 注释钉住，免得以后被当 bug 改回去。
                cover=page.first_frame or self.cover,
                # 同上：主力给 0，替补回落到投稿时间。
                timestamp=page.ctime or self.published_at,
                part=page.title,
            )
        # 单 P：用视频级的标题 / 时长 / 封面 / 时间，只借 P1 的 cid。
        # ⚠️ index 照抄主力（= page_num - 1，**不做取模**）：主力单 P 时也不取模，
        # 所以 ``?p=3`` 打在一个单 P 视频上，两边都得出 index=2。
        # 早先这里硬编码 index=0，导致两模式的缓存文件名（``{bvid}-{index+1}``）
        # 和 URL 里的 ``?p=`` 都不互认 —— 同一个链接在两种模式下各下一份。
        page = self.pages[0]
        return ResolvedPage(
            cid=page.cid,
            index=index,
            title=self.title,
            duration=self.duration,
            cover=self.cover,
            timestamp=self.published_at,
            part=page.title,
        )


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize(payload: Dict[str, Any]) -> VideoInfo:
    """把 ``view`` 接口的 ``data`` 归一化成 :class:`VideoInfo`。

    单独拆出来是为了能拿 fixture 直接测，不必联网。
    """
    data = (payload or {}).get("data") or payload or {}
    owner = data.get("owner") or {}

    pages = []
    for i, item in enumerate(data.get("pages") or []):
        if not isinstance(item, dict):
            continue
        cid = _to_int(item.get("cid"))
        if not cid:
            continue
        pages.append(
            PageInfo(
                cid=cid,
                index=_to_int(item.get("page"), i + 1) - 1 or i,
                title=str(item.get("part") or "").strip(),
                duration=_to_float(item.get("duration")),
                first_frame=str(item.get("first_frame") or ""),
                ctime=_to_int(item.get("ctime")),
            )
        )

    bvid = str(data.get("bvid") or "").strip()
    aid = _to_int(data.get("aid"))
    if not bvid and aid:
        # 2026-10-06 实测：playurl 已不接受 aid 参数（-400），所以 bvid 是必需的
        bvid = av2bv(aid)

    return VideoInfo(
        bvid=bvid,
        aid=aid,
        title=str(data.get("title") or "").strip(),
        description=str(data.get("desc") or "").strip(),
        cover=str(data.get("pic") or "").strip(),
        author_name=str(owner.get("name") or "").strip(),
        author_face=str(owner.get("face") or "").strip(),
        published_at=_to_int(data.get("pubdate")),
        duration=_to_float(data.get("duration")),
        pages=pages,
        stat={
            k: _to_int(v)
            for k, v in (data.get("stat") or {}).items()
            if k in ("view", "like", "coin", "favorite", "share", "reply", "danmaku")
        },
        rights=data.get("rights") or {},
    )


async def fetch_video_info(
    client_factory,
    *,
    bvid: str = "",
    avid: int = 0,
    headers: Optional[Dict[str, str]] = None,
) -> VideoInfo:
    """查一条视频的信息。

    :raises ValueError: ``bvid`` 与 ``avid`` 都没给。
    :raises RuntimeError: 接口返回非 0 ``code``。
    """
    if not bvid and not avid:
        raise ValueError("必须提供 bvid 或 avid")
    params: Dict[str, Any] = {"bvid": bvid} if bvid else {"aid": int(avid)}

    async with client_factory() as client:
        resp = await client.get(VIEW_API, params=params, headers=headers or {})
        resp.raise_for_status()
        payload = resp.json()

    code = (payload or {}).get("code")
    if code != 0:
        raise RuntimeError(
            f"B站 view 返回 code={code}: {(payload or {}).get('message')}"
        )
    return normalize(payload)
