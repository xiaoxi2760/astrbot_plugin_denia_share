"""跑冒烟测试并输出报告。

    py -3 tests/smoke/run_smoke.py                       # 默认走 http://127.0.0.1:7897
    py -3 tests/smoke/run_smoke.py http://127.0.0.1:7897
    py -3 tests/smoke/run_smoke.py ""                    # 不走代理（Steam 等可能需要直连）

分两层：

1. **派发**（不联网）：URL 能否被正确的解析器认领。这层是确定性的 ——
   解析器的 ``@handle`` 正则写错、平台顺序变了、改名了，这里立刻炸。
2. **解析**（联网）：真的跑一遍 parse，断言结构完整。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.smoke import harness  # noqa: E402
from tests.smoke.cases import CASES, Case, by_platform, runnable  # noqa: E402

DEFAULT_PROXY = "http://127.0.0.1:7897"
TIMEOUT = 45.0


def dispatch_check(cases) -> list[tuple[Case, str | None]]:
    """第一层：只验派发，不联网。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="denia_dispatch_"))
    parsers = harness.build_parsers(harness.build_downloader(tmp, None))
    out = []
    for case in cases:
        if not case.url:
            out.append((case, None))
            continue
        out.append((case, harness.match_parser(parsers, case.url)))
    return out


async def main() -> int:
    proxy = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROXY
    proxy = proxy or None
    print(f"代理: {proxy or '直连'}")
    print(f"用例: 共 {len(CASES)} 条，其中有 URL 可跑 {len(runnable())} 条")

    # ---- 第一层：派发 ----
    print("\n" + "=" * 60)
    print("第一层：派发（离线，确定性）")
    print("=" * 60)
    dispatch = dispatch_check(CASES)
    bad_dispatch = []
    for case, got in dispatch:
        if not case.url:
            print(f"  跳过  {case.id:<14} {case.kind:<12} （URL 待补）")
            continue
        mark = "OK  " if got == case.platform else "BAD "
        if got != case.platform:
            bad_dispatch.append((case, got))
        print(f"  {mark}  {case.id:<14} {case.kind:<12} → {got}")

    # ---- 第二层：解析 ----
    print("\n" + "=" * 60)
    print("第二层：解析（联网）")
    print("=" * 60)
    results = await harness.run_all(runnable(), proxy, TIMEOUT)

    passed = [v for v in results if v.ok]
    by_stage: dict[str, list] = {}
    for v in results:
        if not v.ok:
            by_stage.setdefault(v.stage, []).append(v)

    for v in results:
        mark = "PASS" if v.ok else "FAIL"
        line = f"  {mark}  {v.platform:<12} {v.kind:<12} {v.elapsed:5.1f}s  {v.summary[:60]}"
        print(line)

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    print(f"  派发错误: {len(bad_dispatch)}")
    print(f"  解析通过: {len(passed)} / {len(results)}")
    for stage, items in sorted(by_stage.items()):
        print(f"  {stage}: {len(items)}")
        for v in items:
            print(f"      {v.platform}/{v.kind}  {v.reason[:90]}")

    missing = [c for c in CASES if not c.url]
    print(f"\n  待补 URL: {len(missing)}")
    for c in missing:
        print(f"      {c.platform:<12} {c.kind:<12} {c.note}")

    return 0 if not bad_dispatch and not by_stage else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
