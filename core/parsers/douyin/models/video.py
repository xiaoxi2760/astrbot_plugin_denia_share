# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

import re
from random import choice
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from msgspec import Struct, field
from msgspec.json import Decoder

from ....exception import ParseException

# 判定「这是一条音频直链」只认 URL 路径后缀。
#
# **不靠标题/描述/时长去猜作品是不是音乐**（研究结论 2）：一次无网络的分享页
# 解析里没有别的可靠信号，而文件名后缀是不需要下载任何字节就能确认的形状。
#
# **.m4s 刻意不在名单里**：DASH 的**视频轨**也是 .m4s，按后缀判音频会把普通
# 视频的音轨/视频轨认成音频（实测 DASH 分离流两边都是 .m4s）。要区分只能靠
# 它出现在 ``music.play_url`` 这种**已知音乐上下文**里，而不是靠文件后缀。
AUDIO_SUFFIX_RE = re.compile(
    r"\.(?:mp3|m4a|aac|wav|flac|ogg|opus|amr|wma)(?:$|[?#])", re.IGNORECASE
)
_EMBEDDED_AUDIO_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp3|m4a|aac|wav|flac|ogg|opus|amr|wma)"
    r"(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
# 播放接口把真正的直链接在 video_id= 后面（样本实测），未编码时靠这些**包装自己
# 的**参数确定内层 URL 的边界 —— 不用它们切就会把内层的签名查询串截断。
_WRAPPER_TAIL = r"(?:&(?:ratio|line|playwm|play|media_type|vr_type|quality|codec_type)=|$)"
_WRAPPER_KEYS = ("video_id", "vid", "url", "play_url", "audio_url", "src")


def looks_like_audio_url(url: str | None) -> bool:
    """这条 URL 自身是否就是一条音频直链（只看路径后缀，忽略查询串）。"""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return bool(AUDIO_SUFFIX_RE.search(path))


def unwrap_audio_url(url: str | None) -> str | None:
    """把「播放接口包装地址」还原成真正的音频直链；不是音频就返回 ``None``。

    样本 note/7697524967416115136 的真实形状::

        https://aweme.snssdk.com/aweme/v1/playwm/?video_id=https://lf26-music-east
        .douyinstatic.com/obj/ies-music-hj/7695252422079499065.mp3&ratio=720p&line=0

    三步都试，顺序不能换：

    1. **按包装参数边界切**（未编码）。用 ``_WRAPPER_TAIL`` 只在包装自己的参数
       （``ratio``/``line``/…）处断开，所以内层 URL 自带的 ``?a=1&b=2`` 签名
       **会完整保留** —— 直接 ``parse_qs`` 会把 ``&b=2`` 当成下一个参数切掉。
    2. **按查询参数解**（``https%3A%2F%2F…`` 这种百分号编码）。
    3. 整串里搜一条内嵌音频直链。
    """
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    # 已经是音频直链：原样返回（调用方拿到的是真地址，不做任何改写）。
    if looks_like_audio_url(url):
        return url

    raw = re.search(r"[?&](?:video_id|vid|url)=(\S*)", url, re.IGNORECASE)
    if raw:
        # 未编码时内层 URL 后面可能还跟着它自己的 ?a=1&b=2，而 &ratio= / &line=
        # 才是**包装自己的**参数 —— 所以在这几个参数名处断开，内层签名才不被切掉。
        inner = re.split(_WRAPPER_TAIL, raw.group(1) + " ", maxsplit=1)[0].rstrip()
        if looks_like_audio_url(inner):
            return inner
        if found := _EMBEDDED_AUDIO_RE.search(inner):
            return found.group(0)

    try:
        query = urlsplit(url).query
    except ValueError:
        query = ""
    if query:
        parsed = parse_qs(query, keep_blank_values=True)
        for key in _WRAPPER_KEYS:
            for value in parsed.get(key, []):
                # parse_qs **已经解过一次码**，这里绝不能再 unquote：
                # 内层签名里的 %2B / %252F 会被解两次，签名就废了
                # （样本：token=abc%252Fdef 会被变成 abc%2Fdef）。
                if looks_like_audio_url(value):
                    return value

    if found := _EMBEDDED_AUDIO_RE.search(url):
        return found.group(0)
    return None


def first_url(url_list: list[str] | None) -> str | None:
    """从 ``url_list`` 里随机取一条；空列表返回 ``None``。

    **不要裸用 ``choice()``**：空列表会抛 ``IndexError``，而调用它的属性
    （``video_url`` / ``cover_url`` / ``avatar_url``）都在解析器的 try **之外**
    被读 —— ``IndexError`` 不是 ``ParseException`` 的子类，``_parse_douyin``
    的 ``except ParseException`` 接不住，会把「换下一条路径」打断，
    slidesinfo 与签名 Web API 两级兜底**完全不执行**。
    也就是说：一个装饰性字段（头像）为空，就能让整条抖音链路从三级退化到一级。
    """
    return choice(url_list) if url_list else None


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    # cover / duration 改成可选：**音乐型作品会缺 cover**，而原来的必填约束
    # 会让 msgspec 在读图片之前就整条解码失败（研究结论里的未决项 1）。
    # video_url / cover_url / duration 三个属性都已就地判空，实际视频的取值不变。
    play_addr: PlayAddr | None = None
    cover: Cover | None = None
    duration: int | None = None


