# -*- coding: utf-8 -*-
"""网络韧性两条护栏的**变异检查**：把实现改坏，对应测试必须真红。

    python tools/mutcheck_resilience.py

## 为什么这两条需要变异检查

`RetryingMarketData` 的「重试时真的重新取了一次」和「重试只是空转」，
在**永远返回同一个结果的桩**上长得一模一样；日历的「超时后不叠加线程」
在只跑一次的测试里也看不出来。这类断言最容易被写成**永远为真**的样子，
而在普通跑测试的输出里，永远为真和真的有效也没有区别。

本脚本的做法与 `tools/mutcheck_output.py` 一致：临时改写源文件 → 跑对应测试 →
看它红不红 → 立刻按原样写回；写回后再读一遍确认字节一致。

★ 它会临时改写 `signalchain/` 下的文件。别在别人正在编辑时跑。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent   # 仓库根
RETRY_TEST = "tests/test_retrying_marketdata.py"
CAL_TEST = "tests/test_calendar_timeout.py"

# (标签, 文件, 原片段, 改成, 测试节点, 期望红?)
MUTATIONS = [
    ("R1 重试不再重新取数", "signalchain/marketdata/online.py",
     "            batch = self.inner.quotes(codes)\n"
     "            if batch.ok:\n",
     "            if batch.ok:\n",
     f"{RETRY_TEST}::test_partial_then_complete_really_refetches", True),

    ("R1b 到期了但没记账", "signalchain/marketdata/online.py",
     '                self.stats["aborted"] += 1\n', "",
     f"{RETRY_TEST}::test_deadline_stops_retrying_and_returns_what_it_has", True),

    ("C1 超时后不再拦新线程", "signalchain/calendar.py",
     "        if self._in_flight is not None and self._in_flight.is_alive():\n"
     "            raise CalendarError(\n"
     '                "上一次日历加载仍未返回，本次放弃（不叠加新线程）")\n',
     "",
     f"{CAL_TEST}::test_timed_out_load_is_not_stacked", True),
]


def _run(node: str):
    proc = subprocess.run([sys.executable, "-m", "pytest", node,
                           "-p", "no:cacheprovider", "--no-header"],
                          capture_output=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    mismatched = []
    for label, rel, old, new, node, expect_red in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"★ {label}: 在 {rel} 里找不到要替换的片段 —— "
                  f"变异没生效，这条检查本身失效了（比红更坏）")
            return 2
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            rc, out = _run(node)
        finally:
            path.write_text(original, encoding="utf-8")
            assert path.read_text(encoding="utf-8") == original, \
                f"{label}: 还原失败，请立刻 git diff 检查 {rel}"
        red = rc != 0
        ok = red == expect_red
        print(f"{'OK ' if ok else '★✗ '}{label:26s} → {'红' if red else '绿'}"
              f"（期望{'红' if expect_red else '绿'}）")
        if not ok:
            mismatched.append(label)
            print("\n".join("      " + line
                            for line in out.strip().splitlines()[-10:]))

    print()
    if mismatched:
        print(f"★ 与期望不符：{mismatched}")
        return 1
    print("[ok] 全部符合预期，且每个文件都已按原样写回")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
