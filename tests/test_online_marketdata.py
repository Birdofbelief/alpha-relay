# -*- coding: utf-8 -*-
"""在线行情源的两条静默失效路径。

它们共同的特征：**假行情/纸面测试全绿，一接真实源就整批 missing**。

1. `to_sina("510050.XSHG")` 原来输出 `sh510050.XSHG`（后缀没剥），
   行情源把它当成不存在的代码 → 整行不返回 → 整批行情不可信。
2. 重试层原来只调 `inner.quotes()`，而源内部有 TTL 快照缓存 ——
   重试读回同一份残表，日志写「重试了」，实际一次网络请求都没再发。
"""
from __future__ import annotations

from signalchain.marketdata.base import MarketDataError, Quote, QuoteBatch
from signalchain.marketdata.online import (
    OnlineMarketData,
    RetryingMarketData,
    to_sina,
)


# ------------------------------------------------------------
# 代码形式：带后缀也要能查
# ------------------------------------------------------------
def test_to_sina_strips_exchange_suffixes():
    assert to_sina("510050") == "sh510050"
    assert to_sina("510050.XSHG") == "sh510050"
    assert to_sina("159919.XSHE") == "sz159919"
    assert to_sina("510050.SS") == "sh510050"
    assert to_sina("159919.SZ") == "sz159919"


class _Resp:
    status_code = 200
    encoding = "gbk"
    # 开, 昨收, 现价, 高, 低
    text = 'var hq_str_sh510050="50ETF,1.000,1.000,1.010,1.020,0.990";\n'


def test_quotes_accepts_suffixed_codes(monkeypatch):
    """★ 请求 URL 里必须是裸代码；带 `510050.XSHG` 的请求也要能对上结果。"""
    from signalchain.marketdata import online

    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(online.requests, "get", fake_get)
    om = OnlineMarketData()

    batch = om.quotes(["510050.XSHG"])

    assert seen and "sh510050" in seen[0]
    assert ".XSHG" not in seen[0], "后缀被拼进了行情 URL —— 整批会查不到"
    assert set(batch.quotes) == {"510050.XSHG"}
    assert batch.quotes["510050.XSHG"].price == 1.010
    assert batch.missing == []


# ------------------------------------------------------------
# 重试必须真的重取，不能空转
# ------------------------------------------------------------
class _CachingSource:
    """内部有快照缓存：不丢弃缓存时，每次 quotes() 都返回同一份残表。"""

    def __init__(self) -> None:
        self.cached = True
        self.calls = 0
        self.drops = 0

    def quotes(self, codes):
        self.calls += 1
        if self.cached:
            return QuoteBatch(missing=list(codes))
        return QuoteBatch(quotes={c: Quote(code=c, price=1.0) for c in codes})

    def invalidate_snapshot(self):
        self.drops += 1
        self.cached = False

    def history(self, code, bars):
        raise MarketDataError("这个假源不提供历史")

    def describe(self):
        return "caching"


def test_retry_drops_the_cached_partial_snapshot_before_refetching():
    """★ 变异拦截：删掉重试前的 `invalidate_snapshot()`，这条必红。

    没有它，`inner.quotes()` 会第二次返回同一份残表 —— 重试计数照涨、
    日志照写，实际没有任何新请求。
    """
    inner = _CachingSource()
    retry = RetryingMarketData(inner, attempts=2, interval=0.01)

    batch = retry.quotes(["510050"])

    assert batch.ok is True
    assert inner.calls == 2, "重试必须真的重新问源"
    assert inner.drops == 1, "重取前必须显式丢弃源内部的残表缓存"
    assert retry.stats["retries"] == 1
