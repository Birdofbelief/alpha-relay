# -*- coding: utf-8 -*-
"""输出安全护栏的**变异检查**：每条护栏都必须在坏代码上真红。

    python tools/mutcheck_output.py

## 为什么这个脚本值得进仓库

`tests/test_output_safety.py` 里有几条断言，光看它们**看不出是不是摆设** ——
一条永远为真的断言和一个有效的断言，在「跑测试」的输出里长得一模一样。
这个仓库反复在防的就是这件事：**声明冒充证据**。

做法：把某个源文件临时改成「我知道它该红」的样子 → 跑对应测试 → 看它红不红
→ 原样写回。全程在内存里留一份原始字节，跑完当场断言「确实还原了」。

## 「预期为绿」的条目也是结论

`M4b` 是我写错话的产物（详情见 `signalchain/console.py` 末节）：
我原以为「先 basicConfig 再 force_utf8」会失效，实测**不会** ——
`reconfigure()` 改的是**同一个** `sys.stderr` 对象。那条变异刻意留着，
哪天它变红就说明 Python 的 stdio 行为变了，那句解释得重写。

★ 它**会临时改写 signalchain/ 下的文件**（跑完立刻写回）。别在别人正在编辑时跑。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

# 输出编码必须抢在任何 print 之前 —— 本脚本要打 ★ / ✗，它们不在 GBK 里。
# 理由与实测记录见 `tools/sanitize_check.py` 顶部（本仓库唯一那份）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent   # 仓库根
TESTS = "tests/test_output_safety.py"

# (标签, 文件, 原片段, 改成, 测试节点, 期望红?)
MUTATIONS = [
    ("M1 裸 print 混进包里", "signalchain/runner.py",
     "    def summary(self) -> str:\n",
     "    def summary(self) -> str:\n        print(\"mutation probe\")\n",
     f"{TESTS}::test_package_never_writes_to_a_stream_directly", True),

    ("M2 用户可见字符串里加 emoji", "signalchain/runner.py",
     '"○ 队列清理 %d 条"', '"🎉 队列清理 %d 条"',
     f"{TESTS}::test_user_visible_strings_are_gbk_encodable", True),

    ("M2b 换成 GBK 编得出的符号（对照组）", "signalchain/runner.py",
     '"○ 队列清理 %d 条"', '"◆ 队列清理 %d 条"',
     f"{TESTS}::test_user_visible_strings_are_gbk_encodable", False),

    ("M3 emit 的兜底被拿掉", "signalchain/console.py",
     '        enc = getattr(stream, "encoding", None) or "utf-8"\n'
     '        stream.write(text.encode(enc, "replace").decode(enc, "replace"))\n',
     '        raise\n',
     f"{TESTS}::test_emit_survives_a_stream_that_rejects_the_text", True),

    ("M4a setup_logging 忘了掰流", "signalchain/console.py",
     '    force_utf8()\n    root = logging.getLogger()\n',
     '    root = logging.getLogger()\n',
     f"{TESTS}::test_logging_path_survives_an_emoji_that_the_programmer_did_not_write",
     True),

    ("M4b 顺序反了（预期绿：同一对象）", "signalchain/console.py",
     '    force_utf8()\n    root = logging.getLogger()\n'
     '    if not root.handlers:\n'
     '        logging.basicConfig(level=level, format=fmt, stream=sys.stderr)\n'
     '        return\n',
     '    root = logging.getLogger()\n'
     '    if not root.handlers:\n'
     '        logging.basicConfig(level=level, format=fmt, stream=sys.stderr)\n'
     '    force_utf8()\n'
     '    if not root.handlers:\n'
     '        return\n',
     f"{TESTS}::test_logging_path_survives_an_emoji_that_the_programmer_did_not_write",
     False),

    ("M6 setup_logging 不管已有 handler", "signalchain/console.py",
     '    for handler in root.handlers:\n'
     '        stream = getattr(handler, "stream", None)\n'
     '        if stream is not None:\n'
     '            force_utf8(stream)\n',
     '',
     f"{TESTS}::test_setup_logging_fixes_handlers_the_caller_already_installed", True),

    ("M5 演示入口不再掰流", "signalchain/demo.py",
     "    force_utf8()\n", "",
     f"{TESTS}::test_redirected_output_is_utf8_not_the_console_codepage", True),
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
        print(f"{'OK ' if ok else '★✗ '}{label:34s} → {'红' if red else '绿'}"
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
