"""解析记录持久化。

插件原本只把解析结果放在内存字典里（``_result_cache``），进程一重启就没了，
WebUI 也就无从「查看管理解析的缓存记录」。本模块把每条成功交付的解析结果落一条
记录到 ``<插件数据目录>/history.jsonl``，供 WebUI 的「缓存」页查询、筛选与删除。

设计取舍：
- **JSONL 而非 sqlite**：记录量小（默认上限 500 条），无需依赖与迁移成本；
  整文件重写在几十万字节级别，代价可忽略。
- **同步实现 + ``asyncio.to_thread`` 调用**：读写都在线程池里跑，
  不阻塞 AstrBot 事件循环（与 card_renderer 的做法一致）。
- **记录与实体文件解耦**：记录里只存文件名（相对 ``cache_dir``），
  文件被缓存清理任务删掉后记录仍在，读取时按文件是否存在展示「已清理」。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MAX_RECORDS = 500


@dataclass(slots=True)
class ParseRecord:
    """一条解析记录。"""

    id: str
    url: str
    platform: str
    platform_display: str
    content_type: str
    title: str
    author: str
    created_at: float
    via: str = "auto"
    card_file: str | None = None
    media_files: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int | None = None

    @classmethod
    def create(
        cls,
        *,
        url: str,
        platform: str,
        platform_display: str,
        content_type: str,
        title: str = "",
        author: str = "",
        via: str = "auto",
        card_file: str | None = None,
        media_files: Iterable[str] = (),
        detail: dict[str, Any] | None = None,
        elapsed_ms: int | None = None,
    ) -> "ParseRecord":
        return cls(
            id=uuid.uuid4().hex[:12],
            url=url,
            platform=platform,
            platform_display=platform_display,
            content_type=content_type,
            title=title,
            author=author,
            created_at=time.time(),
            via=via,
            card_file=card_file,
            media_files=list(media_files),
            detail=detail or {},
            elapsed_ms=elapsed_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "platform": self.platform,
            "platform_display": self.platform_display,
            "content_type": self.content_type,
            "title": self.title,
            "author": self.author,
            "created_at": self.created_at,
            "via": self.via,
            "card_file": self.card_file,
            "media_files": self.media_files,
            "media_count": len(self.media_files),
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParseRecord | None":
        try:
            return cls(
                id=str(data["id"]),
                url=str(data.get("url") or ""),
                platform=str(data.get("platform") or ""),
                platform_display=str(data.get("platform_display") or ""),
                content_type=str(data.get("content_type") or ""),
                title=str(data.get("title") or ""),
                author=str(data.get("author") or ""),
                created_at=float(data.get("created_at") or 0.0),
                via=str(data.get("via") or "auto"),
                card_file=data.get("card_file") or None,
                media_files=[str(x) for x in (data.get("media_files") or [])],
                detail=data.get("detail") if isinstance(data.get("detail"), dict) else {},
                elapsed_ms=data.get("elapsed_ms"),
            )
        except (KeyError, TypeError, ValueError):
            return None


class HistoryStore:
    """``history.jsonl`` 的读写门面，行序即写入序（旧 → 新）。

    所有公开方法都是同步的，调用方在异步上下文里应包一层 ``asyncio.to_thread``。
    """

    def __init__(self, path: Path, max_records: int = DEFAULT_MAX_RECORDS):
        self.path = path
        self.max_records = max(20, int(max_records))
        self._lock = threading.Lock()

    # ---------------- 读 ---------------- #

    def load(self) -> list[ParseRecord]:
        """读取全部记录（旧 → 新）。文件缺失或个别行损坏时跳过而不抛错。"""
        if not self.path.exists():
            return []
        records: list[ParseRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict) and (record := ParseRecord.from_dict(raw)):
                        records.append(record)
        except OSError:
            return []
        return records

    def query(
        self,
        *,
        keyword: str = "",
        platform: str = "",
        via: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ParseRecord], int]:
        """按关键词/平台/来源过滤并分页，返回 (当页记录(新→旧), 命中总数)。"""
        keyword = (keyword or "").strip().lower()
        platform = (platform or "").strip().lower()
        via = (via or "").strip().lower()

        matched: list[ParseRecord] = []
        for record in reversed(self.load()):
            if platform and record.platform != platform:
                continue
            if via and record.via != via:
                continue
            if keyword:
                haystack = " ".join(
                    (record.title, record.author, record.url, record.platform_display)
                ).lower()
                if keyword not in haystack:
                    continue
            matched.append(record)

        total = len(matched)
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        return matched[offset : offset + limit], total

    def get(self, record_id: str) -> ParseRecord | None:
        for record in self.load():
            if record.id == record_id:
                return record
        return None

    def stats(self) -> dict[str, Any]:
        """返回记录总数、平台分布与文件大小，供总览页展示。"""
        records = self.load()
        by_platform: dict[str, int] = {}
        for record in records:
            key = record.platform or "unknown"
            by_platform[key] = by_platform.get(key, 0) + 1
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "count": len(records),
            "max_records": self.max_records,
            "size_bytes": size,
            "by_platform": by_platform,
            "oldest_at": records[0].created_at if records else None,
            "newest_at": records[-1].created_at if records else None,
        }

    # ---------------- 写 ---------------- #

    def add(self, record: ParseRecord) -> bool:
        """追加一条记录，超出上限时丢弃最旧的若干条。

        Returns:
            是否**真的落盘**了。原先不检查 ``_write`` 的返回值，于是 Windows 上
            ``os.replace`` 撞到开着的文件（记录页开着时并发新增 → PermissionError
            WinError 5）会被静默吞掉 —— 用户少了一条记录却毫无察觉。
            ``delete`` / ``clear`` 一直是检查的，只有这里漏了。
        """
        with self._lock:
            records = self.load()
            records.append(record)
            overflow = len(records) - self.max_records
            if overflow > 0:
                records = records[overflow:]
            return self._write(records)

    def delete(self, record_ids: Iterable[str]) -> int:
        """按 id 删除记录，返回**实际落盘**的删除条数（写盘失败返回 0）。"""
        targets = {str(x) for x in record_ids}
        if not targets:
            return 0
        with self._lock:
            records = self.load()
            kept = [r for r in records if r.id not in targets]
            removed = len(records) - len(kept)
            if removed and not self._write(kept):
                # 没写成功就等于没删掉，不能报「删了 N 条」让前端显示成功
                return 0
            return removed

    def clear(self) -> int:
        """清空全部记录，返回**实际落盘**的删除条数（写盘失败返回 0）。"""
        with self._lock:
            records = self.load()
            count = len(records)
            if not self._write([]):
                return 0
            return count

    def _write(self, records: list[ParseRecord]) -> bool:
        """把记录整体落盘（先写临时文件再 os.replace）。

        Returns:
            是否写入成功。调用方必须据此判断删除是否真的生效。
        """
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            # mkdir 也要在 try 里：目录建不出来（父路径被同名文件占住、
            # 权限不足、磁盘满）同样属于「写失败」，不能让它直接抛出去
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fp:
                for record in records:
                    fp.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            from astrbot.api import logger as _logger
            _logger.warning(f"[denia_share] 解析记录写入失败: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True
