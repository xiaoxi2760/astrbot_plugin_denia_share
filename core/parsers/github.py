"""GitHub 仓库解析器

使用官方 REST API（api.github.com）。
免 key 限额 60 次/小时/**出口 IP**，共享 IP（常见代理/容器 NAT）极易被打满，
因此强烈建议配置 GITHUB_TOKEN（5000 次/小时，按账号计）。
"""

import re
from typing import Any, ClassVar

from httpx import AsyncClient
from astrbot.api import logger

from ..base_parser import BaseParser, PlatformEnum, ParseException, handle
from ..config import get_config
from ..data import Platform, platform_of
from ... import __version__

API_BASE = "https://api.github.com"

# 描述性 UA：GitHub 要求，且便于官方联系。
# 版本号取自包级 ``__version__``，别再手写 —— 原先这里与 steam.py 各手写了一份，
# 插件已经 0.6.x 而它们还停在 0.5.0。
USER_AGENT = (
    f"DeniaShare/{__version__} "
    "(+https://github.com/xiaoxi2760/astrbot_plugin_denia_share)"
)


def _fmt_count(n: int | None) -> str:
    """1.2k 风格计数。"""
    if not n:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


class GitHubParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.GITHUB)

    def __init__(self, downloader, token: str = ""):
        super().__init__(downloader)
        self.token = (token or "").strip()

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @handle("github.com", r"github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)")
    async def _parse(self, searched: re.Match[str]):
        owner = searched.group("owner")
        repo = searched.group("repo")
        # 排除 github.com 自身的非仓库路径
        if owner.lower() in {"about", "pricing", "sponsors", "features", "enterprise",
                             "security", "login", "join", "topics", "collections",
                             "events", "marketplace", "explore", "trending", "settings",
                             "notifications", "new", "search", "orgs", "apps"}:
            raise ParseException(f"不是仓库地址: {owner}/{repo}")

        repo = repo.removesuffix(".git")
        api_url = f"{API_BASE}/repos/{owner}/{repo}"
        headers = self._api_headers()

        async with self.new_client() as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code == 404:
                raise ParseException(f"仓库不存在: {owner}/{repo}")
            if resp.status_code == 403:
                raise ParseException(
                    "GitHub API 限额已满，请在插件配置里填写 GITHUB_TOKEN"
                )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        if data.get("full_name") is None:
            raise ParseException("GitHub API 返回异常")

        name = data.get("name") or repo
        full_name = data.get("full_name") or f"{owner}/{repo}"
        description = (data.get("description") or "").strip()

        # 主页 / README 摘要
        homepage = (data.get("homepage") or "").strip()
        topics = data.get("topics") or []

        stats = [
            f"⭐ {_fmt_count(data.get('stargazers_count'))}",
            f"🍴 {_fmt_count(data.get('forks_count'))}",
            f"⚠️ {_fmt_count(data.get('open_issues_count'))}",
        ]
        language = data.get("language")
        if language:
            stats.append(f"语言 {language}")
        license_info = data.get("license") or {}
        if isinstance(license_info, dict) and license_info.get("spdx_id") not in (None, "NOASSERTION"):
            stats.append(f"许可 {license_info['spdx_id']}")

        lines = [" · ".join(stats)]
        if topics:
            lines.append("标签: " + "、".join(topics[:8]))
        pushed = (data.get("pushed_at") or "")[:10]
        if pushed:
            lines.append(f"最近提交: {pushed}")
        if homepage:
            lines.append(f"主页: {homepage}")

        author = self.create_author(
            (data.get("owner") or {}).get("login") or owner,
            (data.get("owner") or {}).get("avatar_url"),
        )

        result = self.result(
            title=full_name,
            text=description or None,
            author=author,
            url=data.get("html_url") or f"https://github.com/{owner}/{repo}",
            extra={
                "content_type": "仓库",
                "stats_line": "\n".join(lines),
            },
        )
        # 仓库没有封面图，用 Social Preview 图（GitHub 自动生成，稳定可用）
        result.contents.append(self.create_image(
            f"https://opengraph.githubassets.com/1/{full_name}"
        ))
        return result
