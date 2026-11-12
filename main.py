"""
希望解析器 - 链接分享自动解析插件

支持 B站 | 抖音 | 快手 | 微博 | 小红书 | Twitter | AcFun | NGA | GitHub | Pixiv | Steam
另有网页截图（thum.io / Cloudflare 双后端）与 Pixiv 关键词搜索。
Steam 历史最低价需配置 ITAD_API_KEY（免费申请），未配置时只显示当前价与折扣。

代码来源（涉及第三方许可的都标在各自源文件里）：
- astrbot_plugin_rika_share（MIT）：解析框架 / 卡片渲染 / B站 / 微博 / 小红书 / AcFun / NGA
- Johnserf-Seed/f2（Apache-2.0）：抖音 a_bogus 签名，见 core/parsers/douyin/sign.py

支持按需先发封面卡片，确认后下载并发送源媒体。
"""

import os
import re
import json
import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator, Dict
from urllib.parse import urlsplit

import aiohttp
import httpx
import qrcode
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register, StarTools

from .core.media_utils import (
    CACHE_MARKER_NAME,
    cleanup_cache_dir,
    ensure_cache_marker,
    is_docker_environment,
)
from .core.config import init_config, get_config, verify_schema_alignment
from .core.download import StreamDownloader
from .core.link_dedup import HINT, PROCEED, SILENT, LinkDedup
from .core.data import (
    ParseResult, ImageContent, VideoContent, Author, platform_of,
)
from .core.history import HistoryStore, ParseRecord
from .core.permissions import is_allowed
from .core.constants import (
    PLATFORM_DISPLAY_NAMES, PlatformEnum, is_custom_platform, platform_meta,
)
from .core.result_cache import ResultCache
from .core.delivery import DeliveryService, _DELIVERY_UNCERTAIN
from .core.confirm_download import ConfirmController
from .core.bili_login import BiliLoginManager
# 版本号只认 __init__.py 那一处：register 装饰器直接读它，
# 不用再在装饰器里手写一遍版本字符串（以前改版本要同时改三处，漏一处就版本不一致）
from . import __version__
from .core.exception import (
    ParseException, IgnoreException, SilentException,
)
from .core.card_renderer import ShareCardRenderer
from .core.screenshot import (
    ScreenshotService,
    describe_capture,
    is_probably_screenshotable,
)
from .core.parsers import (
    NeteaseMusicParser, QQMusicParser,
    BilibiliParser, DouyinParser, KuaiShouParser, WeiBoParser,
    XiaoHongShuParser, TwitterParser, NGAParser, AcfunParser,
    GitHubParser, PixivParser, SteamParser, XiaoheiheParser,
)
from .core.custom_parsers import (
    SUBDIR_NAME as CUSTOM_PARSERS_SUBDIR,
    CustomParserLoader,
    LoadResult as CustomParserLoadResult,
)

PLUGIN_NAME = "astrbot_plugin_denia_share"

def _get_plugin_data_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


# ========== URL 匹配模式 ==========
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}|av\d{6,})")
DOUYIN_PATTERN = re.compile(r"(v\.douyin\.com|douyin\.com|iesdouyin\.com|m\.douyin\.com|jx\.douyin\.com|jingxuan\.douyin\.com)")
KUAISHOU_PATTERN = re.compile(r"(v\.kuaishou\.com|kuaishou\.com|chenzhongtech\.com)")
WEIBO_PATTERN = re.compile(r"(weibo\.com|weibo\.cn|m\.weibo\.cn|video\.weibo\.com|mapp\.api\.weibo\.cn)")
XHS_PATTERN = re.compile(r"(xhslink\.com|xhslink\.cn|xiaohongshu\.com)")
TWITTER_PATTERN = re.compile(r"(?:^|[\s./@])x\.com(?:[/:\s]|$)|twitter\.com")
NGA_PATTERN = re.compile(r"(nga\.178\.com|ngabbs\.com|bbs\.nga\.cn)")
ACFUN_PATTERN = re.compile(r"acfun\.cn")
GITHUB_PATTERN = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+")
PIXIV_PATTERN = re.compile(r"pixiv\.net/(?:artworks|i/|member_illust)")
STEAM_PATTERN = re.compile(r"store\.steampowered\.com")
XIAOHEIHE_PATTERN = re.compile(r"https?://(?:(?:www|api)\.)?xiaoheihe\.cn/", re.I)

URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")

#: 确认下载：严格匹配「确认下载」或「确认下载 <短编号>」，不吞普通聊天。
CONFIRM_DOWNLOAD_PATTERN = re.compile(r"^确认下载(?:\s+([0-9A-Za-z]{1,8}))?$")


class _EventUrlWrapper:
    """把任意 URL 伪装成一个只含该 URL 的事件，供解析流程复用。"""

    def __init__(self, event: AstrMessageEvent, url: str):
        self._event = event
        self.message_str = url

    def __getattr__(self, name):
        return getattr(self._event, name)


