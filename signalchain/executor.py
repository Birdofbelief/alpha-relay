# -*- coding: utf-8 -*-
"""执行端抽象 —— 「接你自己的券商终端」这件事的全部接口。

## 为什么这里是 Protocol，而不是像 `Transport` 那样的 ABC

`Transport` 是 ABC：它**要求**显式继承。因为后端是一族要被逐条扫过的实现，
契约测试拿同一套用例跑遍它们，写错继承关系本身就是错误。

执行端不一样。它只有**一个**内置实现（`PaperExecutor`，无券商的纸面占位），
真实用法是你在旁边写一个类去接券商终端 —— 而那个类通常**已经有基类了**
（券商 SDK 的适配层、你自己项目里的 `Broker`）。要求它再继承一个框架基类，
是拿「为了统一而统一」去换一次真实的继承冲突。

所以这里用结构化类型（`typing.Protocol`）：**长成这个样子就算数。**
不必继承任何东西，也不必 import 这个模块。

## 契约面只有三个方法

`Chain` 实际调用的就是这三个：

    publish_positions(now) -> bool        报持仓（★ 产生端对账的唯一途径）
    step(now) -> dict[str, int]           消化信号 → 写回执；返回 {状态: 条数}
    describe() -> str                     一句话自报身份（★ 不得含凭据）

`PaperExecutor` 上其余那几个（`poll_and_execute` / `open_order_ids` /
`_try_fill`）**不在契约里** —— 它们是纸面实现自己的事，别照着抄。

## ★ 构造时必须自己包的两样东西

协议管不到构造函数，而这里恰好有一个**必须遵守**的约定：

    self.transport = SingleWriterGuard(transport, Role.EXECUTOR)
    self.stores = Stores(self.transport)

第一行是「单一写入者」：执行端只准写 `positions` / `executions`，
写 `signals` 会当场抛 `OwnershipError`。**别绕过它去拿裸后端** ——
绕过之后两端互相覆盖会重新变成静默故障，而那正是这条约定要根除的东西。

第二行是四个 channel 的读写门面。

`PaperExecutor.__init__`（`signalchain/paper.py`）就是这两行的现成样板。

## ★ 接券商终端时，这个框架只负责一半

本框架保证的是：**信号被确定性地、幂等地、带风控地送到你手上。**
从 `step()` 里拿到信号之后怎么下单、怎么处理拒单与部分成交、怎么跟券商对账，
是执行端自己的事 —— 本仓库不带任何券商的客户端，也不打算带。
你的 `publish_positions` 报什么，产生端就信什么，所以**它必须来自券商返回的
真实持仓，而不是你自己记的账**。纸面执行端可以自己记账，是因为它同时扮演了
「券商」那一侧；换成真券商之后，账在券商那里。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Executor(Protocol):
    """执行端的结构化契约。三个方法，长这样就算数。

    `@runtime_checkable` 只为让「我这个类合不合格」可被机械地问一句
    （`isinstance(obj, Executor)`）。**它只检查方法名在不在，不检查签名** ——
    所以它是一道顺手的自检，不是保证。真正的保证是
    `tests/test_executor_contract.py`：一个最小执行端真的被装进链、跑完一轮。
    """

    def publish_positions(self, now: datetime) -> bool:
        """把持仓回写。**这是产生端能完成对账的唯一途径。**

        返回是否写成功。产生端读不到持仓时，`PositionsSyncedGate` 会禁买 ——
        所以这里返回 `False` 不是「这一轮白干了」，是「下一轮别买」。

        ⚠️ 报出去的必须是**券商那边的真实持仓**，不是你本地记的账。
        两者一旦背离，`PositionsSyncedGate` 就失去意义：它会以为自己对上了账，
        而实际上对的是你那份已经漂移的副本。
        `snapshot.updated_at` 是给使用者的新鲜度线索（框架刻意不拿它判闸门，
        理由见 `docs/ARCHITECTURE.md` 第七节第 1 条）。
        """

    def step(self, now: datetime) -> dict[str, int]:
        """一次「嘀嗒」：消化挂在那里的信号 → 写回执；返回 `{状态: 条数}`。

        ⚠️ **重复调用必须安全。** 主循环是 30 秒一轮的，`step` 会被反复调用；
        而且进程随时可能重启。两条防线缺一不可：

          - 信号自带的 `sig_id` 是**幂等键** —— 已到终态
            （`filled` / `canceled`）的不再处理。这是第一道。
          - 下单前先查**自己有没有挂着的单**。这是第二道 ——
            执行端记性不好（重启、丢回执）时，靠的是它。
            纸面实现里它叫 `open_order_ids()`，你可以直接读实现：
            查一遍回执索引里还停在 `submitted` 的 id。

        少了第二道，一次重启就可能变成重复下单。
        """

    def describe(self) -> str:
        """一句话描述本执行端（**不得包含凭据**）。会打进日志与心跳。"""
