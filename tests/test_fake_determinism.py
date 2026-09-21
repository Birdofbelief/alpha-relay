# -*- coding: utf-8 -*-
"""假行情必须真的确定性 —— 而且**跨进程**也确定。

## 为什么需要单独一组

`FakeMarketData` 自己写着「刻意做成确定性」：整套测试能离线跑、能快速造出
停牌/NaN/除权跳变这类难等场景，都建立在「同一份输入永远给同一份输出」之上。

这条性质曾经是**假的**。它用内建 `hash()` 给每个标的算价格偏移：

    offset = (hash(bare) % 100) / 1000.0

而 **CPython 对 str 的 `hash()` 每个进程都不一样**（PYTHONHASHSEED 随机化，
防哈希碰撞攻击）。于是：

- 同一份演示连跑三次，**三次挑中三只不同的 ETF**；
- 任何断言「策略选中了 X」的测试都有**三分之二的概率红**；
- 而它红得毫无规律 —— 重跑一次就绿，于是被当成 flaky，加个重试糊过去，
  **而真正的原因（价格每次都在变）永远不会被发现**。

## ★ 为什么这组必须跨进程

**在同一个进程里测不出这个 bug。** 一个进程内 `hash()` 是稳定的常数，
所以「连调两次，结果一样」永远通过。要让随机化暴露出来，必须换一个进程、
而且还得**换一个 PYTHONHASHSEED**。

所以下面那条测试真的起子进程 —— 它比较慢（约一秒），但这是唯一能测到点的做法。
**另一条测试钉死具体数值**，作为第二道：任何改动只要让价格变了，它就红，
不必依赖子进程。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from signalchain.marketdata.fake import FakeMarketData

CODES = ["510050", "510500", "159919"]

#: ★ 钉死的期望值。**它一旦变了就说明「同一份输入给同一份输出」被破坏了。**
#: 不要为了让它变绿而改这几个数 —— 那正是这条测试存在的意义。
PINNED = {"510050": 1.034, "510500": 1.104, "159919": 1.057}

#: 子进程里跑的脚本：打印三个标的的价格。
_CHILD = """
import json
from signalchain.marketdata.fake import FakeMarketData
m = FakeMarketData(%r, drift=0.002)
print(json.dumps({c: m.quotes([c]).quotes[c].price for c in %r}))
""" % (CODES, CODES)


def _prices_in_fresh_process(seed: str) -> dict[str, float]:
    """在**新进程**里取一次价格。`seed` 决定该进程的 PYTHONHASHSEED。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    env["PYTHONPATH"] = str(root)      # 子进程不继承 pytest 的 pythonpath
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"子进程失败（PYTHONHASHSEED={seed}）:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_price_is_pinned_to_an_exact_value():
    """价格必须**逐值**等于钉死的期望 —— 变一点就说明确定性被破坏了。"""
    market = FakeMarketData(CODES, drift=0.002)
    actual = {c: market.quotes([c]).quotes[c].price for c in CODES}
    assert actual == PINNED, (
        "假行情的价格变了。若这是你有意调整的，请一并更新 PINNED；"
        "**若你没动过 generator，那就是「确定性」被破坏了** ——\n"
        f"  期望 {PINNED}\n  实际 {actual}")


def test_same_prices_in_a_fresh_process_with_a_different_hash_seed():
    """★ 核心那条：**换进程、换 hash 种子，价格必须一模一样。**

    这一条会在「用 `hash()` 算偏移」时红 —— 而进程内的任何测试都不会。
    """
    a = _prices_in_fresh_process("0")
    b = _prices_in_fresh_process("1")

    assert a == b, (
        "同一个标的在两个不同 PYTHONHASHSEED 的进程里价格不同 —— "
        "假行情不是确定性的。\n"
        "最可能的原因：某处用了内建 `hash()` 去派生数值。它对 str 是**每进程随机**的，"
        "必须换成稳定哈希（`zlib.crc32` / `hashlib`）。\n"
        f"  seed=0 → {a}\n  seed=1 → {b}")
    assert a == PINNED, (
        f"跨进程一致，但与钉死的期望值不符：期望 {PINNED}，实际 {a}")