class Music(Struct):
    """作品关联的音频。整条字段可选：普通视频也带 music，但那是配乐片段。"""

    # **单位是秒**（样本 music.duration=66 与实测约 66 秒一致），
    # 与 video.duration 的毫秒**不是一回事**，别套用视频的 //1000。
    duration: int | None = None
    title: str | None = None
    author: str | None = None
    play_url: PlayAddr | None = None


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None
    music: Music | None = None
    aweme_type: int | None = None

    @property
    def image_urls(self) -> list[str]:
        # 跳过 url_list 为空的条目：choice([]) 会抛 IndexError 把整条解析炸掉
        return [choice(image.url_list) for image in self.images if image.url_list] if self.images else []

    @property
    def video_url(self) -> str | None:
        # 音频包装地址（video_id 里塞的是音频直链）**不能**当视频返回：
        # 去水印后是 200 + 0 字节的假成功，纯音频作品会变成一条打不开的视频。
        # 这里返回 None，让控制流落到 audio_url 分支。
        if not self.video or not self.video.play_addr:
            return None
        url = first_url(self.video.play_addr.url_list)
        if not url or unwrap_audio_url(url):
            return None
        # playwm 是带水印的播放地址，play 是去水印的
        return url.replace("playwm", "play")

    @property
    def cover_url(self) -> str | None:
        if self.video and self.video.cover:
            return first_url(self.video.cover.url_list)
        return None

    @property
    def duration(self) -> int | None:
        if self.video and self.video.duration is not None:
            return self.video.duration // 1000
        return None

    @property
    def audio_url(self) -> str | None:
        """作品自带音频的真实直链（**秒级时长另见** :attr:`audio_duration`）。

        优先取 ``music.play_url``；它在样本里是空的，所以真正的兜底是
        ``video.play_addr`` —— 那条「播放地址」的 ``video_id`` 参数里塞的
        其实就是音频直链。两者都过 :func:`unwrap_audio_url`，所以播放接口
        包装地址会被解开，**并且不会**被当成视频去做 playwm→play 改写
        （实测那条包装地址改写后是 200 + 0 字节的假成功）。
        """
        if self.music and self.music.play_url:
            for url in self.music.play_url.url_list or []:
                if real := unwrap_audio_url(url):
                    return real
        if self.video and self.video.play_addr:
            for url in self.video.play_addr.url_list or []:
                if real := unwrap_audio_url(url):
                    return real
        return None

    @property
    def audio_duration(self) -> float | None:
        """音频时长（**秒**）。``music.duration`` 本来就是秒，不要再除 1000。"""
        if self.music and self.music.duration is not None:
            return float(self.music.duration)
        return None

    @property
    def avatar_url(self) -> str | None:
        # 两个字段都试：thumb 存在但 url_list 为空时退到 medium。
        # 原先是「thumb 存在就 choice 它的 url_list」—— 空列表直接抛，头像这个
        # 纯装饰字段能把整条解析链打断（详见 first_url 的说明）。
        for avatar in (self.author.avatar_thumb, self.author.avatar_medium):
            if avatar and (url := first_url(avatar.url_list)):
                return url
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(name="videoInfoRes", default_factory=VideoInfoRes)
    # 图文页的另外两种写法。**必须与 session.has_work_data 认的字段名一致**：
    # 那边用「有没有 item_list」决定要不要继续重试，这边负责真正读出来。
    # 两边不一致的后果是「会话以为拿到了数据 → 停止重试 → 解析却读不出来」，
    # 明明在页面里的作品就这么丢了。
    slides_info_res: VideoInfoRes = field(name="slidesInfoRes", default_factory=VideoInfoRes)
    note_detail_res: VideoInfoRes = field(name="noteDetailRes", default_factory=VideoInfoRes)

    @property
    def work(self) -> VideoInfoRes:
        """三个字段里第一个真带 item_list 的；都没有就返回 videoInfoRes 那个空的，
        由 ``video_data`` 抛出统一的「找不到数据」。"""
        for candidate in (self.video_info_res, self.slides_info_res, self.note_detail_res):
            if candidate.item_list:
                return candidate
        return self.video_info_res


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)
    slides_page: VideoOrNotePage | None = field(name="slides_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        for page in (
            self.loader_data.video_page,
            self.loader_data.note_page,
            self.loader_data.slides_page,
        ):
            if page is not None:
                return page.work.video_data
        raise ParseException("can't find video_(id)/page or note_(id)/page in router data")


decoder = Decoder(RouterData)
