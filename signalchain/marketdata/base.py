# -*- coding: utf-8 -*-
"""行情层抽象。

## 为什么「数据质量」是这一层的头等公民

行情接口的失败方式不止一种，而且**不同失败的含义相反**。把它们合并成
「这个标的数据不好」，就会做出方向完全错误的降级。本模块用类型把三者分开：

| 现象 | 含义 | 该怎么办 |
|---|---|---|
| 这个标的一行都没返回 | 数据管道出了问题 | **停**：整批数据都不可信 |
| 返回了，但价格是 NaN/inf | 上游给了脏值 | **停**：同上 |
| 返回了，字段完整，价格是 0 | **停牌**（很常见，很正常） | **只跳过这一只**，其余照常 |

把「停牌」当管道故障 → 只要有任一标的停牌，策略就整体瘫痪。
把「管道故障」当停牌 → 拿脏数据下单。

这套区分是实盘换来的（实测：行情源对停牌标的返回的是**完整字段、价格全 0**，
形如 `某某500,0.000,1.026,0.000,...`；而真正取不到的代码是整行不返回）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

import pandas as pd


@dataclass(frozen=True)
class Quote:
    """一个标的的实时快照。

    所有价格字段用 `float | None` 语义：`None` = 值不可信（NaN/inf），
    `0.0` = 真实的零（停牌）。**不要**把两者归一成 0。
    """

    code: str
    name: str = ""
    price: float | None = 0.0
    prev_close: float | None = 0.0
    open: float | None = 0.0
    high: float | None = 0.0
    low: float | None = 0.0

    @staticmethod
    def _finite(v: float | None) -> bool:
        return v is not None and math.isfinite(v)

    @property
    def is_finite(self) -> bool:
        """数值可信（不是 NaN/inf/None）。"""
        return all(self._finite(v) for v in
                   (self.price, self.prev_close, self.open, self.high, self.low))

    @property
    def is_halted(self) -> bool:
        """停牌：字段完整、数值可信，但最新价为 0。

        注意前提是 `is_finite` —— 脏数据（None）不算停牌。
        """
        return self.is_finite and self.price == 0

    @property
    def pct_change(self) -> float | None:
        """相对昨收的涨跌幅。**停牌时返回 `None`，不是 -100%。**

        ★ 这一条不是数值细节，是安全属性。停牌标的的价格字段是 0，
        照公式算出来就是 `-100%` —— 而那是「今天一笔都没成交」，
        不是「跌没了」。把两者混同的后果非常具体：策略看到 -100%
        会把它当成一次毁灭性破位，**进而触发一次不该有的卖出**。
        而本模块的分类原则本来就是「停牌只跳过这一只」，
        这里返回 None 才是那个原则在数值层面的落实。

        同理，`None`（脏值）也返回 None —— 不可信的值不能伪装成 0 涨跌。
        """
        if not self.is_finite or not self.prev_close or self.is_halted:
            return None
        return (self.price - self.prev_close) / self.prev_close


@dataclass
class QuoteBatch:
    """一批行情，附带**分类过的**质量问题。"""

    quotes: dict[str, Quote] = field(default_factory=dict)
    #: 请求了但一行都没返回的代码 ⇒ 管道异常
    missing: list[str] = field(default_factory=list)
    #: 返回了但含 NaN/inf 的代码 ⇒ 数据失效
    invalid: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None

    @property
    def ok(self) -> bool:
        """整批是否可信。任一 missing/invalid 都判不可信（fail-closed）。"""
        return not self.missing and not self.invalid

    def halted_codes(self) -> list[str]:
        return [c for c, q in self.quotes.items() if q.is_halted]

    def tradable(self) -> dict[str, Quote]:
        """可正常参与决策的标的（排除停牌与不可信）。"""
        return {c: q for c, q in self.quotes.items()
                if q.is_finite and not q.is_halted}

    def problems(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"缺失{len(self.missing)}只({','.join(self.missing[:5])})")
        if self.invalid:
            parts.append(f"失效{len(self.invalid)}只({','.join(self.invalid[:5])})")
        return "；".join(parts) or "无"


class MarketData(Protocol):
    """行情数据源。

    实现者只需提供「一批快照」和「一段历史」。缓存、新鲜度、复权校验
    全部由装饰器完成（见 `cache.py`）—— 数据源本身应该只管怎么把数据取回来。
    """

    def quotes(self, codes: Sequence[str]) -> QuoteBatch:
        ...

    def history(self, code: str, bars: int) -> pd.DataFrame:
        """返回按日期升序的日线，列：date/open/close/high/low/volume。

        取不到时抛 `MarketDataError` —— **不要返回空 DataFrame**。
        空表会被调用方当成「这只标的历史很短」，从而静默走错分支。
        """
        ...

    def describe(self) -> str:
        ...


class MarketDataError(RuntimeError):
    """行情不可用。调用方应当降级（通常是 fail-closed 停手），而不是继续。"""


def validate_history(df: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    """OHLC 合法性校验，返回 `(清洗后的表, 问题描述列表)`。

    丢掉的坏行有几类，每一类都真的出现过：
      - high < low                ：字段错位
      - 开/收 不在 [low, high] 内  ：同上
      - 任一价格 <= 0             ：停牌或占位符混进来了
      - 日期重复                   ：拼接了两份来源

    保留问题描述而不是静默丢行，因为「丢了多少行」是判断数据源是否劣化的唯一线索。
    """
    problems: list[str] = []
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"]), ["空表"]

    work = df.copy()
    before = len(work)

    for col in ("date", "open", "close", "high", "low", "volume"):
        if col not in work.columns:
            return work.iloc[0:0], [f"缺少列 {col}"]

    # ★ 归一日期列的类型。**这不是格式化，是正确性。**
    # 在线取回来的 `date` 是 datetime；而缓存走 CSV 往返，读回来是**字符串**。
    # 两种混在一起时，任何「比较日期」的代码都会抛
    # `TypeError: '<' not supported between instances of 'str' and 'datetime.date'`
    # —— 而且只在**第二次读缓存**时才出现（第一次是刚取回来的，类型还对）。
    # 把它放在这里而不是放在缓存的读侧，是因为本函数是两条路的唯一漏斗：
    # 类型一致从此是「清洗后的保证」，而不是每个调用点各自记得的事。
    try:
        work["date"] = pd.to_datetime(work["date"]).dt.date
    except (ValueError, TypeError) as e:
        return work.iloc[0:0], [f"date 列无法解析为日期: {e}"]

    numeric = work[["open", "close", "high", "low"]]
    bad_nonpositive = (numeric <= 0).any(axis=1)
    bad_range = work["high"] < work["low"]
    bad_inside = ~(
        work["open"].between(work["low"], work["high"])
        & work["close"].between(work["low"], work["high"])
    )
    bad_nan = numeric.isna().any(axis=1)

    mask = bad_nonpositive | bad_range | bad_inside | bad_nan
    if mask.any():
        problems.append(f"丢弃 {int(mask.sum())} 行非法 OHLC")

    work = work[~mask]
    dup = work["date"].duplicated()
    if dup.any():
        problems.append(f"丢弃 {int(dup.sum())} 行重复日期")
        work = work[~dup]

    if len(work) != before and not problems:  # pragma: no cover - 兜底
        problems.append(f"丢弃 {before - len(work)} 行（原因未分类）")

    return work.sort_values("date").reset_index(drop=True), problems


def detect_adjustment_jump(df: pd.DataFrame, threshold: float = 0.25) -> str | None:
    """检测「复权口径不一致」造成的假跳变。

    背景：免费行情源对部分标的只返回**未复权**日线。未复权序列在除权日会出现
    一个凭空的巨大跳空 —— 而涨跌停限制意味着**真实单日波动不可能超过阈值**。
    于是「单日涨跌超过阈值」这个条件，几乎只可能由复权口径问题触发。

    危害：策略会把那个假暴跌当成真实趋势破位，触发一次不该有的卖出。

    返回 `None`（正常）或问题描述。阈值取 0.25 的理由：A 股 ETF 涨跌停是 10%，
    留足 2.5 倍余量，正常的极端行情不会误报。
    """
    if df is None or len(df) < 3:
        return None
    closes = df["close"].astype(float).tolist()
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0:
            continue
        move = abs(cur - prev) / prev
        if move > threshold:
            day = df["date"].iloc[i]
            return (f"{day} 单日变动 {move:.1%}，超过 {threshold:.0%} —— "
                    f"疑似未复权数据混入（真实涨跌停不可能达到）")
    return None
