# -*- coding: utf-8 -*-
"""风控闸门 —— 本仓库最值得复用的部分，也是最需要测试钉住的部分。

这组测试守的核心性质只有一条：**失效方向**。
三个闸门在数据不全时**不是**一律停手，而是各自朝一个方向退化：

    行情失效     → 买卖全停
    未对账       → 只禁买，卖出照常
    辅助数据缺失 → 只禁开新仓，卖出照常

「一律全停」看着更安全，实际会让你在数据小毛病时错过该有的止损 ——
所以这些测试断言的是**差异**，不是「拦住了」。
"""
from __future__ import annotations

import pytest

from signalchain.gates import (
    AuxDataGate,
    DataQuality,
    GateChain,
    PositionsSyncedGate,
    QuoteIntegrityGate,
)
from signalchain.models import ACTION_BUY, ACTION_SELL, Signal

from .conftest import CLEAN_CODES, FIXED_NOW


def _sig(code: str, action: str) -> Signal:
    return Signal(code=code, action=action, amount=100, created_at=FIXED_NOW)


@pytest.fixture
def buy():
    return _sig(CLEAN_CODES[0], ACTION_BUY)


@pytest.fixture
def sell():
    return _sig(CLEAN_CODES[0], ACTION_SELL)


@pytest.fixture
def healthy() -> DataQuality:
    """一切正常。每个用例**只制造一个问题**，其余项显式置为正常 ——
    否则多个闸门同时触发，就看不出是哪一道拦下的了。"""
    return DataQuality(quote_ok=True, positions_synced=True, aux_data_ok=True)


# ------------------------------------------------------------
# 行情完整性：买卖全停
# ------------------------------------------------------------
def test_quote_gate_stops_everything_when_quotes_are_bad(buy, sell):
    """行情失效 → **买入和卖出都不做**。

    价格是所有决策的输入。拿可信的 A 和不可信的 B 比较来选股，结果没有意义；
    更糟的是「用乱码价格去卖出止损」。此时唯一正确的动作是这一轮什么都不做。
    """
    dq = DataQuality(quote_ok=False, quote_failed_codes=[CLEAN_CODES[0]])
    vetoes = QuoteIntegrityGate().check([buy, sell], dq)
    assert {v.signal.action for v in vetoes} == {ACTION_BUY, ACTION_SELL}


def test_quote_gate_passes_when_quotes_are_complete(buy, sell, healthy):
    assert QuoteIntegrityGate().check([buy, sell], healthy) == []


def test_quote_gate_reason_is_a_complete_sentence(buy):
    """理由必须自成一句，调用方**不再套一层**。

    回归测试：初版 reason 是「行情失效(1只)：…」，调用方又包了一层
    「行情失效（…）」，日志里出现「行情失效（行情失效(1只): …）」。
    告警文本是给人看的，套娃会让人怀疑告警本身坏了。
    """
    dq = DataQuality(quote_ok=False, quote_failed_codes=[CLEAN_CODES[0]])
    summary = dq.quote_summary()
    assert summary.startswith("行情失效：")
    assert "行情失效（行情失效" not in QuoteIntegrityGate().check([buy], dq)[0].reason


def test_quote_summary_truncates_long_lists():
    """失败的标的很多时不能把整张清单塞进日志 —— 只列前几个并标出省略。

    注意断言的是「省略号出现过」而不是「以省略号结尾」：省略号在收尾
    括号**里面**。这种位置细节不值得写进契约，被它绊住只会让测试变脆。
    """
    dq = DataQuality(quote_ok=False,
                     quote_failed_codes=[f"{100000 + i}" for i in range(20)])
    summary = dq.quote_summary()
    assert "20 只" in summary
    assert "…" in summary
    assert "100005" not in summary          # 第 6 个及以后不再列出


# ------------------------------------------------------------
# 持仓对账：只禁买
# ------------------------------------------------------------
def test_positions_gate_blocks_only_buys(buy, sell):
    """未对账 → **禁止开新仓，卖出/止损照常**。

    代价不对称是这里唯一的判据：
      - 卖错了 → 执行端发现「没有这只持仓」→ 跳过。最坏是「该卖没卖成，下次再来」。
      - 买错了 → 本地以为空仓、实际持有 A，于是买入 B → **同时持有 A 和 B**。
        这不是少赚，是持仓结构被破坏，得人工收拾。
    """
    dq = DataQuality(positions_synced=False, positions_sync_reason="读不到执行端持仓")
    vetoes = PositionsSyncedGate().check([buy, sell], dq)
    assert [v.signal.action for v in vetoes] == [ACTION_BUY]


def test_positions_gate_reason_carries_the_cause(buy):
    """理由里要带上**为什么**没对账上 —— 排障时「未对账」三个字没有信息量。"""
    dq = DataQuality(positions_synced=False, positions_sync_reason="Gist 读取超时")
    assert "Gist 读取超时" in PositionsSyncedGate().check([buy], dq)[0].reason


def test_positions_gate_says_unknown_when_reason_missing(buy):
    dq = DataQuality(positions_synced=False, positions_sync_reason="")
    assert "原因未知" in PositionsSyncedGate().check([buy], dq)[0].reason


