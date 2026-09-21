# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""AcFun 解析器"""
import asyncio
import re
from typing import ClassVar
from httpx import AsyncClient
from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..data import Platform, platform_of


class AcfunParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.ACFUN)

    def __init__(self, downloader):
        super().__init__(downloader)
        self.headers["referer"] = "https://www.acfun.cn/"

    @handle("acfun.cn", r"(?:ac=|/ac)(?P<acid>\d+)")
    async def _parse(self, searched: re.Match[str]):
        from ..models.acfun.video import decoder as video_decoder

        acid = int(searched.group("acid"))
        url = f"https://www.acfun.cn/v/ac{acid}"
        query_url = f"{url}?quickViewId=videoInfo_new&ajaxpipe=1"

        async with AsyncClient(**self.client_kwargs(headers=self.headers)) as client:
            response = await client.get(query_url)
            response.raise_for_status()
            raw = response.text

        matched = re.search(r"window\.videoInfo =(.*?)</script>", raw)
        if not matched:
            raise ParseException("解析 acfun 视频信息失败")

        raw_json = str(matched.group(1))
        raw_json = re.sub(r'\\{1,4}"', '"', raw_json)
        raw_json = raw_json.replace('"{', "{").replace('}"', "}")
        video_info = video_decoder.decode(raw_json)

        author = self.create_author(video_info.name, video_info.avatar_url)
        result = self.result(
            title=video_info.title, text=video_info.text, author=author,
            timestamp=video_info.timestamp,
        )
        # 超时长不再把整条结果丢掉：标题 / 作者 / 封面照常返回，缺料信息走
        # extra["limit_warnings"]（卡片与聊天消息都消费这个通道），与其他平台一致。
        # 原先直接 raise IgnoreException，用户连标题都拿不到；判定用的还是 `>=`，
        # 而全仓其他地方（base_parser / bilibili）都是 `>`。
        self._add_limit_warning(result, video_info.duration)

        # 时长判定交给 create_video：超限时它会取消刚起的下载任务，并把内容换成一个
        # 被 await 时抛 IgnoreException 的「策略跳过」—— 缺料审计会把策略跳过与真失败
        # 分开算，不会出现「不会下载视频」与「下载失败」两条自相矛盾的警告。
        # **必须包成 Task**：create_video 的取消分支靠 isinstance(..., asyncio.Task)，
        # 直接传裸协程既取消不掉，又会留下 "coroutine was never awaited" 警告。
        video_content = self.create_video(
            asyncio.create_task(
                self.downloader.download_m3u8(
                    video_info.m3u8_url, video_name=f"acfun_{acid}.mp4",
                )
            ),
            video_info.coverUrl, video_info.duration,
        )
        result.contents.append(video_content)
        return result
