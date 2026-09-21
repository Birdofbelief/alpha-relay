# -*- coding: utf-8 -*-
"""执行端契约 —— 「换执行端不用动产生端」这句话的**证据**。

## 这套测试在证明什么

`docs/EXECUTOR_GUIDE.md` 教人接自己的券商执行端。教材最容易犯的错是**它没被跑过**：
读者照着写完，装不进链、或者装进去了但从来没被调用 —— 而这两种失败**都不报错**。
链照常跑完，只是「什么都没发生」，跟「今天没有信号」长得一模一样。

所以这里不测文档，测**行为**：写一个最小的自定义执行端，真的注入进链、
真的跑完一轮、它写的东西真的被产生端读到。

## 与 `test_transport_contract.py` 的区别，以及为什么

那边是「同一套用例跑遍所有后端」，因为后端有一族、且彼此可替换。
执行端只有一个内置实现（`PaperExecutor`），所以「跑遍所有实现」在这里没有意义。

这里要证明的是另一件事：**第三方写的执行端能被装进去。** 所以被测对象
刻意**不继承任何东西** —— 因为真实用法正是如此：你的执行端多半已经有基类了
（券商 SDK 的适配层、你自己项目里的 `Broker`），框架没理由逼你再继承一个。
"""
from __future__ import annotations

from datetime import date

import pytest

from signalchain import build_chain, build_file_chain
from signalchain.calendar import weekday_calendar
from signalchain.config import Config
from signalchain.executor import Executor
from signalchain.marketdata import FakeMarketData
from signalchain.models import (
    ACTION_SELL,
    CH_EXECUTIONS,
    CH_POSITIONS,
    CH_SIGNALS,
    EXEC_CANCELED,
    EXEC_FILLED,
    Execution,
    PositionSnapshot,
)
from signalchain.paper import PaperExecutor
from signalchain.store import Stores
from signalchain.transport import FileTransport, Role, SingleWriterGuard
from signalchain.transport.base import OwnershipError

from .conftest import CLEAN_CODES, FIXED_NOW


# ------------------------------------------------------------
# 三个「不合规/不做事」的最小对象 —— 用来验这道检查本身有效
# ------------------------------------------------------------
class _MissingStep:
    """少一个方法。`step` 是契约三件套里最要紧的那个 —— 只有它真的动钱。"""

    def publish_positions(self, now) -> bool:
        return True

    def describe(self) -> str:
        return "missing-step"


class _WrongSignature:
    """方法名全对、**签名全错** —— `isinstance` 照样判它合格。

    ★ 这不是缺陷，是必须写下来的机制边界：`typing.Protocol` 的
    `@runtime_checkable` **只数方法名在不在**，不做签名比对。
    所以那道 `isinstance` 是顺手的自检，**不是保证**。
    真正的保证是下面那组「真的装进去、真的跑一轮」。
    """

    def publish_positions(self) -> bool:
        return True

    def step(self) -> dict:
        return {}

    def describe(self) -> str:
        return "wrong-signature"


class _NullExecutor:
    """最小合格执行端：什么也不做。只用来验注入通道本身。"""

    def publish_positions(self, now) -> bool:
        return True

    def step(self, now) -> dict[str, int]:
        return {}

    def describe(self) -> str:
        return "null-executor"


# ------------------------------------------------------------
# 照着指南写出来的那个执行端
# ------------------------------------------------------------
class _RecordingExecutor:
    """`docs/EXECUTOR_GUIDE.md` 里那份骨架的可运行版本。

    它**不继承任何东西**，也不 import 任何基类 —— 这就是要证明的点。
    行为上它是最简的「照单全收」：信号给什么价就按什么价成交，自己记一份持仓、
    把回执写回去。真实的券商适配层要做的是把这几步换成券商 API 调用，形状一样。

    ★ 注意 `__init__` 里那两行 —— 协议管不到构造函数，
    而这两行是**必须**的（理由见 `signalchain/executor.py` 顶部）。
    """

    def __init__(self, config, market, transport) -> None:
        self.config = config
        self.market = market
        self.transport = SingleWriterGuard(transport, Role.EXECUTOR)
        self.stores = Stores(self.transport)
        self.holdings: dict[str, int] = {}
        #: 记下每次被调用，好让测试能断言「真的被调到了」。
        self.calls: list[str] = []

    def publish_positions(self, now) -> bool:
        self.calls.append("publish_positions")
        return self.stores.positions.write(PositionSnapshot(
            positions={code: {"amount": n, "cost": 1.0, "enable_amount": n}
                       for code, n in self.holdings.items()},
            total_value=0.0, cash=0.0, updated_at=now))

    def step(self, now) -> dict[str, int]:
        self.calls.append("step")
        settled = self.stores.executions.index()
        counts: dict[str, int] = {}

        for sig in self.stores.signals.signals():
            prior = settled.get(sig.sig_id)
            if prior is not None and prior.status in (EXEC_FILLED, EXEC_CANCELED):
                continue                     # ★ 幂等：已到终态的不再处理

            if sig.action == ACTION_SELL:
                # 卖出信号 amount=0 是「全部卖出」的语义 —— 与 PaperExecutor 一致
                shares = self.holdings.pop(sig.code, 0)
                if shares <= 0:
                    continue
            else:
                shares = int(sig.amount or 0)
                if shares <= 0:
                    continue
                self.holdings[sig.code] = self.holdings.get(sig.code, 0) + shares

            self.stores.executions.append(Execution(
                signal_id=sig.sig_id, code=sig.code, action=sig.action,
                status=EXEC_FILLED, reason="",
                order_id=f"recording-{sig.sig_id}",
                price=float(sig.price or 0.0), amount=shares, created_at=now))
            counts[EXEC_FILLED] = counts.get(EXEC_FILLED, 0) + 1

        if counts:
            self.publish_positions(now)
        return counts

    def describe(self) -> str:
        return f"recording(持仓 {len(self.holdings)}只)"


