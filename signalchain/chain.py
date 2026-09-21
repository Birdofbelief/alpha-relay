# -*- coding: utf-8 -*-
"""装配层：把各个部件拼成一条能跑的链。

这里**只做装配**，不含任何业务判断。分层的好处在这一层体现得最直接：
换后端、换行情源、换策略都只是换一个参数，下游代码一行不动。

## 一条容易被忽略的设计：同一个进程可以同时扮演两端

`build_chain(..., with_executor=True)` 时，产生端和执行端跑在同一个进程里，
各自持有**不同的** `SingleWriterGuard`。它们共用同一个 `Transport` 对象，
但每个 guard 只放行自己角色的 channel —— 所以即使同进程，
产生端也写不了 `positions`，执行端也写不了 `signals`。

这不是为了演示而做的特例，它说明所有权约束是**绑在角色上、而不是绑在进程上**的。
真实部署时拆成两个进程，约束一字不改地继续生效。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .calendar import TradingCalendar, weekday_calendar
from .config import Config
from .marketdata import FakeMarketData
from .marketdata.base import MarketData
from .notify import ConsoleNotifier, MultiNotifier, Notifier, NullNotifier
from .paper import PaperExecutor
from .runner import RoundResult, Runner, build_runner
from .strategy import Strategy, load_strategy
from .transport import FileTransport, NullTransport, Transport

log = logging.getLogger(__name__)


@dataclass
class Chain:
    """一条装好的链。演示、测试与真实运行共用同一个结构。

    ## ★ 不要拿 `raw_transport` 去写 channel

    `raw_transport` 是**没有套 guard 的裸后端**。它在这里只是为了装配 ——
    Runner 与 Executor 各自用它包出自己角色视角的 guard。

    要读写 channel，用那两个**已经套好 guard** 的把手：

        chain.runner.transport      # 产生端视角：能写 signals / heartbeat
        chain.executor.transport    # 执行端视角：能写 positions / executions

    读走哪个都行（guard 不拦读）；**写走错了会抛 `OwnershipError`** ——
    这正是本仓库那条「单一写入者」约定被强制的地方。

    它原来叫 `transport`。**改名的理由**：那个名字太好用，于是成了最顺手的把手 ——
    而它恰好绕过本仓库最核心的那条不变量。有人（一位独立复审者）第一版探针
    就抓了它，并因此报了一个假 FAIL。**一个把「绕过核心约束」变成最省事路径的
    字段名，本身就是缺陷。**
    """

    config: Config
    calendar: TradingCalendar
    market: MarketData
    strategy: Strategy
    #: ★ 裸后端，**没有 guard**。读写 channel 请走 runner/executor 上的把手。
    raw_transport: Transport
    runner: Runner
    executor: PaperExecutor | None = None

    @property
    def guarded_transport(self) -> Transport:
        """产生端视角的 guard。等价于 `chain.runner.transport`。

        存在只是为了让「正确的那个把手」比裸后端更好敲 —— 顺手度决定了
        别人会走哪条路，这比文档里写一句「请不要」有效得多。
        """
        return self.runner.transport

    def step(self, now: datetime | None = None) -> tuple[RoundResult, dict[str, int]]:
        """一个完整的「嘀嗒」：执行端先报持仓 → 产生端决策 → 执行端消化信号。

        顺序不能反。执行端**必须先报持仓**，产生端才能完成对账、
        `PositionsSyncedGate` 才会放行买入 —— 这正是那道闸门存在的意义。
        """
        now = now or datetime.now()
        fills: dict[str, int] = {}
        if self.executor is not None:
            self.executor.publish_positions(now)
        result = self.runner.run_round(now, task="step")
        if self.executor is not None:
            fills = self.executor.step(now)
        return result, fills

    def describe(self) -> str:
        parts = [
            f"role={self.config.role}",
            f"策略={self.strategy.describe()}",
            f"行情={self.market.describe()}",
            f"传输={self.guarded_transport.describe()}",
            f"日历={self.calendar.describe()}",
        ]
        if self.executor is not None:
            parts.append(self.executor.describe())
        return " | ".join(parts)


def _build_transport(config: Config) -> Transport:
    """按配置造后端。**缺凭据时降级到文件后端并告警，而不是带着空 token 发请求。**"""
    kind = config.transport
    if kind == "gist":
        from .transport.gist import GistTransport
        if not (config.gist_id and config.github_token):
            log.error("⛔ 配置为 gist 后端但凭据不全，**降级为文件后端**（不带着空凭据发请求）")
            return FileTransport(config.channel_dir)
        return GistTransport.from_env()
    if kind == "null":
        return NullTransport()
    return FileTransport(config.channel_dir)


def _build_notifier(config: Config, console: bool) -> Notifier:
    parts: list[Notifier] = []
    if config.serverchan_key:
        from .notify import ServerChanNotifier
        parts.append(ServerChanNotifier(config.serverchan_key))
    elif console:
        parts.append(ConsoleNotifier())
    if not parts:
        parts.append(NullNotifier())
    return parts[0] if len(parts) == 1 else MultiNotifier(parts)


def build_chain(
    config: Config | None = None,
    *,
    strategy: Strategy | str | None = None,
    market: MarketData | None = None,
    calendar: TradingCalendar | None = None,
    notifier: Notifier | None = None,
    with_executor: bool = False,
    initial_cash: float = 100_000.0,
    console: bool = True,
    schedule: Sequence[tuple[str, str]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Chain:
    """按配置装一条链。未显式传入的部件按配置推断。"""
    config = config or Config.from_env()
    config.ensure_dirs()

    problems = config.validate()
    for p in problems:
        log.warning("⚠️ 配置问题: %s", p)

    # 策略：显式传入 > 配置里的路径 > 内置示例
    if strategy is None:
        strategy = load_strategy(config.strategy_path or "demo")
    elif isinstance(strategy, str):
        strategy = load_strategy(strategy)

    # 日历：显式传入 > 交易日历（在线失败退静态兜底，见 calendar.py）
    if calendar is None:
        calendar = TradingCalendar()
        if not calendar.load():
            log.error("🗓️ 交易日历加载失败，本进程将**保守地判定所有日期为非交易日**。"
                      "要么修好日历源，要么显式传入一个 calendar。")

    # 行情：显式传入 > 在线（重试 + 缓存）
    if market is None:
        from .marketdata.cache import CachedMarketData
        from .marketdata.online import OnlineMarketData, RetryingMarketData
        market = CachedMarketData(
            RetryingMarketData(OnlineMarketData(timeout=config.http_timeout)),
            cache_dir=config.cache_dir, calendar=calendar,
            max_stale_days=config.max_stale_days,
        )

    transport = _build_transport(config)
    runner = build_runner(
        config, strategy, market, transport, calendar,
        notifier=notifier or _build_notifier(config, console),
        schedule=schedule,
    )
    if clock is not None:
        runner.clock = clock

    executor = None
    if with_executor:
        executor = PaperExecutor(config, market, transport, initial_cash=initial_cash)

    return Chain(config=config, calendar=calendar, market=market,
                 strategy=strategy, raw_transport=transport,
                 runner=runner, executor=executor)


def build_file_chain(
    runtime_dir: str | Path = "./runtime",
    *,
    strategy: Strategy | str | None = None,
    fake: bool = True,
    codes: Sequence[str] | None = None,
    drift: float = 0.002,
    halt: set[str] | None = None,
    initial_cash: float = 100_000.0,
    start: str = "2026-01-01",
    end: str = "2026-12-31",
    console: bool = True,
) -> Chain:
    """**零凭据、零网络**的链：文件后端 + 假行情 + 纸面执行器。

    `demo.py`、绝大多数测试、以及「想先看看这东西长什么样」的人，都走这条路。
    它跑通就说明链路本身是完整的 —— 与「有没有配好账号」无关。
    """
    config = Config(role="producer", transport="file",
                    runtime_dir=Path(runtime_dir))
    config.ensure_dirs()

    strategy = strategy or load_strategy("demo")
    if isinstance(strategy, str):
        strategy = load_strategy(strategy)

    universe = list(codes or strategy.universe())
    market = (FakeMarketData(universe, drift=drift, halt=halt)
              if fake else None)
    calendar = _calendar_between(start, end)

    return build_chain(
        config, strategy=strategy, market=market, calendar=calendar,
        with_executor=True, initial_cash=initial_cash, console=console,
        schedule=[],
    )


def _calendar_between(start: str, end: str) -> TradingCalendar:
    from datetime import date as _date
    return weekday_calendar(
        _date.fromisoformat(start), _date.fromisoformat(end))
