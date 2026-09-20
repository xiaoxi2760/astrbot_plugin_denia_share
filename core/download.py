"""下载系统 - 提供媒体文件下载功能"""

import asyncio
import os
from uuid import uuid4
from pathlib import Path
from functools import partial
from contextlib import contextmanager
from urllib.parse import urljoin

import httpx
import aiofiles
from astrbot.api import logger

from .media_utils import merge_av, safe_unlink, generate_file_name, is_module_available
from .constants import COMMON_HEADER, DOWNLOAD_TIMEOUT
from .exception import IgnoreException, DownloadException

# 图片并发上限。
#
# 远端返回的图集长度不受控（微博长文、抖音图文、NGA 帖），而每个条目都会立刻
# 变成一条下载任务；没有闸门时并发数等于远端给的数量。这里按连接数与内存
# 取一个保守值，配合 base_parser 的数量截断一起用。
#
# 图片与视频**分池**：视频动辄几十上百 MB，一个就能占住槽几分钟，而头像、
# 封面这类几 KB 的小图和它排同一条队时，忙群里会出现「回复明显变慢，但日志里
# 什么异常都没有」——因为确实没有异常，只是小图在等大视频。
MAX_CONCURRENT_DOWNLOADS = 8

# 视频 / 音频并发上限（含 m3u8 合并下载）。
#
# 取 3 是因为 B站走 ``download_av_and_merge`` 时一次占 2 槽（视频 + 音频），
# 留 1 槽给「同时有人发了另一条视频链接」，不至于第二条链接完全排队。
MAX_CONCURRENT_MEDIA_DOWNLOADS = 3

# m3u8 分片数上限：字节上限之外再兜一层，防止「超多超小分片」把循环拖死
MAX_M3U8_SEGMENTS = 3000


