# -*- coding: utf-8 -*-
"""调度器的失败语义，以及失败重试时的**幂等**。

## 这一组防的是什么

`run_round` 把异常写进 `result.error` 就返回（单轮失败不该杀进程），
而 `run_due` 原来只看「有没有抛」——于是默认路径上：

  - 任务天天失败，`executed_tasks` 照记 ⇒ **当天不再重试**
  - 心跳照报 alive ⇒ 监控不会响
  - 「连续失败上限」永远不触发 ⇒ 那三条日志永远不会出现

三条合起来的结果是：**进程活着、心跳正常、任务天天失败**，
而终端上没有任何痕迹。这正是本仓库反复在防的静默失效。

## 第二条：修它不能把重复下单放回来

一旦失败真的会重试，`decide` 就会再跑一遍。如果信号 id 用的是**实际**时刻，
重试会拿到新 id，执行端的去重失效 ⇒ 同一笔卖出被推两次。
所以 id 锚在**计划时刻**上 —— 下面有两条测试钉住它。
"""
from __future__ import annotations

import pytest
from datetime import date, datetime

from signalchain.calendar import weekday_calendar
from signalchain.chain import build_chain
from signalchain.config import Config
from signalchain.marketdata.fake import FakeMarketData
from signalchain.models import (ACTION_BUY, PositionSnapshot, Signal,
                                payload_to_signals)
from signalchain.strategy import Strategy
from signalchain.transport.file import FileTransport

from .conftest import CLEAN_CODES, FIXED_NOW

SCHEDULE = [("14:45", "afternoon")]
TASK = "afternoon"
PLANNED = datetime(2026, 9, 18, 14, 45, 0)
DAY = FIXED_NOW.strftime("%Y-%m-%d")


# ------------------------------------------------------------
# 策略替身
# ------------------------------------------------------------

class _Quiet(Strategy):
    name = "quiet"

    def universe(self):
        return list(CLEAN_CODES)

    def decide(self, ctx):
        return []


class _Boom(Strategy):
    name = "boom"

    def universe(self):
        return list(CLEAN_CODES)

    def decide(self, ctx):
        raise RuntimeError("策略炸了")


class _Emit(Strategy):
    name = "emit"

    def universe(self):
        return list(CLEAN_CODES)

    def decide(self, ctx):
        return [ctx.signal(CLEAN_CODES[0], ACTION_BUY, amount=100)]


# ------------------------------------------------------------
# 装配
# ------------------------------------------------------------

def _chain(tmp_path, strategy, *, fail_limit=3, stale_days=0,
           with_executor=False):
    config = Config(runtime_dir=tmp_path / "rt",
                    task_fail_limit=fail_limit,
                    max_position_stale_days=stale_days)
    return build_chain(
        config,
        strategy=strategy,
        market=FakeMarketData(list(CLEAN_CODES)),
        transport=FileTransport(tmp_path / "ch"),
        calendar=weekday_calendar(date(2026, 1, 1), date(2026, 12, 31)),
        console=False,
        schedule=SCHEDULE,
        with_executor=with_executor,
    )


# ------------------------------------------------------------
# 失败 = 失败
# ------------------------------------------------------------

def test_a_failed_round_is_not_recorded_as_success(tmp_path):
    runner = _chain(tmp_path, _Boom()).runner

    runner.run_due(FIXED_NOW)

    assert runner._fail_count[TASK] == 1                    # noqa: SLF001
    assert TASK not in runner.state.executed(DAY), \
        "失败了却记成「今日已执行」—— 当天不会再重试，而且没人看得出来"


def test_repeated_failures_retry_until_the_limit_then_give_up(tmp_path):
    runner = _chain(tmp_path, _Boom(), fail_limit=3).runner

    for _ in range(2):
        runner.run_due(FIXED_NOW)
    assert TASK not in runner.state.executed(DAY)

    runner.run_due(FIXED_NOW)          # 第 3 次 = 到上限
    assert TASK in runner.state.executed(DAY), "到上限必须显式放弃当天，否则每 30 秒刷一次"


def test_a_successful_round_is_still_marked_executed(tmp_path):
    runner = _chain(tmp_path, _Quiet()).runner

    runner.run_due(FIXED_NOW)

    assert TASK in runner.state.executed(DAY)
    assert runner._fail_count.get(TASK, 0) == 0             # noqa: SLF001


def test_the_round_result_is_still_returned_on_failure(tmp_path):
    """失败也要把 RoundResult 交出去 —— 调用方需要它来打日志/汇总。"""
    runner = _chain(tmp_path, _Boom()).runner

    results = runner.run_due(FIXED_NOW)

    assert len(results) == 1
    assert results[0].error


