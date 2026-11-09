# -*- coding: utf-8 -*-
"""在线行情源的两条静默失效路径。

它们共同的特征：**假行情/纸面测试全绿，一接真实源就整批 missing**。

1. `to_sina("510050.XSHG")` 原来输出 `sh510050.XSHG`（后缀没剥），
   行情源把它当成不存在的代码 → 整行不返回 → 整批行情不可信。
2. 重试层原来只调 `inner.quotes()`，而源内部有 TTL 快照缓存 ——
   重试读回同一份残表，日志写「重试了」，实际一次网络请求都没再发。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

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
# 快照缓存必须按「问过哪些代码」来判断，不能只看 TTL
# ------------------------------------------------------------
class _TextResp:
    status_code = 200
    encoding = "gbk"

    def __init__(self, text: str) -> None:
        self.text = text


def _sina_lines(url: str) -> str:
    rows = []
    if "sh510050" in url:
        rows.append('var hq_str_sh510050="50ETF,1.000,1.000,1.010,1.020,0.990";')
    if "sz159919" in url:
        rows.append('var hq_str_sz159919="300ETF,4.000,4.000,4.100,4.200,3.900";')
    return "\n".join(rows) + "\n"


def test_a_new_code_forces_one_refetch_within_the_ttl(monkeypatch):
    """★ TTL 内的快照只覆盖**上次问过的代码**。

    原实现只要快照没过期就直接拿它比对：第一次问 510050、第二次问 159919 时，
    159919 **必然 missing，而且 TTL 内永远不会补请求** —— 表现为「这只标的
    行情一直拿不到」，排查时极易误判成上游不提供它。更糟的是它会一路
    走到 quote 闸门，把整轮判成「行情不可信」。
    """
    from signalchain.marketdata import online

    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _TextResp(_sina_lines(url))

    monkeypatch.setattr(online.requests, "get", fake_get)
    om = OnlineMarketData(snapshot_ttl=300.0)

    first = om.quotes(["510050"])
    assert first.ok and len(seen) == 1

    second = om.quotes(["159919"])
    assert second.ok, f"新代码在 TTL 内被当成 missing: {second.missing}"
    assert len(seen) == 2, "新代码必须触发一次补请求"
    assert "sz159919" in seen[1]

    # 已经问过的代码不再重复请求 —— 配额是有限的
    third = om.quotes(["159919"])
    assert third.ok and len(seen) == 2, "同一批已问过的代码不该在 TTL 内反复重取"


def test_a_requested_but_absent_code_is_not_refetched_every_call(monkeypatch):
    """问过、但源确实没返回的那一行：不重复请求（重取结果不会变）。"""
    from signalchain.marketdata import online

    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return _TextResp('var hq_str_sh510050="50ETF,1,1,1.01,1.02,0.99";\n')

    monkeypatch.setattr(online.requests, "get", fake_get)
    om = OnlineMarketData(snapshot_ttl=300.0)

    assert om.quotes(["159919"]).missing == ["159919"]
    assert om.quotes(["159919"]).missing == ["159919"]
    assert len(seen) == 1, "已经问过且确实没有的代码不该在 TTL 内反复重取"


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
