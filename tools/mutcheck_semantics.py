# -*- coding: utf-8 -*-
"""语义补丁的**变异检查**：把实现改回那个坏样子，测试必须真红。

    python tools/mutcheck_semantics.py

## 为什么这几条尤其需要

这一批修的全是「不抛异常、只是不再干活」的形状：

- 失败的轮次被记成成功 —— 进程照跑、心跳照报 alive，**没有任何报错**
- 重试时 id 变了 —— 执行端去重失效，**没有任何报错**，只是重复下单
- 写坏的信号被跳过 —— 调用方看到的是「队列里就这些」，**没有任何报错**
- 涨停价照成交 —— 回测顺畅地把钱赚了，**没有任何报错**
- 持仓三天没更新仍判已对账 —— 闸门不落，**没有任何报错**

这类 bug 的测试有一个共同的失败模式：断言写得**永远为真**（比如只断言「没崩」），
于是无论实现好坏都绿。变异检查是唯一能区分「真断言」和「装饰断言」的办法。

做法与另外两个 mutcheck 一致：临时改写 → 跑对应测试 → 看红不红 → 原样写回 → 核对字节。

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

RUNNER = "tests/test_runner_failure.py"
MODELS = "tests/test_models.py"
LIMITS = "tests/test_limits.py"

# (标签, 文件, 原片段, 改成, 测试节点, 期望红?)
MUTATIONS = [
    ("S1 失败了也记「今日已执行」", "signalchain/runner.py",
     "                if outcome.error:\n"
     "                    # ★ `run_round` 把异常写进 `result.error` 而不是抛出（见它的\n"
     "                    # except 分支：单轮失败不该杀进程）。所以**这里必须当成失败**。\n"
     "                    # 不看这一行，就是「任务天天失败、`mark_executed` 照记、心跳照报\n"
     "                    # alive」—— 连续失败上限与心跳 error 对默认路径全部失效，\n"
     "                    # 而这种失效在终端上没有任何痕迹。\n"
     "                    self._record_failure(day, name, outcome.error)\n"
     "                    continue\n",
     "",
     f"{RUNNER}::test_a_failed_round_is_not_recorded_as_success", True),

    ("S2 幂等键用实际时刻", "signalchain/models.py",
     '        base = self.anchor or self.created_at.strftime("%Y%m%d%H%M%S")\n',
     '        base = self.created_at.strftime("%Y%m%d%H%M%S")\n',
     f"{RUNNER}::test_a_retry_gets_the_same_signal_id", True),

    ("S3 写坏的信号又静默跳过", "signalchain/models.py",
     "        try:\n"
     "            out.append(Signal.from_dict(item))\n"
     "        except Exception as e:  # noqa: BLE001 - 统一转成可诊断的线格式错误\n"
     "            raise WireFormatError(\n"
     '                f"信号队列第 {position} 条无法解析"\n'
     "                f\"（id={item.get('id')!r}，code={item.get('code')!r}，\"\n"
     "                f\"action={item.get('action')!r}）：{type(e).__name__}: {e}\") from e\n",
     "        try:\n"
     "            out.append(Signal.from_dict(item))\n"
     "        except Exception:  # noqa: BLE001\n"
     "            continue\n",
     f"{MODELS}::test_signal_shaped_but_unparseable_raises_with_position_and_id", True),

    ("S4 涨停板上照买", "signalchain/paper.py",
     "            if band is not None and price >= band[0]:\n"
     "                return self._exec(\n"
     "                    sig, EXEC_SUBMITTED,\n"
     '                    f"涨停（现价 {price:.4f} ≥ {band[0]:.4f}），买入挂单等待", now)\n',
     "",
     f"{LIMITS}::test_buy_at_limit_up_is_not_filled", True),

    ("S6 直接构造的信号丢锚", "signalchain/runner.py",
     "            self._anchor_signals(signals, planned_at)\n",
     "",
     f"{RUNNER}::test_a_directly_constructed_signal_is_anchored_too", True),

    ("S7 派发时丢掉计划时刻", "signalchain/runner.py",
     '        if accepts_kw or "planned_at" in params:\n'
     '            kwargs["planned_at"] = planned_at\n',
     "",
     f"{RUNNER}::test_a_subclassed_run_round_still_receives_the_planned_time", True),

    ("S8 缺 id 的信号又凭空补一个", "signalchain/models.py",
     '        if not item.get("id"):\n'
     "            # ★ 缺 id 不能「补一个现在的时间」。那样每一次重读都会得到**不同的**\n"
     "            #   幂等键，重试就变成重复下单 —— 而且没有任何报错。\n"
     "            #   `id` 是线格式的必需项（`Signal.to_dict()` 一定写它），\n"
     "            #   缺了就说明这一条不是本框架写出来的，或上游把它改坏了。\n"
     "            raise WireFormatError(\n"
     '                f"信号队列第 {position} 条没有 id"\n'
     "                f\"（code={item.get('code')!r}，action={item.get('action')!r}）\"\n"
     '                " —— id 是幂等键，缺了它无法安全执行")\n',
     "",
     f"{RUNNER}::test_a_signal_without_an_id_raises_instead_of_inventing_one", True),

    ("S5 保鲜期开关被忽略", "signalchain/runner.py",
     "        max_age = self.config.max_position_stale_days\n"
     "        if not max_age:\n"
     '            return ""\n',
     '        return ""\n',
     f"{RUNNER}::test_stale_positions_are_rejected_when_the_switch_is_on", True),
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
