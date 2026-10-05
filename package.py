"""把插件打成可分发的 zip。

沿用仓库既有 16 个包的规则（对照 astrbot_plugin_denia_share_v1.2.1.zip 的 83 项）：

- **文件直接放根**，不套 astrbot_plugin_denia_share/ 这一层。AstrBot 装插件时
  自己补目录名，套一层会变成 astrbot_plugin_denia_share/astrbot_plugin_denia_share/。
- 排除 ``__pycache__`` / ``*.pyc``（构建产物，且 .gitignore 已忽略）。
- 排除 ``DEVELOPMENT.md``（开发者本地文档，.gitignore 里就没进仓库）。
- 排除 ``tests/``（离线自检用，运行时不需要；前 16 个包都没带）。

用法（插件根目录下）::

    py -3 package.py            # 自动按 __init__.py 的 __version__ 命名
    py -3 package.py --out D:\\somewhere
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 目录级排除（相对 ROOT）
EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "tests",
    ".pytest_cache",
    ".ruff_cache",
}

# 文件名级排除
EXCLUDE_NAMES = {"DEVELOPMENT.md", ".DS_Store", "Thumbs.db", "package.py"}

# 后缀级排除
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}


def read_version() -> str:
    """从 ``__init__.py`` 的 ``__version__`` 读版本号。"""
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\'](.+?)["\']', source, re.MULTILINE)
    if not match:
        raise SystemExit("__init__.py 里找不到 __version__")
    return match.group(1)


def iter_files():
    """产出要打进包的文件（已排序，已排除）。"""
    files = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
            continue
        if not path.is_file():
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def build(out_path: Path) -> tuple[int, int]:
    files = iter_files()
    with zipfile.ZipFile(
        out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for path in files:
            arcname = path.relative_to(ROOT).as_posix()
            # 固定时间戳，避免同一份内容打出两个字节不同的包
            info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return len(files), out_path.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT.parent), help="输出目录")
    parser.add_argument("--name", default=None, help="包名（不含 .zip）")
    args = parser.parse_args()

    version = read_version()
    name = args.name or f"astrbot_plugin_denia_share_v{version}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.zip"

    count, size = build(out_path)

    print(f"版本  : {version}")
    print(f"条目  : {count}")
    print(f"大小  : {size:,} bytes")
    print(f"输出  : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
