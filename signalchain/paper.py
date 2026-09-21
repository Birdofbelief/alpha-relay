# -*- coding: utf-8 -*-
"""纸面执行器 —— 在本地把整条链路闭环，不需要券商、不需要凭据。

## 它为什么值得存在

一个「信号链路」框架如果只能把信号写进一个 JSON 就结束了，那它验证不了
自己最要紧的部分：信号写得对不对、幂等有没有生效、限价单会不会挂住、
持仓对账失败时闸门有没有真的拦住。这些都要**有另一端在动**才看得见。

纸面执行器就是那个另一端。它扮演执行端该扮演的全部角色：

    读信号 → 判断能不能成交 → 记账 → 回写回执 → 回写持仓

于是整条链路在 `py -m signalchain.demo` 下就能看到**完整闭环**，
而且全程零凭据、零网络（配合 `FakeMarketData`）。

## 它刻意保留的三个真实语义

**一、限价单会挂住，不是立刻成交。**
买单只有当市价 ≤ 限价时才成交，否则留在 `submitted`（挂单等待）。
这条不是细节 —— 它让「挂单未成交」这个真实状态在本地就能被复现，
从而把 `open_order_ids()` 那条防重复下单的逻辑也测到了。

**二、停牌不会成交，但订单仍然挂着。**
真实券商接受停牌标的的委托，单子就挂在那里。判成 `failed` 是错的 ——
它既没成功也没失败，而这两者的后续处理完全不同。

**三、手续费会吃掉小额订单。**
按「万 2.5、最低 5 元」计。最低 5 元这一条在小额订单上非常致命：
买 1000 元的 ETF，手续费 5 元 = 千分之五，**比很多策略的单次收益还高**。
不建模这一项的回测会系统性高估收益。

★ **本框架不做回测** —— 上面这句话是说给**你自己那套回测**听的。
框架与回测框架是正交的：把判断写成纯函数 `decide(ctx)`，回测那边写一层薄适配去调它，
这里调的是**同一个函数**。策略本体不写两遍，两边各写一次的只有适配层。
（理由见 `docs/DESIGN_NOTES.md` 第六节。）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config
from .marketdata.base import MarketDataError
from .models import (
    ACTION_BUY,
    ACTION_SELL,
    EXEC_CANCELED,
    EXEC_FILLED,
    EXEC_SKIPPED,
    EXEC_SUBMITTED,
    Execution,
    PositionSnapshot,
    Signal,
)
from .store import Stores
from .transport import Role, SingleWriterGuard, Transport

log = logging.getLogger(__name__)

#: 佣金费率与最低收费（券商常见档位）
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0


def commission(turnover: float) -> float:
    """一笔成交的佣金。**最低收费那一条在小额订单上是主要成本**，不是舍入误差。"""
    return max(turnover * COMMISSION_RATE, COMMISSION_MIN)


@dataclass
class Account:
    """纸面账户。"""

    cash: float = 100_000.0
    initial_cash: float = 100_000.0
    #: `{code: {"amount": int, "cost": float, "enable_amount": int}}`
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def held(self, code: str) -> int:
        return int(self.positions.get(code, {}).get("amount", 0) or 0)

    def sellable(self, code: str) -> int:
        """可卖数量。

        本实现里等于持有量 —— **A 股股票型 ETF 支持当日回转（T+0）**。
        如果你要做的是 T+1 的品种（股票、部分债券 ETF），必须把当日买入的部分
        从 `enable_amount` 里扣掉，否则会凭空多出当日可卖的量，
        表现为「你自己的回测里天天能日内止损，实盘却卖不掉」。
        """
        return int(self.positions.get(code, {}).get("enable_amount", 0) or 0)

    def buy(self, code: str, shares: int, price: float) -> float:
        """买入并返回现金变化（负数）。加权平均更新成本。"""
        turnover = shares * price
        fee = commission(turnover)
        old = self.positions.get(code, {"amount": 0, "cost": 0.0, "enable_amount": 0})
        new_amount = int(old["amount"]) + shares
        # 成本价含手续费 —— 不含的话会低估持仓成本，止盈点被系统性提前
        new_cost = ((float(old["cost"]) * int(old["amount"])) + turnover + fee) / new_amount
        self.positions[code] = {
            "amount": new_amount,
            "cost": round(new_cost, 4),
            "enable_amount": new_amount,
        }
        self.cash -= turnover + fee
        return -(turnover + fee)

    def sell(self, code: str, shares: int, price: float) -> float:
        """卖出并返回现金变化（正数）。全部卖出时清掉记录。"""
        turnover = shares * price
        fee = commission(turnover)
        old = self.positions.get(code, {"amount": 0, "cost": 0.0, "enable_amount": 0})
        left = int(old["amount"]) - shares
        if left <= 0:
            self.positions.pop(code, None)
        else:
            self.positions[code] = {
                "amount": left,
                "cost": float(old["cost"]),
                "enable_amount": max(0, int(old["enable_amount"]) - shares),
            }
        self.cash += turnover - fee
        return turnover - fee

    def snapshot(self, now: datetime) -> PositionSnapshot:
        # 市值用成本价估（纸面账户没有实时估值），并在日志里说明 ——
        # 把它当成真实市值会让人以为总资产在动，而实际上是静态的。
        market_value = sum(int(p["amount"]) * float(p["cost"])
                           for p in self.positions.values())
        return PositionSnapshot(
            positions={k: dict(v) for k, v in self.positions.items()},
            total_value=round(self.cash + market_value, 2),
            cash=round(self.cash, 2),
            updated_at=now,
        )


class PaperExecutor:
    """本地纸面执行端。与真实执行端只差「下单」那一步的实现。"""

    def __init__(
        self,
        config: Config,
        market,
        transport: Transport,
        initial_cash: float = 100_000.0,
    ) -> None:
        self.config = config
        self.market = market
        # 执行端角色 —— 它只能写 positions / executions，写 signals 会直接抛
        self.transport = SingleWriterGuard(transport, Role.EXECUTOR)
        self.stores = Stores(self.transport)
        self.account = Account(cash=initial_cash, initial_cash=initial_cash)
        self.stats = {"filled": 0, "submitted": 0, "skipped": 0}

    # ---------------- 持仓 ----------------

    def publish_positions(self, now: datetime) -> bool:
        """把持仓回写。**这是产生端能完成对账的唯一途径。**"""
        return self.stores.positions.write(self.account.snapshot(now))

    # ---------------- 信号处理 ----------------

    def open_order_ids(self) -> set[str]:
        """已有挂单（`submitted` 但未到终态）的 signal_id。

        这是**防重复下单的最后一道闸**：执行端记性不好（重启、丢回执），
        信号本身的幂等 id 是第一道，这里是第二道 —— 下单前先看看这只单子
        是不是已经挂在那里了。少了这道，一次重启就可能变成重复下单。
        """
        index = self.stores.executions.index()
        return {sid for sid, rec in index.items() if rec.status == EXEC_SUBMITTED}

    def poll_and_execute(self, now: datetime) -> list[Execution]:
        """读信号 → 逐条尝试成交 → 写回执。返回本轮产生的回执。"""
        signals = self.stores.signals.signals()
        if not signals:
            return []

        settled = self.stores.executions.index()
        open_orders = self.open_order_ids()
        produced: list[Execution] = []

        for sig in signals:
            prior = settled.get(sig.sig_id)
            if prior is not None and prior.status in (EXEC_FILLED, EXEC_CANCELED):
                # 已被消化 —— 幂等，静默跳过。这正是确定性 signal_id 的价值。
                continue
            if sig.sig_id in open_orders:
                log.info("● %s 已有挂单，不重复下单", sig.sig_id)
                continue

            execution = self._try_fill(sig, now)
            if execution is None:
                continue
            self.stores.executions.append(execution)
            produced.append(execution)
            self.stats[execution.status] = self.stats.get(execution.status, 0) + 1

        if produced:
            self.publish_positions(now)
        return produced

    def _try_fill(self, sig: Signal, now: datetime) -> Execution | None:
        """单条信号 → 回执。

        返回 `None` 表示**不该产生回执**（比如信号格式不认识），
        与「产生了 `skipped` 回执」不同 —— 后者是一个需要被记录的判定结果。
        """
        code, action = sig.code, sig.action

        # 1) 拿行情。停牌/取不到 → 挂单等待，**不是失败**。
        try:
            batch = self.market.quotes([code])
        except MarketDataError as e:
            return self._exec(sig, EXEC_SUBMITTED, f"行情暂不可用，挂单等待: {e}", now)
        quote = batch.quotes.get(code)
        if quote is None or not quote.is_finite:
            return self._exec(sig, EXEC_SUBMITTED, "行情不可信，挂单等待", now)
        if quote.is_halted:
            # 真实券商接受停牌委托，单子挂着。判 failed 是错的：它既没成功也没失败。
            return self._exec(sig, EXEC_SUBMITTED, "标的停牌，挂单等待", now)
        price = quote.price
        if not price:
            return self._exec(sig, EXEC_SUBMITTED, "最新价为 0，挂单等待", now)

        limit = float(sig.price or 0)

        if action == ACTION_BUY:
            # 限价约束：市价高于限价 → 挂着等，不追价。
            if limit > 0 and price > limit:
                return self._exec(sig, EXEC_SUBMITTED,
                                  f"市价 {price:.4f} 高于限价 {limit:.4f}，挂单等待", now)
            shares = int(sig.amount or 0)
            if shares <= 0:
                # 信号没给份数 → 按可用资金与限价反推（信号端不掌握账户余额）
                unit = limit if limit > 0 else price
                affordable = self.account.cash / (unit * (1 + COMMISSION_RATE))
                shares = int(affordable / 100) * 100
            if shares <= 0:
                return self._exec(sig, EXEC_SKIPPED, "资金不足，买不起 100 股", now)
            need = shares * price
            if need + commission(need) > self.account.cash:
                return self._exec(sig, EXEC_SKIPPED,
                                  f"资金不足（需 {need + commission(need):.2f}，"
                                  f"可用 {self.account.cash:.2f}）", now)
            self.account.buy(code, shares, price)
            return self._exec(sig, EXEC_FILLED, "", now,
                              order_id=f"paper-{sig.sig_id}", filled=shares,
                              fill_price=price)

        # --- 卖出 ---
        held = self.account.held(code)
        if held <= 0:
            # 这是 PositionsSyncedGate 允许卖出的**前提**：执行端会在这里兜住。
            return self._exec(sig, EXEC_SKIPPED, "账户无该标的持仓，跳过（不视为失败）", now)
        if limit > 0 and price < limit:
            return self._exec(sig, EXEC_SUBMITTED,
                              f"市价 {price:.4f} 低于限价 {limit:.4f}，挂单等待", now)
        shares = int(sig.amount or 0) or held     # amount=0 语义 = 全部卖出
        shares = min(shares, self.account.sellable(code))
        if shares <= 0:
            return self._exec(sig, EXEC_SKIPPED, "可卖数量为 0", now)
        self.account.sell(code, shares, price)
        return self._exec(sig, EXEC_FILLED, "", now,
                          order_id=f"paper-{sig.sig_id}", filled=shares,
                          fill_price=price)

    @staticmethod
    def _exec(sig: Signal, status: str, reason: str, now: datetime,
              order_id: str = "", filled: int = 0,
              fill_price: float = 0.0) -> Execution:
        """造一条回执。

        `Execution.to_dict()` 会**按 status 决定输出哪种形状**，所以这里可以
        把所有字段都填上 —— 非终态那条路会自己把 `order_id`/`price`/`amount` 丢掉。
        这样就不必在每个调用点重复「我现在是哪种形状」这个判断。
        """
        return Execution(
            signal_id=sig.sig_id, code=sig.code, action=sig.action,
            status=status, reason=reason, order_id=order_id,
            price=fill_price or float(sig.price or 0),
            amount=filled or int(sig.amount or 0), created_at=now,
        )

    # ---------------- 一步到位 ----------------

    def step(self, now: datetime) -> dict[str, int]:
        """发布持仓 → 处理信号 → 再发布持仓。演示与测试里的一次「嘀嗒」。"""
        self.publish_positions(now)
        produced = self.poll_and_execute(now)
        counts: dict[str, int] = {}
        for e in produced:
            counts[e.status] = counts.get(e.status, 0) + 1
        return counts

    def describe(self) -> str:
        return (f"paper(现金 {self.account.cash:.2f}/"
                f"持仓 {len(self.account.positions)}只/"
                f"成交 {self.stats.get(EXEC_FILLED, 0)})")
