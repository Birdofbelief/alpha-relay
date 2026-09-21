# -*- coding: utf-8 -*-
"""channel 的领域化读写：队列的幂等、上限、清理规则，以及一条保真原则。

这些规则的共同点是**它们都只在特定时刻才生效**（队列非空、有回执、跨日、
超上限），所以最省事的写法往往在空队列上「看起来没问题」。
本仓库真的踩过这个形状：`prune` 里 `rec.get("status")` 只在
「队列非空且该信号有回执」时才抛 AttributeError，最简单的路径上完全不出现。
"""
from __future__ import annotations

import pytest

from signalchain.models import (
    ACTION_BUY,
    ACTION_SELL,
    CH_POSITIONS,
    Execution,
    PositionSnapshot,
    Signal,
    executions_to_index,
)
from signalchain.store import SIGNAL_QUEUE_MAX, SignalQueue, Stores
from signalchain.transport.base import Role, SingleWriterGuard
from signalchain.transport.file import FileTransport

from .conftest import CLEAN_CODES, FIXED_NOW

TODAY = "2026-09-18"


@pytest.fixture
def producer(tmp_path):
    return SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)


@pytest.fixture
def executor(tmp_path):
    return SingleWriterGuard(FileTransport(tmp_path), Role.EXECUTOR)


def _exec(signal_id: str, status: str, action: str = ACTION_BUY) -> Execution:
    return Execution(signal_id=signal_id, code=CLEAN_CODES[0], action=action,
                     status=status, created_at=FIXED_NOW)


# ------------------------------------------------------------
# 追加与幂等
# ------------------------------------------------------------
def test_append_dedupes_by_signal_id(producer):
    """同一个 sig_id 追加两次只留一条。

    信号 id 是确定性的（时间戳+代码+动作），所以「同一个任务跑了两遍」
    会自然去重。这正是把 id 设计成确定性的目的 —— 重试不等于重复下单的**第一层**。
    """
    q = SignalQueue(producer)
    s = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, amount=100, created_at=FIXED_NOW)
    assert q.append(s) is True
    assert q.append(s) is True          # 幂等：返回成功，但不新增
    assert len(q.raw()) == 1


def test_append_keeps_same_code_different_time(producer):
    """同一标的、不同时刻 → 两条都留。

    去重只按 id。若按「代码+动作」去重，当天第二次信号会被静默吞掉 ——
    而那可能正是「早上那笔没成交，下午再来一次」。
    """
    q = SignalQueue(producer)
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW))
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_BUY,
                    created_at=FIXED_NOW.replace(hour=10, minute=30)))
    assert len(q.raw()) == 2


def test_queue_is_capped(producer):
    """队列有上限，超出后只留最新的。

    上限存在的理由很实际：清理逻辑（prune）依赖回执和跨日判断，
    一旦它连续几周没跑成，文件会无界膨胀，而 gist 的单文件体积有实际限制。
    """
    q = SignalQueue(producer, max_items=5)
    for i in range(12):
        q.append(Signal(code=CLEAN_CODES[0], action=ACTION_BUY,
                        created_at=FIXED_NOW.replace(second=i)))
    items = q.raw()
    assert len(items) == 5
    # 留的是**最新**的 5 条（队尾），不是最早那 5 条
    assert items[-1]["id"].endswith("_buy")


def test_default_cap_matches_production():
    """默认上限必须是 100 —— 与线上那份实现保持一致。

    这不是随手取的数：它意味着「即使清理连续失败好几周也还够用」，
    同时单文件体积仍在 gist 的舒适区内。改了它等于改变了线上行为。
    """
    assert SIGNAL_QUEUE_MAX == 100


# ------------------------------------------------------------
# 清理规则：三条规则各有各的理由
# ------------------------------------------------------------
def _seed(producer) -> SignalQueue:
    q = SignalQueue(producer)
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW))
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_SELL, created_at=FIXED_NOW))
    return q


