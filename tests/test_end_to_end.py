# -*- coding: utf-8 -*-
"""端到端：整条链在**零凭据、零网络**下真的跑通，而且各层的衔接是对的。

## 为什么单独一组

前面每个测试文件都在验一个部件「本身对不对」。这一组验的是**接缝**：

    策略 → 闸门 → 信号队列 → 执行端 → 回执 → 持仓回流 → 下一轮对账

接缝上的错误有个共同特征：**每个部件单独看都正确**。信号写对了、回执写对了、
持仓读对了 —— 但顺序反了、或者中间某一步把 `None` 当成了空值，
于是整条链给出一个错的结果，而没有任何一个部件的测试会红。

本仓库真踩过这个形状：纸面执行端写的持仓 key 是 `.XSHG`，而线格式（也是
既有执行端）用 `.SS` —— 每个文件自己的测试都过，因为测试是**手写 key** 传进去的。
所以这里断言的是**真的落了盘的那份字节**，不是对象。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from signalchain.chain import build_file_chain
from signalchain.models import (
    ACTION_BUY,
    ACTION_SELL,
    CH_EXECUTIONS,
    CH_POSITIONS,
    CH_SIGNALS,
    CHANNEL_FILENAMES,
    EXEC_FILLED,
    EXEC_SUBMITTED,
    Execution,
    PositionSnapshot,
    payload_to_signals,
)
from signalchain.strategy import Strategy

from .conftest import CLEAN_CODES, FIXED_NOW

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(chain, channel: str):
    """直接读**磁盘上的字节** —— 不经过任何解析器。

    故意的：这一组要验的就是「落盘格式对不对」。走解析器读回来，
    会把「写错了」和「读错了」混在一起，而两者要分开定位。
    """
    path = Path(chain.config.channel_dir) / CHANNEL_FILENAMES[channel]
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _chain(tmp_path, **kw):
    return build_file_chain(tmp_path / "run", console=False, **kw)


# ------------------------------------------------------------
# 闭环
# ------------------------------------------------------------
def test_full_round_trip_closes_the_loop(tmp_path):
    """一轮走完，四个 channel 都该有东西，而且互相自洽。"""
    chain = _chain(tmp_path)
    result, fills = chain.step(FIXED_NOW)

    assert result.ok, result.error
    assert result.emitted > 0, "演示策略在这组输入下应当产出信号"

    # 1) 信号落了盘，且格式与线上一致
    signals = _read(chain, CH_SIGNALS)
    assert signals, "signals channel 应当非空"
    for s in signals:
        assert set(s) == {"id", "code", "action", "amount", "price",
                          "time", "signal_type", "processed"}
        assert s["processed"] is False
        assert "." in s["code"], "信号里的 code 必须带交易所后缀"

    # 2) 执行端产生了回执，且**至少有一条成交**
    executions = _read(chain, CH_EXECUTIONS) or []
    assert executions, "执行端应当至少产生一条回执"
    assert any(e["status"] == EXEC_FILLED for e in executions), \
        f"这一轮应当有成交，实际回执：{executions}"

    # 3) 持仓回流，而且是 PT 形式的后缀（线格式）
    positions = _read(chain, CH_POSITIONS)
    assert positions and positions["positions"], "执行端应当回写持仓"
    for key in positions["positions"]:
        assert key.endswith((".SS", ".SZ")), \
            f"positions 的 key 必须是 PT 形式，实际 {key!r}"
        assert not key.endswith((".XSHG", ".XSHE")), key

    # 4) 心跳也写了
    assert _read(chain, "heartbeat") is not None


def test_executor_reports_positions_before_decision(tmp_path):
    """★ 顺序不变量：执行端**先报持仓**，产生端才能完成对账。

    这个顺序是 `Chain.step` 里唯一一处「顺序错了会静默改变结果」的地方：
    反过来的话，第一轮里 `PositionsSyncedGate` 会因为读不到持仓而拦下所有买入 ——
    而表现只是「今天没买」，不会报错。

    这里用「第一轮就有买入信号被放行」来反向证明顺序是对的。
    """
    chain = _chain(tmp_path)
    result, _ = chain.step(FIXED_NOW)

    buy_vetoes = [v for v in result.vetoes if v.gate == "positions_synced"]
    assert not buy_vetoes, (
        "第一轮就因持仓未对账而禁买 —— 说明执行端没有先报持仓（Chain.step 顺序反了）")
    assert result.allowed > 0


def test_two_rounds_settle_and_are_idempotent(tmp_path):
    """第二轮不该重复下同一笔单（回执 + 队列去重共同保证）。"""
    chain = _chain(tmp_path)
    chain.step(FIXED_NOW)
    first = _read(chain, CH_EXECUTIONS)

    chain.step(FIXED_NOW.replace(hour=15))
    second = _read(chain, CH_EXECUTIONS)

    # 回执按 (signal_id, status) 去重，所以同一个 signal_id 的 filled 只有一条
    filled = [e for e in second if e["status"] == EXEC_FILLED]
    ids = [e["signal_id"] for e in filled]
    assert len(ids) == len(set(ids)), f"同一笔成交出现了重复回执：{ids}"
    assert len(second) >= len(first)


def test_replay_produces_identical_signal_ids(tmp_path):
    """同一天重放两次，信号 id 必须完全一致 —— 可回放性的落地检验。

    分别在两个目录里各跑一遍。id 里若掺了墙上时钟，这里就会错开。
    """
    ids = []
    for i in range(2):
        chain = _chain(tmp_path / f"run{i}")
        chain.step(FIXED_NOW)
        ids.append([s["id"] for s in _read(chain, CH_SIGNALS)])
    assert ids[0] == ids[1]
    assert ids[0], "没有信号的话这条测试什么也没验到"


# ------------------------------------------------------------
# ★ 接缝：持仓读不出来 → 只卖不买（跨四层的联动）
# ------------------------------------------------------------
class _AlwaysBoth(Strategy):
    """永远同时发一条买入和一条卖出。

    故意不用演示策略：它要排名、要历史、还依赖上一轮的状态，
    很容易在某些输入下**一条信号都不发** —— 于是「闸门拦下了它」这类断言
    会因为「根本没有信号可拦」而通过（假绿）。用这个策略，
    每一轮都必然有一条 buy 和一条 sell，断言才有意义。
    """

    name = "always_both"

    def universe(self):
        return [CLEAN_CODES[0]]

    def decide(self, ctx):
        return [
            ctx.signal(CLEAN_CODES[0], ACTION_BUY, amount=100, price=1.0),
            ctx.signal(CLEAN_CODES[0], ACTION_SELL, amount=100, price=1.0),
        ]


def _run_round_only(chain, now):
    """只跑产生端那一轮，**不让执行端先报持仓**。

    `Chain.step` 会先 `publish_positions` —— 那会把我们刚伪造的「坏持仓」
    覆盖掉，于是测不到我们想测的东西。这里直接调 `run_round`，
    精确地模拟「产生端单独运行、读到一份它读不懂的持仓」这个场景。
    """
    return chain.runner.run_round(now, task="step")


def test_unreadable_positions_blocks_buys_but_allows_sells_end_to_end(tmp_path):
    """★ 从「格式认不出」一路穿到「闸门拦下买入」的完整联动。

    这条断言把四个层次串起来验：

        PositionSnapshot.from_dict 抛 WireFormatError
          → PositionStore.read 返回 None（未知）
          → Runner 置 positions_synced=False
          → PositionsSyncedGate 拦下 buy、放行 sell

    任何一层改成「宽容处理」（比如把异常吞掉、返回空快照），这条测试都会红。
    而那种宽容看起来无害，方向却是反的：读不懂持仓时反而放心开新仓。

    先跑一次**正常**的，确认买和卖都能过 —— 否则「被拦下」可能只是因为
    这一轮压根没有买入信号，那种绿是假的。
    """
    from signalchain.models import CH_POSITIONS as _CH
    from signalchain.transport.base import Role, SingleWriterGuard
    from signalchain.transport.file import FileTransport

    chain = _chain(tmp_path, strategy=_AlwaysBoth())
    executor = SingleWriterGuard(
        FileTransport(chain.config.channel_dir), Role.EXECUTOR)

    # 基线：持仓正常 → 买卖都放行
    executor.write(_CH, PositionSnapshot(
        positions={f"{CLEAN_CODES[0]}.XSHG": {"amount": 100}}, cash=1.0).to_dict())
    baseline = _run_round_only(chain, FIXED_NOW)
    assert baseline.allowed == 2, f"基线应当两条都放行：{baseline.summary()}"

    # 现在把持仓写成一个「认不出」的形状（模拟执行端换了格式而我们没跟上）
    executor.write(_CH, {"positions": ["这不是一个合法的持仓表"]})

    result = _run_round_only(chain, FIXED_NOW)
    assert result.emitted == 2, "策略仍然发了两条信号，问题只出在持仓"

    vetoed_actions = {v.signal.action for v in result.vetoes}
    assert "buy" in vetoed_actions, "持仓未知时必须拦下买入"
    assert "sell" not in vetoed_actions, "持仓未知不构成禁止卖出的理由"
    assert result.allowed == 1


def test_missing_positions_file_also_blocks_buys(tmp_path):
    """与上一条同向，但走「文件根本不存在」这条路。

    两条路径（格式错误 / 文件缺失）在实现上分别由 `WireFormatError` 和
    `payload is None` 触发，所以必须各测一次 —— 只测一条的话，
    另一条可能根本没接到闸门上。
    """
    chain = _chain(tmp_path, strategy=_AlwaysBoth())
    assert chain.runner.stores.positions.read() is None   # 从没写过

    result = _run_round_only(chain, FIXED_NOW)
    vetoed_actions = {v.signal.action for v in result.vetoes}
    assert "buy" in vetoed_actions
    assert "sell" not in vetoed_actions
    assert result.allowed == 1


# ------------------------------------------------------------
# 零凭据
# ------------------------------------------------------------
def test_file_chain_needs_no_credentials(tmp_path, monkeypatch):
    """★ 清空所有相关环境变量后，整条链仍然跑得通。

    这是「开源仓库零凭据」的**可执行证明**：不是「我们检查过没有硬编码」，
    而是「把环境里所有可能提供凭据的东西都拿掉，它照样端到端跑完」。
    """
    for var in ("SC_GIST_ID", "SC_GITHUB_TOKEN", "SC_SERVERCHAN_KEY",
                "GIST_ID", "GITHUB_TOKEN", "SERVERCHAN_SENDKEY"):
        monkeypatch.delenv(var, raising=False)

    chain = _chain(tmp_path)
    result, _ = chain.step(FIXED_NOW)
    assert result.ok, result.error
    assert result.written > 0

    # 而且后端身份里不该出现任何像凭据的东西
    described = chain.guarded_transport.describe()
    assert "token" not in described.lower()
    assert "gist" not in described.lower()


def test_demo_entry_point_returns_zero(tmp_path):
    """`python -m signalchain.demo` 的入口函数返回 0（可被 CI 断言）。"""
    import signalchain.demo as demo

    assert demo.run_demo(keep=False, quiet=True) == 0


def test_documented_command_survives_a_non_utf8_console(tmp_path):
    """★ 文档里那条命令，必须在一个**不是 UTF-8 的 stdout** 下也能跑完。

    上面那条入口测试证明不了这件事，而且它的盲区很典型：

      - 它用 `run_demo(quiet=True)`，把**日志**关掉了 —— 但真正崩的是
        `run_demo` 里的**演示内容打印**（那条 `⇒` 不在 GBK 里）。
      - 它在本进程内调用，而 pytest 捕获 stdout 用的是 UTF-8 ⇒ **进程内怎么调都不复现**。
      - 开发机设了 `PYTHONUTF8=1` ⇒ 让环境顺着漏进来，这条测试在本机**永远绿**，
        而「别人 clone 下来在 Windows 上跑」照样崩。

    ⇒ 所以这条必须**起子进程**，并且**显式摘掉 PYTHONUTF8 / PYTHONIOENCODING**。
    实测形状（2026-09-21，干净克隆、未修复前）：
        `python -m signalchain.demo`（本机 Git Bash，或重定向到文件）
        → UnicodeEncodeError: 'gbk' codec can't encode character '⇒' → 退出码 1
    详见 signalchain/demo.py 里 main() 顶部那段。
    """
    # ★ 启动器就用**跑测试的这个解释器**，不挑。
    #
    #   被测的是「那个模块 + 非 UTF-8 控制台」，启动器从来不是被测对象；
    #   而 `sys.executable` 是唯一**被证明装好了依赖**的解释器 —— 它就是正在跑
    #   pytest 的这个（依赖不齐的话本文件顶部的 import 早就红了）。
    #
    #   这里原来写的是 `shutil.which("py") or sys.executable`，理由是「别让没有
    #   py 的机器整条跳过」。2026-09-22 CI 第一次真跑就现形：windows runner 上
    #   `py` **存在**、却指向另一个没装 requests/pandas 的 Python ⇒ 一批测试全红，
    #   报的是「缺少运行时依赖：pandas」—— 一条与代码无关的**机器属性**被拉进了
    #   断言。挑启动器＝把「这台机器上 py 指向谁」放进测试，而它每台机器都不同
    #   （CI 上 setup-python 装包的是 `python`，`py` 是 runner 自带的另一套）。
    #
    # ★ 这里刻意**不**用 `python`：在 Windows 上 `python` 经常是微软商店的占位程序 ——
    #   本机实测 `shutil.which("python")` 指到 `…\WindowsApps\python.EXE`，
    #   执行它是**退出码 49 + 零输出**（静默失败），换上去这条会假红。
    #   文档正文写 `python -m` 是因为它在 macOS / Linux 上才是通用写法，
    #   README 的「快速上手」里另有一段说明 Windows 上该用 `py`。
    launcher = sys.executable
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    proc = subprocess.run(
        [launcher, "-m", "signalchain.demo", "--quiet"],
        capture_output=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=env)

    assert proc.returncode == 0, (
        f"退出码 {proc.returncode} —— 文档里那条命令在非 UTF-8 的 stdout 下崩了。\n"
        f"stdout={proc.stdout[-2000:]!r}\nstderr={proc.stderr[-2000:]!r}")
    assert proc.stdout.strip(), (
        "零输出 = 和「通过」无法区分。演示不许有安静的失效路径。")


# ------------------------------------------------------------
# 环境变量与这个入口：不许静默换策略
# ------------------------------------------------------------
# ★ 标的代码从 `CLEAN_CODES` 取，不随手写：我第一版随手敲了一个真实宽基代码，
#   当场被 `test_sanitize_rules.py::test_whole_repo_passes_the_scanner` 拦下 ——
#   它在作者自备的黑名单里。后来把它写进**注释**里说明这件事，**又**被拦了一次：
#   扫描器看的是全文，注释不例外，而注释恰恰是最容易顺手抄进公开文档的地方。
#   所以这里只留纪律、不留那个代码。纪律见 conftest 顶部。
_PRIVATE_STRATEGY = f'''\
# -*- coding: utf-8 -*-
"""假装是用户放在仓库外的那份 alpha。"""
from signalchain import Strategy


class MyPrivateStrategy(Strategy):
    name = "MY_PRIVATE"

    def universe(self):
        return [{CLEAN_CODES[0]!r}]

    def decide(self, ctx):
        return []
'''


def test_env_var_never_silently_swaps_the_strategy(tmp_path, monkeypatch, caplog):
    """设了 `SC_STRATEGY_PATH` 的人走这个入口，**必须听得到**这次是谁在跑。

    这个入口按设计不读环境变量（=「零凭据、零网络」承诺的来源），所以它不会
    采纳那个路径。但**沉默**会骗人，实测 2026-09-22（设同一个环境变量）：

        build_chain()       → MY_PRIVATE（采纳）
        build_file_chain()  → demo_basic（内置示例，**且没有任何输出**）

    于是「我设了环境变量」被读成「我的策略进来了」，接下来他会去怀疑策略、
    怀疑框架 —— 而两条都不是问题所在。这正是 `Missing is not zero` 的形状。

    断言两件事**同时**成立，缺一不可：
      ① 有 warning 说清楚「本入口不读环境变量 / 本次跑的是 demo_basic / 是哪个路径」；
      ② 链的形状**一点没变** —— 只出声，不采纳。
    ②是关键：把 warning 顺手换成「那就采纳吧」也能让 ① 变绿，但那是另一个东西 ——
    采纳之后用户环境里的 `SC_TRANSPORT` / `SC_ROLE` 会跟着渗进来，
    「跑通即说明链路完整」就不成立了。
    """
    priv = tmp_path / "my_private.py"
    priv.write_text(_PRIVATE_STRATEGY, encoding="utf-8")
    monkeypatch.setenv("SC_STRATEGY_PATH", str(priv))

    with caplog.at_level(logging.WARNING):
        chain = _chain(tmp_path)

    assert chain.strategy.name == "demo_basic", (
        f"这个入口本该只认显式传入的 strategy=，却拿到了 "
        f"{chain.strategy.name!r} —— 「零凭据」承诺就是靠「不读环境变量」成立的")

    said = [r.getMessage() for r in caplog.records]
    hit = [m for m in said if "SC_STRATEGY_PATH" in m]
    assert hit, (
        "设了 SC_STRATEGY_PATH 却一声不吭：用户会把「我设了环境变量」读成"
        "「我的策略进来了」，然后去查策略或框架。\n"
        f"本次日志：{said}")
    assert "不读环境变量" in hit[0] and "demo_basic" in hit[0], (
        f"这条 warning 没把「谁在跑、为什么不是你的策略」说全：{hit[0]!r}")
    assert str(priv) in hit[0], (
        f"没报出用户设的那个路径，他会去翻自己到底写错了哪 —— 实测报错指向一个"
        f"用户没写过的路径比不报更坏：{hit[0]!r}")

    # ③ 对照：环境干净时**不许出声**。一条会在正常用法下也响的 warning，
    #    结局是所有人学会忽略它 —— 那等于又变回静默。
    caplog.clear()
    monkeypatch.delenv("SC_STRATEGY_PATH")
    with caplog.at_level(logging.WARNING):
        _chain(tmp_path)
    noise = [m for m in (r.getMessage() for r in caplog.records)
             if "SC_STRATEGY_PATH" in m]
    assert not noise, f"环境里没有这个变量，却警告了：{noise}"
