# -*- coding: utf-8 -*-
"""确定性假行情 —— 测试与演示用。

**它是整套测试能离线跑的前提。** 链路的绝大多数逻辑（闸门、缓存新鲜度、
调度、所有权、纸面执行）都与「价格从哪来」无关，用假数据测它们既快又稳，
而且能在几毫秒内造出真实行情里很难等的场景：停牌、整批缺失、NaN 脏值、除权跳变。

刻意做成**确定性**：否则 flaky 测试会让人开始忽略失败，
而忽略失败比没有测试更坏。

★ 「确定性」在这里曾经是**假的**：早先用内建 `hash()` 给每个标的算价格偏移，
而 CPython 对 str 的 `hash()` 每个进程都不同（PYTHONHASHSEED 随机化）。
后果不是「偶尔不一样」，而是**每次运行都换一套价格** —— 同一份演示连跑三次，
三次挑中三只不同的 ETF；任何断言具体标的的测试都会有三分之二的概率红。
`tests/test_fake_determinism.py` 现在用「跨进程比对」把这条钉住。
"""
from __future__ import annotations

import math
import zlib
from datetime import datetime, timedelta

import pandas as pd

from ..models import to_bare
from .base import Quote, QuoteBatch


class FakeMarketData:
    """按规则生成行情的假数据源。

    参数：
        codes:       标的池
        halt:        停牌的代码（价格全 0、字段完整 —— 与真实停牌一致）
        missing:     整行不返回的代码（模拟管道异常）
        invalid:     返回 NaN 的代码（模拟上游脏值）
        base_price:  起始价格
        drift:       每日收益率（用来造趋势）
        history_bars: 每次 history() 返回多少根
        jump_on:     指定代码在某一天制造一次假跳变（模拟未复权）
    """

    def __init__(
        self,
        codes: list[str],
        halt: set[str] | None = None,
        missing: set[str] | None = None,
        invalid: set[str] | None = None,
        base_price: float = 1.0,
        drift: float = 0.001,
        history_bars: int = 150,
        jump_on: tuple[str, int] | None = None,
    ) -> None:
        self.codes = list(codes)
        self.halt = set(halt or ())
        self.missing = set(missing or ())
        self.invalid = set(invalid or ())
        self.base_price = base_price
        self.drift = drift
        self.history_bars = history_bars
        self.jump_on = jump_on
        self.quote_calls = 0
        self.history_calls = 0
        #: 允许测试指定「当下时刻」，让生成的日期可控
        self.now: datetime | None = None

    # ---------------- 行情 ----------------

    def _price_for(self, code: str, idx: int) -> float:
        # ★ 先归一化代码格式再哈希。
        # 信号里的代码是 `510500.XSHG`，策略的标的池里是 `510500` —— 同一个标的。
        # 若直接对原始字符串取哈希，两者会得到**不同的价格**，
        # 表现为「策略按 1.041 下单，执行端看到 1.0630 所以不成交」。
        # 这类 bug 只在特定数据源上出现，而且看起来像策略问题，实际是接口问题。
        bare = to_bare(code)
        # ★★ 用 crc32 而不是内建 `hash()`。
        # 这不是风格问题：**CPython 对 str 的 `hash()` 每个进程都不一样**
        # （PYTHONHASHSEED 随机化，防哈希碰撞攻击）。用它等于让「确定性假行情」
        # 每次运行都换一套价格 —— 于是同一份演示每次挑中不同的标的，
        # 而任何断言「策略选中了 X」的测试会**三分之二的概率红**，
        # 且红得毫无规律，最后被误判成 flaky 而加个重试糊过去。
        # 这个 bug 真的发生过：连跑三次，三次买的是三只不同的 ETF。
        offset = (zlib.crc32(bare.encode("utf-8")) % 100) / 1000.0
        return round(self.base_price + offset + idx * self.drift, 4)

    def quotes(self, codes) -> QuoteBatch:
        self.quote_calls += 1
        requested = list(codes)
        batch = QuoteBatch(fetched_at=self.now or datetime.now())

        idx = 7  # 任意固定的「当前」位置，保证可复现
        for code in requested:
            bare = to_bare(code)
            if bare in self.missing:
                batch.missing.append(code)
                continue
            if bare in self.invalid:
                batch.invalid.append(code)
                batch.quotes[code] = Quote(
                    code=code, name=f"标的{bare}",
                    price=None, prev_close=None, open=None, high=None, low=None,
                )
                continue
            if bare in self.halt:
                prev = self._price_for(bare, idx - 1)
                # 停牌：字段完整、数值可信、价格为 0 —— 与真实行情源的返回一致
                batch.quotes[code] = Quote(
                    code=code, name=f"标的{bare}",
                    price=0.0, prev_close=prev, open=0.0, high=0.0, low=0.0,
                )
                continue
            price = self._price_for(bare, idx)
            prev = self._price_for(bare, idx - 1)
            batch.quotes[code] = Quote(
                code=code, name=f"标的{bare}",
                price=price, prev_close=prev,
                open=round(prev * 1.001, 4),
                high=round(max(price, prev) * 1.002, 4),
                low=round(min(price, prev) * 0.998, 4),
            )
        return batch

    # ---------------- 历史 ----------------

    def history(self, code: str, bars: int) -> pd.DataFrame:
        self.history_calls += 1
        code = to_bare(code)
        n = min(bars, self.history_bars)
        end = (self.now or datetime(2026, 9, 18)).date()
        dates = [end - timedelta(days=n - i - 1) for i in range(n)]

        closes = []
        for i in range(n):
            closes.append(self._price_for(code, i))

        if self.jump_on and self.jump_on[0] == code:
            pos = self.jump_on[1] % max(n, 1)
            # 制造一次远超涨跌停的跳变 —— 模拟未复权序列的除权缺口
            closes[pos] = closes[pos] * 0.6

        return pd.DataFrame({
            "date": dates,
            "open": [round(c * 0.999, 4) for c in closes],
            "close": closes,
            "high": [round(c * 1.01, 4) for c in closes],
            "low": [round(c * 0.99, 4) for c in closes],
            "volume": [100000 + i for i in range(n)],
        })

    def describe(self) -> str:
        tags = []
        if self.halt:
            tags.append(f"停牌{len(self.halt)}")
        if self.missing:
            tags.append(f"缺失{len(self.missing)}")
        if self.invalid:
            tags.append(f"脏值{len(self.invalid)}")
        return f"fake({len(self.codes)}只" + ("," + ",".join(tags) if tags else "") + ")"


def make_flat_history(bars: int = 150, price: float = 1.0,
                      end: datetime | None = None) -> pd.DataFrame:
    """一条不涨不跌的历史，用于只想验证管道、不关心策略的测试。"""
    end = end or datetime(2026, 9, 18)
    dates = [end.date() - timedelta(days=bars - i - 1) for i in range(bars)]
    return pd.DataFrame({
        "date": dates,
        "open": [price] * bars,
        "close": [price] * bars,
        "high": [price * 1.005] * bars,
        "low": [price * 0.995] * bars,
        "volume": [1000] * bars,
    })
