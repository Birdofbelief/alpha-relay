# -*- coding: utf-8 -*-
"""风控闸门 —— 本仓库最值得复用的部分。

## 一条贯穿全部闸门的设计主张

    ★ 失效方向必须朝「不做」，而不是朝「照做」。

绝大多数系统的默认失效方向是「照做」：拿不到数据 → 跳过这项检查 → 继续跑。
在交易上有时候这在数学上是灾难。举个具体形状：某个「防止在极端价位买入」的
检查，在它拿不到数据时静默放行 —— 于是它唯一失效的方式，恰好是**在最需要它的
那一刻失效**。平时表现完美（因为平时数据都是好的），只在极端行情下裸奔。

所以本模块的三个闸门全部 fail-closed：数据不全时降级为「不做那件危险的事」，
而不是「当作检查通过」。

## 但「朝不做」不等于「一律停手」

三个闸门的方向**不一样**，因为代价不对称：

| 闸门 | 失效时 | 为什么 |
|---|---|---|
| 行情完整性 | **全部停**（买和卖都不做） | 价格本身不可信，任何决策都建立在假数据上 |
| 持仓对账 | **只禁买**，卖出照常 | 卖错有执行端「无持仓跳过」兜底；买错直接变双持仓 |
| 辅助数据 | **只禁开新仓**，卖出照常 | 少赚一次 vs 在无保护下满仓，代价不对称 |

判断依据始终是「哪一边错了更疼」，不是「哪边更保守」。
一律全停看着更安全，实际上会让你在数据小毛病时错过该有的止损。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

from .models import ACTION_BUY, Signal

log = logging.getLogger(__name__)


@dataclass
class DataQuality:
    """本轮的输入数据质量。由 Runner 在调用策略**之前**填好。

    它同时有两个消费者：
      - 闸门（决定要不要拦信号）
      - 策略（`ctx.data`，让策略自己知道「这一轮的数据可信度如何」）

    给策略看是刻意的：一个策略可能想在数据不全时改用更保守的逻辑，
    那是策略的判断；闸门只负责兜住**不管策略怎么想都不该发生**的事。
    """

    quote_ok: bool = True
    quote_failed_codes: list[str] = field(default_factory=list)

    positions_synced: bool = False
    positions_sync_reason: str = ""

    aux_data_ok: bool = True
    aux_missing: list[str] = field(default_factory=list)
    aux_reason: str = ""

    def quote_summary(self) -> str:
        """一句话描述行情状态。**它已经是完整的一句**，调用方不要再套一层
        「行情失效（…）」，否则会打印成「行情失效（行情失效(1只): …）」。"""
        if self.quote_ok:
            return "行情完整"
        codes = ",".join(self.quote_failed_codes[:5])
        more = "…" if len(self.quote_failed_codes) > 5 else ""
        return f"行情失效：{len(self.quote_failed_codes)} 只不可信（{codes}{more}）"


@dataclass
class Veto:
    """一条被拦下的信号，附带理由。理由会进日志，也可能进告警。"""

    signal: Signal
    gate: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.gate}] {self.signal.action} {self.signal.code}: {self.reason}"


class Gate(Protocol):
    """闸门接口。`check` 返回被**拒绝**的信号；放行的不用返回。"""

    name: str

    def check(self, signals: Sequence[Signal], data: DataQuality) -> list[Veto]:
        ...


class QuoteIntegrityGate:
    """行情不完整 → 本轮**不产生任何信号**（买卖都不做）。

    为什么买卖都停：价格是所有决策的输入。当一部分标的的实时价不可信时，
    「换仓」这类决策会拿可信的 A 和不可信的 B 做比较，选出来的结果没有意义。
    此时正确的动作是**这一轮什么都不做**，而不是「只做看起来安全的那一半」。

    注意区分两种「价格异常」，它们的含义相反：
      - 行情**缺失/NaN/非正** → 数据管道坏了 → 停（本闸门）
      - 价格**恰好是 0** 且行情字段完整 → 标的**停牌** → 只跳过这一只
    把停牌当成管道故障会让整个策略在有人停牌时瘫痪；
    把管道故障当成停牌会让策略拿乱码价格去下单。
    """

    name = "quote_integrity"

    def check(self, signals: Sequence[Signal], data: DataQuality) -> list[Veto]:
        if data.quote_ok:
            return []
        reason = f"{data.quote_summary()}，本轮不产生任何信号"
        return [Veto(s, self.name, reason) for s in signals]


class PositionsSyncedGate:
    """没能与执行端对账 → **禁止开新仓**，卖出/止损照常放行。

    这是三个闸门里最反直觉的一个：读不到真实持仓时，为什么卖出还能做？

    因为两者的失败代价完全不对称：
      - **卖出**错了 → 执行端发现自己没有这只持仓 → 跳过。
        最坏结果 = 该卖没卖成，下次再来。
      - **买入**错了 → 本地以为空仓、实际持有 A，于是买入 B → 变成同时持有 A 和 B。
        这不是「少赚」，是持仓结构被破坏，需要人工干预才能收拾。

    所以本地持仓在未知状态下，只有「减少持仓」的操作是安全的。
    """

    name = "positions_synced"

    def check(self, signals: Sequence[Signal], data: DataQuality) -> list[Veto]:
        if data.positions_synced:
            return []
        reason = (f"本交易日未完成与执行端的持仓对账"
                  f"（{data.positions_sync_reason or '原因未知'}），只卖不买")
        return [Veto(s, self.name, reason) for s in signals if s.action == ACTION_BUY]


class AuxDataGate:
    """策略所需的辅助数据集整体缺失 → **禁止开新仓**，卖出照常放行。

    「辅助数据」由策略定义：可以是溢价率、可以是财报、可以是任何外部因子。
    框架不关心它是什么，只关心**它没拿到的时候该怎么退化**。

    这里刻意不做成「单只缺失就禁全部」：单只缺失由策略在选股阶段排除那一只，
    是更精确的处理。本闸门只兜住「整体拿不到」这个更严重的情况 ——
    因为那时策略可能连哪些标的有问题都不知道。
    """

    name = "aux_data"

    def check(self, signals: Sequence[Signal], data: DataQuality) -> list[Veto]:
        if data.aux_data_ok:
            return []
        reason = (f"辅助数据不全（{data.aux_reason or '未说明'}），"
                  f"无保护状态下不开新仓")
        return [Veto(s, self.name, reason) for s in signals if s.action == ACTION_BUY]


class GateChain:
    """按顺序跑闸门，收集全部否决理由。

    刻意**不短路**：一条信号可能同时触发多个闸门，把理由都记下来，
    排障时才能看到全貌。短路会让「其实有两个问题」看起来像只有一个。
    """

    def __init__(self, gates: Iterable[Gate] | None = None) -> None:
        self.gates: list[Gate] = list(gates) if gates is not None else [
            QuoteIntegrityGate(),
            PositionsSyncedGate(),
            AuxDataGate(),
        ]

    def apply(self, signals: Sequence[Signal], data: DataQuality) -> tuple[list[Signal], list[Veto]]:
        """返回 `(放行的信号, 被拦的信号)`。"""
        vetoes: list[Veto] = []
        for gate in self.gates:
            vetoes.extend(gate.check(signals, data))

        # 按**对象身份**而非值来过滤。
        # Signal 是 dataclass（eq=True ⇒ 不可哈希），且字段完全相同的两条信号
        # 是两个不同的实体（同一时刻对同一标的下两次单），不能合并。
        # 所以这里必须用 id()，不要改成 set(信号) 或按字段比较。
        vetoed_ids = {id(v.signal) for v in vetoes}
        allowed = [s for s in signals if id(s) not in vetoed_ids]

        for v in vetoes:
            log.warning("⛔ 闸门拦下信号 %s", v)
        return allowed, vetoes
