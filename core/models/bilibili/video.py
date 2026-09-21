# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from dataclasses import dataclass
from typing import Any
from msgspec import Struct
from .common import Upper


class Stats(Struct):
    view: int
    danmaku: int
    reply: int
    favorite: int
    coin: int
    share: int
    like: int


class Page(Struct):
    cid: int
    part: str
    ctime: int
    duration: int
    first_frame: str | None = None


@dataclass(frozen=True, slots=True)
class PageInfo:
    index: int
    title: str
    duration: int
    timestamp: int
    cid: int | None = None
    cover: str | None = None


class VideoInfo(Struct):
    bvid: str
    title: str
    desc: str
    duration: int
    owner: Upper
    stat: Stats
    pubdate: int
    ctime: int
    pic: str | None = None
    pages: list[Page] | None = None
    # 版权 / 权限标记（`ugc_pay` 充电专属、`arc_pay`/`pay` 付费专享、`free_watch` 试看…）。
    # 只在「拿不到完整视频」时用来判断**是什么性质的受限**，见 core/bili_access.py。
    # 用 dict 而不是逐个字段：B站会加新标记，写死字段就要跟着改结构体。
    rights: dict[str, Any] | None = None

    @property
    def title_with_part(self) -> str:
        if self.pages and len(self.pages) > 1:
            return f"{self.title} - {self.pages[0].part}"
        return self.title

    def extract_info_with_page(self, page_num: int = 1) -> PageInfo:
        page_idx = page_num - 1
        title = self.title
        duration = self.duration
        cover = self.pic
        timestamp = self.pubdate
        cid = None
        if self.pages and len(self.pages) > 1:
            page_idx = page_idx % len(self.pages)
            page = self.pages[page_idx]
            title += f" | 分集 - {page.part}"
            duration = page.duration
            cover = page.first_frame
            timestamp = page.ctime
            cid = page.cid
        elif self.pages:
            cid = self.pages[0].cid
        return PageInfo(index=page_idx, title=title, duration=duration, timestamp=timestamp, cover=cover, cid=cid)


class ModelResult(Struct):
    summary: str


class AIConclusion(Struct):
    model_result: ModelResult | None = None

    @property
    def summary(self) -> str:
        if self.model_result and self.model_result.summary:
            return f"AI总结: {self.model_result.summary}"
        return "该视频暂不支持AI总结"