# ------------------------------------------------------------
# 信号 id 锚在计划时刻
# ------------------------------------------------------------

def _emit_chain(tmp_path, strategy=None):
    """带执行端、且持仓已对账 —— 否则闸门会把买入拦下，测不到 id。"""
    chain = _chain(tmp_path, strategy or _Emit(), with_executor=True)
    chain.executor.stores.positions.write(PositionSnapshot(updated_at=FIXED_NOW))
    return chain


def test_signal_id_uses_the_planned_time_not_the_actual_time(tmp_path):
    runner = _emit_chain(tmp_path).runner
    late = datetime(2026, 9, 18, 14, 47, 30)      # 计划 14:45，晚了 2 分半

    runner.run_round(late, task=TASK, planned_at=PLANNED)

    signals = runner.stores.signals.signals()
    assert len(signals) == 1
    assert signals[0].sig_id.startswith("20260918144500_")
    # 而 `time` 字段仍然是**实际**时刻 —— 它记录的是「什么时候做的决定」。
    assert signals[0].created_at == late


def test_a_retry_gets_the_same_signal_id(tmp_path):
    """★ 这一条是「修好失败语义」的安全带。

    重试会重新跑一遍 `decide`，于是会再造一条信号。id 必须与第一次相同，
    否则执行端的幂等键失效 —— 一笔卖出会被推两次。
    """
    runner = _emit_chain(tmp_path).runner

    runner.run_round(PLANNED, task=TASK, planned_at=PLANNED)
    runner.run_round(datetime(2026, 9, 18, 14, 46, 10), task=TASK, planned_at=PLANNED)

    signals = runner.stores.signals.signals()
    # 队列本身按 id 去重（`SignalQueue.append`），所以第二遍只会在日志里留一行
    # 「已在队列中，跳过」。★ 真正要紧的是**下面那半句**：两次拿到的 id 相同。
    # 如果 id 随实际时刻走，这里会变成两条不同 id 的信号，执行端各下一单。
    assert len(signals) == 1
    assert signals[0].sig_id == "20260918144500_510050.XSHG_buy"


def test_manual_runs_without_a_plan_still_use_the_actual_time(tmp_path):
    """没有计划时刻（手工 run_round）时行为不变 —— 锚退回 created_at。"""
    runner = _emit_chain(tmp_path).runner

    runner.run_round(PLANNED)

    sig = runner.stores.signals.signals()[0]
    assert sig.sig_id.startswith("20260918144500_")    # 这一次两者恰好相同


# ------------------------------------------------------------
# 持仓新鲜度开关
# ------------------------------------------------------------

def _write_positions(chain, updated_at):
    chain.executor.stores.positions.write(
        PositionSnapshot(updated_at=updated_at))


def test_stale_positions_are_accepted_by_default(tmp_path):
    chain = _chain(tmp_path, _Quiet(), with_executor=True)
    _write_positions(chain, datetime(2026, 9, 1, 15, 0))

    ctx = chain.runner.build_context(FIXED_NOW)

    assert ctx.data.positions_synced is True, "默认（0）必须保持旧行为，一个字都不能变"


def test_stale_positions_are_rejected_when_the_switch_is_on(tmp_path):
    chain = _chain(tmp_path, _Quiet(), with_executor=True, stale_days=1)
    _write_positions(chain, datetime(2026, 9, 1, 15, 0))

    ctx = chain.runner.build_context(FIXED_NOW)

    assert ctx.data.positions_synced is False
    assert "落后" in ctx.data.positions_sync_reason


def test_fresh_positions_pass_with_the_switch_on(tmp_path):
    chain = _chain(tmp_path, _Quiet(), with_executor=True, stale_days=1)
    _write_positions(chain, FIXED_NOW.replace(hour=9, minute=5))

    ctx = chain.runner.build_context(FIXED_NOW)

    assert ctx.data.positions_synced is True


def test_a_snapshot_without_a_timestamp_is_stale_only_when_the_switch_is_on(tmp_path):
    """★ 这一条直接打 `_positions_staleness`，不走落盘。

    原因是 `PositionSnapshot.to_dict()` 在 `updated_at=None` 时会**补上当前时刻**
    ——落盘再读回来永远有时间戳，构造不出这个形态。补时刻本身是对的（线格式
    要求这个字段），但它也意味着：从文件读到的快照，新鲜度只能信 `update_time`。
    """
    chain = _chain(tmp_path, _Quiet(), with_executor=True, stale_days=1)
    ctx = chain.runner.build_context(FIXED_NOW)
    bare = PositionSnapshot(updated_at=None)

    assert chain.runner._positions_staleness(ctx, bare)       # noqa: SLF001
    assert "时间戳" in chain.runner._positions_staleness(ctx, bare)  # noqa: SLF001

    off = _chain(tmp_path / "off", _Quiet(), with_executor=True).runner
    off_ctx = off.build_context(FIXED_NOW)
    assert off._positions_staleness(off_ctx, bare) == ""       # noqa: SLF001