class StreamDownloader:
    def __init__(
        self,
        cache_dir: Path,
        proxies: str | None = None,
        max_size_mb: int = 0,
        verify_ssl: bool = True,
    ):
        self.headers: dict[str, str] = COMMON_HEADER.copy()
        self.cache_dir: Path = cache_dir
        # max_size_mb <= 0 表示不限制
        self.max_size_mb: int = max(0, int(max_size_mb or 0))
        self.proxies: str | None = (proxies or "").strip() or None
        # 下载请求带含 Cookie 的 ext_headers，默认必须校验证书
        self.verify_ssl: bool = bool(verify_ssl)
        self._image_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._media_slots = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_DOWNLOADS)
        self.client: httpx.AsyncClient = self._new_client(self.proxies)

    def _new_client(self, proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, verify=self.verify_ssl, proxy=proxy,
        )

    async def aclose(self):
        await self.client.aclose()

    async def reconfigure(
        self,
        *,
        proxies: str | None = None,
        max_size_mb: int | None = None,
        cache_dir: Path | None = None,
        verify_ssl: bool | None = None,
    ) -> bool:
        """就地更新下载参数，供 WebUI 保存配置后热生效。

        体积上限与缓存目录只影响后续写入，直接改即可；代理与证书校验绑定在
        连接池上，变化时才重建 client 并关掉旧的（避免每次保存配置都泄漏一个连接池）。

        Returns:
            是否重建了连接池。
        """
        if max_size_mb is not None:
            self.max_size_mb = max(0, int(max_size_mb))
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)

        normalized = (proxies or "").strip() or None
        new_verify = self.verify_ssl if verify_ssl is None else bool(verify_ssl)
        if normalized == self.proxies and new_verify == self.verify_ssl:
            return False

        self.proxies = normalized
        self.verify_ssl = new_verify

        old_client = self.client
        self.client = self._new_client(normalized)
        try:
            await old_client.aclose()
        except Exception:
            logger.debug("关闭旧下载连接池失败", exc_info=True)
        return True

    @staticmethod
    def _part_path(file_path: Path) -> Path:
        """下载中的临时文件名（每次调用都不同）。

        先写 ``*.part`` 再 ``os.replace`` 成正式名：中途失败留下的半截文件
        不会顶着正式文件名被下一次的 ``exists()`` 当成有效缓存复用。

        **随机后缀不能省**：同一条链接被并发解析两次、或图集里同一个 URL 出现
        两次时，两个任务会拿到同一个正式名。若 .part 名也是确定的，两者会向
        同一个文件交叉写入，然后**都**执行 ``os.replace`` —— 半截内容照样
        顶着正式名落地，正是这个机制本来要防的事。加随机后缀后各写各的，
        最后由先完成的那份胜出，两份都是完整文件。
        """
        return file_path.with_name(f"{file_path.name}.{uuid4().hex[:8]}.part")

    def _validate_content_length(self, response: httpx.Response) -> int | None:
        """校验明确声明的响应大小。

        抖音等平台 CDN 常用 ``Transfer-Encoding: chunked``，此时**不会**返回
        ``Content-Length``。缺少该头并不代表响应为空，因此这里返回 ``None``
        让下载继续，真实大小在流式写入过程中统计（见 ``_validate_downloaded_bytes``）。

        上游 rika 在 2026-09 修复了此问题：旧实现把缺失的 Content-Length 当成 0，
        会直接取消下载——抖音视频因此全部下不下来。
        """
        content_length = response.headers.get("Content-Length")
        if not content_length:
            return None

        content_length = int(content_length)
        if content_length == 0:
            logger.warning(f"媒体 url: {response.url}, 大小为 0, 取消下载")
            raise IgnoreException

        # 体积上限：超过限制的媒体直接放弃，避免下载完才发现发不出去
        if self.max_size_mb > 0 and content_length > self.max_size_mb * 1024 * 1024:
            size_mb = content_length / 1024 / 1024
            logger.warning(
                f"媒体大小 {size_mb:.1f}MB 超过上限 {self.max_size_mb}MB，取消下载: {response.url}"
            )
            raise IgnoreException(
                f"媒体大小({size_mb:.1f}MB)超过上限({self.max_size_mb}MB)"
            )
        return content_length

    @staticmethod
    async def _validate_downloaded_bytes(file_path: Path, url: str, received_bytes: int, max_bytes: int = 0):
        """防止把空响应当成成功下载，并在超限时清理已写入的文件。"""
        if received_bytes <= 0:
            await safe_unlink(file_path)
            logger.warning(f"媒体 url: {url}, 实际写入 0 字节, 取消下载")
            raise IgnoreException

        if max_bytes > 0 and received_bytes > max_bytes:
            await safe_unlink(file_path)
            mb = received_bytes / 1024 / 1024
            logger.warning(f"媒体 url: {url}, 实际下载 {mb:.1f}MB 超过上限, 已丢弃")
            raise IgnoreException(f"媒体大小({mb:.1f}MB)超过上限({max_bytes // 1024 // 1024}MB)")

    @property
    def _max_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024 if self.max_size_mb > 0 else 0

    async def _download_file_with_httpx(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
        chunk_size: int = 64 * 1024,
    ) -> Path:
        part = self._part_path(file_path)
        async with self.client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            self._validate_content_length(response)
            received_bytes = 0
            try:
                async with aiofiles.open(part, "wb") as file:
                    async for chunk in response.aiter_bytes(chunk_size):
                        if not chunk:
                            continue
                        await file.write(chunk)
                        received_bytes += len(chunk)
                        # chunked 场景没有 Content-Length，边下边判大小
                        if self._max_bytes and received_bytes > self._max_bytes:
                            await safe_unlink(part)
                            mb = received_bytes / 1024 / 1024
                            logger.warning(
                                f"媒体 url: {response.url}, 下载到 {mb:.1f}MB 超过上限，中断"
                            )
                            raise IgnoreException(
                                f"媒体大小超过上限({self.max_size_mb}MB)"
                            )
            except IgnoreException:
                raise
            except Exception:
                # 下载中断时清掉半截文件，避免下次命中缓存拿到坏文件
                await safe_unlink(part)
                raise
            await self._validate_downloaded_bytes(part, str(response.url), received_bytes)
        os.replace(part, file_path)
        return file_path

    async def _download_file_with_curl_cffi(
        self,
        url: str,
        *,
        file_path: Path,
        headers: dict[str, str],
    ) -> Path:
        try:
            import curl_cffi
        except ImportError:
            raise DownloadException("curl_cffi 未安装")

        # 兜底通道同样要带代理：代理-only 环境里不带代理等于必失败，
        # 而这条路径的存在意义恰恰是「httpx 走不通时再试一次」
        session_kwargs: dict[str, object] = {
            "allow_redirects": True,
            "verify": self.verify_ssl,
        }
        if self.proxies:
            session_kwargs["proxies"] = {"http": self.proxies, "https": self.proxies}

        part = self._part_path(file_path)
        async with curl_cffi.AsyncSession(**session_kwargs) as session:
            response: curl_cffi.Response = await session.get(
                url, headers=headers, timeout=DOWNLOAD_TIMEOUT, stream=True,
            )
            response.raise_for_status()
            self._validate_content_length(response)
            received_bytes = 0
            try:
                async with aiofiles.open(part, "wb") as file:
                    async for chunk in response.aiter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        await file.write(chunk)
                        received_bytes += len(chunk)
                        if self._max_bytes and received_bytes > self._max_bytes:
                            await safe_unlink(part)
                            raise IgnoreException(
                                f"媒体大小超过上限({self.max_size_mb}MB)"
                            )
            except IgnoreException:
                raise
            except Exception:
                await safe_unlink(part)
                raise
            await self._validate_downloaded_bytes(part, str(response.url), received_bytes)
        os.replace(part, file_path)
        return file_path

    async def _download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
        chunk_size: int = 64 * 1024,
        slots: asyncio.Semaphore | None = None,
    ) -> Path:
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cache_dir / file_name
        if file_path.exists():
            return file_path

        headers = {**self.headers, **(ext_headers or {})}

        # 并发闸门：图集场景下每个条目都会走到这里，远端给多少就并发多少。
        # 视频 / 音频走单独的池（见 MAX_CONCURRENT_MEDIA_DOWNLOADS 的说明）。
        async with (slots or self._image_slots):
            if file_path.exists():
                return file_path
            try:
                return await self._download_file_with_httpx(
                    url, file_path=file_path, headers=headers, chunk_size=chunk_size
                )
            except httpx.HTTPError as primary_error:
                from .config import get_config
                try:
                    return await self._download_file_with_curl_cffi(url, file_path=file_path, headers=headers)
                except Exception as fallback_error:
                    # 两条通道都失败才算真失败：这属于「产物缺料」，必须无条件留痕，
                    # 否则用户看到卡片少图、日志里却找不到原因。开关只决定要不要堆栈。
                    logger.warning(
                        f"媒体下载失败（httpx 与 curl_cffi 均失败）| url: {url} | "
                        f"httpx: {type(primary_error).__name__}: {primary_error} | "
                        f"curl_cffi: {type(fallback_error).__name__}: {fallback_error}",
                        exc_info=get_config().DEBUG_LOG_ENABLED,
                    )
                    raise DownloadException("媒体下载失败")

    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self._download_file(
            url,
            file_name=video_name,
            ext_headers=ext_headers,
            chunk_size=1024 * 1024,
            slots=self._media_slots,
        )

    async def download_m3u8(
        self,
        m3u8_url: str,
        *,
        video_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        """下载 m3u8 视频 - 解析分片列表并合并下载"""
        if video_name is None:
            video_name = generate_file_name(m3u8_url, ".mp4")

        video_path = self.cache_dir / video_name
        if video_path.exists():
            return video_path

        headers = {**self.headers, **(ext_headers or {})}
        part = self._part_path(video_path)

        # m3u8 合并下载原先完全在信号量之外：它同样是一次「大文件下载」，
        # 不限流的话同时来几条就又是一次资源上溢。整段（含最后 rename）都占槽，
        # 避免「文件还没落盘但槽已释放」。
        async with self._media_slots:
            try:
                # 1. 获取并解析 m3u8 分片列表
                response = await self.client.get(m3u8_url, headers=headers)
                response.raise_for_status()
                slices_text = response.text

                slices: list[str] = []
                for line in slices_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    slices.append(urljoin(m3u8_url, line))

                if not slices:
                    raise DownloadException("m3u8 分片列表为空")

                if len(slices) > MAX_M3U8_SEGMENTS:
                    logger.warning(
                        f"m3u8 分片数 {len(slices)} 超过上限 {MAX_M3U8_SEGMENTS}，取消下载: {m3u8_url}"
                    )
                    raise IgnoreException(f"m3u8 分片数超过上限({MAX_M3U8_SEGMENTS})")

                # 2. 逐个下载分片并追加到文件
                #
                # 体积上限必须在这里也算一遍：分片是逐片流式写盘的，不经过
                # _validate_content_length / _validate_downloaded_bytes，
                # 只判 Content-Length 的话 VIDEO_SIZE_MAXIMUM_MB 在 m3u8 上完全失效。
                received_bytes = 0
                async with aiofiles.open(part, "wb") as f:
                    for seg_url in slices:
                        async with self.client.stream("GET", seg_url, headers=headers) as response:
                            response.raise_for_status()
                            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                                if not chunk:
                                    continue
                                await f.write(chunk)
                                received_bytes += len(chunk)
                                if self._max_bytes and received_bytes > self._max_bytes:
                                    mb = received_bytes / 1024 / 1024
                                    logger.warning(
                                        f"m3u8 视频下载到 {mb:.1f}MB 超过上限 "
                                        f"{self.max_size_mb}MB，中断: {m3u8_url}"
                                    )
                                    raise IgnoreException(
                                        f"媒体大小超过上限({self.max_size_mb}MB)"
                                    )

            except (DownloadException, IgnoreException):
                await safe_unlink(part)
                raise
            except httpx.HTTPError:
                await safe_unlink(part)
                # 视频下不下来属于「产物缺料」，日志不能被调试开关门控
                logger.warning(f"m3u8 视频下载失败 | url: {m3u8_url}", exc_info=True)
                raise DownloadException("m3u8 视频下载失败")
            except Exception:
                # 写盘失败等意外异常也要清理半截文件，避免留下坏缓存被后续命中
                await safe_unlink(part)
                logger.warning(f"m3u8 视频下载异常 | url: {m3u8_url}", exc_info=True)
                raise DownloadException("m3u8 视频下载失败")

            os.replace(part, video_path)
        return video_path

    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self._download_file(
            url, file_name=audio_name, ext_headers=ext_headers,
            slots=self._media_slots,
        )

    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self._download_file(url, file_name=img_name, ext_headers=ext_headers)

    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        ext_headers: dict[str, str] | None = None,
    ) -> Path:
        v_path, a_path = await asyncio.gather(
            self._download_file(v_url, ext_headers=ext_headers, slots=self._media_slots),
            self._download_file(a_url, ext_headers=ext_headers, slots=self._media_slots),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path
