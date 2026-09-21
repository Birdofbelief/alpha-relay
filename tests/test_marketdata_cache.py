# -*- coding: utf-8 -*-
"""历史缓存：新鲜度三档、复权口径、未收盘 K 线，以及 OHLC 校验。

这一层是「行情层最容易被低估的一层」—— 它出的错几乎都是**静默**的：
拿旧数据算出的信号照样合规、照样发得出去，只是它是错的。
所以这里的测试大多在断言「它有没有**停下来**」。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from signalchain.calendar import make_static_calendar, weekday_calendar
from signalchain.marketdata import cache as cache_mod
from signalchain.marketdata.base import (
    MarketDataError,
    Quote,
    QuoteBatch,
    detect_adjustment_jump,
    validate_history,
)
from signalchain.marketdata.cache import CachedMarketData
from signalchain.marketdata.fake import FakeMarketData

from .conftest import CLEAN_CODES

NOW = datetime(2026, 9, 18, 14, 45, 0)
CODE = CLEAN_CODES[0]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """把缓存模块里的 `datetime.now()` 钉死。

    缓存的新鲜度判断完全依赖「现在是什么时候」。不钉死它，测试就会随
    运行日期改变结果 —— 而这种 flaky 比稳定地红更坏：它会训练人忽略失败。
    """
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(cache_mod, "datetime", _Frozen)
    return NOW


def _calendar():
    """2026-09 的工作日当作交易日（离线，不联网）。"""
    return weekday_calendar(date(2026, 9, 1), date(2026, 9, 30))


def _frame_ending(day: date, bars: int = 40) -> pd.DataFrame:
    """造一段截止到 `day` 的连续日线。"""
    days = [day - timedelta(days=bars - 1 - i) for i in range(bars)]
    closes = [1.0 + i * 0.001 for i in range(bars)]
    return pd.DataFrame({
        "date": days, "open": closes, "close": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "volume": [1000] * bars,
    })


def _fake(**kw) -> FakeMarketData:
    fake = FakeMarketData([CODE], **kw)
    fake.now = NOW
    return fake


# ------------------------------------------------------------
# 实时行情：永不缓存
# ------------------------------------------------------------
def test_quotes_are_never_cached(tmp_path):
    """实时行情**每次都要真的去取**，而且一个字节都不落盘。

    给快照加缓存是危险的：进程重启后拿到一份「看起来正常」的陈旧报价，
    而所有闸门都会判它合格（数值确实都合法），于是拿昨天的价格下单。
    快照的新鲜度**无法从数据本身判断** —— 这正是它不能被缓存的原因。
    """
    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, _calendar())
    for _ in range(3):
        cached.quotes([CODE])
    assert fake.quote_calls == 3
    assert list(tmp_path.glob("*.csv")) == []


def test_quotes_pass_through_unchanged(tmp_path):
    fake = _fake(halt={CODE})
    batch = CachedMarketData(fake, tmp_path, _calendar()).quotes([CODE])
    assert isinstance(batch, QuoteBatch)
    assert batch.halted_codes() == [CODE]


# ------------------------------------------------------------
# 新鲜度三档
# ------------------------------------------------------------
def test_first_call_fetches_and_persists(tmp_path):
    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, _calendar())
    frame = cached.history(CODE, 60)

    assert fake.history_calls == 1
    assert not frame.empty
    assert list(tmp_path.glob("*.csv")), "首次取数应当落盘"


def test_second_call_uses_cache_when_fresh(tmp_path):
    """缓存够新时**不再联网**。

    这条同时验证「先试在线」没写反：如果顺序反了，就变成「有缓存就永远不更新」，
    缓存会退化成一份越来越旧、却从不报错的快照。
    """
    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, _calendar())
    cached.history(CODE, 60)
    cached.history(CODE, 60)
    assert fake.history_calls == 1
    assert cached.stats["hit"] >= 1


def test_unclosed_bar_is_not_persisted(tmp_path):
    """**未收盘的那根 K 线绝不能落盘。**

    盘中取回来的「今天」是一根还在变的 K 线。若把它存下来，明天它就会被
    当成「已收盘的今天」正常读出来用 —— 而且用的是上午十点那个版本。
    读取时丢弃**不能**补救已经写进磁盘的脏行，所以必须在写之前丢。

    这里断言的是磁盘上的内容（不是返回值）—— 返回值一直是对的，
    bug 恰恰只在磁盘上。
    """
    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, _calendar())
    cached.history(CODE, 60)

    on_disk = pd.read_csv(next(tmp_path.glob("*.csv")))
    last = pd.to_datetime(on_disk["date"]).dt.date.max()
    assert last < NOW.date(), f"磁盘上出现了今天（{last}）的未收盘 K 线"


def test_one_day_behind_is_usable_but_flagged(tmp_path):
    """落后**1 个交易日**：告警但可用。

    免费行情源当天日线经常要收盘后才补齐，此时缓存天然落后一天。
    若判它失效，每个交易日的前半程都会整体跳过 —— 而策略要的恰恰是
    「截至昨收」的序列，昨收本来就是完整的。
    """
    cal = _calendar()
    yesterday = cal.previous_trading_day(NOW)
    two_days_back = cal.previous_trading_day(yesterday)
    assert yesterday and two_days_back

    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, cal)
    cached._store(CODE, _frame_ending(two_days_back))

    # 在线取数失败 → 退缓存；缓存落后 1 天 → 可用
    fake.history = lambda code, bars: (_ for _ in ()).throw(RuntimeError("网络断了"))
    frame = cached.history(CODE, 60)
    assert cached.stats["stale_used"] == 1
    assert not frame.empty


def test_two_days_behind_is_skipped(tmp_path):
    """落后 **≥2 个交易日**：跳过这一只（不是整批停）。

    没有它的信号是拿旧数据算出来的；而其它标的可能还好好的，
    所以停这一只就够了。注意这里是**抛异常**，让上层把这只排除掉 ——
    返回旧数据比报错危险得多，因为它不报错。
    """
    cal = _calendar()
    three_back = cal.previous_trading_day(cal.previous_trading_day(
        cal.previous_trading_day(NOW)))
    assert three_back

    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, cal, max_stale_days=1)
    cached._store(CODE, _frame_ending(three_back))
    fake.history = lambda code, bars: (_ for _ in ()).throw(RuntimeError("网络断了"))

    with pytest.raises(MarketDataError) as e:
        cached.history(CODE, 60)
    assert "落后" in str(e.value)
    assert cached.stats["stale_skip"] == 1


def test_no_cache_and_no_network_raises(tmp_path):
    """既没缓存又取不到 → 抛异常，**不返回空表**。

    空表会被调用方当成「这只标的历史很短」，从而静默走错分支。
    """
    fake = _fake()
    fake.history = lambda code, bars: (_ for _ in ()).throw(RuntimeError("网络断了"))
    with pytest.raises(MarketDataError):
        CachedMarketData(fake, tmp_path, _calendar()).history(CODE, 60)


def test_missing_calendar_refuses_to_guess(tmp_path):
    """日历不可用 → 无从判断新旧 → **保守跳过**，而不是赌它新。

    这是「零日历」那条保守原则在缓存层的落实：宁可这一只不参与，
    也不拿一份不知道多旧的数据去算信号。
    """
    empty = make_static_calendar([])
    fake = _fake()
    cached = CachedMarketData(fake, tmp_path, empty)
    cached._store(CODE, _frame_ending(date(2026, 9, 17)))
    fake.history = lambda code, bars: (_ for _ in ()).throw(RuntimeError("网络断了"))

    with pytest.raises(MarketDataError) as e:
        cached.history(CODE, 60)
    assert "日历" in str(e.value)


# ------------------------------------------------------------
# 复权口径
# ------------------------------------------------------------
def test_adjust_mode_change_discards_the_whole_cache(tmp_path):
    """复权口径变了 → **整份丢弃重取**，绝不在旧数据后面接新口径的尾巴。

    两份口径不同的序列拼在一起，会在拼接处凭空造出一个巨大缺口，
    而策略会把它读成「趋势破位」—— 触发一次不该有的卖出。
    """
    cal = _calendar()
    fake = _fake()
    qfq = CachedMarketData(fake, tmp_path, cal, adjust="qfq")
    qfq.history(CODE, 60)
    assert fake.history_calls == 1

    hfq = CachedMarketData(fake, tmp_path, cal, adjust="hfq")
    hfq.history(CODE, 60)
    assert fake.history_calls == 2, "口径变了必须重新取数，不能沿用旧缓存"


def test_adjustment_jump_is_rejected_and_not_cached(tmp_path):
    """单日变动超过涨跌停限制 → 判为未复权数据 → **拒用且不缓存**。

    危害非常具体：策略会把那个凭空的除权缺口当成真实破位。A 股 ETF 涨跌停
    是 10%，所以「单日 >25%」几乎只可能由复权口径问题触发。
    """
    fake = _fake(jump_on=(CODE, 20))
    cached = CachedMarketData(fake, tmp_path, _calendar())

    with pytest.raises(MarketDataError):
        cached.history(CODE, 60)
    assert cached.stats["rejected"] == 1
    assert list(tmp_path.glob("*.csv")) == [], "被拒的数据不该落盘"


# ------------------------------------------------------------
# 纯函数：OHLC 校验与跳变检测
# ------------------------------------------------------------
def test_validate_history_drops_impossible_rows():
    """非法 OHLC 行被丢弃，并**报告丢了几行**。

    保留问题描述而不是静默丢行：丢了多少行是判断数据源是否劣化的唯一线索。
    """
    df = _frame_ending(date(2026, 9, 17), bars=5)
    df.loc[1, "high"] = df.loc[1, "low"] - 1          # high < low
    df.loc[2, "close"] = 0.0                          # 非正价格
    df.loc[3, "open"] = None                          # 缺失值
    clean, problems = validate_history(df)
    assert len(clean) == 2
    assert problems and "非法" in problems[0]


def test_validate_history_flags_missing_columns():
    """缺列 → 返回空表**并说明原因**，不要让缺列往后传。"""
    clean, problems = validate_history(pd.DataFrame({"date": [1], "close": [1.0]}))
    assert clean.empty
    assert any("缺少列" in p for p in problems)


def test_validate_history_handles_empty_input():
    clean, problems = validate_history(None)
    assert clean.empty and problems == ["空表"]


def test_detect_adjustment_jump_ignores_normal_moves():
    """正常波动不许误报 —— 误报会让好数据被拒用（即策略无端瘫痪）。"""
    assert detect_adjustment_jump(_frame_ending(date(2026, 9, 17), bars=30)) is None


def test_detect_adjustment_jump_catches_unadjusted_gap():
    df = _frame_ending(date(2026, 9, 17), bars=30)
    df.loc[15, "close"] = df.loc[14, "close"] * 0.6    # 凭空的 -40% 缺口
    jump = detect_adjustment_jump(df)
    assert jump is not None and "复权" in jump


def test_detect_adjustment_jump_needs_enough_bars():
    """序列太短就不判 —— 两根 K 线判不出「跳变」还是「本来就这样」。"""
    assert detect_adjustment_jump(_frame_ending(date(2026, 9, 17), bars=2)) is None
    assert detect_adjustment_jump(None) is None


# ------------------------------------------------------------
# 停牌与脏值的区别（数值层面）
# ------------------------------------------------------------
def test_halted_quote_has_no_percentage_change():
    """停牌标的的涨跌幅是 `None`，**不是 -100%**。

    价格字段是 0，照公式算是 `-100%` —— 而那是「今天一笔都没成交」，
    不是「跌没了」。混同的后果很具体：策略看到 -100% 会当成毁灭性破位，
    触发一次不该有的卖出。本模块的分类原则是「停牌只跳过这一只」，
    返回 None 才是在数值层面落实了那个原则。
    """
    halted = Quote(code=CODE, price=0.0, prev_close=1.0,
                   open=0.0, high=0.0, low=0.0)
    assert halted.is_halted and halted.pct_change is None

    dirty = Quote(code=CODE, price=None, prev_close=1.0)
    assert not dirty.is_halted and dirty.pct_change is None

    normal = Quote(code=CODE, price=1.1, prev_close=1.0,
                   open=1.0, high=1.1, low=1.0)
    assert normal.pct_change == pytest.approx(0.1)
