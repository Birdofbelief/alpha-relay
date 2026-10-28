# -*- coding: utf-8 -*-
"""涨跌停：交易所规则进了链路层，纸面成交才不会再「买到买不到的价」。

这一组钉两件事：

1. **幅度怎么算** —— 默认 10%、`588` 前缀 20%、显式清单优先；价格按 0.01 元
   **四舍五入**（不是 Python 的银行家舍入），因为差一分钱就决定能不能成交。
2. **纸面成交怎么用它** —— 涨停买不到、跌停卖不掉，两者都应当是 `submitted`
   （挂单等待），而不是 `filled`。

★ `submitted` 不是失败也不是成功，它和「停牌挂单等待」是同一个语义。
把它判成 failed 会让人以为下单被拒；判成 filled 会凭空多出一笔成交。
"""
from __future__ import annotations

import pytest

from signalchain.config import Config
from signalchain.limits import DEFAULT_RATE, WIDE_RATE, limit_prices, limit_rate
from signalchain.marketdata.base import MarketDataError, Quote, QuoteBatch
from signalchain.models import (
    ACTION_BUY,
    ACTION_SELL,
    EXEC_FILLED,
    EXEC_SKIPPED,
    EXEC_SUBMITTED,
    Signal,
    to_jq,
)
from signalchain.paper import PaperExecutor
from signalchain.transport.file import FileTransport

from .conftest import CLEAN_CODES, FIXED_NOW

CODE = CLEAN_CODES[0]
WIDE_CODE = "588999"      # 不存在的科创板代码：只用来验前缀规则
EXTRA_CODE = "234567"     # conftest 里的合成代码


# ------------------------------------------------------------
# 幅度与价格
# ------------------------------------------------------------

def test_default_rate_is_ten_percent():
    assert limit_rate(CODE) == DEFAULT_RATE


def test_588_prefix_is_twenty_percent():
    assert limit_rate(WIDE_CODE) == WIDE_RATE


def test_explicit_list_wins_over_the_default():
    assert limit_rate(EXTRA_CODE) == DEFAULT_RATE
    assert limit_rate(EXTRA_CODE, twenty_codes=[EXTRA_CODE]) == WIDE_RATE


def test_limit_prices_round_half_up_to_the_tick():
    # 1.05 × 1.1 = 1.155 → 交易所口径 1.16（Python 的 round() 会给 1.15 或 1.16，
    # 取决于二进制表示；这里要的是确定的那一个）。
    up, down = limit_prices(1.05, CODE)
    assert up == 1.16
    assert down == 0.95          # 1.05 × 0.9 = 0.945 → 0.95
    assert limit_prices(1.0, CODE) == (1.10, 0.90)


@pytest.mark.parametrize("prev", [None, 0, 0.0, -1.0, "bad"])
def test_limit_prices_returns_none_when_prev_close_is_unusable(prev):
    """★ None 的意思是「不知道」，不是「没有涨跌停」—— 调用方必须分开处理。"""
    assert limit_prices(prev, CODE) is None


# ------------------------------------------------------------
# 纸面执行端
# ------------------------------------------------------------

class _QuoteMarket:
    """永远返回同一个报价的假行情源。"""

    def __init__(self, quote: Quote) -> None:
        self._quote = quote

    def quotes(self, codes):
        return QuoteBatch(quotes={self._quote.code: self._quote})

    def history(self, code, bars):
        raise MarketDataError("这一组不需要历史")

    def describe(self) -> str:
        return "quote-stub"


def _executor(tmp_path, price: float, prev_close: float | None,
              limit_20: tuple[str, ...] = ()) -> PaperExecutor:
    config = Config(runtime_dir=tmp_path, limit_20_codes=frozenset(limit_20))
    # ★ 报价的 key 必须是 `Signal` 归一化之后的代码（`.XSHG`），否则执行端按
    #   `sig.code` 取不到这一条，会退回「行情不可信」—— 那就测不到涨跌停了。
    quote = Quote(code=to_jq(CODE), price=price, prev_close=prev_close,
                  open=price, high=price, low=price)
    return PaperExecutor(config, _QuoteMarket(quote),
                         FileTransport(tmp_path / "ch"), initial_cash=1_000_000)