# ------------------------------------------------------------
# 锚不依赖「策略作者用了 ctx.signal()」
# ------------------------------------------------------------
class _RawEmit(Strategy):
    """直接构造 `Signal(...)` —— 公开 API，文档只是建议用 `ctx.signal()`。"""

    name = "raw"

    def universe(self):
        return list(CLEAN_CODES)

    def decide(self, ctx):
        return [Signal(code=CLEAN_CODES[0], action=ACTION_BUY, amount=100,
                       created_at=ctx.now)]


def test_a_directly_constructed_signal_is_anchored_too(tmp_path):
    """★ 这条堵的是「绕开 `ctx.signal()` 就丢锚」的漏洞。

    `Signal` 是公开导出的，策略可以自己 `Signal(...)`。如果调度器只信
    `ctx.signal()` 带进来的锚，这种策略在重试时会换 id ⇒ 执行端去重失效。
    把关口放在「所有策略出口都过一遍这里」，比要求每个策略作者自觉可靠。
    """
    runner = _emit_chain(tmp_path, _RawEmit()).runner

    runner.run_round(PLANNED, task=TASK, planned_at=PLANNED)
    runner.run_round(datetime(2026, 9, 18, 14, 46, 10), task=TASK, planned_at=PLANNED)

    signals = runner.stores.signals.signals()
    assert len(signals) == 1
    assert signals[0].sig_id.startswith("20260918144500_")


def test_an_explicitly_given_id_is_never_rewritten(tmp_path):
    """别人指定过的 id 是别人定的幂等键 —— 调度器不许改写。"""
    class _Fixed(Strategy):
        name = "fixed"

        def universe(self):
            return list(CLEAN_CODES)

        def decide(self, ctx):
            return [Signal(code=CLEAN_CODES[0], action=ACTION_BUY, amount=100,
                           created_at=ctx.now, sig_id="别人给的-id")]

    runner = _emit_chain(tmp_path, _Fixed()).runner
    runner.run_round(PLANNED, task=TASK, planned_at=PLANNED)

    assert runner.stores.signals.signals()[0].sig_id == "别人给的-id"


# ------------------------------------------------------------
# 派发：按签名判断，不按对象身份
# ------------------------------------------------------------
def test_a_subclassed_run_round_still_receives_the_planned_time(tmp_path):
    """★ 继承 Runner 并重写 `run_round` 是很正常的事。

    原来的身份判断（`fn.__func__ is Runner.run_round`）一遇到重写就为假，
    `planned_at` 悄悄丢掉，幂等锚退回实际时刻 —— 又变成重复下单。
    """
    from signalchain.runner import Runner

    seen: list = []

    class _Sub(Runner):
        def run_round(self, now, task="manual", planned_at=None):
            seen.append(planned_at)
            return super().run_round(now, task=task, planned_at=planned_at)

    config = Config(runtime_dir=tmp_path / "rt")
    runner = _Sub(config, _Quiet(), FakeMarketData(list(CLEAN_CODES)),
                  FileTransport(tmp_path / "ch"),
                  weekday_calendar(date(2026, 1, 1), date(2026, 12, 31)),
                  schedule=SCHEDULE)
    runner.register(TASK, runner.run_round)

    runner.run_due(FIXED_NOW)

    assert seen == [PLANNED]


def test_a_plain_custom_task_is_still_called_with_only_now(tmp_path):
    """自定义任务保持 `fn(now)` 的公开契约，不被塞参数。"""
    runner = _chain(tmp_path, _Quiet()).runner
    got: list = []

    def task(now):
        got.append(now)
        return None

    runner._dispatch(TASK, task, FIXED_NOW, PLANNED)       # noqa: SLF001

    assert got == [FIXED_NOW]


# ------------------------------------------------------------
# 缺 id 的信号：不能再被凭空补一个「现在」
# ------------------------------------------------------------
def test_a_signal_without_an_id_raises_instead_of_inventing_one():
    from signalchain.models import WireFormatError

    with pytest.raises(WireFormatError) as info:
        payload_to_signals([{"code": CLEAN_CODES[0], "action": ACTION_BUY,
                             "amount": 100}])

    assert "id" in str(info.value)


def test_a_signal_with_an_id_still_round_trips():
    sig = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW)
    back = payload_to_signals([sig.to_dict()])[0]
    assert back.sig_id == sig.sig_id
