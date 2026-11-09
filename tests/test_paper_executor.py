# -*- coding: utf-8 -*-
"""纸面执行端：挂单会继续撮合、回执写失败要回滚账户。

这两个行为原来都是错的，而且**只在特定时刻**才显形：

  - 限价单挂上之后永远没人再看它一眼 → 价格进入区间也不成交，
    持仓/回执永远停在 `submitted`；
  - 回执写入返回 False 时账户已经改了，下一轮按「没有回执」又成交一次。

平时测试全绿，因为「挂单后来成交」「写入恰好失败」都不常发生。
"""
from __future__ import annotations

import pytest

from signalchain.config import Config
from signalchain.models import (
    ACTION_BUY,
    CH_EXECUTIONS,
    EXEC_FILLED,
    EXEC_SUBMITTED,
    Signal,
)
from signalchain.paper import PaperExecutor
from signalchain.marketdata.base import MarketDataError, Quote, QuoteBatch
from signalchain.store import Stores
from signalchain.transport.base import Role, SingleWriterGuard
from signalchain.transport.file import FileTransport

from .conftest import CLEAN_CODES, FIXED_NOW

CODE = CLEAN_CODES[0]


class _MutableMarket:
    """价格可变 —— 用来模拟「挂单之后价格才进入限价区间」。"""

    def __init__(self, price: float) -> None:
        self.price = price

    def quotes(self, codes):
        return QuoteBatch(quotes={
            c: Quote(code=c, name="测试", price=self.price,
                     prev_close=self.price, open=self.price,
                     high=self.price, low=self.price)
            for c in codes
        })

    def history(self, code, bars):
        raise MarketDataError("这个假源不提供历史")

    def describe(self):
        return "mutable"


class _FailExecutionWrites(FileTransport):
    """除 executions 外都能写 —— 模拟回执 channel 的存储抖动。"""

    def write(self, channel, doc):
        if channel == CH_EXECUTIONS:
            return False
        return super().write(channel, doc)


def _seed_signal(producer, price: float = 1.0) -> Signal:
    sig = Signal(code=CODE, action=ACTION_BUY, amount=100, price=price,
                 created_at=FIXED_NOW)
    Stores(producer).signals.append(sig)
    return sig


def test_a_submitted_order_is_rechecked_and_fills_later(tmp_path):
    """★ 挂单进入限价区间后必须成交。

    原实现把所有 `submitted` id 直接跳过，限价单一挂上就永远不会成交 ——
    持仓与回执永远停在「挂单等待」，而没有任何报错。
    """
    config = Config(runtime_dir=tmp_path / "rt")
    config.ensure_dirs()
    channel_dir = tmp_path / "ch"
    market = _MutableMarket(1.10)
    ex = PaperExecutor(config, market, FileTransport(channel_dir),
                       initial_cash=10_000.0)
    producer = SingleWriterGuard(FileTransport(channel_dir), Role.PRODUCER)
    sig = _seed_signal(producer, price=1.00)

    first = ex.poll_and_execute(FIXED_NOW)
    assert [e.status for e in first] == [EXEC_SUBMITTED]
    assert ex.account.held(sig.code) == 0

    market.price = 0.99                      # 价格进入限价区间
    second = ex.poll_and_execute(FIXED_NOW)

    assert [e.status for e in second] == [EXEC_FILLED]
    assert ex.account.held(sig.code) == 100
    # 回执历史里 submitted → filled 两条都在
    statuses = [r["status"] for r in Stores(ex.transport).executions.raw()]
    assert statuses == [EXEC_SUBMITTED, EXEC_FILLED]


def test_a_failed_receipt_write_rolls_back_the_account(tmp_path):
    """★ 回执没写进去，内存账户必须回滚。

    否则下一轮会按「没有回执」重新成交一次 —— 账实分离，而且只在存储抖动
    时出现，平时完全测不到。
    """
    config = Config(runtime_dir=tmp_path / "rt")
    config.ensure_dirs()
    channel_dir = tmp_path / "ch"
    ex = PaperExecutor(config, _MutableMarket(0.99),
                       _FailExecutionWrites(channel_dir),
                       initial_cash=10_000.0)
    producer = SingleWriterGuard(FileTransport(channel_dir), Role.PRODUCER)
    sig = _seed_signal(producer, price=1.00)

    produced = ex.poll_and_execute(FIXED_NOW)

    assert produced == []
    assert ex.account.cash == pytest.approx(10_000.0), "回执失败后现金没回滚"
    assert ex.account.held(sig.code) == 0, "回执失败后持仓没回滚"
    assert Stores(ex.transport).executions.raw() == []


def test_publish_positions_reports_a_failed_write(tmp_path):
    """持仓写失败要返回 False（调用方/测试都看得见），而不是假装成功。"""

    class _FailPositionWrites(FileTransport):
        def write(self, channel, doc):
            if channel == "positions":
                return False
            return super().write(channel, doc)

    config = Config(runtime_dir=tmp_path / "rt")
    config.ensure_dirs()
    ex = PaperExecutor(config, _MutableMarket(1.0),
                       _FailPositionWrites(tmp_path / "ch"))
    assert ex.publish_positions(FIXED_NOW) is False
