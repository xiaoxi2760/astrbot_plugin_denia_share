"""对**解压出来的**包做自检，而不是对着工作区。

漏打包一个新文件是发布事故里最难查的一种：工作区测试全绿，装到机器上白屏。
所以校验对象必须是 zip 本身 —— 解到临时目录，从那里 import、跑 schema 一致性、
比对两份 logo 是不是同一张图。

用法::

    py -3 tests/verify_package.py                 # 校验工作区上一个最新包
    py -3 tests/verify_package.py path/to.zip
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# 与 package.py 保持一致的排除规则
FORBIDDEN = ("__pycache__", "tests/", "DEVELOPMENT.md", "package.py")


def default_zip() -> Path:
    """工作区里版本号最高的包。

    **必须按版本号比，不能按文件名排序** —— 字符串排序下 ``v1.3.0`` 排在
    ``v1.2.5`` 之后，于是「验最新的包」会静默验到一个旧包，而且照样全绿。
    """
    candidates = list(PLUGIN_DIR.parent.glob("astrbot_plugin_denia_share_v*.zip"))
    if not candidates:
        raise SystemExit("工作区里没有找到 astrbot_plugin_denia_share_v*.zip")

    def version_of(path: Path) -> tuple[int, ...]:
        digits = re.findall(r"\d+", path.stem.split("_v", 1)[-1])
        return tuple(int(d) for d in digits) or (0,)

    return max(candidates, key=version_of)


def check(archive: Path) -> int:
    failures: list[str] = []
    extracted_root = Path(tempfile.mkdtemp(prefix="denia_pkg_"))

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        bad = [n for n in names if n.startswith(FORBIDDEN) or n.endswith((".pyc", ".pyo"))]
        if bad:
            failures.append(f"不该打进包的文件: {bad}")
        nested = [n for n in names if n.startswith("astrbot_plugin_denia_share/")]
        if nested:
            failures.append(f"多套了一层目录: {nested[:3]}")
        zf.extractall(extracted_root)

    # ---- 版本号必须和包名一致 ----
    name_version = archive.stem.rsplit("_v", 1)[-1]
    init_src = (extracted_root / "__init__.py").read_text(encoding="utf-8")
    meta_src = (extracted_root / "metadata.yaml").read_text(encoding="utf-8")
    import re

    init_version = re.search(r'__version__\s*=\s*["\'](.+?)["\']', init_src).group(1)
    meta_version = re.search(r'^version:\s*(\S+)', meta_src, re.MULTILINE).group(1)
    for label, value in (("__init__.py", init_version), ("metadata.yaml", meta_version)):
        if value != name_version:
            failures.append(f"{label} 版本 {value} 与包名 {name_version} 不一致")
    if not (init_version == meta_version):
        failures.append(f"__init__.py({init_version}) 与 metadata.yaml({meta_version}) 不一致")

    # ---- 可编译 ----
    import py_compile

    for py in extracted_root.rglob("*.py"):
        try:
            py_compile.compile(str(py), doraise=True, cfile=str(Path(tempfile.gettempdir()) / "x.pyc"))
        except py_compile.PyCompileError as exc:
            failures.append(f"编译失败 {py.relative_to(extracted_root)}: {exc}")
            break

    # ---- 从解压目录跑 schema 一致性自检（插件启动时跑的那条）----
    # PLUGIN_DIR 是为了能 import tests._stubs（取宿主桩）；
    # extracted_root.parent 是为了让解压出来的那份可被 import。
    # 用 archive.stem 当包名而非 astrbot_plugin_denia_share，
    # 免得 sys.path 上还挂着工作区那份时被解析成同一模块。
    sys.path.insert(0, str(PLUGIN_DIR))
    sys.path.insert(0, str(extracted_root.parent))
    from tests._stubs import install_host_stubs  # noqa: PLC0415

    install_host_stubs()
    try:
        spec_dir = str(extracted_root)
        import importlib  # noqa: PLC0415
        import types  # noqa: PLC0415

        # 以真实包名导入解压出来的那份
        pkg = types.ModuleType(archive.stem)
        pkg.__path__ = [spec_dir]
        pkg.__version__ = init_version
        sys.modules[archive.stem] = pkg
        cfg = importlib.import_module(f"{archive.stem}.core.config")
        problems = cfg.verify_schema_alignment(
            json.loads((extracted_root / "_conf_schema.json").read_text(encoding="utf-8"))
        )
        if problems:
            failures.append(f"schema 一致性自检未通过: {problems}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"从解压目录导入 core.config 失败: {type(exc).__name__}: {exc}")

    # ---- 两份 logo 必须逐字节一致（index.html 注释里承诺的自检项）----
    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    root_logo, page_logo = extracted_root / "logo.png", extracted_root / "pages/denia/logo.png"
    if not (root_logo.is_file() and page_logo.is_file()):
        failures.append("缺 logo.png（根目录或 pages/denia/）")
    elif digest(root_logo) != digest(page_logo):
        failures.append("两份 logo.png 不同步")

    print(f"包    : {archive.name}")
    print(f"版本  : {init_version}（包名 / __init__.py / metadata.yaml 三处一致）")
    print(f"条目  : {len(names)}")
    if failures:
        print("\n失败：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n自检通过：结构、版本一致、可编译、schema 自检、logo 同步")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default_zip()
    raise SystemExit(check(target))
