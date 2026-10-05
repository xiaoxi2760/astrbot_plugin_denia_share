"""一次跑完前后端测试。

    py -3 tests/run_tests.py

分开跑也行：

    py -3 -m unittest discover -s tests -t .
    node --test "tests/frontend/*.test.mjs"

任何一端失败即以非零码退出，便于挂到 CI 或提交前钩子上。
Node 缺失不算失败 —— 页面测试只影响开发体验，不影响插件运行。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(label: str, cmd: list[str], cwd: Path) -> bool:
    print(f"\n{'=' * 12} {label} {'=' * 12}", flush=True)
    result = subprocess.run(cmd, cwd=cwd)
    return result.returncode == 0


def main() -> int:
    ok = run("Python 单元测试", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], ROOT)

    node = shutil.which("node")
    if not node:
        print("\n未找到 node，跳过页面测试（不影响插件运行）", flush=True)
    else:
        # Node 22 的 --test 收到目录会当成模块路径去加载，必须给 glob
        ok &= run("页面测试", [node, "--test", "tests/frontend/*.test.mjs"], ROOT)

    print(f"\n{'全部通过' if ok else '存在失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
