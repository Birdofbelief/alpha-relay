# -*- coding: utf-8 -*-
"""`RetryingMarketData` 的三条行为，逐条钉住。

它此前**有实现、有 docstring、零测试** —— 而「读代码时完全正常、只有真载荷才暴露」
正是这一层最容易出的错（姊妹项目的真 bug 就长这样：备源因为一个无条件 setdefault
永远走不到，代码看着毫无问题）。

所以这里刻意用**有状态的假源**，而不是永远返回同一个结果的桩：
返回同一个结果的桩，会让「重试时真的重新取了一次」和「重试只是空转」长得一模一样。
`test_partial_then_complete_really_refetches` 就是那条变异体的拦网 ——
把 `quotes()` 里重取的那一行删掉，它必须红。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from signalchain.marketdata import RetryingMarketData
from signalchain.marketdata.base import MarketDataError, Quote, QuoteBatch


def _partial() -> QuoteBatch:
    return QuoteBatch(missing=["510050"])


def _complete() -> QuoteBatch:
    return QuoteBatch(quotes={"510050": Quote(code="510050", price=1.0)})


class ScriptedMarket:
    """按脚本逐次返回的假源。**每次调用都会前进一格**，这是关键。"""

    def __init__(self, batches: list[QuoteBatch]) -> None:
        self._batches = list(batches)
        self.calls = 0

    def quotes(self, codes):
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return batch

    def history(self, code: str, bars: int):
        raise MarketDataError("这个假源不提供历史")

    def describe(self) -> str:
        return "scripted"


def test_partial_then_complete_really_refetches():
    inner = ScriptedMarket([_partial(), _complete()])
    retry = RetryingMarketData(inner, attempts=2, interval=0.01)

    batch = retry.quotes(["510050"])

    assert batch.ok is True
    assert retry.stats["retries"] == 1
    # ★ 变异拦截点：删掉重试分支里的 `batch = self.inner.quotes(codes)`，
    #   这条断言会变成 1（只调了一次），测试红。
    assert inner.calls == 2, "重试必须真的重新问源，而不是拿残表空转"


def test_deadline_stops_retrying_and_returns_what_it_has():
    inner = ScriptedMarket([_partial()])
    retry = RetryingMarketData(
        inner, attempts=3, interval=5.0,
        deadline=lambda: datetime.now() + timedelta(seconds=1),
    )

    started = time.monotonic()
    batch = retry.quotes(["510050"])
    elapsed = time.monotonic() - started

    assert batch.ok is False, "到期时应当把当前的（残缺）结果交给上层 fail-closed"
    assert retry.stats["aborted"] >= 1
    assert retry.stats["retries"] == 0, "已经到截止时间就不该再睡一轮"
    assert elapsed < 1.0, "截止判断必须在 sleep 之前"
    assert inner.calls == 1


def test_gives_up_after_attempts_and_keeps_the_last_batch():
    inner = ScriptedMarket([_partial()])
    retry = RetryingMarketData(inner, attempts=2, interval=0.01)

    batch = retry.quotes(["510050"])

    assert batch.ok is False
    assert retry.stats["retries"] == 2
    assert inner.calls == 3          # 1 次首发 + 2 次重试


def test_history_is_retried_and_then_propagates():
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def quotes(self, codes):
            return _complete()

        def history(self, code, bars):
            self.calls += 1
            if self.calls == 1:
                raise MarketDataError("第一次失败")
            return pd.DataFrame({"date": pd.to_datetime(["2026-09-17"]),
                                 "close": [1.0]})

        def describe(self) -> str:
            return "flaky"

    inner = Flaky()
    retry = RetryingMarketData(inner, attempts=2, interval=0.01)

    frame = retry.history("510050", 1)
    assert list(frame["close"]) == [1.0]
    assert inner.calls == 2

    inner.calls = 0
    inner.history = lambda code, bars: (_ for _ in ()).throw(
        MarketDataError("永远失败"))
    with pytest.raises(MarketDataError):
        retry.history("510050", 1)


def test_describe_mentions_the_inner_source():
    retry = RetryingMarketData(ScriptedMarket([_complete()]), attempts=2)
    assert "scripted" in retry.describe()
