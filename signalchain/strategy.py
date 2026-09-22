# -*- coding: utf-8 -*-
"""策略接口 —— 私有 alpha 与开源链路之间的那道缝。

## 契约只有一条，但它是硬的

    `decide(ctx)` 必须是**纯函数**：不联网、不读文件、不写状态、不依赖当前时间
    （时间从 `ctx.now` 拿）。

这不是洁癖。它是三个具体能力的**前提**：

1. **可回放。** 给定同一份 `ctx`，`decide` 必须给出同一个结果。
   做不到这一点，就无法用历史数据复现某一天的信号 ——
   于是「那天为什么卖」永远只能靠猜。
2. **可测试。** 不联网的策略可以用假行情在毫秒内测完。
   一旦策略里藏了一次网络请求，测试就必须联网，于是测试会变得又慢又脆，
   最后没人跑。
3. **可替换数据源。** 策略不该知道价格来自腾讯还是 Gist。它只该知道
   「我拿到了一批快照和一个历史序列」。

## 那策略需要的外部数据（行业分类、财报…）怎么办？

这正是 `decide` 保持纯净的关键设计：**由框架取，不由策略取。**

策略声明它要什么（`aux_sources()`），框架负责去取、记录成败、
把结果塞进 `ctx.aux`。于是：

  - `decide` 里只读 `ctx.aux["industry"]`，一行 I/O 都没有
  - 取数失败会自动喂给 `AuxDataGate`，变成「禁止开新仓」——
    **策略作者不需要记得处理取数失败**，这件事在框架层兜住了

最后一条是本设计最值钱的地方：它把「取数失败怎么办」从一个
「每个策略作者都要记得写对」的问题，变成「框架统一负责」的问题。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import pandas as pd

from .calendar import TradingCalendar
from .gates import DataQuality
from .marketdata.base import MarketDataError, Quote
from .models import Signal, to_bare, to_jq

log = logging.getLogger(__name__)


# ============================================================
# 辅助数据源
# ============================================================

@dataclass
class AuxResult:
    """一次辅助数据获取的结果。

    `missing` 的语义是「**应该有的、但没拿到的**」。它和 `error` 分开是因为
    两者的处理方式不同：
      - 整体 `error` → 框架判整批不可信
      - 部分 `missing` → 策略可以只排除那几只（更精确），而不是全盘停手

    把两者合并成一个 `ok` 布尔，会让「21 只里漏了 1 只」和
    「整个接口挂了」受到同样的处置 —— 前者本可以只跳过一只。
    """

    name: str
    values: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.missing

    def reason(self) -> str:
        if self.error:
            return f"{self.name}: {self.error}"
        if self.missing:
            return f"{self.name}: 缺 {len(self.missing)} 项"
        return ""


class AuxSource(Protocol):
    """辅助数据源的接口。实现者负责取数，**不负责决定失败后怎么办**。"""

    name: str

    def fetch(self, ctx: "StrategyContext") -> AuxResult:
        ...


# ============================================================
# 上下文
# ============================================================

@dataclass
class StrategyState:
    """策略自己的持久化状态（跨进程、跨天存活）。

    刻意做成一个「哑容器」：框架只负责**存取**，不解释内容。
    策略想在里面记什么（上次调仓日、连续亏损次数、冷却期起点…）都由策略定。

    一个纪律：**状态里不要放假行情**。把上次的价格存进来、下次拿来比较，
    等于绕开行情层自己造了一份不新鲜的数据，而所有新鲜度检查都不会看到它。
    状态里应该存「决策痕迹」（哪天做了什么），不是「市场数据」。
    """

    data: dict[str, Any] = field(default_factory=dict)
    dirty: bool = False

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True

    def update(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            self.set(k, v)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.data)


class StrategyContext:
    """策略能看到的**全部**外部世界。它只有只读入口 + 状态读写。

    刻意不把 `Runner`、`Transport`、`requests` 放进来 —— 策略拿不到它们，
    就写不出「顺手发个请求」的代码。能力的边界由这里的接口定义，
    而不是由文档里的「请不要」定义。
    """

    def __init__(
        self,
        now: datetime,
        calendar: TradingCalendar,
        market,
        state: StrategyState | None = None,
        data: DataQuality | None = None,
        aux: dict[str, Any] | None = None,
    ) -> None:
        self.now = now
        self.calendar = calendar
        self.market = market
        self.state = state or StrategyState()
        self.data = data or DataQuality()
        self.aux = aux or {}
        self.log = log
        self._quote_cache: dict[str, Quote] = {}
        self._quote_loaded = False

    # ---------------- 行情（只读） ----------------

    def load_quotes(self, codes: Sequence[str]) -> None:
        """由框架在调用 `decide` **之前**预热。策略自己不该调它。

        ★ 索引时**同时登记三种代码写法**（原样 / 带后缀 / 裸代码）。
        行情源那一层已经因为这件事栽过一次：`510050` 与 `510050.XSHG` 是同一个
        标的，但按字符串取键会得到两个条目 —— 表现是「策略按 1.041 下单，
        执行端看到 1.0630 所以不成交」。上下文这里若不归一化，就会把同一个坑
        再挖一遍，而且更难发现（策略拿到的价格干脆是 `None`，它会以为停牌）。
        """
        batch = self.market.quotes(list(codes))
        index: dict[str, Quote] = {}
        for code, quote in batch.quotes.items():
            index[code] = quote
            index.setdefault(to_jq(code), quote)
            index.setdefault(to_bare(code), quote)
        self._quote_cache = index
        self._quote_loaded = True
        self.data.quote_failed_codes = list(batch.missing) + list(batch.invalid)
        self.data.quote_ok = batch.ok

    def quote(self, code: str) -> Quote | None:
        """取一只标的的快照。停牌会返回 `Quote`（`is_halted` 为真），不是 `None`。

        `None` 只表示「这只的行情不可信」—— 两者的处理完全不同，
        所以不要用 `if not quote` 来判断。

        传入的代码用哪种写法都可以（裸代码或带后缀），见 `load_quotes`。
        """
        if not self._quote_loaded:
            raise RuntimeError("行情尚未预热：这是框架的调用顺序错误，不是策略的问题")
        found = self._quote_cache.get(code)
        if found is not None:
            return found
        return (self._quote_cache.get(to_jq(code))
                or self._quote_cache.get(to_bare(code)))

    def price(self, code: str) -> float | None:
        """可用的最新价。停牌/不可信时返回 `None` —— 调用方必须处理 `None`。"""
        q = self.quote(code)
        if q is None or not q.is_finite or q.is_halted:
            return None
        return q.price

    def pct_change(self, code: str) -> float | None:
        q = self.quote(code)
        return q.pct_change if q else None

    def history(self, code: str, bars: int) -> pd.DataFrame:
        """历史日线。取不到抛 `MarketDataError`。

        ★ **不要把抛出的异常吞掉。** 一只标的取不到历史，正确的处理是
        把它从本轮候选中排除；但如果你 `except: return None` 然后继续当成
        「没有信号」，那就变成了一次静默的漏判 —— 而漏判和没有信号长得一模一样。
        """
        return self.market.history(code, bars)

    def try_history(self, code: str, bars: int) -> pd.DataFrame | None:
        """`history` 的「取不到算 None」版本，供遍历标的池时使用。

        它把「取不到」变成一个显式的 `None`，让调用点必须写出来怎么处理。
        """
        try:
            return self.history(code, bars)
        except MarketDataError as e:
            self.log.warning("▼ %s 历史不可用，本轮排除该标的: %s", code, e)
            return None

    # ---------------- 时间 ----------------

    @property
    def today(self):
        return self.now.date()

    def is_trading_day(self) -> bool:
        return self.calendar.is_trading_day(self.now)

    def aux_get(self, name: str, default: Any = None) -> Any:
        return self.aux.get(name, default)

    # ---------------- 信号构造 ----------------

    def signal(self, code: str, action: str, amount: int = 0,
               price: float | None = None, signal_type: str = "normal") -> "Signal":
        """构造一条信号。**策略应当用这个，而不是自己 `Signal(...)`。**

        唯一的区别是 `created_at` 取 `ctx.now` 而不是墙上时钟 —— 但这一点直接决定
        可回放性：

        信号 id = `时间戳 + 代码 + 动作`，它的作用是幂等键。如果时间戳来自墙上时钟，
        那么**回放同一天的行情会生成一批全新的 id**，于是「这条信号处理过没有」
        这个判断永远答「没有」—— 重放会变成重复下单。

        `price=None` 时自动取当前有效价（停牌/不可信时为 0.0）。
        """
        if price is None:
            price = self.price(code) or 0.0
        return Signal(code=code, action=action, amount=amount, price=price,
                      signal_type=signal_type, created_at=self.now)


# ============================================================
# 策略基类
# ============================================================

class Strategy(ABC):
    """策略基类。子类通常只需要实现 `universe()` 与 `decide()`。"""

    #: 策略名。会进日志、信号备注与心跳 —— 多策略共用一个后端时靠它区分。
    name: str = "unnamed"

    def universe(self) -> Sequence[str]:
        """标的池。链路据此决定「要拉哪些行情、缓存哪些历史」。

        ★ 由策略决定而不是由配置决定，是刻意的：池子的大小直接决定
        `warmup_bars` 和请求量，两者是同一个决策的两面，分开配置必然对不上。
        """
        raise NotImplementedError(f"{type(self).__name__} 必须实现 universe()")

    def warmup_bars(self) -> int:
        """需要多少根历史 K 线。链路用它决定缓存拉多长、何时判缓存不足。

        默认 120。**宁可多取**：多取几十根的成本是每个标的几十 KB，
        而少取会让指标在序列开头失真，且这种失真不会报错。
        """
        return 120

    def aux_sources(self) -> Sequence[AuxSource]:
        """声明需要的外部数据集。框架负责取，见模块文档。"""
        return ()

    @abstractmethod
    def decide(self, ctx: StrategyContext) -> Iterable["SignalLike"]:
        """核心。**必须是纯函数** —— 见模块文档。

        返回 `Signal` 的可迭代对象（通常就是一个 list）。
        去重、限流、投递、重试、回执全部由链路负责，策略不需要关心。
        """
        raise NotImplementedError

    def on_start(self, ctx: StrategyContext) -> None:
        """可选钩子：开盘前的准备。允许写 `ctx.state`。"""

    def on_finish(self, ctx: StrategyContext, results) -> None:
        """可选钩子：本轮结束后的记账。允许写 `ctx.state`。"""

    def describe(self) -> str:
        return f"{self.name}(池{len(self.universe())}只/预热{self.warmup_bars()}根)"


#: 只是为了让 `decide` 的返回类型注解不必 import Signal（避免循环依赖）
SignalLike = Any


# ============================================================
# 加载
# ============================================================

def load_strategy(target: str) -> Strategy:
    """按名字加载内置示例，或按路径加载仓库外的策略。

        load_strategy("demo")                       # 内置示例
        load_strategy("../my_private_strategy.py")  # 仓库外的私有策略

    第二个用法是本仓库存在的主要理由之一：**你的真策略可以对着这套框架开发、
    本地跑，而永远不会进入这个仓库**。`.gitignore` 里还有一层 `strategies/private*`
    兜底。

    ⚠️ 按路径加载 = 执行该文件里的任意代码。只指向你自己的文件，
    绝不要指向下载来的、别人给的路径。
    """
    if target in ("demo", "demo_basic"):
        # 相对导入：内置示例在**包内**（`signalchain/strategies/`）。
        # 它曾经在仓库根的 `strategies/` 下 → `pip install .` 之后这一行
        # 就是 `ModuleNotFoundError: No module named 'strategies'`。
        from .strategies.demo_basic import DemoBasicStrategy
        return DemoBasicStrategy()

    path = Path(target)
    if not path.exists():
        path = Path(target + ".py")
    if not path.exists():
        raise FileNotFoundError(
            f"找不到策略 {target!r}（既不是内置名，也不是存在的文件路径）")

    path = path.resolve()
    module_name = f"signalchain_user_strategy_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载策略文件: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    strategy = _find_strategy(module, path)
    log.info("已加载外部策略 %s（来自仓库外路径 %s）", strategy.name, path)
    return strategy


def _find_strategy(module, path: Path) -> Strategy:
    """在模块里找 Strategy 子类。

    允许多个候选（一个文件里放辅助类很常见），但**有且只有一个主策略**时
    才自动选它；多个就必须显式指定，避免猜错 —— 静默选中错误的策略
    会表现为「策略没生效」，而那种 bug 极难发现。

    显式指定的方式：模块里定义 `STRATEGY = 你的类`。
    """
    explicit = getattr(module, "STRATEGY", None)
    if explicit is not None:
        if not (isinstance(explicit, type) and issubclass(explicit, Strategy)):
            raise TypeError(f"{path}: STRATEGY 必须是 Strategy 的子类，当前是 {explicit!r}")
        return explicit()

    candidates = [
        obj for name, obj in vars(module).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and issubclass(obj, Strategy)
        and obj is not Strategy
        and obj.__module__ == module.__name__
    ]
    if not candidates:
        raise ImportError(
            f"{path}: 没找到 Strategy 子类。若你的策略类在辅助模块里，"
            f"请在本文件里加一行 `STRATEGY = 你的类`")
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise ImportError(
            f"{path}: 找到多个 Strategy 子类（{names}），无法判断该用哪个。"
            f"请加一行 `STRATEGY = 你要用的那个类`")
    return candidates[0]()
