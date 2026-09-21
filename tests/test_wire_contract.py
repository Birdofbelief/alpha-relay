# -*- coding: utf-8 -*-
"""线格式冻结 —— golden-file 测试。

## 为什么值得单独一组测试

链路的两端可能由不同的人、不同的语言、不同的部署节奏维护。格式一旦漂移，
症状是「信号发出去了但执行端读不懂」——**静默失败**，往往要等一笔该成交的
单没成交才发现，而那时已经错过了行情。

所以这里的 golden 文件是**契约的可执行版本**：改动它们之前先想清楚，
线上那端能不能一起改。

## 这些 golden 是怎么来的

信号那条来自线上实现里构造字典的那几行，字段名与**顺序**逐条对齐
（`json.dumps` 按插入序输出，所以顺序也是契约的一部分）。
`positions` 里那个 `update_time` 尤其要留意：**它不是 dataclass 的字段名**
（那个叫 `updated_at`），而是线上就长这样 —— 这种「内部叫法和对外叫法不一致」
的地方，正是最需要 golden 钉住的。

## 怎么正确地改这里

线格式的变更**只有一种合法方式**：两端同时改 + 迁移文档 + 更新 golden。
若只是为了让某个测试变绿而改 golden，那这个测试从一开始就白写了。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from signalchain.models import (
    CHANNEL_FILENAMES,
    EXEC_CANCELED,
    EXEC_FAILED,
    EXEC_FILLED,
    EXEC_SKIPPED,
    EXEC_SUBMITTED,
    TERMINAL_EXEC_STATUSES,
    WireFormatError,
    Execution,
    Heartbeat,
    PositionSnapshot,
    Signal,
    fmt_dt,
    fmt_dt_secs,
    to_pt,
)

from .conftest import CLEAN_CODES, FIXED_NOW

GOLDEN = Path(__file__).parent / "golden"


def _dumps(doc) -> str:
    """**与两个后端逐字一致的序列化方式。**

    `ensure_ascii=False` + `indent=2` 不是随手选的：中文不转义是为了人工
    在 gist 页面上直接看得懂，缩进 2 是为了人工 diff / 快照对比。
    两处（FileTransport 默认、GistTransport 的 content）必须一致，
    否则「换后端」会连带改变落盘字节，快照对比就废了。
    """
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8").strip()


# ------------------------------------------------------------
# 文件名 —— 它也是契约
# ------------------------------------------------------------
def test_channel_filenames_are_frozen():
    """四个 channel 的**落盘文件名**是契约的一部分。

    在 Gist 后端上，这些名字就是 gist 里的文件名 —— 改了它，执行端会
    「读不到文件」，而它把「文件不存在」当成「还没写过」，于是安静地什么都不做。
    这是最容易被随手改掉、后果最隐蔽的一处。
    """
    assert CHANNEL_FILENAMES == {
        "signals": "trade_signals.json",
        "heartbeat": "heartbeat.json",
        "positions": "positions.json",
        "executions": "executions.json",
    }


# ------------------------------------------------------------
# 各文档的 golden 对比
# ------------------------------------------------------------
def test_signals_match_golden():
    signals = [
        Signal(code=CLEAN_CODES[0], action="buy", amount=1000, price=1.2346,
               signal_type="demo_entry", created_at=FIXED_NOW),
        Signal(code=CLEAN_CODES[2], action="sell", amount=0, price=3.5,
               signal_type="demo_exit", created_at=FIXED_NOW),
    ]
    assert _dumps([s.to_dict() for s in signals]) == _golden("trade_signals.json")


def test_executions_match_golden():
    """★ golden 里刻意放了**两条**记录 —— 这个 channel 有两种互斥形状。

    终态（filled/canceled）：有 `order_id`/`price`/`amount`，**没有** `reason`。
    非终态（submitted/skipped/failed）：有 `reason`，**没有**那三个。

    把它们拼成一份「完整字段表」是最容易犯的错，而且线上执行端的 docstring
    本身就是那么写的（还多列了一个全库不存在的 `limit_price`）。
    """
    filled = Execution(signal_id=f"20260918144500_{CLEAN_CODES[0]}.XSHG_buy",
                       code=f"{CLEAN_CODES[0]}.XSHG", action="buy", status="filled",
                       order_id="PAPER-1", price=1.2346, amount=1000,
                       created_at=FIXED_NOW)
    submitted = Execution(signal_id=f"20260918144500_{CLEAN_CODES[2]}.XSHE_sell",
                          code=f"{CLEAN_CODES[2]}.XSHE", action="sell",
                          status="submitted", reason="市价低于限价，挂单等待",
                          created_at=FIXED_NOW)
    assert _dumps([filled.to_dict(), submitted.to_dict()]) == _golden("executions.json")


def test_execution_shapes_are_mutually_exclusive():
    """两种形状的字段集**必须互斥** —— 这是契约，不是实现细节。

    多写字段看着无害，实际会让消费端的分支判断失去意义：
    「有 price 就是成交了」这种写法在有第三种形状时会静默错判。
    """
    terminal = Execution(signal_id="s", code=CLEAN_CODES[0], action="buy",
                         status="filled", order_id="O", price=1.0, amount=100,
                         reason="这段 reason 不该出现在终态里").to_dict()
    assert "reason" not in terminal
    assert set(terminal) == {"signal_id", "code", "action", "status",
                             "order_id", "price", "amount", "time"}

    non_terminal = Execution(signal_id="s", code=CLEAN_CODES[0], action="buy",
                             status="skipped", reason="资金不足",
                             order_id="O", price=1.0, amount=100).to_dict()
    assert set(non_terminal) == {"signal_id", "code", "action", "status",
                                 "reason", "time"}
    # 非终态即便对象里带着 order_id/price/amount，也不许写出去
    assert "order_id" not in non_terminal
    assert "price" not in non_terminal
    assert "amount" not in non_terminal


def test_every_status_falls_on_the_right_side_of_the_shape_split():
    """五个 status 恰好二分，没有第三个形状。

    线上取值域就是这五个（穷举自全部回执构造点）。多出来的状态值意味着
    有一个构造点我们没对齐 —— 让它在这里红，而不是等到线上。
    """
    all_statuses = {EXEC_SUBMITTED, EXEC_FILLED, EXEC_CANCELED,
                    EXEC_FAILED, EXEC_SKIPPED}
    for status in all_statuses:
        d = Execution(signal_id="s", code=CLEAN_CODES[0], action="buy",
                      status=status, reason="r", order_id="o",
                      price=1.0, amount=1).to_dict()
        if status in TERMINAL_EXEC_STATUSES:
            assert "order_id" in d and "reason" not in d, status
        else:
            assert "reason" in d and "order_id" not in d, status


def test_positions_match_golden():
    snap = PositionSnapshot(positions={f"{CLEAN_CODES[0]}.SS": {"amount": 1000,
                                                               "cost": 1.2346}},
                            total_value=12345.6, cash=2345.6, updated_at=FIXED_NOW)
    assert _dumps(snap.to_dict()) == _golden("positions.json")


def test_heartbeat_matches_golden():
    hb = Heartbeat(last_time=FIXED_NOW, last_task="push_signal", status="alive")
    assert _dumps(hb.to_dict()) == _golden("heartbeat.json")


# ------------------------------------------------------------
# 能被自己读回去 —— 格式自洽
# ------------------------------------------------------------
def test_golden_files_are_readable_by_the_parsers():
    """golden 不只是「输出长得对」，还要能被解析器读回来。

    只对比序列化结果是不够的：一个把 `time` 写成别的格式、又把解析器改成
    只认新格式的实现，两边可以「自洽」地通过一个只做 dumps 对比的测试，
    却和线上那端完全不兼容。这里从 golden **反向**读一遍，
    确保它们是同一套格式的两种表达。
    """
    sigs = [Signal.from_dict(d)
            for d in json.loads(_golden("trade_signals.json"))]
    assert [s.code for s in sigs] == [f"{CLEAN_CODES[0]}.XSHG",
                                      f"{CLEAN_CODES[2]}.XSHE"]
    assert all(s.created_at == FIXED_NOW for s in sigs)   # 时间能被解析回原值

    rec = Execution.from_dict(json.loads(_golden("executions.json"))[0])
    assert rec.is_terminal and rec.order_id == "PAPER-1"

    snap = PositionSnapshot.from_dict(json.loads(_golden("positions.json")))
    assert snap.updated_at == FIXED_NOW
    assert snap.bare_codes == [CLEAN_CODES[0]]
    assert snap.cash == 2345.6

    hb = Heartbeat.from_dict(json.loads(_golden("heartbeat.json")))
    assert hb.last_time == FIXED_NOW and hb.last_task == "push_signal"


# ------------------------------------------------------------
# 几个特别容易被「顺手规范化」掉的细节
# ------------------------------------------------------------
def test_time_has_no_iso_separator():
    """时间格式里**不能出现 `T`**（那是 `isoformat()` 的写法）。

    消费端只用前 10 个字符取日期，所以改成 isoformat 通常不会立刻炸 ——
    正因如此，它得靠测试钉住，否则某次「顺手统一一下格式」会留下一类
    极难排查的跨日误判。另外字典序也必须与时间序一致（信号按时间追加）。
    """
    early = fmt_dt(FIXED_NOW.replace(hour=9, minute=5))
    late = fmt_dt(FIXED_NOW.replace(hour=14, minute=45))
    assert "T" not in early
    assert early < late                       # 字典序 == 时间序


def test_processed_is_always_false_when_written():
    """产生端写出的 `processed` **恒为 False**。

    这个字段在线上是「写了但没人改」的死字段（执行端只读它、从不回写，
    因为回写 signals 会破坏单一写入者约定）。保留它纯粹是为了与线上格式
    逐字节兼容。这条测试的作用是：将来有人想「顺手把它删掉」时，
    会先看到这里写着「它是兼容性的一部分，不是没用的残留」。
    """
    s = Signal(code=CLEAN_CODES[0], action="buy", created_at=FIXED_NOW)
    assert s.to_dict()["processed"] is False
    assert Signal.from_dict(s.to_dict()).processed is False


def test_key_order_is_part_of_the_contract():
    """字段顺序也算契约 —— 因为序列化按插入序输出。

    顺序一变，落盘字节就变，于是「快照对比」「人工 diff」这些排查手段
    会在没有任何语义变化的情况下开始报差异，制造假的告警。
    """
    s = Signal(code=CLEAN_CODES[0], action="buy", created_at=FIXED_NOW)
    assert list(s.to_dict()) == ["id", "code", "action", "amount", "price",
                                 "time", "signal_type", "processed"]
    assert list(PositionSnapshot().to_dict()) == ["update_time", "total_value",
                                                  "cash", "positions"]
    assert list(Heartbeat().to_dict()) == ["last_time", "last_task", "status"]


# ------------------------------------------------------------
# ★ 同一份 gist 里两套代码后缀 —— 故意不一致
# ------------------------------------------------------------
def test_positions_keys_use_broker_suffix_and_executions_use_jq_suffix():
    """**这两个 channel 的后缀不一样，而且必须继续不一样。**

        positions 的 key  → `.SS` / `.SZ`     （券商端形式）
        executions 的 code → `.XSHG` / `.XSHE`（聚宽形式）

    成因是执行端两边写法不同：`positions` 直接遍历券商持仓字典并把 key
    原样写出去（那些 key 本来就是券商端形式），而 `executions` 每个写点都
    显式转换过一次。

    ★ 这条测试的形式本身就是重点：**两条断言必须分开写**。
    如果图省事写成一个「后缀正确」的公共断言，就等于把两套后缀的存在
    从测试里抹掉了 —— 下一个人会以为它们本就该一致，然后「顺手统一」，
    于是其中一端再也认不出另一端的输出。
    """
    submitted = Execution(signal_id="s", code=f"{CLEAN_CODES[0]}.XSHG",
                          action="buy", status="submitted", reason="r").to_dict()
    assert submitted["code"].endswith(".XSHG")

    snap = PositionSnapshot(positions={f"{CLEAN_CODES[0]}.XSHG": {"amount": 1}})
    key = next(iter(snap.to_dict()["positions"]))
    assert key.endswith(".SS"), f"positions 的 key 必须是券商端形式，实际 {key!r}"


def test_both_suffix_conventions_survive_a_round_trip():
    """两套后缀都要能原样走一个来回 —— 落盘写券商端、读回归一。

    内部统一成聚宽形式（与 `Signal.code` 一致），线格式写券商端形式（与线上一致），
    转换只发生在 `to_dict`/`from_dict` 这一对里。
    """
    for bare, jq, pt in ((CLEAN_CODES[0], ".XSHG", ".SS"),
                         (CLEAN_CODES[2], ".XSHE", ".SZ")):
        snap = PositionSnapshot(positions={f"{bare}{jq}": {"amount": 100}})
        wire = snap.to_dict()
        assert f"{bare}{pt}" in wire["positions"]
        assert f"{bare}{jq}" not in wire["positions"]

        back = PositionSnapshot.from_dict(wire)
        assert list(back.positions) == [f"{bare}{jq}"], "读回后必须归一到聚宽形式"
        assert back.bare_codes == [bare]
        assert back.held_codes() == [f"{bare}{jq}"]


def test_first_char_decides_the_exchange():
    """交易所判据是**首位字符**：`5`/`6`/`9` → 沪市，其余 → 深市。

    `5` 与 `1` 的分界正是沪深 ETF 的分界，这是**唯一**让 `1` 开头（深市）
    与 `5` 开头（沪市）正确分开的依据。不要用「51x 才是沪市」这类更细的规则
    替换它 —— 那会在遇到非 ETF 的沪市代码时判错。
    """
    assert to_pt(CLEAN_CODES[0]).endswith(".SS")      # 5 开头 → 沪
    assert to_pt(CLEAN_CODES[1]).endswith(".SS")      # 5 开头 → 沪
    assert to_pt(CLEAN_CODES[2]).endswith(".SZ")      # 1 开头 → 深
    assert to_pt("600000").endswith(".SS")            # 6 开头 → 沪
    assert to_pt("000001").endswith(".SZ")            # 0 开头 → 深


# ------------------------------------------------------------
# ★ 心跳时间戳 —— 全仓库唯一一个「写错会静默降级」的字段
# ------------------------------------------------------------
def test_heartbeat_time_is_parsable_by_the_monitors_exact_format():
    """`heartbeat.last_time` 必须能被监控端的格式串**原样解析**。

    监控端用的是这一行（线上 线上监控端）：

        datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')

    ★ 为什么这条要单独测：`str(datetime)` 在**微秒非零**时会输出
    `2026-09-20 09:50:56.878982`，而上面那个格式串遇到 `.878982` 会抛
    `ValueError`。抛了之后被上层吞掉 → `last_dt = None` →
    滞后检测走 fail-open 分支 → **静默失效**：不报错、不告警、日志只少一行。

    所以这里刻意构造一个**微秒非零**的时刻。`FIXED_NOW` 的微秒恰好是 0，
    用它测等于没测 —— 本仓库初版就是这么漏掉的。
    """
    from datetime import datetime as _dt

    ticky = _dt(2026, 9, 18, 14, 45, 0, 878982)
    wire = Heartbeat(last_time=ticky, last_task="push_signal").to_dict()

    parsed = _dt.strptime(wire["last_time"], "%Y-%m-%d %H:%M:%S")
    assert parsed == ticky.replace(microsecond=0)
    assert "." not in wire["last_time"], (
        f"心跳时间带了微秒（{wire['last_time']!r}），监控端的 strptime 会抛异常"
        f"并被吞掉，导致心跳滞后检测静默失效")


def test_the_heartbeat_trap_is_real_not_imagined():
    """★ **证明上一条测试不是摆设** —— 用朴素写法必然踩坑。

    如果不专门做这一步，`test_heartbeat_time_is_parsable_...` 有可能
    只是因为「恰好今天没出问题」而通过。这里直接验证陷阱本身存在：
    拿 `fmt_dt`（= `str(dt)`）的输出喂给监控端的格式串，**必须**抛异常。

    这是「测那个测你的东西」的同一个纪律：一个从不失败的断言和一条
    通过的断言，在终端上长得一模一样。
    """
    from datetime import datetime as _dt

    ticky = _dt(2026, 9, 18, 14, 45, 0, 878982)
    naive = fmt_dt(ticky)                     # 朴素写法：str(datetime)
    assert "." in naive

    with pytest.raises(ValueError, match="unconverted data remains"):
        _dt.strptime(naive, "%Y-%m-%d %H:%M:%S")

    # 而正确的那个不会抛
    assert fmt_dt_secs(ticky) == "2026-09-18 14:45:00"


def test_heartbeat_reader_tolerates_legacy_microsecond_values():
    """读的时候要宽容：线上历史数据里可能已经存在带微秒的心跳。

    写严格、读宽容 —— 否则一个陈年格式会让人连读都读不了。
    """
    hb = Heartbeat.from_dict({"last_time": "2026-09-18 14:45:00.878982",
                              "last_task": "old", "status": "alive"})
    assert hb.last_time == __import__("datetime").datetime(2026, 9, 18, 14, 45, 0, 878982)


# ------------------------------------------------------------
# 持仓三形态 + 认不出时必须失败
# ------------------------------------------------------------
def test_positions_decodes_all_three_wire_shapes_identically():
    """三种形态解码结果必须一致（线上确实三种都出现过）。

      A 包装 dict ／ B 裸 dict ／ C 旧的 list（执行端老版本遗留）

    形态 C 的存在本身就说明线上曾经换过格式 —— 所以兼容不是防御性编程。
    """
    holding = {"510050.SS": {"amount": 1000, "cost": 1.2346}}
    a = PositionSnapshot.from_dict({"update_time": "2026-09-18 14:45:00",
                                    "total_value": 1.0, "cash": 2.0,
                                    "positions": holding})
    b = PositionSnapshot.from_dict(holding)
    c = PositionSnapshot.from_dict([{"code": "510050.SS", "amount": 1000,
                                     "cost": 1.2346}])
    for snap in (a, b, c):
        assert snap.held_codes() == ["510050.XSHG"]
        assert snap.bare_codes == ["510050"]


def test_unrecognizable_positions_raise_instead_of_looking_empty():
    """★ 认不出的结构**必须失败**，不能静默变成「确认空仓」。

    这是失效方向的问题，不是健壮性问题：

        返回空快照 → 「账户是空的，可以放心买」 → **双持仓**
        抛异常     → 「持仓未知」 → 禁买（降级）

    两者在代码上只差一行，在风控上方向相反。线上对这种情况的处理就是
    判对账失败（线上实测），本仓库初版这里返回空快照 —— 方向反了。
    """
    for junk in (None, "字符串", 42, {"a": 1, "b": 2}):
        with pytest.raises(WireFormatError):
            PositionSnapshot.from_dict(junk)

    # 形态 A 里 positions 不是 dict（线上明确判失败，不退化）
    with pytest.raises(WireFormatError):
        PositionSnapshot.from_dict({"positions": [1, 2, 3], "cash": 1.0})


def test_legitimately_empty_positions_still_decode_to_empty():
    """但**合法的空持仓**仍要解成空快照 —— 空仓是正常状态，不是错误。

    区分点在于「结构认识不认识」，不是「有没有持仓」。
    这条与上一条是一对：少了它，上一条可以通过「一律抛异常」来作弊。
    """
    assert PositionSnapshot.from_dict({"positions": {}}).positions == {}
    assert PositionSnapshot.from_dict({}).positions == {}
    assert PositionSnapshot.from_dict([]).positions == {}


# ------------------------------------------------------------
# 心跳时间戳格式
# ------------------------------------------------------------
def test_heartbeat_wire_time_matches_the_producers_strftime_not_str():
    """线格式上，心跳时间用的是产生端的 `strftime`，不是 `str(datetime)`。

    线上产生端在这一处写的是 `current_dt.strftime("%Y-%m-%d %H:%M:%S")`
    （线上实测），而信号那边写的是 `str(current_dt)`（线上实测）——
    **同一个文件里两个格式化方式**。本仓库把它们分开成 `fmt_dt_secs`
    与 `fmt_dt` 两个函数，正是为了照抄这个差异，而不是「统一一下」。
    """
    from datetime import datetime as _dt

    ticky = _dt(2026, 9, 18, 14, 45, 0, 500000)
    assert Heartbeat(last_time=ticky).to_dict()["last_time"] == "2026-09-18 14:45:00"
    # 而 signals 那边**照抄线上**：str(datetime)，微秒在就有
    assert Signal(code=CLEAN_CODES[0], action="buy",
                  created_at=ticky).to_dict()["time"] == "2026-09-18 14:45:00.500000"
