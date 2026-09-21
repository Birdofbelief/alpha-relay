# -*- coding: utf-8 -*-
"""行情层。

对外只暴露这几样，其余是内部协作细节：

    Quote / QuoteBatch   —— 快照与**分类过的**质量问题
    MarketData           —— 协议（实现者只需提供 quotes / history）
    MarketDataError      —— 取不到数据时抛它，**不要返回空表**
    FakeMarketData       —— 确定性假行情，测试与离线演示的默认
    OnlineMarketData     —— 腾讯日线 + 新浪快照
    RetryingMarketData   —— 重试 + 硬截止
    CachedMarketData     —— 本地历史缓存 + 新鲜度三档 + 复权校验
"""
from .base import (
    MarketData,
    MarketDataError,
    Quote,
    QuoteBatch,
    detect_adjustment_jump,
    validate_history,
)
from .fake import FakeMarketData, make_flat_history
from .online import OnlineMarketData, RetryingMarketData, parse_sina_num, to_jq, to_sina

__all__ = [
    "MarketData", "MarketDataError", "Quote", "QuoteBatch",
    "validate_history", "detect_adjustment_jump",
    "FakeMarketData", "make_flat_history",
    "OnlineMarketData", "RetryingMarketData", "to_jq", "to_sina", "parse_sina_num",
]


def __getattr__(name: str):
    """`CachedMarketData` 走惰性导入。

    它是唯一依赖 pandas 写盘路径的模块，而大量测试（闸门、传输、调度）
    根本不需要缓存 —— 让它们不必为一个用不到的模块付导入代价。
    """
    if name == "CachedMarketData":
        from .cache import CachedMarketData
        return CachedMarketData
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
