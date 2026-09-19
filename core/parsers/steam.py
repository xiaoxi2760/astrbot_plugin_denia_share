"""Steam 商店解析器

三档数据来源，逐级增强，任何一层失败都不影响其它层：

1. **Steam 官方 appdetails**（免 key）—— 名称、简介、开发商、发行日期、类型、
   国区价格（`price_overview`，按 `STEAM_REGION` 决定货币）
2. **CheapShark**（免 key）—— Metacritic 评分、Steam 好评率、当前折扣率
3. **IsThereAnyDeal**（**需 API key**）—— 真史低 `historyLow`（all / y1 / m3）

关于"历史价格"的实测结论（2026-09-19）：
- Steam 官方接口**不提供**任何历史价格
- SteamDB 的 `steamdb.info/api` 已被 Cloudflare 拦死（403），且社区明确禁止爬取
- CheapShark 的 `games?id=` 有个 `cheapestPriceEver` 字段，但实测三个游戏**恒为 None**，
  已废弃，不可依赖
- 因此要真史低只能用 ITAD，且**必须申请 key**（免费，isthereanydeal.com/apps）
  没填 key 时本模块照常工作，只是不显示史低

CheapShark 的一个坑：**必须带描述性 User-Agent**，用 httpx 默认 UA 或浏览器 UA
都会返回 400 `Missing or generic User-Agent header detected`。
"""

import re
from typing import Any, ClassVar

from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle, COMMON_TIMEOUT
from ..config import get_config
from ..data import Platform

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails/"
CHEAPSHARK_URL = "https://www.cheapshark.com/api/1.0"
ITAD_BASE = "https://api.isthereanydeal.com"

# CheapShark 要求描述性 UA
CHEAPSHARK_UA = "DeniaShare/0.5.0 (+https://github.com/xiaoxi2760/astrbot_plugin_denia_share)"

_STEAM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# 简介里的 HTML 要去掉
_TAG_RE = re.compile(r"<[^>]+>")


class SteamParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name=PlatformEnum.STEAM, display_name="Steam")

    def __init__(self, downloader, itad_key: str = "", region: str = "cn"):
        super().__init__(downloader)
        self.itad_key = (itad_key or "").strip()
        self.region = (region or "cn").strip().lower()

    # ────────────── 入口 ────────────── #

    @handle("steampowered.com", r"store\.steampowered\.com/(?:app|sub|bundle)/(?P<appid>\d+)")
    async def _parse(self, searched: re.Match[str]):
        appid = searched.group("appid")
        canonical = f"https://store.steampowered.com/app/{appid}/"

        game = await self._fetch_appdetails(appid)
        name = str(game.get("name") or "").strip()
        if not name:
            raise ParseException("Steam API 未返回游戏名称")

        devs = [str(d) for d in (game.get("developers") or []) if d]
        pubs = [str(p) for p in (game.get("publishers") or []) if p]
        genres = [str(g.get("description")) for g in (game.get("genres") or [])
                  if isinstance(g, dict) and g.get("description")]

        # 价格信息（官方 + CheapShark + ITAD 三层拼装）
        price_lines = self._format_price(game)
        cs = await self._fetch_cheapshark(appid)
        if cs:
            price_lines.extend(self._cheapshark_lines(cs))
        elif not price_lines:
            price_lines = []

        itad_lines = await self._fetch_itad_low(appid)
        price_lines.extend(itad_lines)

        lines: list[str] = []
        if genres:
            lines.append("类型: " + "、".join(genres[:5]))
        release = self._format_release(game.get("release_date"))
        if release:
            lines.append(f"发行: {release}")
        if pubs:
            lines.append("发行商: " + "、".join(pubs[:3]))
        platforms = game.get("platforms") or {}
        if isinstance(platforms, dict):
            plats = [k for k, v in platforms.items() if v and k in ("windows", "mac", "linux")]
            if plats:
                lines.append("平台: " + "/".join(
                    {"windows": "Win", "mac": "Mac", "linux": "Linux"}[p] for p in plats
                ))

        short_desc = self._clean_text(
            game.get("short_description") or game.get("about_the_game") or ""
        )

        result = self.result(
            title=name,
            text=short_desc or None,
            author=self.create_author("、".join(devs) if devs else "Steam", None),
            url=canonical,
            extra={"content_type": "游戏"},
        )
        if lines or price_lines:
            result.extra["info"] = "\n".join(lines + ([""] if lines else []) + price_lines)

        # 封面 + 截图
        cover = game.get("header_image")
        shots: list[str] = []
        for shot in (game.get("screenshots") or [])[:3]:
            if isinstance(shot, dict):
                u = shot.get("path_full") or shot.get("path_thumbnail")
                if u:
                    shots.append(str(u))
        if cover:
            result.contents.append(self.create_image(str(cover)))
        if shots:
            result.contents.extend(self.create_images(shots))
        return result

    # ────────────── Steam 官方 ────────────── #

    async def _fetch_appdetails(self, appid: str) -> dict[str, Any]:
        params = {"appids": appid, "l": "schinese", "cc": self.region}
        async with self.new_client() as client:
            resp = await client.get(
                APPDETAILS_URL, params=params,
                headers={"User-Agent": _STEAM_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=COMMON_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()

        if not isinstance(payload, dict):
            raise ParseException("Steam API 返回格式异常")
        entry = payload.get(appid)
        if not isinstance(entry, dict) or entry.get("success") is not True:
            raise ParseException(f"Steam 上找不到 app {appid}（可能是区域限制或未发行）")
        game = entry.get("data")
        if not isinstance(game, dict):
            raise ParseException("Steam API 未返回游戏详情")
        return game

    # ────────────── CheapShark ────────────── #

    async def _fetch_cheapshark(self, appid: str) -> dict[str, Any] | None:
        """拿 Metacritic / 好评率 / 折扣率。失败返回 None（不影响主流程）。"""
        try:
            async with self.new_client() as client:
                resp = await client.get(
                    f"{CHEAPSHARK_URL}/deals",
                    params={"steamAppID": appid},
                    headers={"User-Agent": CHEAPSHARK_UA, "Accept": "application/json"},
                    timeout=COMMON_TIMEOUT,
                )
                if resp.status_code != 200:
                    logger.debug(f"[steam] CheapShark 返回 {resp.status_code}")
                    return None
                arr = resp.json()
            return arr[0] if isinstance(arr, list) and arr else None
        except Exception as e:
            logger.debug(f"[steam] CheapShark 不可用: {type(e).__name__}: {str(e)[:100]}")
            return None

    @staticmethod
    def _cheapshark_lines(cs: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        try:
            savings = float(cs.get("savings") or 0)
        except (TypeError, ValueError):
            savings = 0.0
        if savings >= 1:
            # CheapShark 固定返回美元价，与上面的国区价格不同源，必须标清楚
            lines.append(
                f"当前折扣: -{savings:.0f}%"
                f"（美元区 ${cs.get('salePrice')} / 原价 ${cs.get('normalPrice')}）"
            )
        # CheapShark 对没有 Metacritic 的游戏返回 "0"，不该当成 0 分显示出来
        mc = str(cs.get("metacriticScore") or "").strip()
        if mc and mc != "0":
            lines.append(f"Metacritic: {mc}")
        rating = cs.get("steamRatingText")
        if rating:
            percent = cs.get("steamRatingPercent")
            lines.append(f"Steam 评价: {rating}" + (f" ({percent}%)" if percent else ""))
        return lines

    # ────────────── IsThereAnyDeal 史低 ────────────── #

    async def _fetch_itad_low(self, appid: str) -> list[str]:
        """拿真史低。没配 key 直接返回空列表（静默跳过）。"""
        if not self.itad_key:
            return []
        try:
            async with self.new_client() as client:
                lookup = await client.get(
                    f"{ITAD_BASE}/games/lookup/v1",
                    params={"key": self.itad_key, "appid": appid},
                    timeout=COMMON_TIMEOUT,
                )
                if lookup.status_code != 200:
                    logger.debug(f"[steam] ITAD lookup {lookup.status_code}")
                    return []
                game_id = ((lookup.json().get("game") or {}).get("id"))
                if not game_id:
                    return []

                prices = await client.post(
                    f"{ITAD_BASE}/games/prices/v3",
                    params={"key": self.itad_key, "country": self.region.upper()},
                    json=[game_id],
                    timeout=COMMON_TIMEOUT,
                )
                if prices.status_code != 200:
                    logger.debug(f"[steam] ITAD prices {prices.status_code}")
                    return []
                data = prices.json()

            entries = data if isinstance(data, list) else []
            if not entries:
                return []
            low = (entries[0] or {}).get("historyLow") or {}
            if not low:
                return []
            parts: list[str] = []
            for label, key in (("史低", "all"), ("近一年低", "y1"), ("近三月低", "m3")):
                node = low.get(key)
                if isinstance(node, dict) and node.get("amount") is not None:
                    parts.append(f"{label} {node['amount']} {node.get('currency', '')}".strip())
            return [f"ITAD: {' · '.join(parts)}"] if parts else []
        except Exception as e:
            logger.debug(f"[steam] ITAD 不可用: {type(e).__name__}: {str(e)[:100]}")
            return []

    # ────────────── 工具 ────────────── #

    @staticmethod
    def _format_price(game: dict[str, Any]) -> list[str]:
        if game.get("is_free"):
            return ["价格: 免费"]
        overview = game.get("price_overview")
        if not isinstance(overview, dict):
            return []
        initial = str(overview.get("initial_formatted") or "").strip()
        final = str(overview.get("final_formatted") or "").strip()
        if not final and not initial:
            return []
        discount = overview.get("discount_percent")
        if initial and final and initial != final:
            base = f"价格: {final}（原价 {initial}"
            if discount:
                base += f"，-{discount}%"
            return [base + "）"]
        return [f"价格: {final or initial}"]

    @staticmethod
    def _format_release(value: Any) -> str:
        if isinstance(value, dict):
            date = value.get("date")
            if value.get("coming_soon"):
                return f"即将发行（{date}）" if date else "即将发行"
            return str(date or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        import html as html_lib

        return html_lib.unescape(_TAG_RE.sub("", text or "")).strip()