# ------------------------------------------------------------
# 辅助数据：只禁开新仓
# ------------------------------------------------------------
def test_aux_gate_blocks_only_buys(buy, sell):
    """辅助数据整体缺失 → 禁止**开新仓**，不是禁止一切。

    「少赚一次」和「在无保护状态下满仓」代价不对称。注意这里是
    「整体拿不到」才触发；单只缺失由策略在选股阶段排除那一只，那更精确。
    """
    dq = DataQuality(aux_data_ok=False, aux_reason="行业分类接口超时")
    vetoes = AuxDataGate().check([buy, sell], dq)
    assert [v.signal.action for v in vetoes] == [ACTION_BUY]


def test_aux_gate_passes_when_data_is_present(buy, sell, healthy):
    assert AuxDataGate().check([buy, sell], healthy) == []


# ------------------------------------------------------------
# 闸门链
# ------------------------------------------------------------
def test_chain_partitions_signals_by_identity():
    """放行/拦下按**对象身份**划分，不按值。

    这条得用一个「只拦其中一条」的自定义闸门才测得出来：
    造两条**字段完全相同**的信号，闸门只否决其中一条。
    按身份过滤 → 另一条放行；按值过滤（`set(信号)` 或字段比较）→ 两条一起没了。

    为什么这个区别要紧：字段相同的两条信号是两个**不同的实体**
    （同一时刻对同一标的下两次单），合并它们等于悄悄吃掉一笔单。
    Signal 是 dataclass 且 eq=True ⇒ 不可哈希，正是为了让「按值去重」这条路走不通。
    """
    from signalchain.gates import Veto

    first = _sig(CLEAN_CODES[0], ACTION_BUY)
    second = _sig(CLEAN_CODES[0], ACTION_BUY)
    assert first == second and first is not second   # 值相等，但是两个实体

    class VetoOnlyFirst:
        name = "veto_first"

        def check(self, signals, data):
            return [Veto(signals[0], self.name, "只拦第一条（测试用）")]

    allowed, vetoes = GateChain([VetoOnlyFirst()]).apply([first, second], DataQuality())
    assert len(vetoes) == 1
    assert allowed == [second] and len(allowed) == 1


def test_chain_passes_everything_when_data_is_healthy(buy, sell, healthy):
    allowed, vetoes = GateChain().apply([buy, sell], healthy)
    assert allowed == [buy, sell] and vetoes == []


def test_chain_does_not_short_circuit(buy):
    """一条信号同时触发多个闸门时，**理由要全部记下**。

    短路会让「其实有两个问题」看起来像只有一个，排障时按修掉的那一个处理完之后
    问题依旧存在，而日志再也不会提到第二个原因。
    """
    dq = DataQuality(quote_ok=False, quote_failed_codes=[CLEAN_CODES[0]],
                     positions_synced=False, aux_data_ok=False)
    _, vetoes = GateChain().apply([buy], dq)
    assert {v.gate for v in vetoes} == {"quote_integrity", "positions_synced", "aux_data"}


def test_chain_does_not_mutate_input(buy, sell, healthy):
    """闸门只做判断，不改信号。"""
    before = [buy.to_dict(), sell.to_dict()]
    GateChain().apply([buy, sell], healthy)
    assert [buy.to_dict(), sell.to_dict()] == before


def test_chain_accepts_custom_gates():
    """闸门表可以替换（例如只想跑行情闸门）。"""
    class RejectAll:
        name = "reject_all"

        def check(self, signals, data):
            from signalchain.gates import Veto
            return [Veto(s, self.name, "全部拒绝（测试用）") for s in signals]

    chain = GateChain([RejectAll()])
    allowed, vetoes = chain.apply([_sig(CLEAN_CODES[0], ACTION_BUY)], DataQuality())
    assert allowed == [] and len(vetoes) == 1


# ------------------------------------------------------------
# 「停牌」与「管道故障」必须区分
# ------------------------------------------------------------
def test_halted_quote_is_not_a_pipeline_failure():
    """停牌（价格 0、字段完整）与管道故障（缺失/NaN）含义相反。

    这条测试放在闸门文件里，是因为它守的是闸门的**输入分类**：
    把停牌当管道故障 → 任一标的停牌时整个策略瘫痪；
    把管道故障当停牌 → 拿乱码价格下单。
    真实的分类逻辑在 `marketdata.base`，这里断言这两个属性不会互相污染。
    """
    from signalchain.marketdata.base import Quote

    halted = Quote(code=CLEAN_CODES[0], price=0.0, prev_close=1.0,
                   open=0.0, high=0.0, low=0.0)
    dirty = Quote(code=CLEAN_CODES[0], price=None, prev_close=None,
                  open=None, high=None, low=None)

    assert halted.is_finite and halted.is_halted        # 停牌：数值可信
    assert not dirty.is_finite and not dirty.is_halted  # 脏值：**不算停牌**
    assert halted.pct_change is None                    # 停牌算不出涨跌幅，不硬编 0