def test_prune_removes_terminally_executed(producer):
    """有**终态**回执的删掉。

    终态 = filled（成交）/ canceled（撤单放弃）。这条容易和 `submitted` 混淆：
    `submitted` 是「已下单、结果未知」，删了就等于「单子还挂着、你却忘了它」。
    """
    q = _seed(producer)
    buy_id = next(i["id"] for i in q.raw() if i["action"] == "buy")
    index = executions_to_index([_exec(buy_id, "filled").to_dict()])
    assert q.prune(index, TODAY) == 1
    assert [i["action"] for i in q.raw()] == ["sell"]


def test_prune_keeps_submitted(producer):
    """`submitted` 不是终态，**不删**。

    回归测试：这条路径曾经会抛 AttributeError（`rec.get("status")` 而
    `executions_to_index` 给的是 `Execution` **对象**）。它只在
    「队列非空且该信号有回执」时触发，所以空队列的测试完全测不出来。
    """
    q = _seed(producer)
    buy_id = next(i["id"] for i in q.raw() if i["action"] == "buy")
    index = executions_to_index([_exec(buy_id, "submitted").to_dict()])
    assert q.prune(index, TODAY) == 0
    assert len(q.raw()) == 2


def test_prune_drops_cross_day_sells_but_keeps_cross_day_buys(producer):
    """跨日：卖单删、买单留。这是刻意的不对称。

    卖出信号有**当日时效**：隔天再执行等于用昨天的价去卖今天的持仓，
    价格早已不可比。而买入信号隔天仍然有效 —— 而且执行端的 signal_id
    幂等键会拦住重复下单，所以保留是安全的。
    """
    q = _seed(producer)
    removed = q.prune({}, TODAY)          # 队列里的两条都记的是 YESTERDAY 吗？不是
    # FIXED_NOW 是 2026-09-18，所以传 TODAY 时它们都算当日，不该被删
    assert removed == 0 and len(q.raw()) == 2

    # 换成「今天是 09-19」→ 两条都跨日 → 只删卖单
    assert q.prune({}, "2026-09-19") == 1
    assert [i["action"] for i in q.raw()] == ["buy"]


def test_prune_on_empty_queue_is_a_noop(producer):
    """空队列直接返回 0，不写盘。

    顺手断言「不写盘」：空跑一次就重写一遍文件，会在 gist 上白白消耗一次
    PATCH 配额 —— 而配额是要省着用的。
    """
    assert SignalQueue(producer).prune({}, TODAY) == 0
    assert producer.read("signals") is None


def test_prune_accepts_none_index(producer):
    """传 None 当作「没有回执」处理，不抛异常。"""
    q = _seed(producer)
    assert q.prune(None, TODAY) == 0


# ------------------------------------------------------------
# 保真原则：不认识的字段不许丢
# ------------------------------------------------------------
def test_rmw_preserves_unknown_fields(producer):
    """读写往返不得丢掉「本端不认识」的字段。

    链路的两个端可以独立升级。执行端某天给信号加一个字段，产生端如果
    「读进 dataclass → 写回去」，那个字段就被**静默抹掉**了，
    而症状是间歇性的（只在产生端恰好在那一刻读写时发生）。
    所以本层的所有 read-modify-write 都在**原始 dict** 上做增删。

    构造上要注意：`prune` **只在真的删掉了东西时才写盘**，
    所以要验「写回时保真」，队列里必须同时有一条该删的和一条该留的 ——
    否则这测试根本走不到写回那一步（那样它就会永远通过，什么也没验到）。
    """
    q = SignalQueue(producer)
    # 该留的：当日的买单，带一个本端不认识的字段
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW))
    # 该删的：昨日的卖单（跨日卖单会被清掉）
    q.append(Signal(code=CLEAN_CODES[0], action=ACTION_SELL,
                    created_at=FIXED_NOW.replace(day=17)))

    raw = q.raw()
    assert len(raw) == 2
    kept = next(i for i in raw if i["action"] == "buy")
    kept["future_field"] = {"nested": [1, 2, 3]}         # 模拟对端新加的字段
    producer.write("signals", raw)

    assert q.prune({}, TODAY) == 1                        # 删掉卖单 → 触发写回

    after = q.raw()
    assert len(after) == 1
    assert after[0]["future_field"] == {"nested": [1, 2, 3]}


