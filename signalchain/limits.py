# -*- coding: utf-8 -*-
"""涨跌停幅度：**交易所规则，不是策略参数**。

## 为什么它属于链路层

决定「这笔单能不能成交」的东西，和决定「该买哪只」的东西，是两回事。
涨跌停属于前者：同一个价格，在涨停板上买不到、在跌停板上卖不掉，
而**回测里没写这一条，就会一路顺畅地成交到一个实盘永远拿不到的价格**。
这类分歧不会报错，只会让纸面结果系统性偏乐观 —— 属于最难自查的一类。

## 规则（A 股，按标的区分）

| 标的 | 幅度 |
|---|---|
| 主板 / 绝大多数 ETF | ±10% |
| 科创板 ETF（`588` 前缀） | ±20% |
| 创业板 ETF 等（**各家取整口径不同，不内置**） | ±20%，用 `SC_LIMIT_20_CODES` 显式列出 |

⚠️ **刻意不内置一份「哪些代码是 20%」的名单。**
那份名单只能靠逐个标的的实测数据得到，写进框架等于把某个人的池子硬编码，
而且它会随时间过期（新发基金、改规则）。框架给的是**机制**：
默认 10%，`588` 前缀 20%，其余由使用者用配置补。

## 价格取整

交易所按 **0.01 元**取整，且是**四舍五入**，不是 Python 的银行家舍入
（`round(2.675, 2)` 给 2.67）。两者在边界价上差一分钱，
而那一分钱正好决定「能不能成交」。所以这里用 `Decimal` + `ROUND_HALF_UP`。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .models import to_jq

#: 默认涨跌停幅度。
DEFAULT_RATE = 0.10

#: 内置按 20% 处理的前缀。目前只有科创板 ETF。
WIDE_PREFIXES: tuple[str, ...] = ("588",)

#: 宽幅标的的幅度。
WIDE_RATE = 0.20

_TICK = Decimal("0.01")


def _round_tick(value: float | Decimal) -> float:
    """按交易所口径取整到 0.01（四舍五入）。

    ⚠️ **必须传 `Decimal`**，或传一个已经算好的 float。`limit_prices()` 里
    先用 `Decimal(str(prev_close))` 做乘法，就是为了避开
    `1.05 * 0.9 == 0.9449999999999998` 这种二进制误差 ——
    它会让跌停价差一分钱，而那一分钱决定「能不能卖掉」。
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(_TICK, rounding=ROUND_HALF_UP))


def limit_rate(code: str, twenty_codes: Iterable[str] = ()) -> float:
    """该标的的涨跌停幅度。

    优先级：显式清单 > 内置前缀 > 默认 10%。

    ★ **清单两边都先归一化再比。** 信号里的代码是 `.XSHG` 形式，而人往
    `SC_LIMIT_20_CODES` 里填的几乎一定是裸六位数（券商盘面就是这么显示的）。
    不归一化的话那条配置永远匹配不上 —— 而且**不会报错**，只会静静地
    把一只 20% 的标的按 10% 判涨停。
    """
    target = to_jq(code)
    if any(target == to_jq(c) for c in twenty_codes):
        return WIDE_RATE
    if target.startswith(WIDE_PREFIXES):
        return WIDE_RATE
    return DEFAULT_RATE


def limit_prices(prev_close: float | None, code: str,
                 twenty_codes: Iterable[str] = (),
                 rate: float | None = None) -> tuple[float, float] | None:
    """返回 `(涨停价, 跌停价)`；缺前收盘价（或它不是正数）时返回 `None`。

    ★ 返回 `None` 的意思是「**不知道**」，不是「没有涨跌停」——
    调用方必须把这两种情况分开处理（见 `paper.py`）：不知道就退回原来的行为，
    而不是把 `None` 当成 0 去比较。
    """
    try:
        base = float(prev_close or 0.0)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    # ★ 不能在这里写 `DEFAULT_RATE`：`588` 前缀与 `twenty_codes` 的 20%
    #   必须走 `limit_rate()` 那一条，否则宽幅标的会被按 10% 判涨停。
    r = limit_rate(code, twenty_codes) if rate is None else float(rate)
    # ★ 用 Decimal 做乘法，不用 float：`1.05 * 0.9` 在二进制里是
    #   0.9449999999999998，取整后会变成 0.94，比交易所口径低一分钱。
    base_d, rate_d = Decimal(str(base)), Decimal(str(r))
    return (_round_tick(base_d * (1 + rate_d)),
            _round_tick(base_d * (1 - rate_d)))
