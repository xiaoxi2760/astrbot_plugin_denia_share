"""通过本地代理探测各平台的公开接口，收集**当前真实可用**的 ID / URL。

为什么不用手写 URL：这些平台的内容随时被删、被改成私有、被风控。凭印象编出来的
链接大概率是死的，而「死链」在冒烟测试里表现为「解析失败」，会把「用例失效」和
「解析器坏了」混成同一件事。公开接口拿到的 ID 至少此刻是活的。

用法：py -3 tests/smoke/discover_urls.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

PROXY = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7897"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}


def get(url: str, headers: dict | None = None, timeout: float = 20.0):
    with httpx.Client(
        proxy=PROXY, timeout=timeout, follow_redirects=True,
        headers={**HEADERS, **(headers or {})},
    ) as c:
        return c.get(url)


def section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def bilibili() -> None:
    section("B站")
    # 1) 热门视频
    try:
        r = get("https://api.bilibili.com/x/web-interface/popular?ps=5&pn=1")
        items = r.json().get("data", {}).get("list", [])
        for it in items[:3]:
            print(f"  视频  https://www.bilibili.com/video/{it['bvid']}   {it['title'][:30]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  热门失败: {type(exc).__name__} {str(exc)[:80]}")

    # 2) 动态图文
    try:
        r = get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all?type=video&features=itemOpusStyle")
        data = r.json()
        items = (data.get("data") or {}).get("items") or []
        for it in items:
            basic = it.get("basic") or {}
            if basic.get("comment_type") == 11 or "opus" in str(basic.get("comment_type_str", "")):
                dyn = basic.get("rid_str") or basic.get("rid")
                if dyn:
                    print(f"  动态  https://t.bilibili.com/{dyn}")
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"  动态失败: {type(exc).__name__} {str(exc)[:80]}")

    # 3) 专栏文章
    try:
        r = get("https://api.bilibili.com/x/article/recommends?cid=0&pn=1&ps=5")
        for it in r.json().get("data", {}).get("list", [])[:3]:
            print(f"  专栏  https://www.bilibili.com/read/cv{it['id']}   {it['title'][:30]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  专栏失败: {type(exc).__name__} {str(exc)[:80]}")


def github() -> None:
    section("GitHub")
    try:
        r = get("https://api.github.com/repos/python/cpython")
        d = r.json()
        print(f"  仓库  https://github.com/python/cpython   stars={d.get('stargazers_count')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  仓库失败: {exc}")
    try:
        r = get("https://api.github.com/repos/python/cpython/releases?per_page=3")
        for rel in r.json()[:2]:
            print(f"  Release https://github.com/python/cpython/releases/tag/{rel['tag_name']}  {rel['name'][:30]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Release 失败: {exc}")
    try:
        r = get("https://api.github.com/repos/python/cpython/issues?state=open&per_page=2")
        for it in r.json()[:2]:
            if "pull_request" in it:
                continue
            print(f"  Issue  https://github.com/python/cpython/issues/{it['number']}  {it['title'][:40]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Issue 失败: {exc}")


def steam() -> None:
    section("Steam")
    for label, url in (
        ("游戏", "https://store.steampowered.com/api/appdetails?appids=730&l=schinese"),
        ("DLC ", "https://store.steampowered.com/api/appdetails?appids=400&l=schinese"),
        ("合集", "https://store.steampowered.com/api/appdetails?appids=1145360&l=schinese"),
    ):
        try:
            d = get(url).json()
            aid = list(d.keys())[0]
            name = d[aid].get("data", {}).get("name", "?")
            print(f"  {label}  https://store.steampowered.com/app/{aid}/   {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label} 失败: {type(exc).__name__} {str(exc)[:70]}")


def pixiv() -> None:
    section("Pixiv（无 Cookie 探测）")
    # 公开接口通常要登录，先确认能不能裸取
    for pid in ("97448759", "87070841", "115779535"):
        try:
            r = get(f"https://www.pixiv.net/ajax/illust/{pid}?lang=zh")
            ok = r.status_code == 200 and '"error":false' in r.text.replace(" ", "")
            print(f"  作品 {pid}: HTTP {r.status_code} 可解析={ok}")
        except Exception as exc:  # noqa: BLE001
            print(f"  作品 {pid} 失败: {type(exc).__name__} {str(exc)[:60]}")


def weibo() -> None:
    section("微博")
    for wid in ("P9M8meR0O", "5054181788092183", "4977266192171722"):
        try:
            r = get(f"https://m.weibo.cn/statuses/show?id={wid}")
            ok = r.status_code == 200
            print(f"  {wid}: HTTP {r.status_code}  {ok}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {wid} 失败: {type(exc).__name__} {str(exc)[:60]}")


if __name__ == "__main__":
    print(f"代理: {PROXY}")
    for fn in (bilibili, github, steam, pixiv, weibo):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {fn.__name__} 整段失败: {type(exc).__name__}: {exc}")