# ------------------------------------------------------------
# 回执日志
# ------------------------------------------------------------
def test_execution_log_dedupes_same_status(executor):
    """同一 `(signal_id, status)` 只留最新一条 —— 重试不该把日志刷满。"""
    log = Stores(executor).executions
    rec = _exec("s1", "submitted")
    for _ in range(3):
        log.append(rec)
    assert len(log.raw()) == 1


def test_execution_log_keeps_distinct_statuses(executor):
    """不同 status **都保留**：submitted → filled 是两条，历史要留着。

    只留最新一条会让「这笔单挂过、后来成交了」这段历史消失，
    而排查时最需要的恰恰是「它当时为什么挂着」。
    """
    log = Stores(executor).executions
    log.append(_exec("s1", "submitted"))
    log.append(_exec("s1", "filled"))
    assert len(log.raw()) == 2
    assert log.index()["s1"].status == "filled"


# ------------------------------------------------------------
# 持仓存储：None 与空快照是两件事
# ------------------------------------------------------------
def test_position_store_distinguishes_missing_from_empty(executor):
    """读不到 → `None`；读到但是空仓 → 空快照。**合并它们会开错仓。**

    这是整套风控的支点：把「读不到持仓」表现成「确认空仓」，
    本地就会以为可以放心买入，而账户里其实还持有别的标的 —— 变成双持仓。
    """
    store = Stores(executor).positions
    assert store.read() is None                      # 从没写过 → 未知

    store.write(PositionSnapshot(positions={}, cash=1.0))
    snapshot = store.read()
    assert snapshot is not None and snapshot.positions == {}   # 已知：空仓


def test_position_store_round_trip(executor):
    store = Stores(executor).positions
    snap = PositionSnapshot(positions={f"{CLEAN_CODES[0]}.SS": {"amount": 100}},
                            total_value=1000.0, cash=10.0, updated_at=FIXED_NOW)
    store.write(snap)
    back = store.read()
    assert back.total_value == 1000.0
    assert back.bare_codes == [CLEAN_CODES[0]]
    # 内部规范形式是聚宽后缀；落盘写的是 PT 后缀（见 models.to_pt）。
    assert back.held_codes() == [f"{CLEAN_CODES[0]}.XSHG"]


def test_heartbeat_store_round_trip(producer):
    hb = Stores(producer).heartbeat
    hb.write("push_signal", status="positions_stale", when=FIXED_NOW)
    back = hb.read()
    assert back.last_task == "push_signal"
    assert back.status == "positions_stale"


def test_heartbeat_store_returns_default_when_unreadable(tmp_path):
    """心跳读不出来时返回**默认心跳**，不抛异常。

    心跳是观测设施，不是交易路径的一部分 —— 它坏掉不该让整轮停摆。
    （注意这与持仓相反：持仓读不到必须降级，因为它影响决策。）
    """
    from signalchain.models import CHANNEL_FILENAMES

    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    (tmp_path / CHANNEL_FILENAMES["heartbeat"]).write_text("坏文件", encoding="utf-8")
    assert Stores(guard).heartbeat.read().status == "alive"


def test_unreadable_positions_format_is_unknown_not_empty(executor):
    """★ 存储层把「格式认不出」归到**未知**（None），而不是「确认空仓」。

    这是整套风控的支点在这一层的落实：`PositionSnapshot.from_dict` 对认不出的
    结构抛 `WireFormatError`，存储层接住并转成 `None`，于是
    `PositionsSyncedGate` 会禁买。

    若这里改成吞掉异常返回空快照，症状是「读不懂持仓时反而放心开新仓」——
    方向反了，而且只在格式出问题时才显形。
    """
    store = Stores(executor).positions
    executor.write(CH_POSITIONS, "这不是持仓该有的样子")
    assert store.read() is None

    executor.write(CH_POSITIONS, {"positions": ["不是", "dict"]})
    assert store.read() is None