def _signal(action: str, price: float = 0.0) -> Signal:
    return Signal(code=CODE, action=action, amount=100, price=price,
                  created_at=FIXED_NOW)


def test_buy_at_limit_up_is_not_filled(tmp_path):
    ex = _executor(tmp_path, price=1.10, prev_close=1.00)

    record = ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)   # noqa: SLF001

    assert record.status == EXEC_SUBMITTED
    assert "涨停" in record.reason


def test_buy_just_below_limit_up_still_fills(tmp_path):
    ex = _executor(tmp_path, price=1.09, prev_close=1.00)

    record = ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)   # noqa: SLF001

    assert record.status == EXEC_FILLED


def test_sell_at_limit_down_is_not_filled(tmp_path):
    ex = _executor(tmp_path, price=0.90, prev_close=1.00)
    ex.account.buy(to_jq(CODE), 1000, 1.00)     # key 必须是归一化后的代码

    record = ex._try_fill(_signal(ACTION_SELL), FIXED_NOW)  # noqa: SLF001

    assert record.status == EXEC_SUBMITTED
    assert "跌停" in record.reason


def test_no_position_beats_the_limit_down_reason(tmp_path):
    """没有持仓时，「无持仓」才是原因 —— 先报「跌停」会把人引到错的方向。"""
    ex = _executor(tmp_path, price=0.90, prev_close=1.00)

    record = ex._try_fill(_signal(ACTION_SELL), FIXED_NOW)  # noqa: SLF001

    assert record.status == EXEC_SKIPPED
    assert "持仓" in record.reason


def test_unknown_prev_close_keeps_the_old_behaviour(tmp_path):
    """昨收是 0（字段可信但算不出幅度）⇒ 不判涨跌停，照常成交。

    ★ 这里用的是 `0.0` 而不是 `None`：在 `Quote` 的语义里 `None` 是
    「这个值不可信」，整条报价会被判 `not is_finite` 而退回「行情不可信」。
    两种情况的区别见 `test_an_untrusted_quote_still_wins_over_the_limit_check`。
    """
    ex = _executor(tmp_path, price=1.50, prev_close=0.0)

    record = ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)   # noqa: SLF001

    assert record.status == EXEC_FILLED


def test_an_untrusted_quote_still_wins_over_the_limit_check(tmp_path):
    """`prev_close=None` = 脏值 ⇒ 连「能不能成交」都不判，直接挂单等待。"""
    ex = _executor(tmp_path, price=1.50, prev_close=None)

    record = ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)   # noqa: SLF001

    assert record.status == EXEC_SUBMITTED
    assert "行情不可信" in record.reason


def test_configured_twenty_percent_code_is_not_blocked_at_ten(tmp_path):
    """配置成 20% 的标的，涨到 +10% 不该被当成涨停。

    ★ 清单里刻意写**裸代码**：那是人往 `SC_LIMIT_20_CODES` 里填时的自然写法，
    而归一化成 `.XSHG` 是框架的事（见 `limit_rate`）。
    """
    ex = _executor(tmp_path, price=1.10, prev_close=1.00, limit_20=(CODE,))

    record = ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)   # noqa: SLF001

    assert record.status == EXEC_FILLED


def test_588_prefix_is_allowed_to_rise_twenty_percent(tmp_path):
    config = Config(runtime_dir=tmp_path)
    quote = Quote(code=to_jq(WIDE_CODE), price=1.19, prev_close=1.00,
                  open=1.19, high=1.19, low=1.19)
    ex = PaperExecutor(config, _QuoteMarket(quote),
                       FileTransport(tmp_path / "ch2"), initial_cash=1_000_000)
    sig = Signal(code=WIDE_CODE, action=ACTION_BUY, amount=100, created_at=FIXED_NOW)

    record = ex._try_fill(sig, FIXED_NOW)                   # noqa: SLF001

    assert record.status == EXEC_FILLED


def test_account_is_untouched_when_the_order_cannot_fill(tmp_path):
    ex = _executor(tmp_path, price=1.10, prev_close=1.00)
    before = dict(ex.account.positions)

    ex._try_fill(_signal(ACTION_BUY), FIXED_NOW)            # noqa: SLF001

    assert ex.account.positions == before == {}
