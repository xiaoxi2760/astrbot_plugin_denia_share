# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from datetime import timezone
from re import sub
from msgspec import Struct
from msgspec.json import Decoder


class LargeInPic(Struct):
    url: str


class Pic(Struct):
    url: str
    large: LargeInPic


class Urls(Struct):
    mp4_720p_mp4: str | None = None
    mp4_hd_mp4: str | None = None
    mp4_ld_mp4: str | None = None

    def get_video_url(self) -> str | None:
        return self.mp4_720p_mp4 or self.mp4_hd_mp4 or self.mp4_ld_mp4 or None


class PagePic(Struct):
    url: str


class MediaInfo(Struct):
    stream_url: str | None = None
    stream_urls_hd: str | None = None
    duration: float | None = None


class PageInfo(Struct):
    title: str | None = None
    media_info: MediaInfo | None = None
    urls: Urls | None = None
    page_pic: PagePic | None = None


class User(Struct):
    id: int
    screen_name: str
    profile_image_url: str


class WeiboData(Struct):
    user: User
    text: str
    bid: str
    created_at: str
    status_title: str | None = None
    pics: list[Pic] | None = None
    page_info: PageInfo | None = None
    retweeted_status: "WeiboData | None" = None
    # 以下四个字段只为「返回的作品是不是被请求的那条」这个校验而声明
    # （见 parsers/weibo.py 的 _assert_status_matches_requested）。
    # **类型一律放宽成 int | str**：写死类型后远端换个类型就会让整条解码失败，
    # 那比不校验更糟 —— 校验的收益是防「发出一条不相干的作品」，不是引入新故障。
    id: int | str | None = None
    idstr: int | str | None = None
    mid: int | str | None = None
    mblogid: int | str | None = None

    @property
    def title(self) -> str | None:
        return self.page_info.title if self.page_info else None

    @property
    def display_name(self) -> str:
        return self.user.screen_name

    @property
    def text_content(self) -> str:
        text = self.text.replace("<br />", "\n")
        text = sub(r"<[^>]*>", "", text)
        return text

    @property
    def cover_url(self) -> str | None:
        if self.page_info is None:
            return None
        if self.page_info.page_pic:
            return self.page_info.page_pic.url
        return None

    @property
    def video_url(self) -> str | None:
        if self.page_info and self.page_info.urls:
            return self.page_info.urls.get_video_url()
        return None

    @property
    def duration(self) -> float | None:
        if self.page_info and self.page_info.media_info:
            return self.page_info.media_info.duration
        return None

    @property
    def image_urls(self) -> list[str]:
        if self.pics:
            return [x.large.url for x in self.pics]
        return []

    @property
    def url(self) -> str:
        return f"https://weibo.com/{self.user.id}/{self.bid}"

    @property
    def timestamp(self) -> int | None:
        """发布时间的 Unix 时间戳；解析不出来返回 ``None``。

        **不用 ``strptime`` 的 ``%a %b``**：它们跟随进程 locale
        （``_strptime`` 按 ``locale.getlocale(LC_TIME)`` 构造匹配表），
        宿主把 LC_TIME 切到中文后 ``'Mon Jan 01 ...'`` 会解析失败并抛
        ``ValueError`` —— 而本属性在 ``_collect_result`` 里、位于任何 try 之外，
        异常会一路穿到事件层，用户只看到「解析异常」+堆栈。
        twitter 解析器早就为同一个坑换掉了这个写法
        （见 ``parsers/twitter.py`` 的 ``_parse_created_at``），微博这里是漏改。

        改用与 locale 无关的 RFC 2822 解析：微博的
        ``Mon Jan 01 00:00:00 +0800 2024`` 正好是它的兼容格式。
        顺带修掉一个隐蔽错误 —— 原先的 ``mktime`` 会**忽略** ``%z`` 解析出的
        偏移、按本地时区解释，所以在非 +0800 的机器上算出来的时间戳本来就是错的。
        """
        if not self.created_at:
            return None
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(str(self.created_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except Exception:
            return None


class WeiboResponse(Struct):
    ok: int
    data: WeiboData


decoder = Decoder(WeiboResponse)
