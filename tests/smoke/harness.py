"""冒烟测试脚手架：脱离 AstrBot 单跑真实解析器。

插件的解析链路对宿主的依赖很浅，这是能离线冒烟的关键：

- ``StreamDownloader(cache_dir, proxies, max_size_mb, verify_ssl)`` —— 只要一个目录
- ``Parser(downloader, ...)`` —— 全部构造参数都有默认值
- ``Parser.search_url(url)`` —— 纯函数，类方法，不碰网络
- ``Parser.parse(keyword, match)`` —— 真正的解析
- ``Parser.parse_with_redirect(url)`` —— 短链先重定向再走上面那条

派发逻辑照抄 ``main.py`` 的 ``_match_parser``：按注册顺序遍历，第一个
``search_url`` 不抛异常的胜出。**照抄而不是重写**是故意的 —— 一旦这里和
main.py 的遍历顺序分叉，冒烟通过而线上派错解析器，测试就白做了。

代理：解析器内部用 httpx 且 ``trust_env=True``，所以只要设好
``HTTPS_PROXY`` 环境变量就能整体走本地代理，不必改插件代码。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from tests._stubs import install_host_stubs  # noqa: E402

_STUBS = install_host_stubs()

# 解析器内部的 logger 走 astrbot，测试里全是 warning 会淹掉结果
logging.getLogger("astrbot.stub").setLevel(logging.CRITICAL)

from astrbot_plugin_denia_share.core.download import StreamDownloader  # noqa: E402
from astrbot_plugin_denia_share.core.exception import (  # noqa: E402
    DownloadException,
    IgnoreException,
    ParseException,
    SilentException,
)
from astrbot_plugin_denia_share.core.parsers import (  # noqa: E402
    AcfunParser,
    BilibiliParser,
    DouyinParser,
    GitHubParser,
    KuaiShouParser,
    NGAParser,
    PixivParser,
    SteamParser,
    TwitterParser,
    WeiBoParser,
    XiaoHongShuParser,
)

# 与 main.py 的 _init_parsers 同序；改这里必须同步改那边
PARSER_ORDER: list[str] = [
    "bilibili",
    "douyin",
    "kuaishou",
    "weibo",
    "xiaohongshu",
    "twitter",
    "nga",
    "acfun",
    "github",
    "pixiv",
    "steam",
]


def build_downloader(cache_dir: Path, proxy: str | None) -> StreamDownloader:
    return StreamDownloader(
        cache_dir=cache_dir,
        proxies=proxy,
        # 冒烟只验「能不能解析出结构」，不该真下几 GB 视频。
        # 5MB 足够覆盖封面/首图，又能把失控下载掐住。
        max_size_mb=5,
        verify_ssl=False,
    )


def build_parsers(downloader: StreamDownloader) -> dict[str, object]:
    """按 main.py 的同序与同参构造 11 个解析器。"""
    from astrbot_plugin_denia_share.core.config import get_config, init_config

    # 平时由 main.py 在 initialize() 里做；离线跑没有宿主，得自己补上，
    # 否则 get_config() 抛 "ParserConfig not initialized yet"。
    try:
        get_config()
    except RuntimeError:
        init_config({}, PLUGIN_DIR, PLUGIN_DIR)

    cfg = get_config()
    return {
        "bilibili": BilibiliParser(
            downloader, bili_ck=cfg.BILI_CK, config_dir=PLUGIN_DIR
        ),
        "douyin": DouyinParser(downloader),
        "kuaishou": KuaiShouParser(downloader),
        "weibo": WeiBoParser(downloader),
        "xiaohongshu": XiaoHongShuParser(downloader, xhs_ck=cfg.XHS_CK),
        "twitter": TwitterParser(downloader),
        "nga": NGAParser(downloader),
        "acfun": AcfunParser(downloader),
        "github": GitHubParser(downloader, token=cfg.GITHUB_TOKEN),
        "pixiv": PixivParser(downloader, cookie=cfg.PIXIV_CK),
        "steam": SteamParser(
            downloader, itad_key=cfg.ITAD_API_KEY, region=cfg.STEAM_REGION
        ),
    }


def match_parser(parsers: dict[str, object], url: str) -> str | None:
    """复刻 main.py::_match_parser。"""
    for name, parser in parsers.items():
        try:
            parser.search_url(url)
            return name
        except Exception:  # noqa: BLE001 —— 语义与 main.py 一致
            continue
    return None


@dataclass
class Verdict:
    """一条用例的结果。"""

    platform: str
    kind: str
    url: str
    ok: bool = False
    stage: str = ""
    reason: str = ""
    matched_parser: str | None = None
    keyword: str = ""
    title: str = ""
    author: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"[{self.stage}] {self.reason}"
        bits = [f"{k}={v}" for k, v in self.counts.items() if v]
        tail = " ".join(bits)
        return f"{self.title[:40] or '(无标题)'}  {tail}".rstrip()


async def run_case(case, parsers: dict[str, object], timeout: float) -> Verdict:
    """跑一条用例。分四段，失败时明确卡在哪一段。"""
    import time

    v = Verdict(platform=case.platform, kind=case.kind, url=case.url)
    t0 = time.perf_counter()
    try:
        # ---- 1. 派发 ----
        v.matched_parser = match_parser(parsers, case.url)
        if v.matched_parser != case.platform:
            v.stage = "派发"
            v.reason = f"派到了 {v.matched_parser or '无解析器'}，期望 {case.platform}"
            return v

        parser = parsers[case.platform]
        keyword, searched = parser.search_url(case.url)

        # ---- 2. 解析 ----
        result = await asyncio.wait_for(
            parser.parse(keyword, searched) if not case.follow_redirect
            else parser.parse_with_redirect(case.url),
            timeout=timeout,
        )
        v.keyword = keyword

        # ---- 3. 结构 ----
        # 媒体计数走 ParseResult 的 img/video/audio_contents 属性，
        # 不是 fields —— contents 混装三种类型，要按类型分。
        v.title = (result.title or "").strip()
        v.author = (result.author.name if result.author else "") or ""
        v.counts = {
            "图": len(result.img_contents),
            "视频": len(result.video_contents),
            "音频": len(result.audio_contents),
        }
        v.elapsed = time.perf_counter() - t0

        for attr in case.expect_fields:
            if not getattr(result, attr, None):
                v.stage = "结构"
                v.reason = f"缺字段 {attr}"
                return v
        if case.expect_media and not any(v.counts.values()):
            v.stage = "结构"
            v.reason = "没有解析出任何图片/视频/音频"
            return v

        v.ok = True
        v.stage = "通过"
        return v

    except SilentException as exc:
        v.stage, v.reason = "派发", f"无法匹配: {exc}"
    except IgnoreException as exc:
        # 策略跳过：解析器主动放弃（如大会员/付费/地区限制），不是 bug
        v.stage, v.reason = "策略跳过", str(exc)
    except ParseException as exc:
        v.stage, v.reason = "解析", f"ParseException: {exc}"
    except DownloadException as exc:
        v.stage, v.reason = "下载", f"DownloadException: {exc}"
    except asyncio.TimeoutError:
        v.stage, v.reason = "超时", f"> {timeout}s"
    except Exception as exc:  # noqa: BLE001
        v.stage, v.reason = "异常", f"{type(exc).__name__}: {exc}"
    finally:
        v.elapsed = time.perf_counter() - t0
    return v


async def run_all(cases, proxy: str | None, timeout: float) -> list[Verdict]:
    tmp = Path(tempfile.mkdtemp(prefix="denia_smoke_"))
    os.environ.setdefault("HTTPS_PROXY", proxy or "")
    os.environ.setdefault("HTTP_PROXY", proxy or "")
    downloader = build_downloader(tmp, proxy)
    parsers = build_parsers(downloader)
    out = []
    for case in cases:
        out.append(await run_case(case, parsers, timeout))
    return out
