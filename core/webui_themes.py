"""Safe installation and recovery of optional Denia WebUI theme packages.

Theme archives are data packages only. A package contains a root ``theme.json``
manifest and a ``webui/`` tree; only that tree is copied into AstrBot's plugin
page directory. No package-provided code is executed by this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import struct
import threading
import unicodedata
import uuid
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import http_env  # noqa: F401 -- normalize environment proxy settings

PLUGIN_ID = "astrbot_plugin_denia_share"
CARD_STYLES = frozenset({"classic", "denia", "lily"})
MANIFEST_FILE = "theme.json"
WEBUI_DIR = "webui"
THEME_STORE_NAME = "webui_themes"
STORE_MARKER = ".managed-theme.json"
MOUNT_MARKER = ".denia-webui-theme.json"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_FILES = 512
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = MAX_UNCOMPRESSED_BYTES
MAX_EXPANSION_RATIO = 200
DOWNLOAD_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 5
CHUNK_BYTES = 64 * 1024

ALLOWED_EXTENSIONS = frozenset(
    {
        ".html",
        ".css",
        ".js",
        ".json",
        ".svg",
        ".png",
        ".webp",
        ".jpg",
        ".jpeg",
        ".woff",
        ".woff2",
        ".ttf",
        ".txt",
        ".md",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "name",
        "version",
        "target_plugin",
        "webui_api",
        "mount",
        "template",
        "entry",
    }
)
_SAFE_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_RESERVED_MOUNTS = frozenset(
    {"classic", "shared-resources", "shared_resources", "shared-resources.js"}
)


class WebUIThemeError(ValueError):
    """A safe, user-displayable theme package or installation error."""


class WebUIThemeManager:
    """Install optional themes under the plugin's native static page directory.

    ``data_dir/webui_themes/<id>`` is the persistent authority. The page mount
    under ``plugin_dir/pages/denia/<mount>`` is a replaceable runtime copy. Both
    roots carry ownership markers so recovery never adopts or overwrites a
    directory that this manager did not create.
    """

    def __init__(self, plugin_dir: Path, data_dir: Path) -> None:
        self.plugin_dir = Path(plugin_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self._store_root = self.data_dir / THEME_STORE_NAME
        self._mount_parent = self.plugin_dir / "pages" / "denia"
        self._lock = threading.RLock()

    def list_installed(self) -> list[dict[str, Any]]:
        """List valid persistent packages and whether their page mount exists."""
        with self._lock:
            if not self._store_root.exists():
                return []
            self._assert_directory_tree(self._store_root, self.data_dir)
            rows: list[dict[str, Any]] = []
            for directory in sorted(self._store_root.iterdir(), key=lambda item: item.name):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    manifest, _marker = self._load_authoritative(directory)
                except WebUIThemeError:
                    continue
                row = self._metadata(manifest)
                row["installed"] = self._mount_is_valid(manifest)
                rows.append(row)
            return rows

    def status(self, theme_id: str) -> dict[str, Any] | None:
        """Return metadata for a persisted theme, or ``None`` if it is absent."""
        if not isinstance(theme_id, str) or not _SAFE_ID.fullmatch(theme_id):
            return None
        with self._lock:
            directory = self._store_root / theme_id
            if not directory.exists():
                return None
            try:
                manifest, _marker = self._load_authoritative(directory)
            except WebUIThemeError:
                return None
            row = self._metadata(manifest)
            row["installed"] = self._mount_is_valid(manifest)
            return row

    def card_assets_path(self, theme_id: str = "denia") -> Path | None:
        """Return a managed theme's persistent card assets directory.

        Only the package copy under ``data_dir/webui_themes`` is considered, so
        upgrading or removing the replaceable page mount cannot break existing
        cards. Returns ``None`` when the theme or asset directory is absent.
        """
        if not isinstance(theme_id, str) or not _SAFE_ID.fullmatch(theme_id):
            return None
        with self._lock:
            directory = self._store_root / theme_id
            if not directory.exists():
                return None
            try:
                self._load_authoritative(directory)
                assets = directory / WEBUI_DIR / "assets" / "cards" / theme_id
                if not assets.exists():
                    return None
                self._assert_directory_tree(assets, directory)
                if assets.is_symlink() or not assets.is_dir():
                    return None
                return assets
            except (OSError, WebUIThemeError):
                return None

    def install_archive(
        self, payload: bytes, expected_sha256: str = ""
    ) -> dict[str, Any]:
        """Validate, persist, and atomically mount a theme ZIP archive."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise WebUIThemeError("主题包内容必须是 ZIP 字节数据")
        payload = bytes(payload)
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise WebUIThemeError("主题包超过压缩文件大小限制")

        if not isinstance(expected_sha256, str):
            raise WebUIThemeError("SHA-256 格式无效")
        expected = expected_sha256.strip()
        if expected and not _SHA256.fullmatch(expected):
            raise WebUIThemeError("SHA-256 格式无效")
        archive_hash = hashlib.sha256(payload).hexdigest()
        if expected and not hmac.compare_digest(archive_hash, expected.lower()):
            raise WebUIThemeError("主题包 SHA-256 校验失败")

        with self._lock:
            self._ensure_directory(self.data_dir, self.data_dir)
            self._ensure_directory(self._store_root, self.data_dir)
            self._ensure_directory(self._mount_parent, self.plugin_dir)
            transaction_id = uuid.uuid4().hex
            store_stage = self._store_root / f".staging-{transaction_id}"
            self._mkdir_owned_stage(store_stage, self._store_root)
            try:
                manifest = self._extract_validated_archive(payload, store_stage)
                store_target = self._store_root / manifest["id"]
                if store_target.exists():
                    previous, _marker = self._load_authoritative(store_target)
                    if previous["mount"] not in {manifest["mount"], f"theme-{manifest['id']}"}:
                        raise WebUIThemeError("已安装主题的挂载目录不能在升级时变更")
                    manifest["mount"] = previous["mount"]
                else:
                    # Preserve the original Bubble Workshop path and its legacy
                    # adoption rules, unless another old theme already owns it.
                    studio = self._mount_parent / "studio"
                    marker = studio / MOUNT_MARKER
                    keep_studio = manifest["id"] == "denia" and manifest["mount"] == "studio" and (
                        not (marker.exists() or marker.is_symlink())
                        or self._mount_is_managed_by(studio, "denia", "studio")
                    )
                    if not keep_studio:
                        manifest["mount"] = f"theme-{manifest['id']}"
                self._write_json(
                    store_stage / STORE_MARKER,
                    self._marker(manifest, archive_hash, transaction_id),
                )
                mount_stage = self._mount_parent / (
                    f".{manifest['mount']}.stage-{transaction_id}"
                )
                self._create_mount_stage(store_stage, mount_stage, manifest, archive_hash, transaction_id)
                try:
                    self._commit_stages(
                        store_stage,
                        mount_stage,
                        manifest,
                        transaction_id,
                        archive_hash,
                    )
                finally:
                    self._remove_stage_if_present(mount_stage, self._mount_parent)
                store_stage = None  # the store stage became the authoritative package
                return {**self._metadata(manifest), "installed": True}
            except WebUIThemeError:
                raise
            except Exception:
                raise WebUIThemeError("主题安装失败，原有主题文件已保留") from None
            finally:
                if store_stage is not None:
                    self._remove_stage_if_present(store_stage, self._store_root)

    async def install_url(
        self, url: str, expected_sha256: str = ""
    ) -> dict[str, Any]:
        """Download an administrator-supplied HTTPS archive and install it."""
        self._validate_url(url)
        payload = await self._download_archive(url)
        return await asyncio.to_thread(
            self.install_archive, payload, expected_sha256=expected_sha256
        )

    def restore_installed(self) -> list[dict[str, Any]]:
        """Restore missing runtime mounts from local persistent copies only."""
        with self._lock:
            if not self._store_root.exists():
                return []
            self._assert_directory_tree(self._store_root, self.data_dir)
            self._ensure_directory(self._mount_parent, self.plugin_dir)
            restored: list[dict[str, Any]] = []
            for directory in sorted(self._store_root.iterdir(), key=lambda item: item.name):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    manifest, marker = self._load_authoritative(directory)
                except WebUIThemeError:
                    continue

                mount = self._mount_parent / manifest["mount"]
                if mount.is_symlink():
                    continue
                if mount.exists():
                    # Existing files are left alone, even if they are not ours.
                    if not self._mount_is_valid(manifest):
                        continue
                    restored.append({**self._metadata(manifest), "installed": True})
                    continue

                transaction_id = uuid.uuid4().hex
                mount_stage = self._mount_parent / (
                    f".{manifest['mount']}.stage-{transaction_id}"
                )
                try:
                    self._create_mount_stage(
                        directory,
                        mount_stage,
                        manifest,
                        str(marker.get("archive_sha256", "")),
                        transaction_id,
                    )
                    self._move_new_directory(mount_stage, mount, self._mount_parent)
                    restored.append({**self._metadata(manifest), "installed": True})
                except (OSError, WebUIThemeError):
                    continue
                finally:
                    self._remove_stage_if_present(mount_stage, self._mount_parent)
            return restored

    async def _download_archive(self, url: str) -> bytes:
        current = url
        seen: set[str] = set()
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers={"Accept": "application/zip, application/octet-stream;q=0.9"},
            ) as client:
                for redirect_count in range(MAX_REDIRECTS + 1):
                    self._validate_url(current)
                    if current in seen:
                        raise WebUIThemeError("主题包下载重定向循环")
                    seen.add(current)
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect_count >= MAX_REDIRECTS:
                                raise WebUIThemeError("主题包下载重定向无效")
                            next_url = urljoin(current, location)
                            self._validate_url(next_url)
                            current = next_url
                            continue
                        if response.status_code != 200:
                            raise WebUIThemeError("主题包下载失败")
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > MAX_ARCHIVE_BYTES:
                                    raise WebUIThemeError("主题包超过压缩文件大小限制")
                            except ValueError:
                                raise WebUIThemeError("主题包下载响应无效") from None
                        chunks: list[bytes] = []
                        downloaded = 0
                        async for chunk in response.aiter_bytes(CHUNK_BYTES):
                            downloaded += len(chunk)
                            if downloaded > MAX_ARCHIVE_BYTES:
                                raise WebUIThemeError("主题包超过压缩文件大小限制")
                            chunks.append(chunk)
                        return b"".join(chunks)
        except WebUIThemeError:
            raise
        except Exception:
            raise WebUIThemeError("主题包下载失败") from None
        raise WebUIThemeError("主题包下载失败")

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str) or not url or len(url) > 4096:
            raise WebUIThemeError("主题包 URL 无效")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise WebUIThemeError("主题包 URL 无效") from None
        if (
            parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(char.isspace() for char in url)
        ):
            raise WebUIThemeError("只允许无凭据、无片段的 HTTPS 主题包 URL")
        if port is not None and not (1 <= port <= 65535):
            raise WebUIThemeError("主题包 URL 端口无效")

        normalized_host = hostname.rstrip(".").lower()
        if (
            normalized_host == "localhost"
            or normalized_host.endswith((".localhost", ".local", ".internal", ".lan"))
        ):
            raise WebUIThemeError("主题包 URL 主机无效")
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise WebUIThemeError("主题包 URL 主机无效")

    def _extract_validated_archive(self, payload: bytes, stage: Path) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(BytesIO(payload), mode="r")
        except (zipfile.BadZipFile, OSError, ValueError):
            raise WebUIThemeError("主题包不是有效的 ZIP 文件") from None

        with archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_FILES:
                raise WebUIThemeError("主题包文件数量无效")
            raw_names = self._raw_zip_member_names(payload)
            if len(raw_names) != len(infos):
                raise WebUIThemeError("主题包目录结构无效")
            targets: dict[str, tuple[str, bool]] = {}
            advertised_total = 0
            normalized_info: list[tuple[zipfile.ZipInfo, str, bool]] = []

            for info, raw_name in zip(infos, raw_names):
                name, is_dir = self._normalize_archive_name(raw_name, info)
                key = name.casefold()
                if key in targets:
                    raise WebUIThemeError("主题包包含重复路径")
                targets[key] = (name, is_dir)
                normalized_info.append((info, name, is_dir))
                if info.flag_bits & 0x1:
                    raise WebUIThemeError("不接受加密的主题包条目")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if file_type == stat.S_IFLNK:
                    raise WebUIThemeError("不接受符号链接主题文件")
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise WebUIThemeError("主题包包含不支持的文件类型")
                if is_dir:
                    if info.file_size != 0:
                        raise WebUIThemeError("主题包目录条目无效")
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    raise WebUIThemeError("主题包文件大小无效")
                if info.file_size > MAX_FILE_BYTES:
                    raise WebUIThemeError("主题包单个文件过大")
                advertised_total += info.file_size
                if advertised_total > MAX_UNCOMPRESSED_BYTES:
                    raise WebUIThemeError("主题包解压后超过大小限制")
                suffix = PurePosixPath(name).suffix.lower()
                if suffix not in ALLOWED_EXTENSIONS:
                    raise WebUIThemeError("主题包包含不允许的文件类型")
                if info.file_size > 1024 * 1024 and (
                    info.compress_size == 0
                    or info.file_size > info.compress_size * MAX_EXPANSION_RATIO
                ):
                    raise WebUIThemeError("主题包压缩率异常")

            self._check_archive_tree(targets)
            manifest_bytes: bytes | None = None
            actual_total = 0
            for info, name, is_dir in normalized_info:
                if is_dir:
                    continue
                if name == MANIFEST_FILE and info.file_size > MAX_MANIFEST_BYTES:
                    raise WebUIThemeError("主题包清单过大")
                relative = PurePosixPath(name)
                should_write = name == MANIFEST_FILE or name.startswith(f"{WEBUI_DIR}/")
                output: Path | None = None
                if should_write:
                    output = stage.joinpath(*relative.parts)
                    self._assert_child_path(output, stage)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    self._assert_directory_tree(output.parent, stage)
                buffer = bytearray() if name == MANIFEST_FILE else None
                written = 0
                try:
                    with archive.open(info, "r") as source:
                        if output is not None:
                            with output.open("xb") as target_file:
                                while True:
                                    chunk = source.read(CHUNK_BYTES)
                                    if not chunk:
                                        break
                                    written += len(chunk)
                                    actual_total += len(chunk)
                                    if written > MAX_FILE_BYTES or actual_total > MAX_UNCOMPRESSED_BYTES:
                                        raise WebUIThemeError("主题包解压后超过大小限制")
                                    target_file.write(chunk)
                                    if buffer is not None:
                                        buffer.extend(chunk)
                        else:
                            while True:
                                chunk = source.read(CHUNK_BYTES)
                                if not chunk:
                                    break
                                written += len(chunk)
                                actual_total += len(chunk)
                                if written > MAX_FILE_BYTES or actual_total > MAX_UNCOMPRESSED_BYTES:
                                    raise WebUIThemeError("主题包解压后超过大小限制")
                except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
                    raise WebUIThemeError("主题包内容损坏或无法读取") from None
                if written != info.file_size:
                    raise WebUIThemeError("主题包文件大小与清单不一致")
                if buffer is not None:
                    manifest_bytes = bytes(buffer)

            if manifest_bytes is None:
                raise WebUIThemeError("主题包缺少根目录 theme.json")
            try:
                manifest_object = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise WebUIThemeError("theme.json 必须是有效 UTF-8 JSON") from None
            manifest = self._validate_manifest(manifest_object)
            self._validate_webui_files(stage / WEBUI_DIR, manifest)
            return manifest

    @staticmethod
    def _raw_zip_member_names(payload: bytes) -> list[str]:
        """Read central-directory names before Windows ZipInfo normalizes backslashes."""
        search_from = max(0, len(payload) - (65_535 + 22))
        end_offset = payload.rfind(b"PK\x05\x06", search_from)
        if end_offset < 0 or end_offset + 22 > len(payload):
            raise WebUIThemeError("主题包 ZIP 目录无效")
        try:
            (
                signature,
                disk_number,
                directory_disk,
                disk_entries,
                total_entries,
                directory_size,
                _directory_offset,
                comment_size,
            ) = struct.unpack_from("<4s4H2LH", payload, end_offset)
        except struct.error:
            raise WebUIThemeError("主题包 ZIP 目录无效") from None
        if (
            signature != b"PK\x05\x06"
            or disk_number != 0
            or directory_disk != 0
            or disk_entries != total_entries
            or total_entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or end_offset + 22 + comment_size != len(payload)
        ):
            raise WebUIThemeError("主题包 ZIP 目录格式不受支持")

        directory_start = end_offset - directory_size
        if directory_start < 0:
            raise WebUIThemeError("主题包 ZIP 目录无效")
        position = directory_start
        names: list[str] = []
        for _ in range(total_entries):
            if position + 46 > end_offset:
                raise WebUIThemeError("主题包 ZIP 目录无效")
            try:
                fields = struct.unpack_from("<4s6H3I5H2I", payload, position)
            except struct.error:
                raise WebUIThemeError("主题包 ZIP 目录无效") from None
            if fields[0] != b"PK\x01\x02":
                raise WebUIThemeError("主题包 ZIP 目录无效")
            flags = fields[3]
            filename_size, extra_size, comment_size = fields[10:13]
            compressed_size, uncompressed_size = fields[8], fields[9]
            local_offset = fields[16]
            if any(
                value == 0xFFFFFFFF
                for value in (compressed_size, uncompressed_size, local_offset)
            ):
                raise WebUIThemeError("不接受 ZIP64 主题包")
            name_start = position + 46
            name_end = name_start + filename_size
            entry_end = name_end + extra_size + comment_size
            if entry_end > end_offset:
                raise WebUIThemeError("主题包 ZIP 目录无效")
            raw_name = payload[name_start:name_end]
            if not raw_name or b"\\" in raw_name or b"\x00" in raw_name:
                raise WebUIThemeError("主题包包含无效路径")
            encoding = "utf-8" if flags & 0x800 else "cp437"
            try:
                names.append(raw_name.decode(encoding))
            except UnicodeDecodeError:
                raise WebUIThemeError("主题包文件名编码无效") from None
            position = entry_end
        if position != directory_start + directory_size:
            raise WebUIThemeError("主题包 ZIP 目录长度无效")
        return names

    @staticmethod
    def _normalize_archive_name(
        raw_name: str, info: zipfile.ZipInfo
    ) -> tuple[str, bool]:
        if not raw_name or "\x00" in raw_name or "\\" in raw_name:
            raise WebUIThemeError("主题包包含无效路径")
        if raw_name.startswith("/") or raw_name.startswith("//") or _DRIVE_PREFIX.match(raw_name):
            raise WebUIThemeError("主题包不能包含绝对路径")
        if ":" in raw_name:
            raise WebUIThemeError("主题包路径不能包含盘符或数据流语法")

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        marked_directory = raw_name.endswith("/")
        if marked_directory != info.is_dir():
            raise WebUIThemeError("主题包目录条目标记无效")
        if file_type == stat.S_IFDIR and not marked_directory:
            raise WebUIThemeError("主题包目录条目标记无效")
        if file_type == stat.S_IFREG and marked_directory:
            raise WebUIThemeError("主题包文件类型标记无效")

        raw_parts = raw_name[:-1].split("/") if marked_directory else raw_name.split("/")
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise WebUIThemeError("主题包包含路径穿越或重复分隔符")
        normalized = "/".join(unicodedata.normalize("NFC", part) for part in raw_parts)
        if not normalized:
            raise WebUIThemeError("主题包包含无效路径")
        return normalized, marked_directory

    @staticmethod
    def _check_archive_tree(targets: dict[str, tuple[str, bool]]) -> None:
        kinds = {key: is_dir for key, (_name, is_dir) in targets.items()}
        for key, (_name, is_dir) in targets.items():
            parts = key.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if parent in kinds and not kinds[parent]:
                    raise WebUIThemeError("主题包文件与目录路径冲突")
            if not is_dir and any(other.startswith(key + "/") for other in kinds):
                raise WebUIThemeError("主题包文件与目录路径冲突")

    @staticmethod
    def _validate_manifest(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - {"card_style"} != _MANIFEST_KEYS:
            raise WebUIThemeError("theme.json 字段不符合主题包规范")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise WebUIThemeError("主题包清单版本不兼容")
        if value.get("target_plugin") != PLUGIN_ID:
            raise WebUIThemeError("主题包目标插件不兼容")
        if type(value.get("webui_api")) is not int or value["webui_api"] != 1:
            raise WebUIThemeError("主题包 WebUI API 版本不兼容")

        theme_id = value.get("id")
        mount = value.get("mount")
        if not isinstance(theme_id, str) or not _SAFE_ID.fullmatch(theme_id) or theme_id == "classic":
            raise WebUIThemeError("主题包 ID 无效")
        if not isinstance(mount, str) or not _SAFE_ID.fullmatch(mount):
            raise WebUIThemeError("主题包挂载目录无效")
        if mount.casefold() in _RESERVED_MOUNTS or mount in {".", ".."}:
            raise WebUIThemeError("主题包不能占用保留挂载目录")
        card_style = value.get("card_style", theme_id if theme_id in CARD_STYLES else "classic")
        if not isinstance(card_style, str) or card_style not in CARD_STYLES:
            raise WebUIThemeError("主题包卡片皮肤无效")

        name = value.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 128
            or any(ord(char) < 32 for char in name)
        ):
            raise WebUIThemeError("主题包名称无效")
        version = value.get("version")
        if not isinstance(version, str) or not _SAFE_VERSION.fullmatch(version):
            raise WebUIThemeError("主题包版本无效")
        template = WebUIThemeManager._validate_manifest_path(value.get("template"), ".html")
        entry = WebUIThemeManager._validate_manifest_path(value.get("entry"), ".js")
        return {
            "schema_version": 1,
            "id": theme_id,
            "name": name.strip(),
            "version": version,
            "target_plugin": PLUGIN_ID,
            "webui_api": 1,
            "mount": mount,
            "template": template,
            "entry": entry,
            "card_style": card_style,
        }

    @staticmethod
    def _validate_manifest_path(value: Any, extension: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise WebUIThemeError("主题入口路径无效")
        if value.startswith("/") or _DRIVE_PREFIX.match(value) or ":" in value:
            raise WebUIThemeError("主题入口必须是相对路径")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise WebUIThemeError("主题入口包含路径穿越")
        normalized = "/".join(unicodedata.normalize("NFC", part) for part in parts)
        if PurePosixPath(normalized).suffix.lower() != extension:
            raise WebUIThemeError("主题入口文件类型无效")
        return normalized

    @staticmethod
    def _validate_webui_files(webui: Path, manifest: dict[str, Any]) -> None:
        if not webui.is_dir() or webui.is_symlink():
            raise WebUIThemeError("主题包缺少 webui 目录")
        paths: set[str] = set()
        folded_paths: set[str] = set()
        total = 0
        count = 0
        for root, directories, files in os.walk(webui, topdown=True, followlinks=False):
            root_path = Path(root)
            for dirname in list(directories):
                directory = root_path / dirname
                if directory.is_symlink():
                    raise WebUIThemeError("主题包 webui 中不接受符号链接")
            for filename in files:
                path = root_path / filename
                if path.is_symlink() or not path.is_file():
                    raise WebUIThemeError("主题包 webui 文件类型无效")
                relative = path.relative_to(webui).as_posix()
                safe = WebUIThemeManager._validate_manifest_path_any(relative)
                folded = safe.casefold()
                if folded in folded_paths:
                    raise WebUIThemeError("主题包 webui 包含重复路径")
                folded_paths.add(folded)
                paths.add(safe)
                if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    raise WebUIThemeError("主题包 webui 包含不允许的文件类型")
                size = path.stat().st_size
                count += 1
                total += size
                if count > MAX_FILES or size > MAX_FILE_BYTES or total > MAX_UNCOMPRESSED_BYTES:
                    raise WebUIThemeError("主题包 webui 大小或文件数超限")
        if manifest["template"] not in paths or manifest["entry"] not in paths:
            raise WebUIThemeError("主题模板或入口文件不存在")

    @staticmethod
    def _validate_manifest_path_any(value: str) -> str:
        if not value or "\\" in value or value.startswith("/") or _DRIVE_PREFIX.match(value):
            raise WebUIThemeError("主题包文件路径无效")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts) or ":" in value:
            raise WebUIThemeError("主题包文件路径无效")
        return "/".join(unicodedata.normalize("NFC", part) for part in parts)

    def _create_mount_stage(
        self,
        store_stage: Path,
        mount_stage: Path,
        manifest: dict[str, Any],
        archive_hash: str,
        transaction_id: str,
    ) -> None:
        self._mkdir_owned_stage(mount_stage, self._mount_parent)
        webui_source = store_stage / WEBUI_DIR
        self._assert_directory_tree(webui_source, store_stage)
        self._copy_tree_safe(webui_source, mount_stage)
        self._write_json(
            mount_stage / MOUNT_MARKER,
            self._marker(manifest, archive_hash, transaction_id),
        )

    def _copy_tree_safe(self, source: Path, destination: Path) -> None:
        self._assert_directory_tree(source, source.parent)
        for root, directories, files in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            relative_dir = root_path.relative_to(source)
            target_dir = destination / relative_dir
            if target_dir != destination:
                self._assert_child_path(target_dir, destination)
            for dirname in list(directories):
                source_dir = root_path / dirname
                if source_dir.is_symlink():
                    raise WebUIThemeError("主题包目录包含符号链接")
                target = target_dir / dirname
                self._assert_child_path(target, destination)
                target.mkdir(exist_ok=True)
            for filename in files:
                source_file = root_path / filename
                if source_file.is_symlink() or not source_file.is_file():
                    raise WebUIThemeError("主题包文件类型无效")
                target_file = target_dir / filename
                self._assert_child_path(target_file, destination)
                shutil.copyfile(source_file, target_file)

    def _commit_stages(
        self,
        store_stage: Path,
        mount_stage: Path,
        manifest: dict[str, Any],
        transaction_id: str,
        archive_hash: str,
    ) -> None:
        theme_id = manifest["id"]
        mount_name = manifest["mount"]
        store_target = self._store_root / theme_id
        mount_target = self._mount_parent / mount_name
        store_backup = self._store_root / f".backup-{theme_id}-{transaction_id}"
        mount_backup = self._mount_parent / f".{mount_name}.backup-{transaction_id}"

        self._assert_child_path(store_target, self._store_root)
        self._assert_child_path(mount_target, self._mount_parent)
        if store_target.exists():
            old_manifest, _marker = self._load_authoritative(store_target)
            if old_manifest["mount"] != mount_name:
                raise WebUIThemeError("已安装主题的挂载目录不能在升级时变更")
        backups_made: list[tuple[Path, Path, Path, bool]] = []
        committed: list[tuple[Path, Path]] = []
        adopting_unmarked_mount = False
        try:
            if store_target.exists():
                self._move_existing_managed(store_target, store_backup, self._store_root)
                backups_made.append((store_backup, store_target, self._store_root, False))
            if mount_target.exists():
                adopting_unmarked_mount = not self._mount_is_managed_by(
                    mount_target, theme_id, mount_name
                )
                if adopting_unmarked_mount:
                    existing_marker = mount_target / MOUNT_MARKER
                    if existing_marker.exists() or existing_marker.is_symlink():
                        raise WebUIThemeError("目标挂载目录已存在且不受主题管理器管理")
                    if not self._directories_match(
                        mount_target, mount_stage, ignored_root_files={MOUNT_MARKER}
                    ):
                        raise WebUIThemeError(
                            "目标挂载目录未受管理且内容与主题包不完全一致"
                        )
                self._move_existing_managed(mount_target, mount_backup, self._mount_parent)
                backups_made.append(
                    (mount_backup, mount_target, self._mount_parent, adopting_unmarked_mount)
                )
                if adopting_unmarked_mount and not self._directories_match(
                    mount_backup, mount_stage, ignored_root_files={MOUNT_MARKER}
                ):
                    raise WebUIThemeError("旧挂载内容在切换期间发生变化，已取消接管")
            self._move_new_directory(store_stage, store_target, self._store_root)
            committed.append((store_target, self._store_root))
            self._move_new_directory(mount_stage, mount_target, self._mount_parent)
            committed.append((mount_target, self._mount_parent))
        except WebUIThemeError:
            self._rollback_transaction(committed, backups_made, transaction_id)
            raise
        except Exception:
            self._rollback_transaction(committed, backups_made, transaction_id)
            raise WebUIThemeError("主题安装失败，原有主题文件已保留") from None

        for backup, _target, parent, verified_unmarked in backups_made:
            if verified_unmarked:
                self._remove_verified_mount_backup(backup, parent, mount_name)
                continue
            marker_name = STORE_MARKER if parent == self._store_root else MOUNT_MARKER
            self._remove_managed_backup(backup, parent, marker_name)

    def _rollback_transaction(
        self,
        committed: list[tuple[Path, Path]],
        backups: list[tuple[Path, Path, Path, bool]],
        transaction_id: str,
    ) -> None:
        for path, parent in reversed(committed):
            marker_name = STORE_MARKER if parent == self._store_root else MOUNT_MARKER
            if self._marker_has_transaction(path, marker_name, transaction_id):
                self._rmtree_safe(path, parent)
        for backup, target, parent, _verified_unmarked in reversed(backups):
            if backup.exists() and not target.exists():
                self._move_new_directory(backup, target, parent)

    def _directories_match(
        self,
        left: Path,
        right: Path,
        *,
        ignored_root_files: frozenset[str] | set[str] = frozenset(),
    ) -> bool:
        """Compare two trees exactly without following links or adopting extras."""
        try:
            left_files, left_dirs = self._directory_inventory(left, ignored_root_files)
            right_files, right_dirs = self._directory_inventory(right, ignored_root_files)
            if left_files.keys() != right_files.keys() or left_dirs != right_dirs:
                return False
            for relative in left_files:
                left_file = left_files[relative]
                right_file = right_files[relative]
                if left_file.stat().st_size != right_file.stat().st_size:
                    return False
                with left_file.open("rb") as left_stream, right_file.open("rb") as right_stream:
                    while True:
                        left_chunk = left_stream.read(CHUNK_BYTES)
                        right_chunk = right_stream.read(CHUNK_BYTES)
                        if left_chunk != right_chunk:
                            return False
                        if not left_chunk:
                            break
            return True
        except (OSError, WebUIThemeError, ValueError):
            return False

    def _directory_inventory(
        self, root: Path, ignored_root_files: frozenset[str] | set[str]
    ) -> tuple[dict[str, Path], set[str]]:
        self._assert_directory_tree(root, root.parent)
        if not root.is_dir() or root.is_symlink():
            raise WebUIThemeError("主题目录无效")
        files: dict[str, Path] = {}
        directories: set[str] = set()
        for current, child_dirs, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for dirname in child_dirs:
                child = current_path / dirname
                if child.is_symlink() or not child.is_dir():
                    raise WebUIThemeError("目录中存在符号链接或特殊文件")
                directories.add(child.relative_to(root).as_posix())
            for filename in filenames:
                child = current_path / filename
                if child.is_symlink() or not child.is_file():
                    raise WebUIThemeError("目录中存在符号链接或特殊文件")
                relative = child.relative_to(root).as_posix()
                if child.parent == root and filename in ignored_root_files:
                    continue
                files[relative] = child
        return files, directories

    def _mount_is_valid(self, manifest: dict[str, Any]) -> bool:
        mount = self._mount_parent / manifest["mount"]
        if not mount.exists() or mount.is_symlink():
            return False
        if not self._mount_is_managed_by(mount, manifest["id"], manifest["mount"]):
            return False
        try:
            self._validate_webui_files(mount, manifest)
        except (OSError, WebUIThemeError):
            return False
        return True

    def _mount_is_managed_by(self, mount: Path, theme_id: str, mount_name: str) -> bool:
        try:
            self._assert_directory_tree(mount, self._mount_parent)
            marker = self._read_json(mount / MOUNT_MARKER, max_bytes=MAX_MANIFEST_BYTES)
        except (OSError, WebUIThemeError):
            return False
        return (
            isinstance(marker, dict)
            and marker.get("owner") == PLUGIN_ID
            and marker.get("theme_id") == theme_id
            and marker.get("mount") == mount_name
        )

    def _load_authoritative(self, directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        self._assert_directory_tree(directory, self._store_root)
        marker = self._read_json(directory / STORE_MARKER, max_bytes=MAX_MANIFEST_BYTES)
        if not isinstance(marker, dict) or marker.get("owner") != PLUGIN_ID:
            raise WebUIThemeError("主题持久副本没有有效管理标记")
        manifest_bytes = (directory / MANIFEST_FILE).read_bytes()
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise WebUIThemeError("主题持久副本清单过大")
        try:
            manifest_object = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WebUIThemeError("主题持久副本清单无效") from None
        manifest = self._validate_manifest(manifest_object)
        if directory.name != manifest["id"]:
            raise WebUIThemeError("主题持久副本 ID 与目录不一致")
        mount = marker.get("mount")
        if (
            marker.get("theme_id") != manifest["id"]
            or mount not in (manifest["mount"], f"theme-{manifest['id']}")
            or marker.get("version") != manifest["version"]
        ):
            raise WebUIThemeError("主题持久副本管理标记不匹配")
        manifest["mount"] = mount
        self._validate_webui_files(directory / WEBUI_DIR, manifest)
        return manifest, marker

    @staticmethod
    def _metadata(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": manifest["id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "mount": manifest["mount"],
            "template": manifest["template"],
            "entry": manifest["entry"],
            "card_style": manifest["card_style"],
        }

    @staticmethod
    def _marker(
        manifest: dict[str, Any], archive_hash: str, transaction_id: str
    ) -> dict[str, str]:
        return {
            "owner": PLUGIN_ID,
            "theme_id": manifest["id"],
            "mount": manifest["mount"],
            "version": manifest["version"],
            "archive_sha256": archive_hash,
            "transaction_id": transaction_id,
        }

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")

    @staticmethod
    def _read_json(path: Path, max_bytes: int) -> Any:
        if path.is_symlink() or not path.is_file():
            raise WebUIThemeError("主题管理标记无效")
        if path.stat().st_size > max_bytes:
            raise WebUIThemeError("主题管理标记过大")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            raise WebUIThemeError("主题管理标记无效") from None

    def _ensure_directory(self, path: Path, boundary: Path) -> None:
        boundary = boundary.resolve()
        if path == boundary:
            if path.is_symlink():
                raise WebUIThemeError("主题目录不能是符号链接")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise WebUIThemeError("主题目录路径无效")
            return
        self._assert_child_path(path, boundary)
        relative = path.relative_to(boundary)
        current = boundary
        if not current.exists():
            current.mkdir(parents=True, exist_ok=True)
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise WebUIThemeError("主题目录路径不能包含符号链接")
            if current.exists():
                if not current.is_dir():
                    raise WebUIThemeError("主题目录路径被文件占用")
            else:
                current.mkdir()
        self._assert_directory_tree(path, boundary)

    def _mkdir_owned_stage(self, path: Path, parent: Path) -> None:
        self._assert_child_path(path, parent)
        if path.exists() or path.is_symlink():
            raise WebUIThemeError("主题暂存目录已存在")
        path.mkdir()

    @staticmethod
    def _assert_child_path(path: Path, parent: Path) -> None:
        parent = parent.resolve()
        try:
            lexical_relative = path.relative_to(parent)
        except ValueError:
            raise WebUIThemeError("主题路径越出管理目录") from None
        if not lexical_relative.parts or any(part in {"", ".", ".."} for part in lexical_relative.parts):
            raise WebUIThemeError("主题路径无效")
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(parent)
        except ValueError:
            raise WebUIThemeError("主题路径符号链接越出管理目录") from None
        current = parent
        for part in lexical_relative.parts:
            current = current / part
            if current.is_symlink():
                raise WebUIThemeError("主题路径不能包含符号链接")

    def _assert_directory_tree(self, path: Path, parent: Path) -> None:
        if path.resolve(strict=False) == parent.resolve(strict=False):
            if path.is_symlink():
                raise WebUIThemeError("主题目录不能是符号链接")
            if path.exists() and not path.is_dir():
                raise WebUIThemeError("主题路径不是目录")
            return
        self._assert_child_path(path, parent)
        if path.exists() and not path.is_dir():
            raise WebUIThemeError("主题路径不是目录")

    def _move_new_directory(self, source: Path, target: Path, parent: Path) -> None:
        self._assert_child_path(source, parent)
        self._assert_child_path(target, parent)
        if not source.is_dir() or source.is_symlink() or target.exists() or target.is_symlink():
            raise WebUIThemeError("主题目录切换条件不满足")
        os.replace(source, target)

    def _move_existing_managed(self, source: Path, target: Path, parent: Path) -> None:
        self._assert_child_path(source, parent)
        self._assert_child_path(target, parent)
        if not source.is_dir() or source.is_symlink() or target.exists() or target.is_symlink():
            raise WebUIThemeError("已安装主题目录切换条件不满足")
        os.replace(source, target)

    def _remove_stage_if_present(self, path: Path, parent: Path) -> None:
        if path.is_symlink():
            return
        if not path.exists():
            return
        try:
            self._assert_child_path(path, parent)
            if not path.is_dir() or not path.name.startswith("."):
                return
            shutil.rmtree(path)
        except (OSError, WebUIThemeError):
            return

    def _remove_managed_backup(self, path: Path, parent: Path, marker_name: str) -> None:
        try:
            self._assert_child_path(path, parent)
            if not path.is_dir() or path.is_symlink():
                return
            marker = self._read_json(path / marker_name, max_bytes=MAX_MANIFEST_BYTES)
            if isinstance(marker, dict) and marker.get("owner") == PLUGIN_ID:
                shutil.rmtree(path)
        except (OSError, WebUIThemeError):
            return

    def _remove_verified_mount_backup(self, path: Path, parent: Path, mount_name: str) -> None:
        try:
            self._assert_child_path(path, parent)
            if (
                path.name != f".{mount_name}.backup-" + path.name.rsplit(".backup-", 1)[-1]
                or not path.is_dir()
                or path.is_symlink()
            ):
                return
            shutil.rmtree(path)
        except (OSError, WebUIThemeError):
            return

    def _rmtree_safe(self, path: Path, parent: Path) -> None:
        self._assert_child_path(path, parent)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)

    @staticmethod
    def _marker_has_transaction(path: Path, marker_name: str, transaction_id: str) -> bool:
        try:
            marker = WebUIThemeManager._read_json(path / marker_name, max_bytes=MAX_MANIFEST_BYTES)
        except (OSError, WebUIThemeError):
            return False
        return isinstance(marker, dict) and marker.get("transaction_id") == transaction_id
