# -*- coding: utf-8 -*-
"""线上数据结构的行为。

这些测试守的是**语义**，不是实现：
id 必须确定性、序列化必须能往返、严重度必须只升不降。
它们每一条都对应一个「静默失败」的场景 —— 写错的后果不是崩溃，是错的行为。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from signalchain.models import (
    ACTION_BUY,
    ACTION_SELL,
    EXEC_CANCELED,
    EXEC_FILLED,
    EXEC_SUBMITTED,
    Execution,
    Heartbeat,
    PositionSnapshot,
    Signal,
    executions_to_index,
    fmt_dt,
    payload_to_signals,
    signals_to_payload,
    to_bare,
    to_jq,
)

from .conftest import CLEAN_CODES, FIXED_NOW, SYNTHETIC_SZ


# ------------------------------------------------------------
# 代码格式
# ------------------------------------------------------------
def test_to_jq_picks_exchange_by_first_digit():
    """后缀规则只看首位数字 —— 上交所 5/6/9，其余归深交所。

    这条规则是**为了兼容既有执行端而冻结**的，不是可以「改进」的东西：
    改了它，券商端 端就会收到认不出的代码，而症状是「信号发了但没成交」。
    """
    for code in ("510050", "600000", "900901"):
        assert to_jq(code) == f"{code}.XSHG"
    for code in ("159919", "000001", "300750"):
        assert to_jq(code) == f"{code}.XSHE"


def test_to_jq_is_idempotent():
    """已经是后缀形式就原样返回。重复调用不能越加越长。"""
    once = to_jq("510050")
    assert to_jq(once) == once


def test_to_bare_accepts_broker_style_suffix():
    """`to_bare` 必须同时吃下两种后缀风格。

    `.XSHG` 是本仓库的线格式；`.SS`/`.SZ` 是 **券商端 端自己的写法** ——
    `positions.json` 里的键就是后者。一个只认前者的实现会在对账时
    把每个持仓都当成「没见过的代码」，而它不会报错，只会对不上账。
    """
    assert to_bare("510050.XSHG") == "510050"
    assert to_bare("510050.SS") == "510050"
    assert to_bare("159919.SZ") == "159919"
    assert to_bare("510050") == "510050"


def test_fmt_dt_matches_str_datetime():
    """时间格式与 `str(datetime)` **逐字符一致**。

    不能改用 `isoformat()`：它用 `T` 分隔日期与时间。消费端只用前 10 个字符取
    日期，所以改了通常也不会立刻炸 —— 正因如此，这条得用测试钉住，
    否则某次「顺手规范化一下」会留下一类极难排查的跨日误判。
    """
    dt = datetime(2026, 9, 18, 14, 45, 0)
    assert fmt_dt(dt) == str(dt) == "2026-09-18 14:45:00"
    assert "T" not in fmt_dt(dt)


# ------------------------------------------------------------
# 信号 id：幂等的地基
# ------------------------------------------------------------
def test_signal_id_is_deterministic():
    """同一时刻 + 同一标的 + 同一动作 → 同一个 id。

    这是「重试 ≠ 重复下单」的全部依据。若这里掺进随机数或自增序号，
    执行端的幂等键就失效了，而失效方式是**静默地重复下单**。
    """
    a = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, amount=100, created_at=FIXED_NOW)
    b = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, amount=999, created_at=FIXED_NOW)
    assert a.sig_id == b.sig_id        # 数量不同不影响身份：id 只认「何时/何标的/何动作」
    assert a.sig_id == f"20260918144500_{CLEAN_CODES[0]}.XSHG_{ACTION_BUY}"


def test_signal_id_distinguishes_action_and_time():
    """不同动作、不同时刻必须是不同 id —— 否则「卖出」会被当成「买入」的重复。"""
    buy = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW)
    sell = Signal(code=CLEAN_CODES[0], action=ACTION_SELL, created_at=FIXED_NOW)
    later = Signal(code=CLEAN_CODES[0], action=ACTION_BUY,
                   created_at=FIXED_NOW.replace(minute=46))
    assert len({buy.sig_id, sell.sig_id, later.sig_id}) == 3


def test_signal_rejects_unknown_action():
    """动作只有买卖两种。写错必须当场炸，不能生成一条谁都不认的信号。"""
    with pytest.raises(ValueError):
        Signal(code=CLEAN_CODES[0], action="hold", created_at=FIXED_NOW)


def test_signal_price_is_rounded_to_4dp():
    """价格截到 4 位小数。

    理由不是美观：`0.30000000000000004` 这种浮点尾巴会让 golden-file 对比
    在不同平台上随机失败，也会让「价格是否相同」这种判断变得不可靠。
    """
    s = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, price=1.23456789,
               created_at=FIXED_NOW)
    assert s.price == 1.2346


# ------------------------------------------------------------
# 序列化往返
# ------------------------------------------------------------
def test_signal_round_trip():
    """`to_dict` → `from_dict` 必须还原出等价的信号（含 id 与时间）。"""
    s = Signal(code=SYNTHETIC_SZ, action=ACTION_SELL, amount=0, price=1.5,
               signal_type="demo_exit", created_at=FIXED_NOW)
    back = Signal.from_dict(s.to_dict())
    assert back.sig_id == s.sig_id
    assert back.code == s.code
    assert back.action == s.action
    assert back.signal_type == s.signal_type
    assert back.created_at == s.created_at
    assert back.price == s.price


def test_signal_from_dict_tolerates_bad_time():
    """时间字段坏掉时**不能抛异常**。

    这里是刻意的「宽进」：消费者面对的是别人写进去的历史数据，抛异常会让
    一条格式有瑕疵的老信号把整轮处理打断。时间解析不出来时退回 None，
    信号本身仍然可用（id 已经在数据里了）。
    """
    d = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW).to_dict()
    d["time"] = "不是时间"
    s = Signal.from_dict(d)
    assert s.sig_id == d["id"]          # id 从数据里取，不靠时间重算
    assert s.created_at is not None     # 解析失败 → 落到「现在」，不是 None 崩溃


def test_payload_helpers_skip_garbage():
    """一层坏数据不该带走整批信号。缺 code/action 的条目被跳过，其余照常。"""
    good = Signal(code=CLEAN_CODES[0], action=ACTION_BUY, created_at=FIXED_NOW)
    payload = signals_to_payload([good]) + [{"code": ""}, "不是字典", {"action": "buy"}]
    out = payload_to_signals(payload)
    assert len(out) == 1 and out[0].sig_id == good.sig_id
    assert payload_to_signals(None) == []
    assert payload_to_signals({"不是": "列表"}) == []


# ------------------------------------------------------------
# 回执索引
# ------------------------------------------------------------
def test_executions_index_keeps_latest_status():
    """同一 signal_id 的多条回执要取**最后**那条。

    顺序语义是：先写 `submitted`，成交后再追加 `filled`。取错方向会让
    「已成交」的信号看起来还挂着，于是执行端再下一次单。
    """
    payload = [
        {"signal_id": "s1", "code": CLEAN_CODES[0], "action": ACTION_BUY,
         "status": EXEC_SUBMITTED},
        {"signal_id": "s1", "code": CLEAN_CODES[0], "action": ACTION_BUY,
         "status": EXEC_FILLED},
    ]
    assert executions_to_index(payload)["s1"].status == EXEC_FILLED
    assert executions_to_index(payload)["s1"].is_terminal


def test_executions_index_can_keep_first_status():
    """`keep_latest=False` 要真的取**第一条**。

    这条是补的回归测试：该分支原本和 `keep_latest=True` 写成了同一句，
    参数等于不存在 —— 传 False 的人会拿到最新的一条，而他以为拿到的是最早的那条。
    这类「参数被静默忽略」的 bug 不会报错，只会给出错误答案。
    """
    payload = [
        {"signal_id": "s1", "code": CLEAN_CODES[0], "action": ACTION_BUY,
         "status": EXEC_SUBMITTED},
        {"signal_id": "s1", "code": CLEAN_CODES[0], "action": ACTION_BUY,
         "status": EXEC_CANCELED},
    ]
    assert executions_to_index(payload, keep_latest=False)["s1"].status == EXEC_SUBMITTED


def test_executions_index_ignores_junk():
    payload = [None, "字符串", {"没有": "signal_id"},
               {"signal_id": "s9", "status": EXEC_FILLED}]
    index = executions_to_index(payload)
    assert set(index) == {"s9"}


# ------------------------------------------------------------
# 持仓
# ------------------------------------------------------------
def test_position_snapshot_accepts_documented_wire_shapes():
    """包装形态与裸 dict 形态都要认，且 key 归一到聚宽形式。

    裸 dict 是**早期版本的存量格式**，线上可能还有。只认一种的实现在遇到
    另一种时会解析出「零持仓」，而对账会据此认为「账户是空的」—— 然后买入，
    变成双持仓。这是最贵的一类兼容性错误。
    """
    wrapped = PositionSnapshot.from_dict({
        "update_time": "2026-09-18 14:45:00", "total_value": 100000.0,
        "cash": 5000.0, "positions": {"510050.SS": {"amount": 100, "cost": 1.0}},
    })
    assert wrapped.cash == 5000.0
    assert wrapped.bare_codes == ["510050"]
    assert wrapped.held_codes() == ["510050.XSHG"]

    bare = PositionSnapshot.from_dict({"510050.SS": {"amount": 100, "cost": 1.0}})
    assert bare.bare_codes == ["510050"]
    assert bare.cash == 0.0


def test_position_snapshot_held_codes_drops_zero_amounts():
    """`amount=0` 的残留记录不算持仓。

    执行端卖出后有时会留下一条数量归零的记录。把它算成持仓，策略就会
    对一只已经空仓的标的反复发卖出信号。
    """
    snap = PositionSnapshot.from_dict({
        "positions": {"510050.SS": {"amount": 0}, "159919.SZ": {"amount": 100}},
    })
    assert snap.held_codes() == ["159919.XSHE"]


def test_position_snapshot_rejects_unrecognizable_structures():
    """认不出的结构抛 `WireFormatError`，**不返回空快照**。

    ★ 这条测试是本仓库初版的一个**真实缺陷**改过来的。初版断言
    「非 dict 输入返回空快照，不抛异常」—— 那句断言本身就是在描述 bug：

        「读不懂」被伪装成「确认空仓」→ 放心买入 → 双持仓

    而失效方向本该朝「不做」。断言写反了，测试就会一直替 bug 背书 ——
    这也是为什么「测试全绿」不能替代「测试断言的是对的事」。
    """
    from signalchain.models import WireFormatError

    for junk in (None, "字符串", 42, {"a": 1, "b": 2}):
        with pytest.raises(WireFormatError):
            PositionSnapshot.from_dict(junk)


# ------------------------------------------------------------
# 心跳严重度
# ------------------------------------------------------------
def test_heartbeat_severity_only_rises():
    """严重度**只升不降**（当天内）。

    监控端按 status 决定要不要发告警。允许降级会让一次已经发生过的异常
    被后续的正常心跳抹掉 —— 表现是「告警从没响过」，而异常确实发生过。
    """
    hb = Heartbeat(status="alive")
    hb.escalate("error")
    assert hb.status == "error"
    hb.escalate("alive")            # 试图降级
    assert hb.status == "error"     # 拒绝
    hb.escalate("positions_stale")  # 更轻的也不行
    assert hb.status == "error"


def test_heartbeat_escalation_is_ordered():
    hb = Heartbeat(status="alive")
    hb.escalate("positions_stale")
    assert hb.status == "positions_stale"
    hb.escalate("error")
    assert hb.status == "error"


def test_heartbeat_round_trip_and_junk():
    hb = Heartbeat(last_time=FIXED_NOW, last_task="push_signal", status="positions_stale")
    back = Heartbeat.from_dict(hb.to_dict())
    assert (back.status, back.last_task, back.last_time) == (
        "positions_stale", "push_signal", FIXED_NOW)
    assert Heartbeat.from_dict("垃圾").status == "alive"