@register(
    "希望解析器",
    "xiaoxi2760",
    # 平台列表也从 PLATFORMS 派生：这是第 5 处会写出平台名的地方，
    # 手写一份就等于又埋一个「改了这里忘了那里」
    "链接分享自动解析，支持 " + "|".join(PLATFORM_DISPLAY_NAMES.values()),
    __version__,
)
class DeniaSharePlugin(Star):

    # ==================== 组合服务（懒加载） ====================
    #
    # 三块实现已按职责抽到 core/ 下的独立服务里（core/delivery.py /
    # core/result_cache.py / core/bili_login.py）。插件保留同名薄委托方法与
    # 状态属性，WebUI / music controller / credentials / 既有测试照旧引用。
    # 这里做懒加载：测试用 object.__new__ 建桩、不跑 __init__ 也能拿到服务。

    @property
    def _delivery(self) -> DeliveryService:
        svc = getattr(self, "_delivery_service", None)
        if svc is None:
            svc = DeliveryService(self)
            self._delivery_service = svc
        return svc

    @property
    def _caches(self) -> ResultCache:
        svc = getattr(self, "_cache_service", None)
        if svc is None:
            svc = ResultCache()
            svc._result_cache = getattr(self, "_result_cache", svc._result_cache)
            svc._render_cache = getattr(self, "_render_cache", svc._render_cache)
            self._result_cache = svc._result_cache
            self._render_cache = svc._render_cache
            self._cache_service = svc
        return svc

    @property
    def _bili_login(self) -> BiliLoginManager:
        mgr = getattr(self, "_bili_login_manager", None)
        if mgr is None:
            mgr = BiliLoginManager(self)
            self._bili_login_manager = mgr
        return mgr

    @property
    def _confirm(self) -> ConfirmController:
        controller = getattr(self, "_confirm_controller", None)
        if controller is None:
            controller = ConfirmController(self)
            self._confirm_controller = controller
        return controller

    @staticmethod
    def _is_onebot(event: AstrMessageEvent) -> bool:
        """合并转发（Comp.Nodes）是 OneBot v11 独有特性。"""
        return DeliveryService.is_onebot(event)

    @staticmethod
    def _fallback_cache_dir(data_dir: Path) -> tuple[Path, str]:
        """回退到插件数据目录，并保证它带缓存哨兵。"""
        default_dir = data_dir / "cache"
        default_dir.mkdir(parents=True, exist_ok=True)
        ensure_cache_marker(default_dir, allow_nonempty=True)
        return default_dir, "fallback"

    @staticmethod
    def _resolve_cache_dir(data_dir: Path, configured: str) -> tuple[Path, str]:
        """决定媒体缓存目录。

        留空用插件数据目录；填了「共享缓存目录」就用它，让协议端容器也能按
        同一个容器内路径读到下载下来的视频。

        共享目录不可用时回退到插件数据目录，避免下载因路径不可写而中断。

        **安全约束**：缓存清理是递归删除（``clear_cache_dir`` / ``cleanup_cache_dir``），
        所以这里必须先把危险路径挡掉，而不是等清理时再补救。三类被拒：
        文件系统根目录、把插件数据目录包在里面的祖先目录、
        已有内容却没有缓存哨兵的目录。三者在**建目录之前**判定。

        Returns:
            (缓存目录, 来源说明)，来源取 ``default`` / ``configured`` / ``fallback``。
        """
        if not configured:
            default_dir = data_dir / "cache"
            default_dir.mkdir(parents=True, exist_ok=True)
            ensure_cache_marker(default_dir, allow_nonempty=True)
            return default_dir, "default"

        candidate = Path(configured).expanduser()
        # resolve() 不要求路径存在（strict=False），所以危险路径可以在建目录**之前**
        # 就判掉。之前是先 mkdir(parents=True) 再校验，虽然删除链已经被哨兵挡住，
        # 但「按一个自由文本配置项在任意路径建出目录树」这个原语还在 —— 顺序调过来
        # 就顺手消掉了，代价为零。
        resolved = candidate.resolve()

        if resolved == Path(resolved.anchor):
            logger.warning(
                f"[denia_share] 拒绝把文件系统根目录 {resolved} 当作缓存目录"
                f"（清理会递归删掉整块盘），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            data_dir.resolve().relative_to(resolved)
        except ValueError:
            pass
        else:
            logger.warning(
                f"[denia_share] 缓存目录 {resolved} 把插件数据目录包在里面，"
                f"清理时会连配置与解析记录一起删掉，已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                f"[denia_share] 共享缓存目录不可用（{configured}）：{exc}；"
                f"已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not os.access(resolved, os.W_OK):
            logger.warning(
                f"[denia_share] 共享缓存目录不可写（{resolved}），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not ensure_cache_marker(resolved, allow_nonempty=False):
            logger.warning(
                f"[denia_share] 共享缓存目录 {resolved} 里已有内容且没有 "
                f"{CACHE_MARKER_NAME} 哨兵，无法确认它就该被当作缓存目录，"
                f"已回退到插件数据目录。确认要用它，请先在该目录下建一个空的 "
                f"{CACHE_MARKER_NAME} 文件"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        return resolved, "configured"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = _get_plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self.config_dir = data_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        pconfig = init_config(config, data_dir / "cache", self.config_dir)

        # 缓存目录可被「共享缓存目录」配置项接管：Docker 分容器部署时，
        # 协议端要按同一个容器内路径才能读到下载下来的视频
        self.cache_dir, self.cache_dir_source = self._resolve_cache_dir(
            data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pconfig.cache_dir = self.cache_dir

        try:
            from .core.config import (
                migrate_audio_send_mode,
                migrate_grouped_config,
                migrate_music_commands,
                migrate_native_choices,
                migrate_output_modes,
                migrate_proxy_switches,
            )

            # 顺序要紧：分代理开关迁移**必须先跑**。
            # migrate_grouped_config 的规则是「分组值 == 默认值且扁平值 ≠ 默认值
            # → 用扁平值」，而开关键默认值就是 False —— 老配置里残留的扁平
            # PROXY_*: true 会被它顶进分组，盖掉用户在新版里显式关掉的同一个开关，
            # 症状是「我明明关了，重启后又走代理」。migrate_proxy_switches 会先把这
            # 些扁平位并入分组再删掉，于是通用迁移再也看不到它们。
            changed = migrate_proxy_switches(config)
            # 输出模式迁移要在通用分组迁移**之前**：它靠「组里没有这三个键」
            # 判别新装还是升级，而 migrate_grouped_config 会按默认值把它们补进
            # 分组（OUTPUT_* 的默认值与迁移写入值可能不同，补完就分不出来了）。
            changed = migrate_output_modes(config) or changed
            changed = migrate_music_commands(config) or changed
            changed = migrate_grouped_config(config) or changed
            changed = migrate_audio_send_mode(config) or changed
            changed = migrate_native_choices(config) or changed
            if changed:
                save = getattr(config, "save_config", None)
                if callable(save):
                    save()
        except Exception:
            logger.warning("旧版配置迁移失败，将使用兼容回退读取", exc_info=True)

        # 直连下载器：常驻。代理下载器按需懒建（见 _downloader_for）——
        # 代理绑定在 httpx 连接池上没法按请求切换，分解析器代理靠两个实例实现。
        # 并发闸门直连 / 代理各池，各自带上配置的并发值（构造时读死，热更新
        # 走 apply_runtime_config 的 reconfigure 请求重建）。
        self.downloader = StreamDownloader(
            self.cache_dir,
            proxies=None,
            max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
            image_concurrency=pconfig.DOWNLOAD_IMAGE_CONCURRENCY,
            media_concurrency=pconfig.DOWNLOAD_MEDIA_CONCURRENCY,
        )
        self._proxy_downloader: StreamDownloader | None = None
        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        self.parsers: dict[str, Any] = {}
        # 用户自定义解析器的加载器。延迟到 _init_parsers 里建（那时 downloader 已就绪）
        self._custom_loader: CustomParserLoader | None = None
        self._custom_result: CustomParserLoadResult | None = None
        # 上一轮装进 self.parsers 的自定义平台键。重扫时靠它把**已经不存在**的
        # 平台（文件被删/改名，或新版本加载失败）摘掉 —— 见 _init_custom_parsers。
        self._custom_parser_keys: set[str] = set()
        self._init_parsers()
        # 结果/渲染缓存由 core/result_cache.py 的 ResultCache 服务统一管理；
        # 这里把服务持有的两本字典以同名属性暴露给既有调用方（webui / 生命周期 /
        # 测试），它们是**同一对象**，.clear() 等直接操作会同步到服务。
        self._cache_service = ResultCache()
        self._result_cache = self._cache_service._result_cache
        self._render_cache = self._cache_service._render_cache
        #: 本次媒体投递是否落在「送达未知」（合并转发回执超时/断连）。
        #: 见 _process_url：未知时保留去重窗口但不记成功历史。
        # 同群重复链接去重：只解析第一个，后来者不解析也不收卡片（见 core/link_dedup）
        self._link_dedup = LinkDedup(lambda: self._dedup_window())
        self._cache_cleanup_task: asyncio.Task | None = None

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())

        # ========== B站 Cookie ==========
        self._bili_cookie: str = ""
        # 上一次**生效过**的配置项 BILI_CK。用来判断「配置项到底变没变」：
        # apply_runtime_config 是「保存配置 / 恢复默认 / 保存外观」三个接口都会走的，
        # 无条件重新应用 cookie 会误伤（详见那里的注释）。
        self._bili_ck_applied: str = ""
        self._bili_http_session: aiohttp.ClientSession | None = None
        self._bili_login_tasks: Dict[str, asyncio.Task] = {}
        self._bili_login_states: Dict[str, dict[str, Any]] = {}
        # 二维码临时文件的延迟清理任务，terminate() 时一并取消
        self._delayed_cleanup_tasks: list[asyncio.Task] = []
        self._bili_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._bili_data_dir.mkdir(parents=True, exist_ok=True)
        self._bili_cookie_file = self._bili_data_dir / "bili_cookie.json"

        # ========== 解析记录（WebUI「缓存」页的数据源） ==========
        # 容量取配置的 HISTORY_MAX_RECORDS，保存后由 apply_runtime_config 热同步。
        self.history = HistoryStore(
            data_dir / "history.jsonl", max_records=pconfig.HISTORY_MAX_RECORDS
        )
        # 确认下载状态机（模式为 auto 时完全不参与解析链路）。
        self._confirm_controller = ConfirmController(self)
        self._confirm.refresh_config()

        self._check_schema_alignment()
        self._register_webui()

        logger.info(
            f"[denia_share] 已启用平台: {', '.join(self.parsers.keys()) or '无'}；"
            f"卡片渲染: {'开' if self._renderer.enabled else '关'}；"
            f"媒体缓存: {self.cache_dir}"
            f"{'（共享目录）' if self.cache_dir_source == 'configured' else ''}"
            f"{'（配置的共享目录不可用，已回退）' if self.cache_dir_source == 'fallback' else ''}；"
            f"媒体中转: {'开' if pconfig.MEDIA_RELAY_ENABLED else '关'}"
            f"{'（容器内）' if is_docker_environment() else ''}"
        )

    def _check_schema_alignment(self):
        """自检 _conf_schema.json 与 CONFIG_META 是否一致（不一致会导致配置静默丢失）。"""
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[denia_share] 无法读取 _conf_schema.json，跳过一致性自检", exc_info=True)
            return
        problems = verify_schema_alignment(schema)
        for problem in problems:
            logger.warning(f"[denia_share] 配置 schema 不一致：{problem}")

    def _register_webui(self):
        """注册插件 WebUI 的后端接口。

        AstrBot < 4.24.2 没有 register_web_api，此时静默跳过——
        聊天命令与解析功能都不受影响，只是没有网页界面。

        4.24.2 ~ 4.26.x 有 register_web_api 但没有 astrbot.api.web，
        由 core/web_compat 自动回退到 quart，两条路径行为一致。
        """
        if not hasattr(self.context, "register_web_api"):
            logger.info("[denia_share] 当前 AstrBot 不支持插件页面，跳过 WebUI 注册")
            self._webui_ready = False
            return
        try:
            from .core.web_compat import HAS_WEB_API
            from .core.webui import WebUIApi

            self._webui = WebUIApi(self)
            self._webui.register()
            self._webui_ready = True
            logger.info(
                "[denia_share] WebUI 接口已注册（插件页面 pages/denia，响应后端=%s）",
                "astrbot.api.web" if HAS_WEB_API else "quart 兼容层",
            )
        except Exception:
            self._webui_ready = False
            logger.warning("[denia_share] WebUI 接口注册失败", exc_info=True)

    def _downloader_for(self, platform_key: str) -> StreamDownloader:
        """按平台的「走代理」开关挑下载器：开了走代理池，没开走直连池。

        StreamDownloader 的代理绑定在连接池上（httpx client 构造参数），
        没法按请求切换，所以「分解析器代理」靠两个下载器实例实现：
        共享同一个缓存目录；直连池常驻，代理池只在有平台开关打开时才建。
        与解析器接口请求同源：都问 ``pconfig.proxy_for(platform_key)``。
        """
        pconfig = get_config()
        if pconfig.proxy_for(platform_key) is None:
            return self.downloader

        if self._proxy_downloader is None:
            self._proxy_downloader = StreamDownloader(
                self.cache_dir,
                proxies=pconfig.PROXY or None,
                max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                verify_ssl=pconfig.HTTP_VERIFY_SSL,
                image_concurrency=pconfig.DOWNLOAD_IMAGE_CONCURRENCY,
                media_concurrency=pconfig.DOWNLOAD_MEDIA_CONCURRENCY,
            )
            logger.info(
                f"[denia_share] 已创建代理下载器（{pconfig.PROXY}），"
                "供打开了「走代理」开关的解析器下载媒体使用"
            )
        return self._proxy_downloader

    def _init_parsers(self):
        pconfig = get_config()
        disabled = self.disabled_platforms
        if "bilibili" not in disabled:
            # bilibili_api 缺失时会换上自建替补解析器（B站视频仍可用）；
            # 连替补都起不来才是 None，此时跳过注册而不是拖垮整个插件。
            if BilibiliParser is None:
                from .core.parsers import BILIBILI_FALLBACK_ERROR, BILIBILI_IMPORT_ERROR

                logger.error(
                    f"[denia_share] B站解析器未启用：bilibili-api-python 导入失败"
                    f"（{BILIBILI_IMPORT_ERROR}），自建替补也不可用"
                    f"（{BILIBILI_FALLBACK_ERROR}）。其余平台不受影响；"
                    f"需要 B站请执行 pip install 'bilibili-api-python>=16.0.0'"
                )
            else:
                from .core.parsers import BILIBILI_IS_FALLBACK

                self.parsers["bilibili"] = BilibiliParser(
                    self._downloader_for("bilibili"), bili_ck=pconfig.BILI_CK, config_dir=self.config_dir
                )
                if BILIBILI_IS_FALLBACK:
                    logger.warning(
                        "[denia_share] bilibili-api-python 不可用，B站已切到自建备用解析："
                        "视频可解析下载，动态 / 直播 / 收藏夹 / 专栏暂不可用"
                    )
        if "douyin" not in disabled:
            self.parsers["douyin"] = DouyinParser(self._downloader_for("douyin"))
        if "kuaishou" not in disabled:
            self.parsers["kuaishou"] = KuaiShouParser(self._downloader_for("kuaishou"))
        if "weibo" not in disabled:
            self.parsers["weibo"] = WeiBoParser(self._downloader_for("weibo"))
        if "xiaohongshu" not in disabled:
            self.parsers["xiaohongshu"] = XiaoHongShuParser(
                self._downloader_for("xiaohongshu"), xhs_ck=pconfig.XHS_CK
            )
        if "twitter" not in disabled:
            self.parsers["twitter"] = TwitterParser(self._downloader_for("twitter"))
        if "nga" not in disabled:
            self.parsers["nga"] = NGAParser(self._downloader_for("nga"))
        if "acfun" not in disabled:
            self.parsers["acfun"] = AcfunParser(self._downloader_for("acfun"))
        if "github" not in disabled:
            self.parsers["github"] = GitHubParser(
                self._downloader_for("github"), token=pconfig.GITHUB_TOKEN
            )
        if "pixiv" not in disabled:
            self.parsers["pixiv"] = PixivParser(
                self._downloader_for("pixiv"), cookie=pconfig.PIXIV_CK
            )
        if "steam" not in disabled:
            self.parsers["steam"] = SteamParser(
                self._downloader_for("steam"),
                itad_key=pconfig.ITAD_API_KEY,
                region=pconfig.STEAM_REGION,
            )
        if "netease" not in disabled:
            self.parsers["netease"] = NeteaseMusicParser(self._downloader_for("netease"))
        if "xiaoheihe" not in disabled:
            self.parsers["xiaoheihe"] = XiaoheiheParser(
                self._downloader_for("xiaoheihe"), cookie=pconfig.XIAOHEIHE_CK
            )
        if "qqmusic" not in disabled:
            self.parsers["qqmusic"] = QQMusicParser(self._downloader_for("qqmusic"))
        for key in ("netease", "qqmusic"):
            if key in self.parsers:
                self.parsers[key].before_play = self._refresh_music_account

        self._init_custom_parsers()
        self._build_screenshot_service()

    def _init_custom_parsers(self) -> None:
        """加载用户自定义解析器（目录扫描 + 失败隔离）。

        每次都会重新扫描目录：这个方法在插件初始化与 ``apply_runtime_config``
        里都会被调到，所以「保存配置 / 切换平台 / 点重新加载」都能让新写的文件
        生效，不必重载插件。加载器自身保证反复调用是幂等的（先反注册再注册）。
        """
        if self._custom_loader is None:
            self._custom_loader = CustomParserLoader(
                self._data_dir / CUSTOM_PARSERS_SUBDIR
            )
        # 自定义解析器一律用直连下载器：它们没有对应的「走代理」配置项
        # （AstrBot 会剔除 schema 之外的键），代理池只服务于内置平台。
        result = self._custom_loader.reload(
            self.downloader, skip_keys=set(self.disabled_platforms)
        )
        self._custom_result = result
        # 先把上一轮装进去的自定义平台摘掉，再装这一轮的。
        #
        # **不能只 update**：dict.update 只覆盖同名键，删掉（或改了平台键）的文件
        # 会把自己的旧实例留在 self.parsers 里 —— 平台列表里已经没有它了
        # （加载器的 _purge 反注册了展示名），但实例还在：
        #   · WebUI 手动解析照旧用**旧代码**解析（_match_parser 找得到它）
        #   · toggle_platform 会对它回「未知平台」（展示名已不在 PLATFORM_DISPLAY_NAMES）
        # 也就是「看起来删了、其实还在跑」，本仓最贵的那类 bug。
        # 用自己记的键来摘，而不是 is_custom_platform(key)：后者查的是 PLATFORMS，
        # 而 _purge 已经把旧键从那里删掉了，问它只会得到「这不是自定义平台」。
        for stale_key in self._custom_parser_keys:
            self.parsers.pop(stale_key, None)
        self._custom_parser_keys = set(result.parsers)
        self.parsers.update(result.parsers)

    def reload_custom_parsers(self) -> CustomParserLoadResult:
        """供 WebUI 的「重新加载自定义解析器」按钮调用：重扫 + 立刻生效。"""
        self._init_custom_parsers()
        return self._custom_result or CustomParserLoadResult()

    def _build_screenshot_service(self) -> None:
        """按当前配置（重）建截图服务。

        抽成独立方法是因为热更新也要调它：截图后端的代理与证书校验都是构造时
        读死的，只在初始化时建一次的话，网页上改这两项要重载插件才生效。
        """
        pconfig = get_config()
        self.screenshot = ScreenshotService(
            self.cache_dir,
            backend=pconfig.SCREENSHOT_BACKEND,
            cf_account_id=pconfig.CF_ACCOUNT_ID,
            cf_api_token=pconfig.CF_API_TOKEN,
            proxy=pconfig.SCREENSHOT_PROXY,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
        )
        self._screenshot_fallback = pconfig.SCREENSHOT_FALLBACK

    async def _cache_cleanup_loop(self):
        """缓存清理循环。

        **每一轮重新读配置**，而不是把 TTL / 间隔烤进闭包 —— 配置页的「维护」
        分组写着「改动后立即生效」，而 apply_runtime_config 并不重建这个任务，
        烤进闭包就等于「网页上改了 CACHE_TTL_HOURS 也不会生效」。
        """
        while True:
            pconfig = get_config()
            ttl = pconfig.CACHE_TTL_HOURS
            interval = max(pconfig.CACHE_CLEANUP_INTERVAL_MINUTES, 1) * 60
            try:
                if ttl > 0:
                    await cleanup_cache_dir(self.cache_dir, ttl_hours=ttl)
                    self._result_cache.clear()
                    self._render_cache.clear()
                    # 去重窗口也一起清：窗口里的「只解析第一个」是基于上一次解析
                    # 建立的，缓存都清空了还锁着，后来者会被无故静默掉
                    self._link_dedup.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一次清理失败（如磁盘满/文件占用）不应杀死整个循环
                logger.warning("[denia_share] 缓存清理失败，下轮重试", exc_info=True)
            await asyncio.sleep(interval)

    async def initialize(self):
        # 循环常驻：TTL=0 时它自己跳过清理，这样「把 TTL 从 0 改成非 0」也能生效
        self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())

        await self._bili_load_cookie()
        self._bili_http_session = aiohttp.ClientSession()
        if self._bili_cookie:
            self._bili_apply_cookie_to_parser(self._bili_cookie)
        from .core.credentials.sync import service_for
        service_for(self).start()

    # ==================== 平台处理器 ====================

    def _access_denied(self, event: AstrMessageEvent) -> bool:
        """白名单 / 黑名单判定：True 表示这次请求该被**静默**拦下。

        管理员由 AstrBot 判定（``event.is_admin()``），与 ``/denia_status`` 等命令的
        ADMIN 门一致，不单独配管理员 ID。判定顺序见 ``core/permissions.py``。

        拦下时只写一条 debug 日志：黑名单群里不该因为有人发链接就刷出提示。

        判定本身抛异常时**按放行处理**并留一条 warning —— 名单配错最多是「没拦住」，
        而在这里静默拦下全部请求会让插件看起来彻底坏了（用户只会看到「零反应」）。
        """
        try:
            allowed = is_allowed(
                get_config(),
                is_admin=event.is_admin(),
                is_private=event.is_private_chat(),
                sender_id=event.get_sender_id(),
                group_id=event.get_group_id(),
            )
        except Exception:
            logger.warning("[denia_share] 权限判定失败，本次按放行处理", exc_info=True)
            return False
        if not allowed:
            logger.debug(
                "[denia_share] 被权限名单拦下：user=%s group=%s",
                event.get_sender_id(),
                event.get_group_id(),
            )
        return not allowed

    async def _dispatch(self, event: AstrMessageEvent, name: str):
        if self._has_json_component(event):
            return
        parser = self.parsers.get(name)
        if not parser:
            return
        async for r in self._process_url(event, parser):
            yield r

    # 内置平台的链接处理器。每条 handler 的 docstring 会**原样显示在插件详情页的
    # 「指令」列表里**（AstrBot 取 handler.__doc__ 作为描述），所以一句话说清解析什么。
    @filter.regex(BILIBILI_PATTERN)
    async def bilibili_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """B站链接解析（视频 / 动态 / 直播 / 专栏 / 收藏夹）"""
        async for r in self._dispatch(event, "bilibili"):
            yield r

    @filter.regex(DOUYIN_PATTERN)
    async def douyin_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """抖音链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "douyin"):
            yield r

    @filter.regex(KUAISHOU_PATTERN)
    async def kuaishou_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """快手链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "kuaishou"):
            yield r

    @filter.regex(WEIBO_PATTERN)
    async def weibo_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """微博链接解析（正文 / 视频 / 长文）"""
        async for r in self._dispatch(event, "weibo"):
            yield r

    @filter.regex(XHS_PATTERN)
    async def xiaohongshu_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """小红书链接解析（图文 / 视频，图片改写为无水印原图）"""
        async for r in self._dispatch(event, "xiaohongshu"):
            yield r

    @filter.regex(TWITTER_PATTERN)
    async def twitter_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Twitter / X 链接解析（图片 / 视频）"""
        async for r in self._dispatch(event, "twitter"):
            yield r

    @filter.regex(NGA_PATTERN)
    async def nga_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """NGA 帖子解析"""
        async for r in self._dispatch(event, "nga"):
            yield r

    @filter.regex(ACFUN_PATTERN)
    async def acfun_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """AcFun 视频解析"""
        async for r in self._dispatch(event, "acfun"):
            yield r

    @filter.regex(GITHUB_PATTERN)
    async def github_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """GitHub 仓库卡片（星标 / 分支 / issue / 主语言 / 最近提交）"""
        async for r in self._dispatch(event, "github"):
            yield r

    @filter.regex(PIXIV_PATTERN)
    async def pixiv_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Pixiv 作品解析（非全年龄内容一律过滤）"""
        async for r in self._dispatch(event, "pixiv"):
            yield r

    @filter.regex(STEAM_PATTERN)
    async def steam_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Steam 商店页卡片（价格 / 折扣 / 类型，可含历史最低价）"""
        async for r in self._dispatch(event, "steam"):
            yield r

    @filter.regex(XIAOHEIHE_PATTERN)
    async def xiaoheihe_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """小黑盒帖子与游戏分享链接解析。"""
        async for result in self._dispatch(event, "xiaoheihe"):
            yield result

    @filter.regex(r"https?://(?:[A-Za-z0-9-]+\.)?(?:music\.163\.com|163cn\.tv)/")
    async def netease_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """网易云音乐单曲分享链接解析。"""
        async for result in self._dispatch(event, "netease"):
            yield result

    @filter.regex(r"https?://(?:[A-Za-z0-9-]+\.)?y\.qq\.com/")
    async def qqmusic_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """QQ音乐单曲分享链接解析。"""
        async for result in self._dispatch(event, "qqmusic"):
            yield result

    # 自定义解析器的聊天入口。内置平台各有自己的 ``@filter.regex(<域名>)`` 处理器，
    # 而自定义平台是**运行时**才知道的，没法为它生成静态过滤器 —— 所以用这个通用 URL
    # 处理器兜住它们。少了这一条，自定义解析器只能在网页上手动解析：群里发链接**零反应**
    # （加载成功、平台开关里能看到它、自检也过，但聊天链路根本没有入口）。
    #
    # 两条让路规则：
    # - 消息带 JSON 组件（QQ 小程序卡片）时直接返回：``json_card_handler`` 已经会遍历
    #   全部解析器（含自定义），不返回就会把同一链接解析两遍。
    # - URL 归一个**已启用**的内置平台时跳过：那是内置处理器的活，重复处理会发两条。
    #   内置平台被禁用时不算命中，此时自定义解析器可以接过去。
    #
    # 上面这段原本是 handler 的 docstring，会整段显示在插件详情页里，太长，改成注释。
    @filter.regex(URL_PATTERN)
    async def custom_parser_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """自定义解析器的聊天入口（通用 URL 匹配）"""
        if self._has_json_component(event):
            return
        for link in dict.fromkeys(URL_PATTERN.findall(event.message_str or "")):
            if self._match_builtin_parser(link) is not None:
                continue
            parser = self._match_custom_parser(link)
            if parser is None:
                continue
            async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                yield r
            return

    # ==================== 网页截图 ====================

    @filter.command("shot")
    async def shot_command(self, event: AstrMessageEvent):
        """对链接截图 /shot <网址>"""
        url = self._first_url(event)
        if not url:
            yield event.plain_result("用法: /shot <网址>")
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.regex(URL_PATTERN)
    async def screenshot_fallback_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """无可解析平台时按需截图兜底（默认关闭）。"""
        if not self._screenshot_fallback or self._has_json_component(event):
            return
        url = self._first_url(event)
        if not url or self._match_parser(url) is not None:
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.command("pixiv")
    async def pixiv_search_command(self, event: AstrMessageEvent):
        """Pixiv 关键词搜索 /pixiv <关键词>"""
        if self._access_denied(event):
            return
        keyword = " ".join((event.message_str or "").split()[1:]).strip()
        if not keyword:
            yield event.plain_result("用法: /pixiv <关键词>\n结果强制过滤非全年龄内容")
            return

        parser = self.parsers.get("pixiv")
        if parser is None:
            yield event.plain_result("Pixiv 平台已被禁用")
            return

        # 三路全关：连搜索 API 都不该打，更不该占结果缓存 / 写历史。
        # 位置在 parser.search 之前 —— 否则「什么都不发」却仍消耗一次联网请求。
        if not get_config().any_output_enabled():
            logger.info("[denia_share] 三路输出全关，跳过 Pixiv 搜索")
            return

        try:
            cache_key = f"pixiv:{keyword}"
            result = self._get_cached_result(cache_key)
            if result is None:
                result = await parser.search(keyword)
                self._remember_result(cache_key, result)
            async for r in self._deliver(event, result, cache_key):
                yield r
            await self._record_history(
                result, self._render_cache.get(cache_key), via="command"
            )
        except IgnoreException as e:
            logger.warning(f"[denia_share] Pixiv 搜索被忽略: {e.message}")
            yield event.plain_result(f"ℹ️ {e.message}")
        except ParseException as e:
            logger.warning(f"[denia_share] Pixiv 搜索失败: {e.message}")
            yield event.plain_result(f"❌ 搜索失败: {e.message}")
        except Exception as e:
            logger.exception("Pixiv 搜索异常")
            # 同样不回显异常原文，避免把 URL / 容器内路径带进群里
            yield event.plain_result(
                f"❌ 搜索出错（{type(e).__name__}），详情见 AstrBot 日志"
            )

    # ==================== JSON 卡片（QQ 小程序分享） ====================

    def _get_music_controller(self):
        controller = getattr(self, "_music_controller", None)
        if controller is None:
            from .core.music.controller import MusicController
            controller = MusicController(self)
            self._music_controller = controller
            self._music_login_manager = controller.login
        return controller

    async def _refresh_music_account(self, platform):
        await self._get_music_controller()._refresh(platform)

    @filter.command("点歌", priority=10)
    async def music_search_command(self, event: AstrMessageEvent):
        """点歌 歌名：发送带编号的搜索卡片。"""
        async for output in self.music_commands_handler(event):
            yield output

    @filter.command("听", priority=10)
    async def music_play_command(self, event: AstrMessageEvent):
        """听 N：播放上一次搜索结果中的第 N 首。"""
        async for output in self.music_commands_handler(event):
            yield output

    @filter.regex(r"(?s)^.*$", priority=10)
    async def music_commands_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """点歌 歌名显示本插件搜歌卡片，听1 按编号发送音频。"""
        from .core.music.controller import music_command
        selected = music_command(event.message_str)
        if selected is None or self._has_json_component(event):
            return
        if self._access_denied(event):
            return
        # 搜索卡片通过 context 主动投递，不会设置宿主的“已发送”标记。
        # 命中后先认领事件，避免搜索/选歌继续落入 LLM 或其他命令入口。
        event.stop_event()
        try:
            async for output in self._get_music_controller().handle(event, *selected):
                yield output
        finally:
            # 兼容停止标记存放于 result 的宿主版本：yield 可能替换 result。
            event.stop_event()

    # ==================== 确认下载入口 ====================

    @filter.command("确认下载")
    async def confirm_download_command(self, event: AstrMessageEvent):
        """确认下载：下载并立即发送待确认的源媒体"""
        if not self._claim_confirm_command(event):
            return
        async for r in self._confirm_entry(event):
            yield r

    @filter.regex(CONFIRM_DOWNLOAD_PATTERN)
    async def confirm_download_regex(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """确认下载（无前缀）：确认下载 [短编号]"""
        if not self._claim_confirm_command(event):
            return
        async for r in self._confirm_entry(event):
            yield r

    def _claim_confirm_command(self, event: AstrMessageEvent) -> bool:
        """同一条消息同时命中 command + regex 时只处理一次（同步抢占）。"""
        try:
            if event.get_extra("_denia_confirm_handled"):
                return False
            event.set_extra("_denia_confirm_handled", True)
            return True
        except Exception:
            flag = getattr(event, "_denia_confirm_handled", False)
            if flag:
                return False
            event._denia_confirm_handled = True
            return True

    async def _confirm_entry(self, event: AstrMessageEvent):
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        match = CONFIRM_DOWNLOAD_PATTERN.match(text)
        if match is None:
            return
        event.stop_event()
        try:
            try:
                event.should_call_llm(False)
            except Exception:
                pass
            async for r in self._confirm.handle(event, match.group(1)):
                yield r
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[denia_share] 确认下载处理异常")
            yield event.plain_result(
                "❌ 确认下载失败（内部错误），详情见 AstrBot 日志"
            )
        finally:
            event.stop_event()

    @filter.regex(r".*")
    async def json_card_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """QQ 小程序分享卡片（Json 消息段 / 官方 B站卡片）里的链接解析"""
        if not self._has_json_component(event):
            link = self._official_bilibili_card_link(event)
            parser = self.parsers.get("bilibili")
            if not link or parser is None:
                return
            try:
                parser.search_url(link)
            except (SilentException, ParseException):
                return
            # 正文中的 B站链接 / BV 已由 bilibili_handler 接管，避免同一事件双发。
            try:
                parser.search_url(event.message_str or "")
            except (SilentException, ParseException):
                pass
            else:
                return
            async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                yield r
            return
        links = self._prioritize_card_links(self._extract_links_from_event(event))
        if not links:
            return
        for link in links:
            for parser in self.parsers.values():
                try:
                    parser.search_url(link)
                    async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                        yield r
                    return
                except (SilentException, ParseException):
                    continue

    def _prioritize_card_links(self, links: list[str]) -> list[str]:
        """卡片里的链接排序：**能在本地认定视频身份的排前面**。

        分享卡片经常同时带短链（``meta.detail_1.qqdocurl``，如 ``b23.tv/…``）和
        原长链（``detail_1.url``）。原来的顺序是 ``qqdocurl`` 在前，于是永远先跳
        一次短链：短链失效时整张卡片直接失败，明明旁边那条长链是好的、不需要跳转。

        平台解析器可以通过 ``dedup_identity``（可选 hook）在本地认领资源身份。
        这里就按「第一个认领它的解析器有没有给出身份」稳定排序 —— 有的优先，
        需要额外跳转的靠后；认不出 / 没有 hook 的解析器保持原顺序（``sorted`` 稳定）。
        """
        unique = list(dict.fromkeys(links))

        def priority(link: str) -> int:
            for parser in self.parsers.values():
                try:
                    _keyword, searched = parser.search_url(link)
                except Exception:
                    continue
                resolver = getattr(parser, "dedup_identity", None)
                if callable(resolver):
                    try:
                        if resolver(searched):
                            return 0
                    except Exception:
                        pass
                return 1
            return 2

        return sorted(unique, key=priority)

    # ==================== 核心流程 ====================

    @staticmethod
    def result_cache_key(url: str) -> str:
        """解析结果缓存键（实现见 core/result_cache.py）。"""
        return ResultCache.result_cache_key(url)

    async def _process_url(
        self, event: AstrMessageEvent, parser: Any
    ) -> AsyncGenerator[MessageEventResult, None]:
        _DELIVERY_UNCERTAIN.set(False)
        from .core.diagnostics import store_for
        diagnostic = store_for(self).start('chat', self._error_platform(parser))
        diagnostic.stage('match', 'running')
        phase = 'match'
        # 白名单 / 黑名单的唯一收口点：内置平台处理器、自定义解析器、
        # JSON 卡片三条路径最后都走这里（/shot 截图与 /pixiv 搜索各自另有入口）。
        if self._access_denied(event):
            diagnostic.stage('match', 'skipped', 'blocked')
            diagnostic.finish('skipped')
            return
        # 这两个必须**在 try 之前**预置：下面的失败分支都要用它们撤销
        # 去重占位，而它们是在 try 里逐个赋值的。若 try 的第一行
        # ``result_cache_key(url)`` 就抛了（url 含孤立代理字符时
        # ``url.encode()`` 会抛 UnicodeEncodeError），except 里引用未赋值的
        # ``scope`` 会再抛 UnboundLocalError —— 真实错误被掩盖，forget 不执行、
        # 「处理出错」提示也发不出去。
        cache_key: str | None = None
        scope: str | None = None
        # 去重键与结果缓存键**分开**：缓存按「匹配到的原文」算，去重按平台给出的
        # 规范身份算（见 _dedup_key）。撤销占位必须用登记时的那个键，所以也预置。
        dedup_key: str | None = None
        # 已经交付出去了就别再撤销占位：_record_history 之类**交付之后**的收尾
        # 步骤失败时撤销占位，会让下一个发同链接的人重新解析并**再发一张卡片** ——
        # 那正好是这个功能要消灭的事。
        delivered = False
        try:
            raw_message = event.message_str.strip()
            # **键必须来自「解析器实际匹配到的那一段」，不能自己抽 URL。**
            #
            # 踩过两次坑：
            #   1. 键用整条消息 → 11 个内置平台处理器传的是整条事件，
            #      「【】看看这个 <url>」和「<url>」成了两个键，不去重也不共用缓存。
            #   2. 改用 `URL_PATTERN.search()` 抽「第一个 URL」→ 那是**独立**的抽取，
            #      和解析器自己的 pattern 可以是**不同的链接**：
            #        · 消息里两个不同平台的链接 → 第二个平台的处理器拿到第一个链接的键，
            #          抖音解析器一次没被调用，却发出一张 B站卡片；
            #        · 前导一个非本平台链接 → 两个不同视频共用一个键，用户收到的是
            #          **第一个视频的卡片**；
            #        · `URL_PATTERN` 的 `[^\s'\"<>]+` 不排除中文与标点，
            #          「…/xxx，挺有意思」会把 8 个中文字一起吞进键。
            #
            # 所以顺序必须是：**先让解析器认领，再按它认领的那段算键**。
            keyword, searched = parser.search_url(raw_message)
            diagnostic.stage('match', 'ok')
            matched = searched.group(0) or raw_message
            cache_key = self.result_cache_key(matched)

            # ---- 三路输出全关：这里就短路 ----
            # 位置很关键：必须在 search_url **之后**（还得知道是哪条链接，便于日志）
            # 但要在**去重占位与解析之前**。否则用户把三项都关掉后，每条链接
            # 仍然会去解析、下载全部媒体，最后什么也不发 —— 既白花流量，
            # 又会把去重窗口占掉让群里其他人也收不到东西。
            if not get_config().any_output_enabled():
                diagnostic.stage('send', 'skipped', 'output_off')
                diagnostic.finish('skipped')
                logger.info(
                    f"[denia_share] 文本/卡片/富媒体三项全关，本次不解析也不响应: "
                    f"url={matched[:60]}"
                )
                return

            # ---- 确认下载：只取封面、生成信息卡，等用户确认后再取源文件 ----
            # 必须排在去重之前：同链接的不同分享 / 同群其他成员确认不能被
            # 同链接去重窗口拦住；本分支也不会占用/撤销去重占位。
            if self._confirm.mode() == "confirm":
                parser_key = str(
                    getattr(getattr(parser, "platform", None), "name", "") or ""
                )
                reason = self._confirm.preflight(parser_key)
                if reason:
                    yield event.plain_result(reason)
                    return
                phase = 'parse'
                diagnostic.stage('parse', 'running')
                result = await parser.parse(keyword, searched)
                diagnostic.stage('parse', 'ok')
                async for r in self._confirm.offer(
                    event,
                    parser=parser,
                    parser_key=parser_key or result.platform.name,
                    result=result,
                    matched=matched,
                    cache_key=cache_key,
                    searched=searched,
                ):
                    yield r
                diagnostic.stage('send', 'ok')
                diagnostic.finish('ok')
                return

            # ---- 同群重复链接去重 ----
            # 必须排在缓存查询**之前**：命中结果缓存时照旧要发卡片，而去重要的
            # 是「不再响应」。先查缓存的话第二个人的卡片就照发出来了，等于没去重。
            if self._dedup_applies(event):
                scope = self._event_scope(event)
                # 短链（bv 藏在跳转后）先展开成实际匹配；展开结果后面直接拿去
                # parse，所以这一跳就是 parse_short 本来要跳的那一次，不重复。
                keyword, searched = await self._prepare_dedup_target(parser, keyword, searched)
                dedup_key = self._dedup_key(parser, searched, cache_key)
                decision = self._link_dedup.check(scope, dedup_key)
                if decision == HINT:
                    diagnostic.stage('parse', 'skipped', 'duplicate')
                    diagnostic.finish('skipped')
                    uncertain = self._link_dedup.is_uncertain(scope, dedup_key)
                    if uncertain:
                        # 送达不确定与「确认重复」是两回事：即使提示文字被清空，
                        # 这条也必须发 —— 用户需要知道上一条可能根本没送达。
                        yield event.plain_result(self._UNCERTAIN_HINT)
                    else:
                        hint = self._dedup_hint_text()
                        if hint:
                            yield event.plain_result(hint)
                        else:
                            # 配置把提示文字清空 = 已确认重复时静默（同 SILENT 的
                            # 不打扰语义，只是保留 HINT 判定的窗口语义）。
                            logger.info(
                                f"[denia_share] 同会话重复链接已按配置静默: scope={scope} "
                                f"url={matched[:60]}"
                            )
                    return
                if decision == SILENT:
                    diagnostic.stage('parse', 'skipped', 'duplicate')
                    diagnostic.finish('skipped')
                    logger.info(
                        f"[denia_share] 同会话重复链接已静默: scope={scope} "
                        f"url={matched[:60]}"
                    )
                    return

            phase = 'parse'
            diagnostic.stage('parse', 'running')
            result = self._get_cached_result(cache_key)
            if result is None:
                result = await parser.parse(keyword, searched)
                self._remember_result(cache_key, result)
                diagnostic.stage('parse', 'ok')
            else:
                diagnostic.stage('parse', 'ok', 'cached')

            def mark_delivered():
                nonlocal delivered
                delivered = True

            phase = 'send'
            queued = False
            async for r in self._deliver(event, result, cache_key, on_delivered=mark_delivered, diagnostic=diagnostic):
                delivered = True
                queued = True
                yield r

            if _DELIVERY_UNCERTAIN.get():
                diagnostic.stage('send', 'unknown', 'receipt_unknown')
                diagnostic.finish('unknown')
                if scope is not None and dedup_key is not None:
                    self._link_dedup.mark_uncertain(scope, dedup_key)
                logger.info("[denia_share] 合并转发送达未知，保留本次去重窗口但不记成功历史")
                return
            if not delivered:
                diagnostic.stage('send', 'error', 'send_failed')
                diagnostic.finish('error')
                self._forget_dedup(scope, dedup_key, False)
                return

            # 交付完成后再记录：此时媒体都已落盘，记录里能带上真实文件名
            await self._record_history(result, self._render_cache.get(cache_key))
            has_error = any(s['state'] == 'error' for s in diagnostic.to_dict()['stages'])
            if not any(s['name'] == 'send' and s['state'] == 'error' for s in diagnostic.to_dict()['stages']):
                diagnostic.stage('send', 'unknown' if queued else 'ok', 'queued' if queued else 'accepted')
            diagnostic.finish('error' if has_error else 'unknown' if queued else 'ok')

        except SilentException as e:
            diagnostic.stage(phase, 'skipped', 'skipped')
            diagnostic.finish('skipped')
            # 「这条链接不归我管」是每条普通消息都会走到的正常流量，只在调试级留痕
            logger.debug(f"[denia_share] 静默跳过: {e.message}")
            self._forget_dedup(scope, dedup_key, delivered)
        except ParseException as e:
            diagnostic.stage(phase, 'error', 'parse_failed')
            diagnostic.finish('error')
            # 解析失败必须无条件留痕：Cookie 全失效、接口改版、被风控都走这条，
            # 而 SEND_ERROR_MESSAGES 默认是关的 —— 只由它决定「发不发群」，
            # 不能连日志一起决定，否则群里不回复、日志也一片空白，用户和运维同时失明。
            logger.warning(f"[denia_share] 解析未完成: {e.message}")
            # 同样要撤销：第一次解析挂了就该让下一个人重试，而不是废掉整个窗口
            self._forget_dedup(scope, dedup_key, delivered)
            # 已经交付出去了就别再报「处理出错」：卡片已经发到群里了，
            # 收尾步骤（记解析记录之类）失败不影响用户拿到了内容，
            # 再补一句报错只会让人以为这条链接没解析成功。
            if not delivered and e.notify_prefix and self._send_errors:
                yield event.plain_result(f"{e.notify_prefix} {e.message}")
        except httpx.RequestError as e:
            diagnostic.stage(phase, 'error', 'network')
            diagnostic.finish('error')
            # 网络层故障（短链展开的 ConnectError / TimeoutException 等）单独归一类：
            # 它不是「解析器不认识这条链接」，也不是代码 bug，报错文案要能区分开。
            # 日志**只记平台与异常类型**：异常原文常带完整 URL（短链里可能含 token），
            # 不能落进日志，也不打 traceback。
            logger.warning(
                f"[denia_share] 网络请求失败: platform={self._error_platform(parser)} "
                f"type={type(e).__name__}"
            )
            # 失败就撤占位（见 _forget_dedup：未交付才撤），下一个人能重试。
            self._forget_dedup(scope, dedup_key, delivered)
            if not delivered and self._send_errors:
                # 不回显异常原文 / URL：只说明「这次没拿到内容、是网络问题」
                yield event.plain_result("❌ 网络请求失败，请检查网络或稍后重试")
        except Exception as e:
            diagnostic.stage(phase, 'error', 'parse_failed' if phase == 'parse' else 'send_failed')
            diagnostic.finish('error')
            logger.exception("解析异常")
            self._forget_dedup(scope, dedup_key, delivered)
            if not delivered and self._send_errors:
                # 不回显异常原文：里面常带完整 URL 与容器内本地路径
                yield event.plain_result(
                    f"❌ 处理出错（{type(e).__name__}），详情见 AstrBot 日志"
                )
        finally:
            if diagnostic.status == 'running':
                diagnostic.finish('unknown')

    # ---- 同群重复链接去重的辅助 ----

    #: 窗口内后来者收到的提示。只发一次（见 core/link_dedup 的说明）。
    #: 这是配置项 ``DEDUP_HINT_TEXT`` 的兜底默认值；实际发送文字见
    #: :meth:`_dedup_hint_text`（配置留空 = 已确认重复时静默）。
    _DEDUP_HINT = "这个链接刚刚已经解析过了哦"
    _UNCERTAIN_HINT = "上一条合并转发的发送结果还没确认，请先查看聊天记录；若未收到，等去重窗口结束后再发。"

    def _dedup_hint_text(self) -> str:
        """已确认重复时发送的提示文字；配置为空白时返回空串（上游静默）。

        不确定送达提示（:attr:`_UNCERTAIN_HINT`）不受本方法影响 —— 调用方
        在确认重复分支里才用它。配置读取失败（旧测试桩未初始化）时回退默认文案。
        """
        try:
            return str(get_config().DEDUP_HINT_TEXT or "").strip()
        except Exception:
            return self._DEDUP_HINT

    @staticmethod
    def _dedup_applies(event: AstrMessageEvent) -> bool:
        """这次要不要走去重。

        **只在群聊里去重**：需求原文是「同一个群多个链接时触发」。私聊里
        一个人连发两次同一链接，第二次多半是「没看到回复再发一遍」，这时候
        拦下来只会让人以为插件坏了 —— 私聊本来就不存在「刷屏」问题。

        判断不出是不是私聊时（``is_private_chat()`` 本身抛异常）按**不去重**
        处理：宁可多发一次，也不要在正常路径上误伤。
        """
        try:
            return not bool(event.is_private_chat())
        except Exception:
            logger.debug("[denia_share] 私聊判定失败，本次不去重", exc_info=True)
            return False

    @staticmethod
    def _error_platform(parser: Any) -> str:
        """日志用的平台字段。**不是** URL —— 只用来区分「哪个平台网络挂了」。"""
        platform = getattr(parser, "platform", None)
        name = getattr(platform, "name", None)
        if name:
            return str(name)
        return type(parser).__name__

    def _forget_dedup(
        self, scope: str | None, dedup_key: str | None, delivered: bool
    ) -> None:
        """撤销去重占位。

        三个条件都要满足才撤：登记过（scope / dedup_key 都有值）、还没交付
        出去、确实走了去重分支。缺一个都会出问题：

        - 没登记过就撤：``None`` 拼出来的键不存在，无害，但会掩盖「忘记登记」
        - **已经交付还撤**：下一个人会重新解析并**再发一张卡片** —— 那正是这个
          功能要消灭的事。``_record_history`` 这类交付后的收尾步骤失败时最容易
          踩到。
        - 传的必须是**登记时的去重键**（``_dedup_key`` 算出来的那个），不是结果
          缓存键：平台做了跨形式归一（如 B站带参长链与裸 BV）时两者不同，传错
          就撤不掉占位，失败一次废掉整个窗口。
        """
        if not delivered and scope and dedup_key:
            self._link_dedup.forget(scope, dedup_key)

    def _dedup_key(self, parser: Any, searched: Any, cache_key: str) -> str:
        """算这次链接的**同群去重键**：优先用平台给出的规范身份。

        平台的规范身份由 :meth:`core.base_parser.BaseParser.dedup_identity` 提供，
        它能把「同一条资源的不同输入形式」归一（B站：带参分享长链 / 裸 BV /
        ``bilibili.com/BV…``），解决「第一人发卡片、第二人发 BV 直链仍再次解析」。

        三条要求：

        - **解析器没有该方法、返回 None / 空串、或实现抛异常时**，一律回落到
          ``cache_key``（= 匹配到的原文哈希），行为与旧版逐字节一致 —— 这个
          hook 是可选的，不能让第三方自定义解析器因为缺它而挂掉；
        - 身份只用于**去重**，不参与结果缓存：缓存仍按用户实际发的链接算，
          去重窗口过期后「换种发法」仍会正常解析（窗口过期行为保持不变）；
        - 加 ``dedup:`` 前缀再哈希，避免规范身份与「原文哈希」落在同一命名空间里
          相互撞键（两个 md5 输入不同，碰撞概率可忽略）。
        """
        resolver = getattr(parser, "dedup_identity", None)
        if not callable(resolver):
            return cache_key
        try:
            identity = resolver(searched)
            if isinstance(identity, str) and identity.strip():
                return self.result_cache_key("dedup:" + identity.strip())
        except Exception:
            # 归一实现抛异常（第三方自定义解析器、脏字符串编码失败）不能拖垮解析：
            # 去重少一次，也不能让这条链接永远解析不了。
            logger.debug("[denia_share] 去重身份归一失败，回落原文键", exc_info=True)
        return cache_key

    async def _prepare_dedup_target(
        self, parser: Any, keyword: str, searched: Any
    ) -> tuple[str, Any]:
        """去重前的**异步**预处理：短链先展开成解析器真正认领的匹配。

        平台解析器可覆写 :meth:`core.base_parser.BaseParser.dedup_expand` 做这件事
        （B站 ``b23.tv`` / ``bili2233.cn``：BV 藏在跳转后面，不跳就按不了视频身份
        去重）。返回的匹配会被调用方**直接拿去 ``parse``**，所以这次跳转就是原来
        ``_parse_short_link`` 本来要跳的那一次 —— 不会为拿身份多跳一次，也不会
        多解析一次。

        三条要求：

        - **没实现该 hook 的解析器**（含所有第三方自定义解析器、以及测试里的替身）
          原样返回，行为与旧版一致；
        - **异常照原样抛出**：展开失败要走现有解析失败策略（撤销占位、让下一个人
          重试），不能在这里吞成一个「看起来解析过了」的静默分支；
        - 返回 ``None`` / 拿不到匹配时也原样返回，由调用方按原文键去重。
        """
        expander = getattr(parser, "dedup_expand", None)
        if not callable(expander):
            return keyword, searched
        expanded = await expander(keyword, searched)
        if not expanded:
            return keyword, searched
        new_keyword, new_searched = expanded
        if new_searched is None:
            return keyword, searched
        return new_keyword, new_searched

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        """取会话标识（``unified_msg_origin`` = 平台:类型:会话ID）。

        取不到时退回发送者 id：宁可把不同群当成同一个（少了去重机会），
        也不要反过来把不同会话混成一条 —— 混成一条会误伤正常解析。
        """
        origin = getattr(event, "unified_msg_origin", "") or ""
        if origin:
            return str(origin)
        try:
            return f"user:{event.get_sender_id()}"
        except Exception:
            return "unknown"

    def _dedup_window(self) -> int:
        """读去重窗口秒数；脏值回落到默认并留一条日志。

        不能让 ``int()`` 裸抛：异常会一路冒到 _process_url 的 except Exception，
        变成「处理出错」而这条链接永远解析不了。
        """
        try:
            return int(get_config().DUPLICATE_LINK_WINDOW_SECONDS)
        except (TypeError, ValueError):
            logger.warning(
                "[denia_share] 同群重复链接去重窗口配置不是整数，按 60 秒处理"
            )
            return 60

    async def _deliver(
        self, event: AstrMessageEvent, result: ParseResult, cache_key: str, *, on_delivered=None, diagnostic=None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """把已得到的 ParseResult 按用户选中的输出模式发出去（实现见 core/delivery.py）。"""
        async for r in self._delivery.deliver(
            event, result, cache_key, on_delivered=on_delivered, diagnostic=diagnostic
        ):
            yield r

    async def _audit_for_modes(
        self, result: ParseResult, *, media_on: bool
    ) -> dict[str, int]:
        """按输出模式结算媒体下载（实现见 core/delivery.py）。"""
        return await self._delivery.audit_for_modes(result, media_on=media_on)

    # ---- 结果 / 渲染缓存：实现见 core/result_cache.py（ResultCache 服务） ----
    # 字典本身仍是 ``self._result_cache`` / ``self._render_cache``（指向服务的同一对象，
    # 见 __init__）；这里只留薄委托，WebUI / 生命周期 / 既有测试照旧调用。

    def _result_cache_limit(self) -> int:
        """当前解析结果缓存条数上限（实现见 core/result_cache.py）。"""
        return self._caches._result_cache_limit()

    def _render_cache_limit(self) -> int:
        """当前渲染索引条数上限（实现见 core/result_cache.py）。"""
        return self._caches._render_cache_limit()

    def _trim_result_cache(self) -> None:
        """按当前上限即时收敛结果缓存（实现见 core/result_cache.py）。"""
        return self._caches._trim_result_cache()

    def _trim_render_cache(self) -> None:
        """按当前上限即时收敛渲染索引（实现见 core/result_cache.py）。"""
        return self._caches._trim_render_cache()

    def _remember_result(self, cache_key: str, result: ParseResult) -> None:
        """写入解析结果内存缓存（实现见 core/result_cache.py）。"""
        return self._caches._remember_result(cache_key, result)

    def _remember_render(self, cache_key: str, path: Path) -> None:
        """登记渲染产物（实现见 core/result_cache.py）。"""
        return self._caches._remember_render(cache_key, path)

    @staticmethod
    def _cache_media_gone(result: ParseResult) -> bool:
        """缓存结果的媒体文件是否已被磁盘清理（实现见 core/result_cache.py）。"""
        return ResultCache._cache_media_gone(result)

    def _get_cached_result(self, cache_key: str) -> ParseResult | None:
        """取可用缓存结果；不可用时已顺手淘汰（实现见 core/result_cache.py）。"""
        return self._caches._get_cached_result(cache_key)

    @staticmethod
    def _cache_media_failed(result: ParseResult) -> bool:
        """缓存结果是否已有确定失败的媒体任务（实现见 core/result_cache.py）。"""
        return ResultCache._cache_media_failed(result)

    def _drop_cache_entry(self, cache_key: str) -> None:
        """摘掉一条结果并连带摘掉渲染卡片（实现见 core/result_cache.py）。"""
        return self._caches._drop_cache_entry(cache_key)

    # ==================== 解析记录 ====================

    async def _record_history(
        self,
        result: ParseResult,
        render_path: Path | None,
        *,
        via: str = "auto",
        elapsed_ms: int | None = None,
    ) -> ParseRecord | None:
        """把一次成功的解析写入历史，供 WebUI 查看与管理。

        这是纯旁路：任何异常都只记日志，绝不影响正常的解析与发送。

        记录里的文件路径一律是 ``cache_dir`` 内的**安全相对 POSIX 路径**
        （如 ``sub/abc.jpg``），不存绝对路径、不泄露容器内位置；只记录
        ``contents`` / ``graphics`` / ``repost`` 链里已 ``resolved`` 且真实位于
        ``cache_dir`` 内的源媒体（不含头像与卡片封面），绝不为记历史触发任何
        下载。旧记录里的裸文件名（``abc.jpg``）仍按原样展示。
        """
        try:
            media_files = self._history_media_files(result, self.cache_dir)
            cache_dir = self.cache_dir
            record = ParseRecord.create(
                url=result.url or "",
                platform=result.platform.name,
                # 记录页是管理视图，用**正式名**：页面上的平台筛选与统计都是正式名，
                # 行内再显示「猴山」这种卡片叫法就会出现同一平台两个名字并排
                platform_display=platform_meta(result.platform.name).display_name,
                content_type=result.content_type,
                title=result.title or "",
                author=result.author.name if result.author else "",
                via=via,
                card_file=self._cache_relative_path(cache_dir, render_path) if render_path else None,
                media_files=media_files,
                detail=self._history_detail(result),
                elapsed_ms=elapsed_ms,
            )
            if not await asyncio.to_thread(self.history.add, record):
                # 写盘失败（Windows 上 os.replace 会撞开着的文件）：记录没能落盘，
                # 不能假装成功 —— 至少要在日志里说清楚，而不是静默少一条。
                logger.warning(
                    "[denia_share] 解析记录未能落盘（记录页可能正被占用），本条记录已丢失"
                )
            return record
        except Exception:
            logger.warning("[denia_share] 写入解析记录失败", exc_info=True)
            return None

    @staticmethod
    def _history_media_files(result: ParseResult, cache_dir: Path) -> list[str]:
        """收集记录页「源文件下载」可用的已落盘媒体（安全相对路径）。

        遍历 ``contents``、``graphics`` 里的 :class:`ImageContent`，以及
        ``repost`` 链（转发原帖）的同类媒体；只读 ``PathTask.resolved``，
        **绝不调用 get()**，因此不会为了记历史触发任何下载。路径统一经
        :meth:`_cache_relative_path` 限制在 ``cache_dir`` 内，并按规范化后的
        相对路径去重（同一条媒体出现在 contents 与 graphics 时只记一次）。

        **刻意不收**头像（``Author.avatar``）与卡片封面
        （``VideoContent/AudioContent.cover``）：它们是卡片装饰，不属于用户要
        下载的源媒体；渲染卡片本体也不在遍历范围，由 ``card_file`` 单独记录。
        """
        files: list[str] = []
        seen: set[str] = set()
        seen_results: set[int] = set()

        def walk(node: ParseResult) -> None:
            # repost 理论上可被异常构造成环；按对象身份兜底，避免死循环。
            if id(node) in seen_results:
                return
            seen_results.add(id(node))
            tasks = [cont.path_task for cont in node.contents]
            tasks.extend(
                gra.path_task for gra in node.graphics if isinstance(gra, ImageContent)
            )
            for task in tasks:
                path = task.resolved
                rel = DeniaSharePlugin._cache_relative_path(cache_dir, path)
                if rel and rel not in seen:
                    seen.add(rel)
                    files.append(rel)
            if node.repost is not None:
                walk(node.repost)

        walk(result)
        return files

    @staticmethod
    def _cache_relative_path(cache_dir: Path, path: Path) -> str | None:
        """把已落盘文件换算成 ``cache_dir`` 内的安全相对 POSIX 路径。

        - 不在 ``cache_dir`` 内（含经符号链接逃逸）的文件返回 ``None``，不记录；
        - 只返回 POSIX 相对路径（正斜杠），**绝不返回绝对路径**；
        - 只读路径形状，不触碰文件系统内容，也不触发下载。
        """
        if path is None:
            return None
        try:
            rel = Path(path).resolve().relative_to(Path(cache_dir).resolve())
        except (ValueError, OSError):
            return None
        if not rel.parts or rel.parts[0] in ("", ".", ".."):
            return None
        return rel.as_posix()

    @staticmethod
    def _history_detail(result: ParseResult) -> dict[str, Any]:
        """挑出适合展示在记录详情里的字段（不含媒体本体）。"""
        extra = result.extra or {}
        return {
            "stats_line": extra.get("stats_line") or "",
            "duration": extra.get("duration") or "",
            "online": extra.get("online") or "",
            "info": extra.get("info") or "",
            "text": (result.text or "")[:300],
            "image_count": len(result.img_contents),
            "video_count": len(result.video_contents),
            "audio_count": len(result.audio_contents),
            "warnings": list(extra.get("limit_warnings") or []),
        }

    # ==================== 配置热生效 ====================

    async def apply_runtime_config(self) -> dict[str, Any]:
        """按最新配置重建运行时组件。

        ``ParserConfig`` 直接读取 AstrBotConfig，所以配置值本身是实时的；
        这里负责重建那些「构造时把配置读死」的对象（解析器表、渲染器、截图服务、
        下载器参数），让网页端保存后不需要重载插件。
        """
        pconfig = get_config()
        summary: dict[str, Any] = {"rebuilt": False}

        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        # 共享缓存目录可能被改了：换目录后旧的内存缓存全部失效
        new_cache_dir, source = self._resolve_cache_dir(
            self._data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir_source = source
        if new_cache_dir != self.cache_dir:
            self.cache_dir = new_cache_dir
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pconfig.cache_dir = self.cache_dir
            self._result_cache.clear()
            self._render_cache.clear()
            self._link_dedup.clear()
            self._confirm.invalidate_all("cache_dir")
            logger.info(f"[denia_share] 媒体缓存目录已切换到 {self.cache_dir}")
        summary["cache_dir"] = str(self.cache_dir)
        summary["cache_dir_source"] = self.cache_dir_source

        try:
            await self.downloader.reconfigure(
                proxies=None,
                max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                cache_dir=self.cache_dir,
                verify_ssl=pconfig.HTTP_VERIFY_SSL,
                image_concurrency=pconfig.DOWNLOAD_IMAGE_CONCURRENCY,
                media_concurrency=pconfig.DOWNLOAD_MEDIA_CONCURRENCY,
            )
        except Exception:
            logger.warning("[denia_share] 更新下载器参数失败", exc_info=True)

        # 代理下载器跟随同一份缓存目录 / 体积上限 / 证书开关，代理地址取配置值。
        # 地址被清空时 reconfigure 会把它的池切成直连 —— 池子留着不关，
        # 省得下次有平台开开关又要重建。并发同样取配置值：直连 / 代理各池。
        if self._proxy_downloader is not None:
            try:
                await self._proxy_downloader.reconfigure(
                    proxies=pconfig.PROXY or None,
                    max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                    cache_dir=self.cache_dir,
                    verify_ssl=pconfig.HTTP_VERIFY_SSL,
                    image_concurrency=pconfig.DOWNLOAD_IMAGE_CONCURRENCY,
                    media_concurrency=pconfig.DOWNLOAD_MEDIA_CONCURRENCY,
                )
            except Exception:
                logger.warning("[denia_share] 更新代理下载器参数失败", exc_info=True)

        # 历史保留条数热同步：容量改后立即收敛既有记录（只裁记录，不删媒体），
        # 与「history.jsonl」的持久化上限保持同一个值。
        try:
            await asyncio.to_thread(
                self.history.set_max_records, pconfig.HISTORY_MAX_RECORDS
            )
        except Exception:
            logger.warning("[denia_share] 同步历史保留条数失败", exc_info=True)

        # 内存缓存条数上限热收敛：容量调小时立即把现有字典裁到新上限
        # （FIFO，保留 TTL —— 过期判定仍在读取侧做，这里只按条数收敛）。
        # 后面的全量 clear 是「配置变了结果失效」的既有逻辑，两者不冲突：
        # 即使只改了条数上限、其它配置没动，这里也保证当场收敛。
        self._trim_result_cache()
        self._trim_render_cache()

        self.parsers = {}
        self._init_parsers()

        music_controller = getattr(self, "_music_controller", None)
        if music_controller is not None:
            music_controller.reconfigure()

        # B站 Cookie **只在配置项真的变了的时候**才动。
        #
        # 为什么不能无条件重新应用：这个方法是「保存配置 / 恢复默认 / 保存外观」
        # 三个接口都会走的（webui.py:433/463/497）。无条件应用会有两种误伤 ——
        #   · 原先「登录态优先」：改 BILI_CK 不生效，接口却回 changed + runtime_ok
        #   · 改成「配置项优先」后：配置项非空时，改任何别的配置（代理/外观）都会
        #     把配置项里的 cookie 再应用一次，覆盖掉期间的扫码登录结果
        # 两种都是「用户没动 cookie，cookie 却被改了」。所以按「变化」触发。
        current_ck = str(pconfig.BILI_CK or "")
        if current_ck != self._bili_ck_applied:
            self._bili_ck_applied = current_ck
            if current_ck:
                self._bili_cookie = current_ck
                self._bili_apply_cookie_to_parser(current_ck)
            else:
                # 配置项被清空 = 用户明确要清掉（「恢复默认」的文案承诺了「包括 Cookie」）。
                # 走 bili_logout 而不是只清配置项：它会把主模块内存、解析器的持久化文件
                # 与 bili_cookie.json 一起清掉，否则「看起来清了、实际还在用」。
                await self.bili_logout()

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())
        # 渲染参数进了产物文件名，配置变了就得让旧缓存失效
        self._render_cache.clear()

        # 解析结果缓存**同样要清**：B站编码 / 清晰度 / 代理 / 限长这些配置项
        # 都影响解析结果。不清的话，用户在配置页把编码从 AV1 换成 H.264，
        # 再发同一条链接，拿到的还是旧结果 —— 配置改了却看不出效果，
        # 正是最容易被当成「插件没生效」的一类问题。
        self._result_cache.clear()
        # 去重窗口同理：窗口长度本身就是配置项之一，热更新后必须立即按新值判定，
        # 否则用户把窗口从 60 改成 0 以为关掉了，实际还要等旧窗口过期
        self._link_dedup.clear()

        # 截图后端的代理 / 证书校验同样是构造时读死的，热更新要一起重建
        self._build_screenshot_service()

        # 确认下载：权限每次实时读取，这里只按相关配置（模式/输出/限额/平台）
        # 失效 pending；无关保存不会清掉任何人的待确认项。
        self._confirm.refresh_config()

        summary["rebuilt"] = True
        summary["platforms"] = sorted(self.parsers)
        summary["render_enabled"] = self._renderer.enabled
        return summary

    # ==================== 网页截图 ====================

    @staticmethod
    def _first_url(event: AstrMessageEvent) -> str | None:
        urls = URL_PATTERN.findall(event.message_str or "")
        return urls[0] if urls else None

    def _match_parser(self, url: str) -> str | None:
        """找出能处理该 URL 的解析器名，没有则返回 None。"""
        for name, parser in self.parsers.items():
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_builtin_parser(self, url: str) -> str | None:
        """该 URL 是否归一个**当前启用**的内置平台；是则返回平台键。

        只在内置平台启用时才算命中：内置平台被禁用后它的处理器会直接 return
        （``_dispatch`` 里 ``self.parsers.get(name)`` 拿不到），这时应当让自定义
        解析器接过去 —— README 承诺过自定义解析器可以「改掉某个内置平台的行为」。
        """
        for name, parser in self.parsers.items():
            if is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_custom_parser(self, url: str):
        """找出能处理该 URL 的自定义解析器实例；没有则返回 None。"""
        for name, parser in self.parsers.items():
            if not is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return parser
            except Exception:
                continue
        return None

    # ==================== 示例卡片（外观页离线预览） ==================== #

    def _sample_image(self, kind: str) -> Path:
        """合成/复用一张离线示例图（渐变 + 几何图形），不联网。"""
        from PIL import Image as _Image, ImageDraw as _Draw

        path = self.cache_dir / f"_sample_{kind}.png"
        if path.is_file():
            return path
        if kind == "hero":
            w, h = 1280, 720
            img = _Image.new("RGB", (w, h))
            top, bottom = (58, 76, 128), (16, 20, 32)
            for yy in range(h):
                t = yy / max(h - 1, 1)
                img.paste(tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                          (0, yy, w, yy + 1))
            dr = _Draw.Draw(img, "RGBA")
            dr.ellipse((int(w * 0.62), -h // 3, int(w * 1.25), h // 2), fill=(139, 124, 246, 90))
            dr.ellipse((-w // 5, int(h * 0.55), w // 3, int(h * 1.4)), fill=(46, 196, 182, 70))
            dr.rounded_rectangle((int(w * 0.08), int(h * 0.6), int(w * 0.5), int(h * 0.78)),
                                 radius=28, fill=(255, 255, 255, 26))
        else:  # avatar
            size = 256
            img = _Image.new("RGB", (size, size), (36, 43, 63))
            dr = _Draw.Draw(img, "RGBA")
            # 椭圆要的是 (x0, y0, x1, y1) 四个坐标，少一个会被 Pillow 判为参数错误
            dr.ellipse((28, 28, size - 28, size - 28), fill=(251, 114, 153, 255))
            dr.ellipse((88, 96, 168, 176), fill=(255, 255, 255, 235))
            dr.arc((64, 150, 192, 236), start=15, end=165, fill=(255, 255, 255, 235), width=14)
        try:
            img.save(path, "PNG")
        except OSError:
            logger.warning("[denia_share] 示例图写入失败", exc_info=True)
        return path

    def build_sample_result(self) -> ParseResult:
        """构造离线示例解析结果（视频卡片：封面 + 头像 + 统计），供外观页实时预览。"""
        from .core.task import PathTask

        async def _static(path: Path) -> Path:
            return path

        hero_path = self._sample_image("hero")
        avatar_path = self._sample_image("avatar")
        video = VideoContent(
            path_task=PathTask(_static(hero_path)),
            cover=PathTask(_static(hero_path)),
            duration=309,
        )
        return ParseResult(
            platform=platform_of(PlatformEnum.BILIBILI),
            author=Author(
                name="希望示例频道",
                avatar=PathTask(_static(avatar_path)),
                description="本卡片为离线示例，用于预览外观效果",
            ),
            title="示例：全新外观设计器效果预览",
            text="这是一条用于预览的示例简介。调整左侧的主题、布局、强调色、水印等参数，"
                 "卡片会立刻按新外观重新渲染，无需发送任何真实链接。",
            timestamp=1760000000,
            url="https://www.bilibili.com/video/BV1uth56uEz3",
            contents=[video],
            extra={
                "content_type": "视频",
                "stats_line": "👍 12.3万 🪙 3.4万 ⭐ 5.6万 👀 210.5万",
                "duration": "05:09",
                "ai_summary": "这条离线示例展示了封面、作者和播放信息的排列。此摘要仅用于预览开关效果，不代表真实视频内容。",
                "ai_summary_status": "ok",
            },
        )

    async def _do_screenshot(self, event: AstrMessageEvent, url: str):
        # /shot 与「匹配不到平台就截图」两个入口都汇到这里
        if self._access_denied(event):
            return
        if not is_probably_screenshotable(url):
            yield event.plain_result("这个地址不支持截图")
            return
        if not self.screenshot.is_configured:
            yield event.plain_result(
                "截图后端未配置：使用 Cloudflare 时请填写 Account ID 与 API Token"
            )
            return

        # 刻意**不套卡片模板**：整页长图塞进 16:9 的封面槽会被裁到只剩约 15%
        # （等于废掉），开「封面不裁切」又会撑出一张两米高的卡片 —— 两种都
        # 没法在聊天里看。截图就是单独发出去的原图，用文字把「这是什么」讲清楚。
        full_page = get_config().SCREENSHOT_FULL_PAGE
        path = await self.screenshot.capture(url, full_page=full_page)
        if path is None:
            detail = self.screenshot.last_error or "未知原因"
            yield event.plain_result(f"截图失败：{detail}\n{url}")
            return

        caption = await self._screenshot_caption(path, url, full_page=full_page)
        await self._send_image(event, path)
        yield event.plain_result(caption)

    async def _screenshot_caption(
        self, path: Path, url: str, *, full_page: bool
    ) -> str:
        """给截图配一句能认出这张图的说明。

        只有 URL 的话，图是匿名的 —— 群里翻上去只能看到一张长图。所以尽量
        补上页面标题、尺寸、体积，让人一眼知道是什么、为什么这么大。

        标题是尽力而为的装饰（见 :meth:`ScreenshotService.page_title`），
        拿不到就少一行，不影响其他信息。
        """
        parts: list[str] = []

        title = await self.screenshot.page_title(url)
        parts.append(f"网页截图：{title}" if title else "网页截图")

        notes: list[str] = []
        size_bytes = 0
        try:
            from PIL import Image as _Image

            with _Image.open(path) as im:
                width, height = im.size
            size_bytes = path.stat().st_size
            parts.append(f"{width}×{height}，{max(1, size_bytes // 1024)} KB")
            notes = describe_capture(
                height, full_page=full_page, backend=self.screenshot.backend
            )
        except Exception:  # noqa: BLE001 —— 读不出尺寸就不写，别为装饰抛异常
            pass

        parts.append(url)
        parts.extend(notes)

        # 超大图提醒一句：群里客户端会压缩/可能拒收，先给用户一个预期
        if size_bytes > 5 * 1024 * 1024:
            parts.append(
                "（整页长图较大，客户端可能压缩或发送失败；"
                "可在配置里关掉「截整页长图」）"
            )
        return "\n".join(parts)

    async def _send_image(self, event: AstrMessageEvent, path: Path) -> bool:
        """主动发送图片，绕开事件回复管线（实现见 core/delivery.py）。"""
        return await self._delivery.send_image(event, path)

    @staticmethod
    def _forward_node_nickname() -> str:
        """合并转发节点的显示昵称（实现见 core/delivery.py）。"""
        return DeliveryService.forward_node_nickname()

    @staticmethod
    async def _build_nodes_result(event: AstrMessageEvent, header: str, items: list[list]):
        """构建合并转发结果（实现见 core/delivery.py）。"""
        return await DeliveryService.build_nodes_result(event, header, items)

    async def _build_output(self, result: ParseResult) -> tuple[str, list[list]]:
        """统一构建 (标题头, 内容节点列表)；已不再用于发送（实现见 core/delivery.py）。"""
        return await self._delivery.build_output(result)

    async def _send_plain_output(self, event: AstrMessageEvent, header: str, nodes_content: list[list]):
        """非 OneBot 平台：合并成一条消息链（实现见 core/delivery.py）。"""
        async for r in self._delivery.send_plain_output(event, header, nodes_content):
            yield r

    async def _send_text(self, event: AstrMessageEvent, result: ParseResult):
        """发送一条普通文本消息（实现见 core/delivery.py）。"""
        async for r in self._delivery.send_text(event, result):
            yield r

    async def _try_send_media(
        self, event: AstrMessageEvent, result: ParseResult, *, on_delivered=None,
        on_failed=None,
    ):
        """按聚合配置发送富媒体，语音和文件保持独立发送（实现见 core/delivery.py）。"""
        async for r in self._delivery.try_send_media(
            event, result, on_delivered=on_delivered, on_failed=on_failed
        ):
            yield r

    async def _send_media_item(
        self, event: AstrMessageEvent, item, *, on_delivered=None, on_failed=None
    ):
        """仅失败兜底模式主动发送，其他媒体仍交给宿主管线（实现见 core/delivery.py）。"""
        async for r in self._delivery.send_media_item(
            event, item, on_delivered=on_delivered, on_failed=on_failed
        ):
            yield r

    async def _iter_media_components(self, result: ParseResult):
        """构建原始媒体组件（实现见 core/delivery.py）。"""
        async for parts in self._delivery.iter_media_components(result):
            yield parts

    async def _try_send_audio(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
    ) -> bool:
        """优先发送语音，接口失败时仅尝试一次文件兜底（实现见 core/delivery.py）。"""
        return await self._delivery.try_send_audio(
            event, result, path, relay_enabled, callback_base, ttl
        )

    def _path_mapping_rules(self, event: AstrMessageEvent) -> list:
        """读取宿主配置的 path_mapping（实现见 core/delivery.py）。"""
        return self._delivery.path_mapping_rules(event)

    def _apply_audio_path_mapping(self, event: AstrMessageEvent, component) -> None:
        """主动发送前按宿主逻辑映射本地 Comp.File 路径（实现见 core/delivery.py）。"""
        return self._delivery.apply_audio_path_mapping(event, component)

    async def _build_audio_component(
        self,
        result: ParseResult,
        path: Path,
        relay_enabled: bool,
        callback_base: str,
        ttl: int,
        *,
        mode: str | None = None,
    ):
        """构建音频组件（实现见 core/delivery.py）。"""
        return await self._delivery.build_audio_component(
            result, path, relay_enabled, callback_base, ttl, mode=mode
        )
    # ==================== B站扫码登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_qr_login(self, event: AstrMessageEvent):
        """B站扫码登录，Cookie 会持久化保存"""
        sender_id = event.get_sender_id()
        if sender_id in self._bili_login_tasks and not self._bili_login_tasks[sender_id].done():
            yield event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")
            return

        try:
            qrcode_url, qrcode_key = await self.bili_qr_create()

            try:
                qr_image = qrcode.make(qrcode_url)
            except Exception:
                yield event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库")
                return

            qr_path = self._bili_data_dir / f"qrcode_{sender_id}.png"
            qr_image.save(str(qr_path), "PNG")
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "📱 请使用 **B站App** 扫描上方二维码\n⏱️ 有效期约3分钟\n📋 扫码后请在手机上点击「确认登录」"
            )

            self.bili_login_start(
                sender_id, qrcode_key, notify_umo=event.unified_msg_origin, qr_path=qr_path
            )
        except ParseException as e:
            yield event.plain_result(f"❌ {e.message}")
        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.plain_result(f"❌ 生成二维码失败: {e}")

    @filter.command("bili_check")
    async def bili_check_cookie(self, event: AstrMessageEvent):
        """检测B站Cookie是否有效"""
        if not self._bili_cookie:
            yield event.plain_result("⚠️ 尚未配置B站Cookie，请使用 /bili_login 扫码登录")
            return
        result = await self._bili_check_cookie_valid()
        if result["valid"]:
            yield event.plain_result(
                f"✅ B站Cookie有效\n用户: {result.get('username', '未知')}\nUID: {result.get('uid', 0)}"
            )
        else:
            yield event.plain_result(f"❌ B站Cookie失效\n错误: {result.get('error', '未知错误')}")

    # ---------- 扫码登录：WebUI 与聊天命令共用的实现（见 core/bili_login.py） ----------

    async def bili_qr_create(self) -> tuple[str, str]:
        """向 B站申请扫码登录二维码（实现见 core/bili_login.py）。"""
        return await self._bili_login.qr_create()

    @staticmethod
    def render_qr_png(data: str) -> bytes:
        """把文本渲染成 PNG 字节，供 WebUI 拼成 data URL 展示（实现见 core/bili_login.py）。"""
        return BiliLoginManager.render_qr_png(data)

    def bili_login_state(self, task_id: str) -> dict[str, Any]:
        """读取扫码登录任务的当前状态（实现见 core/bili_login.py）。"""
        return self._bili_login.state(task_id)

    async def bili_login_cancel(self, task_id: str | None = None) -> bool:
        """停止后台扫码轮询，不登出、不清除已保存的 Cookie（实现见 core/bili_login.py）。"""
        return await self._bili_login.cancel(task_id)

    def bili_login_start(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ) -> None:
        """启动后台轮询任务（实现见 core/bili_login.py）。"""
        return self._bili_login.start(
            task_id, qrcode_key, notify_umo=notify_umo, qr_path=qr_path
        )

    async def bili_cookie_status(self) -> dict[str, Any]:
        """检查当前 Cookie 是否仍有效（实现见 core/bili_login.py）。"""
        return await self._bili_login.cookie_status()

    async def bili_logout(self) -> bool:
        """清除本地保存的 B站 Cookie（不会撤销 B站侧的登录态）（实现见 core/bili_login.py）。"""
        return await self._bili_login.logout()

    # ---------- 内部方法（实现见 core/bili_login.py） ----------

    @staticmethod
    def _bili_headers() -> dict:
        return BiliLoginManager._headers()

    async def _bili_poll_qr_login(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ):
        """轮询扫码结果，成功后写入并持久化 Cookie（实现见 core/bili_login.py）。"""
        return await self._bili_login._poll_qr_login(
            task_id, qrcode_key, notify_umo=notify_umo, qr_path=qr_path
        )

    def _schedule_login_state_cleanup(self, task_id: str) -> None:
        """登录任务结束后延迟回收状态（实现见 core/bili_login.py）。"""
        return self._bili_login._schedule_state_cleanup(task_id)

    def _schedule_qr_cleanup(self, qr_path: Path, delay: int = 120) -> None:
        """延迟删除二维码临时文件（实现见 core/bili_login.py）。"""
        return self._bili_login._schedule_qr_cleanup(qr_path, delay=delay)

    def _bili_apply_cookie_to_parser(self, cookie_str: str):
        """把 Cookie 应用到 B站解析器（实现见 core/bili_login.py）。"""
        return self._bili_login._apply_cookie_to_parser(cookie_str)

    async def _bili_check_cookie_valid(self, cookie_str: str | None = None) -> dict:
        """校验 B站 Cookie（实现见 core/bili_login.py）。"""
        return await self._bili_login._check_cookie_valid(cookie_str)

    async def _bili_load_cookie(self):
        """启动时加载 B站 Cookie（实现见 core/bili_login.py）。"""
        return await self._bili_login.load_cookie()

    async def _bili_commit_login_cookie(self, cookie_str: str) -> str:
        """提交已校验的扫码凭据；失败返回公开错误信息（实现见 core/bili_login.py）。"""
        return await self._bili_login.commit_login_cookie(cookie_str)

    async def _bili_notify(self, session: str, message: str):
        """把登录结果发到聊天会话（实现见 core/bili_login.py）。"""
        return await self._bili_login.notify(session, message)

    # ==================== JSON 卡片工具 ====================

    @staticmethod
    def _official_bilibili_card_link(event: AstrMessageEvent) -> str | None:
        """只取官方事件当前卡片的跳转链接，不遍历引用、附件或描述文本。"""
        get_platform_name = getattr(event, "get_platform_name", None)
        try:
            platform_name = get_platform_name() if callable(get_platform_name) else None
        except Exception:
            return None
        if platform_name not in {"qq_official", "qq_official_webhook"}:
            return None
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        # AstrBot 4.29.3 的 Patched*Message 在 raw_data 中保留原始事件 d。
        data = raw.get("raw_data", raw) if isinstance(raw, dict) else getattr(raw, "raw_data", None)
        if not isinstance(data, dict) or data.get("message_type") not in (3, "3"):
            return None
        ark = data.get("ark_data")
        fields = ark.get("fields") if isinstance(ark, dict) else None
        link = fields.get("jump_url") if isinstance(fields, dict) else None
        if not isinstance(link, str) or not link or len(link) > 4096:
            return None
        # urlsplit 会忽略部分控制字符；先拒绝这些字符及浏览器歧义反斜杠。
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in link) or "\\" in link:
            return None
        try:
            url = urlsplit(link)
            host = url.hostname
            if (
                url.scheme not in {"http", "https"}
                or url.username is not None
                or url.password is not None
                or url.port is not None
            ):
                return None
        except ValueError:
            return None
        if host in {"b23.tv", "bili2233.cn"}:
            valid_path = re.fullmatch(r"/[0-9A-Za-z]+/?", url.path)
        elif host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
            valid_path = re.fullmatch(
                r"/(?:(?:video/)?(?:BV[0-9A-Za-z]{10}|av\d{6,})|(?:dynamic|opus)/\d+|read/cv\d+)/?",
                url.path,
            )
        elif host in {"t.bilibili.com", "live.bilibili.com"}:
            valid_path = re.fullmatch(r"/\d+/?", url.path)
        else:
            return None
        if not valid_path:
            return None
        # 规范主机大小写，供已有区分大小写的解析器匹配。
        return url._replace(netloc=host).geturl()

    @staticmethod
    def _has_json_component(event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for c in event.message_obj.message:
            if isinstance(c, Comp.Json):
                return True
            t = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
            if t and t != "reply" and "json" in str(t).lower():
                return True
        return False

    def _extract_links_from_event(self, event: AstrMessageEvent) -> list[str]:
        links: list[str] = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for c in event.message_obj.message:
                if isinstance(c, Comp.Json):
                    links.extend(self._extract_links_from_json(c.data))
                elif isinstance(c, Comp.Plain):
                    links.extend(URL_PATTERN.findall(c.text or ""))
                elif isinstance(c, dict) and "json" in str(c.get("type", "")).lower():
                    links.extend(self._extract_links_from_json(c.get("data", c)))
        links.extend(URL_PATTERN.findall(event.message_str or ""))
        return links

    @staticmethod
    def _extract_links_from_json(data) -> list[str]:
        links: list[str] = []
        try:
            if isinstance(data, str):
                data = json.loads(data)

            def search(obj):
                found = []
                if isinstance(obj, dict):
                    meta = obj.get("meta") or {}
                    if isinstance(meta, dict):
                        for dk in ("detail_1", "detail", "news", "music"):
                            d = meta.get(dk) or {}
                            if isinstance(d, dict):
                                for uk in ("qqdocurl", "url", "jumpUrl"):
                                    v = d.get(uk)
                                    if isinstance(v, str) and v:
                                        found.append(v)
                    for v in obj.values():
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            found.append(v)
                        elif isinstance(v, (dict, list)):
                            found.extend(search(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(search(item))
                return found

            links.extend(search(data))
        except Exception as e:
            logger.warning(f"解析 JSON 消息组件失败: {e}")
        return links

    # ==================== 管理命令 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_status")
    async def denia_status(self, event: AstrMessageEvent):
        """查看希望解析器的运行状态"""
        from . import __version__

        platforms = "、".join(self.parsers) if self.parsers else "（无）"
        backend = self.screenshot.backend
        shot_ready = "已就绪" if self.screenshot.is_configured else "未配置"
        yield event.plain_result(
            f"希望解析器 v{__version__}\n"
            f"已启用平台：{platforms}\n"
            f"卡片渲染：{'开' if self._renderer.enabled else '关'}\n"
            f"B站 Cookie：{'已配置' if self._bili_cookie else '未配置'}\n"
            f"截图后端：{backend}（{shot_ready}）"
            f"{'，链接兜底开' if self._screenshot_fallback else ''}\n"
            f"错误提示：{'开' if self._send_errors else '关'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_clear")
    async def denia_clear_cache(self, event: AstrMessageEvent):
        """立即清空解析缓存（含渲染卡片与已下载媒体）"""
        from .core.media_utils import clear_cache_dir

        try:
            cleaned = await clear_cache_dir(self.cache_dir)
        except Exception as e:
            yield event.plain_result(f"❌ 清空缓存失败: {str(e)[:120]}")
            return
        self._result_cache.clear()
        self._render_cache.clear()
        self._link_dedup.clear()
        self._confirm.invalidate_all("cache_cleared")
        yield event.plain_result(f"🧹 已清空 {cleaned} 个缓存文件")

    async def terminate(self):
        # 先取消确认协程并等它们退出，避免下载器 / 缓存目录关闭后仍在下载发送。
        controller = getattr(self, "_confirm_controller", None)
        if controller is not None:
            await controller.close()
        cookiecloud = getattr(self, '_cookiecloud_sync', None)
        if cookiecloud is not None:
            await cookiecloud.close()
        music_controller = getattr(self, "_music_controller", None)
        if music_controller is not None:
            await music_controller.close()
        if self._cache_cleanup_task is not None and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
        await self.downloader.aclose()
        if self._proxy_downloader is not None:
            await self._proxy_downloader.aclose()
            self._proxy_downloader = None

        for task in list(self._bili_login_tasks.values()):
            if not task.done():
                task.cancel()
        self._bili_login_tasks.clear()

        for task in list(self._delayed_cleanup_tasks):
            if not task.done():
                task.cancel()
        self._delayed_cleanup_tasks.clear()

        if self._bili_http_session and not self._bili_http_session.closed:
            await self._bili_http_session.close()