# ------------------------------------------------------------
# 装配
# ------------------------------------------------------------
def _fake_market():
    return FakeMarketData(list(CLEAN_CODES), drift=0.002)


def _calendar():
    return weekday_calendar(date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture
def chain_and_executor(tmp_path):
    """装一条链，把执行端换成 `_RecordingExecutor`。

    ★ 执行端拿到的是**自己的一份后端句柄**（另一个 `FileTransport`，
    指向同一个 channel 目录），与链内部那份是两个对象 —— 这正是真实部署的样子：
    两端是两个进程、两份句柄、同一处存储。图省事用同一个对象，
    也就测不到「跨进程也能对上账」这件事了。
    """
    config = Config(role="producer", transport="file",
                    runtime_dir=tmp_path / "runtime")
    config.ensure_dirs()
    executor = _RecordingExecutor(config, None,
                                  FileTransport(config.channel_dir))
    chain = build_chain(
        config, strategy="demo", market=_fake_market(), calendar=_calendar(),
        executor=executor, console=False, schedule=[],
    )
    return chain, executor


# ------------------------------------------------------------
# 一、协议本身：结构化，不靠继承
# ------------------------------------------------------------
def test_a_class_that_does_not_inherit_anything_still_qualifies(chain_and_executor):
    """★ 没有继承、没有 import 基类，`isinstance` 照样合格。

    这是用 Protocol 而不是 ABC 的全部意义：你的执行端可能已经有基类了。
    """
    _, executor = chain_and_executor
    assert isinstance(executor, Executor)


def test_the_builtin_paper_executor_satisfies_the_same_protocol(tmp_path):
    """内置的那个也合格 —— 否则「同一个契约」是句空话。"""
    chain = build_file_chain(tmp_path / "r", console=False)
    assert isinstance(chain.executor, PaperExecutor)
    assert isinstance(chain.executor, Executor)


def test_the_protocol_check_rejects_a_missing_method():
    """★ **测这道检查本身** —— 上面那两条证不了它有效。

    一个从不拒绝任何东西的 `isinstance`，和一条通过了的测试在终端上长得一样。
    """
    assert not isinstance(_MissingStep(), Executor)


def test_the_protocol_check_only_looks_at_method_names():
    """★ 把机制边界写下来：签名错了它**照样放行**。

    知道这条，才不会把 `isinstance` 当成保证 —— 它只是顺手的自检。
    真正确认「插得进去」的，是下面那组真的跑一轮的测试。
    """
    probe = _WrongSignature()
    assert isinstance(probe, Executor)               # 它不合格，但检查放行了
    with pytest.raises(TypeError):
        probe.publish_positions(FIXED_NOW)           # 链就是这么调的，它会炸


# ------------------------------------------------------------
# 二、注入：真的装进去了，而且没被换掉
# ------------------------------------------------------------
def test_the_custom_executor_is_what_the_chain_actually_holds(chain_and_executor):
    """注入的那个就是链手里的那个 —— **没被内置纸面执行端顶掉。**

    顶掉的后果很隐蔽：链照常跑、回执照常写，只是写的人不是你。
    而你会以为自己的券商适配层在跑。
    """
    chain, executor = chain_and_executor
    assert chain.executor is executor


def test_the_custom_executor_is_really_called(chain_and_executor):
    """★ 跑一轮，两个入口都必须被调到。

    顺序不能反，理由见 `docs/ARCHITECTURE.md` 第三节：执行端**先报持仓**，
    产生端才能完成对账、`PositionsSyncedGate` 才会放行买入。少了第一次
    `publish_positions`，这一轮的买单会被闸门全部拦掉 —— 而那是**正确**行为，
    所以测试不会红，只是什么都不成交。这正是要盯住顺序的原因。
    """
    chain, executor = chain_and_executor
    chain.step(FIXED_NOW)

    assert executor.calls[0] == "publish_positions", (
        f"第一件事必须是报持仓，实际顺序 {executor.calls}")
    assert "step" in executor.calls


def test_the_round_really_produced_signals(chain_and_executor):
    """★ 先确认这一轮**真的有信号流过**，否则上面那条什么也没验到。

    「执行端没被调用」和「这一轮没信号所以不必调用」，在断言上长得一模一样。
    所以要么断言 `emitted > 0`，要么这条测试就是摆设。
    """
    chain, executor = chain_and_executor
    result, _ = chain.step(FIXED_NOW)

    assert result.ok, result.error
    assert result.emitted > 0, f"没有信号流过：{result.summary()}"
    assert sum(executor.holdings.values()) > 0, (
        f"执行端一份持仓都没建 —— 信号没到它手上：{executor.holdings}")


def test_the_fills_come_from_the_injected_executor(chain_and_executor):
    """`chain.step()` 第二个返回值，就是**你的** `step()` 的返回值。"""
    chain, _ = chain_and_executor
    _, fills = chain.step(FIXED_NOW)

    assert fills.get(EXEC_FILLED, 0) > 0
    assert set(fills) == {EXEC_FILLED}, (
        f"这个执行端只该产生 filled，实际 {fills}")


def test_the_executor_shows_up_in_describe(chain_and_executor):
    """`describe()` 会进日志与心跳 —— 换成自己的执行端之后必须看得见。"""
    chain, executor = chain_and_executor
    assert executor.describe() in chain.describe()


# ------------------------------------------------------------
# 三、闭环：它写的东西，产生端真的读得到
# ------------------------------------------------------------
def test_what_the_executor_writes_is_what_the_producer_reads(chain_and_executor):
    """★ 整套东西的意义所在：执行端写的回执与持仓，产生端**从存储里**读得到。

    刻意读 `chain.guarded_transport`（重新走一遍真实的反序列化），而不是读
    执行端自己的内存字段 —— 内存里对不对外面看不出来，链路对不上账就是白搭。
    """
    chain, _ = chain_and_executor
    chain.step(FIXED_NOW)

    receipts = chain.guarded_transport.read(CH_EXECUTIONS)
    assert receipts, "回执一个都没落盘"
    assert all(str(r["order_id"]).startswith("recording-") for r in receipts), (
        "落盘的回执不是自定义执行端写的 —— 说明链上跑的还是内置那个")

    snapshot = chain.guarded_transport.read(CH_POSITIONS)
    assert snapshot is not None, "读不到持仓 —— 产生端将无法完成对账（会被禁买）"
    assert snapshot["positions"], "持仓是空的"


def test_the_executor_cannot_write_the_producers_channels(chain_and_executor):
    """★ 构造时那两行 guard **真的在做事**，不是仪式。

    执行端拿到的句柄写 `signals` 会当场抛。这条约束靠的是执行端自己在
    `__init__` 里包 guard —— 协议管不到构造函数。所以这里把「包了会怎样」
    变成一次可见的抛出，而不是靠文档里写一句「请务必」。
    """
    _, executor = chain_and_executor
    with pytest.raises(OwnershipError):
        executor.transport.write(CH_SIGNALS, [])


# ------------------------------------------------------------
# 四、两种指定方式互斥，框架不猜
# ------------------------------------------------------------
def test_executor_and_with_executor_together_are_refused(tmp_path):
    """两个都给 → 直接报错。**不许静默挑一个。**

    与「策略文件里有多个 `Strategy` 子类必须显式指定」同一条纪律：
    静默选中一个的代价不是报错，是你以为跑的是 A、实际跑的是 B。
    """
    config = Config(role="producer", transport="file",
                    runtime_dir=tmp_path / "runtime")
    config.ensure_dirs()

    with pytest.raises(ValueError, match="互斥"):
        build_chain(
            config, strategy="demo", market=_fake_market(),
            calendar=_calendar(), executor=_NullExecutor(),
            with_executor=True, console=False,
        )


def test_with_executor_alone_still_builds_the_paper_one(tmp_path):
    """反方向：只给 `with_executor=True` 时，旧的用法一字不改地继续有效。"""
    config = Config(role="producer", transport="file",
                    runtime_dir=tmp_path / "runtime")
    chain = build_chain(
        config, strategy="demo", market=_fake_market(), calendar=_calendar(),
        with_executor=True, console=False,
    )
    assert isinstance(chain.executor, PaperExecutor)
