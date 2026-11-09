# -*- coding: utf-8 -*-
"""channel 的领域化读写 —— read-modify-write 全部收在这一层。

## 为什么要单独一层

`Transport` 只会「整体读」和「整体写」。但链路真正需要的是这些操作：

    往信号队列追加一条
    把已被执行/已过期的信号从队列里摘掉
    取每个信号的最新回执
    读执行端报上来的真实持仓

这些操作都是 read-modify-write，而且每一步都有**领域规则**
（清理哪些、保留哪些、取哪条）。把它们散在各个调用点，
规则就会各写一份、迟早分叉 —— 这正是这个文件要解决的问题。

## ★ 保真原则：回写时不许丢掉你不认识的字段

本层的所有 RMW 都在**原始 dict** 上做增删，不经过 dataclass 往返。

这条不是洁癖，是契约问题：链路的两个端可以独立升级。执行端某天多写一个
`retry_count` 字段，产生端如果读进来 → 转成 dataclass → 写回去，
那个字段就被**静默抹掉**了。执行端会以为自己的字段丢了，行为变得不可预测，
而且是间歇性的（只在产生端恰好在那一刻读写时才发生）。

所以：**只碰你知道的字段，其余原样带过。**
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from .models import (
    CH_EXECUTIONS,
    CH_HEARTBEAT,
    CH_POSITIONS,
    CH_SIGNALS,
    Heartbeat,
    PositionSnapshot,
    Signal,
    WireFormatError,
    executions_to_index,
    payload_to_signals,
    terminal_signal_ids,
)
from .transport.base import SingleWriterGuard, TransportError

log = logging.getLogger(__name__)

#: signals 队列上限。超出后只保留最新的一批，防止文件无限膨胀。
#: 取 100 的理由：正常一天最多几条信号，100 意味着「即使清理连续失败好几周也还够用」，
#: 同时单个文件的体积仍在 gist 的舒适区内。
SIGNAL_QUEUE_MAX = 100

#: executions 日志上限。回执是只增的，不清会一直涨。
EXECUTION_LOG_MAX = 500


class SignalQueue:
    """信号队列。产生端写，执行端读。"""

    def __init__(self, transport: SingleWriterGuard, max_items: int = SIGNAL_QUEUE_MAX) -> None:
        self._t = transport
        self._max = max_items

    def raw(self) -> list[dict[str, Any]]:
        """原始列表。**保留全部字段**，供需要精确保真的场景使用。

        ★ 非列表形状是**文件坏了**，不是「空队列」。若在这里静默返回 []，
        随后的 append / prune 会拿空列表做 read-modify-write，把整份队列
        覆盖掉 —— 信号凭空消失，而日志显示「成功」。
        """
        payload = self._t.read(CH_SIGNALS)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise WireFormatError(
                f"信号队列顶层不是列表（{type(payload).__name__}） —— "
                f"拒绝在此基础上读写，避免覆盖原内容")
        return [item for item in payload if isinstance(item, dict)]

    def signals(self) -> list[Signal]:
        """解析成对象。只读用途，不要拿它的结果去回写。"""
        return payload_to_signals(self._t.read(CH_SIGNALS))

    def append(self, signal: Signal) -> bool:
        """追加一条。读—改—写。

        并发安全性完全依赖「本 channel 只有一个写入者」这一前提
        （由 SingleWriterGuard 在运行期保证）。这不是可以放松的地方。
        """
        items = self.raw()
        new = signal.to_dict()
        # 幂等保护：同一个 sig_id 已在队列里就不重复追加。
        # 信号 id 是确定性的（时间戳+代码+动作），所以重跑同一个任务时
        # 这里会自然去重 —— 这正是 id 设计成确定性的目的。
        if any(item.get("id") == new["id"] for item in items):
            log.info("信号 %s 已在队列中，跳过（幂等）", new["id"])
            return True
        items.append(new)
        if len(items) > self._max:
            items = items[-self._max:]
        return self._t.write(CH_SIGNALS, items)

    def prune(self, terminal_ids: set[str] | None, today: str) -> int:
        """清理队列，返回删掉的条数。规则有两条，**每条都有理由**：

        1. **曾经到过终态的回执的删掉**（filled 成交 / canceled 撤单放弃）。
           注意 `submitted`（已下单、结果未知）**不删** —— 那不是终态，
           删了就等于单子还挂着、你却忘了它。
           ★ 判定用「曾经出现过终态」，不是「最新一条是终态」：执行端可能在
           终态之后补写一条非终态（次日重试写 `skipped`），只看最新一条会把
           已经结束的单重新判成没结束，见 `models.terminal_signal_ids`。
        2. **跨日的一律删掉，买卖同规则**。信号是「某个时刻的决定」，隔天再执行
           等于拿昨天的判断去下今天的单：卖出是用昨天的价卖今天的持仓；买入则
           可能把刚止损的票按当前价买回来。
           ★ 这里曾经**刻意保留跨日买单**，理由是「signal_id 幂等能拦住重复下单」。
           那个理由只对终态成立：执行端的 `skipped` / `failed` 都不是终态，
           一条没成交的买单会永久留在队列里，等某天资金 / 持仓腾出来被执行。
           （来源：2026-11-01 实盘链路 v7.1 的孤儿买入事故。）
        """
        items = self.raw()
        if not items:
            return 0
        terminal_ids = terminal_ids or set()
        keep: list[dict[str, Any]] = []
        removed = 0
        for item in items:
            sid = item.get("id", "")
            if sid in terminal_ids:
                removed += 1
                continue
            sig_day = str(item.get("time", ""))[:10]
            if sig_day and sig_day != today:
                removed += 1
                continue
            keep.append(item)
        if removed:
            if not self._t.write(CH_SIGNALS, keep):
                # 清理没写成功 ⇒ 队列其实没变。不能报「已删 N 条」：
                # 调用方会以为清干净了，而队列仍带着旧信号（跨日单尤其危险）。
                log.warning("× 队列清理写回失败，队列保持不变（本次未删除任何信号）")
                return 0
        return removed


class ExecutionLog:
    """执行回执日志。执行端写，产生端读。"""

    def __init__(self, transport: SingleWriterGuard, max_items: int = EXECUTION_LOG_MAX) -> None:
        self._t = transport
        self._max = max_items

    def raw(self) -> list[dict[str, Any]]:
        payload = self._t.read(CH_EXECUTIONS)
        if payload is None:
            return []
        if not isinstance(payload, list):
            # ★ 回执读坏了绝不能当空：空回执 = 「这些单都没执行过」，
            # 执行端据此会重新下单。宁可整次读取失败（上层 fail-closed）。
            raise WireFormatError(
                f"回执列表顶层不是列表（{type(payload).__name__}） —— "
                f"拒绝当作「没有任何回执」")
        return [item for item in payload if isinstance(item, dict)]

    def index(self) -> dict[str, Any]:
        """`{signal_id: 最新一条回执}`。回答「这笔单现在是什么状态」。"""
        return executions_to_index(self._t.read(CH_EXECUTIONS))

    def terminal_ids(self) -> set[str]:
        """`{曾经到过终态的 signal_id}`。回答「这笔单结束过没有」。

        与 `index()` 不能互相替代：一笔单可能先 `filled`、之后又追加一条
        `skipped`（次日重试时执行端写的）。`index()` 会把它当成非终态，
        `terminal_ids()` 仍然认它是终态。队列清理必须用后者。
        """
        return terminal_signal_ids(self._t.read(CH_EXECUTIONS))

    def append(self, execution: Any) -> bool:
        """追加一条回执。

        同一 `(signal_id, status)` 只保留最新的一条 —— 否则重试会把日志刷满重复项。
        不同 status 之间**都保留**（submitted → filled 是两条，历史要留着）。
        """
        record = execution.to_dict() if hasattr(execution, "to_dict") else dict(execution)
        items = self.raw()
        items = [
            r for r in items
            if not (r.get("signal_id") == record.get("signal_id")
                    and r.get("status") == record.get("status"))
        ]
        items.append(record)
        if len(items) > self._max:
            items = items[-self._max:]
        return self._t.write(CH_EXECUTIONS, items)


class PositionStore:
    """真实持仓。执行端写，产生端只读。

    ★ **执行端的持仓是唯一真相源。** 产生端自己记的持仓是缓存，
    随时可能因为「卖了但没成交」「被人工干预」「部分成交」而与真实情况不一致。
    读不到它的时候必须降级，不能猜 —— 见 `gates.PositionsSyncedGate`。
    """

    def __init__(self, transport: SingleWriterGuard) -> None:
        self._t = transport

    def read(self) -> PositionSnapshot | None:
        """读快照。**返回 None 与返回空快照含义完全不同**：

        - None     → 读失败 / 文件不存在 / **格式认不出** ⇒ **未知**，必须降级
        - 空快照   → 读到了，确认就是空仓 ⇒ 正常状态

        这个区分是整套风控的支点。把它们合并成一个「空」，
        就会在「读不到持仓」时表现得像「确认空仓」，进而放心开新仓 —— 变成双持仓。

        注意第三个 None 来源（`WireFormatError`）：**格式认不出来时必须走这条路**。
        让异常冒出去会把整轮打断，比降级更糟；而把异常吞掉返回空快照，
        则是把「未知」伪装成「确认空仓」—— 那正是双持仓的成因。
        """
        try:
            payload = self._t.read(CH_POSITIONS)
        except TransportError as e:
            log.warning("读取持仓失败，持仓状态未知: %s", e)
            return None
        if payload is None:
            return None
        try:
            return PositionSnapshot.from_dict(payload)
        except (WireFormatError, ValueError, TypeError) as e:
            # ValueError/TypeError 是兜底：from_dict 已经把金额字段转成
            # WireFormatError，但任何解析实现都可能漏掉一个分支。这里的原则
            # 不变 —— 认不出 = 未知 = 降级禁买，绝不让异常冒出去打断整轮。
            log.error("× 持仓格式认不出，按对账失败处理（不是当成空仓）: %s", e)
            return None

    def write(self, snapshot: PositionSnapshot) -> bool:
        """回写持仓。**只有执行端角色的进程能走到这里** ——
        产生端调用会在 `SingleWriterGuard` 上抛 `OwnershipError`，
        而不是静默写坏对方的 channel。这个方向性由 `CHANNEL_OWNERS` 定义，
        不靠调用方自觉。
        """
        return self._t.write(CH_POSITIONS, snapshot.to_dict())


class HeartbeatStore:
    """心跳。产生端写，监控端读。"""

    def __init__(self, transport: SingleWriterGuard) -> None:
        self._t = transport

    def read(self) -> Heartbeat | None:
        """读心跳。三种结果必须分开：

        - `Heartbeat`        → 读到了（文件不存在时也会返回一份**全新**心跳）
        - `None`             → **读失败/内容认不出** ⇒ 严重度未知，不能当成 alive
        - 全新 `Heartbeat()` → 文件不存在 = 首次运行，确实还没有任何历史严重度

        ★ 原实现把「读失败」也退回默认 alive，于是下一次 `escalate("alive")`
        在阶梯上不升，写回去就把昨天的 `error` **静默抹掉**了。监控端看到
        alive 就不会告警 —— 一次传输抖动足以让一个已发生的故障消失。
        """
        try:
            payload = self._t.read(CH_HEARTBEAT)
        except TransportError:
            return None
        if payload is None:
            return Heartbeat()
        if not isinstance(payload, dict):
            return None
        return Heartbeat.from_dict(payload)

    def write(self, last_task: str, status: str = "alive",
              when: datetime | None = None) -> bool:
        hb = Heartbeat(last_time=when or datetime.now(), last_task=last_task, status=status)
        return self._t.write(CH_HEARTBEAT, hb.to_dict())


class Stores:
    """四个 channel 的集合入口。Runner 与 Executor 各持有自己角色的那一份。"""

    def __init__(self, transport: SingleWriterGuard) -> None:
        self.transport = transport
        self.signals = SignalQueue(transport)
        self.executions = ExecutionLog(transport)
        self.positions = PositionStore(transport)
        self.heartbeat = HeartbeatStore(transport)

    def describe(self) -> str:
        return self.transport.describe()
