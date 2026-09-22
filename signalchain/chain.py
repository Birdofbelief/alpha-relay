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
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .calendar import TradingCalendar, weekday_calendar
from .config import Config
from .executor import Executor
from .marketdata import FakeMarketData
from .marketdata.base import MarketData
from .notify import ConsoleNotifier, MultiNotifier, Notifier, NullNotifier
from .paper import PaperExecutor
from .runner import RoundResult, Runner, build_runner
from .strategy import Strategy, load_strategy
from .transport import Transport, load_transport

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
    #: 执行端。**契约是 `Executor` 那三个方法**，不必是 `PaperExecutor` ——
    #: 装上你自己的券商执行端就是换这一个参数，产生端一行不动。
    executor: Executor | None = None

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
    transport: Transport | None = None,
    calendar: TradingCalendar | None = None,
    notifier: Notifier | None = None,
    executor: Executor | None = None,
    with_executor: bool = False,
    initial_cash: float = 100_000.0,
    console: bool = True,
    schedule: Sequence[tuple[str, str]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Chain:
    """按配置装一条链。未显式传入的部件按配置推断。

    每个部件都是「**显式传入 > 配置 > 内置默认**」这一条优先级，
    `transport=` 也不例外：

        build_chain(config)                      # 按 config.transport 造
        build_chain(config, transport=my_store)  # 用你给的这个，配置不管了

    想换投递后端而不写代码，就把 `SC_TRANSPORT` 指到一个**仓库外**的
    `.py` 文件路径上，见 `signalchain/transport/__init__.py` 的 `load_transport`。
    ★ 名字写错了**会报错，不会退回文件后端** —— 理由在 `load_transport` 的 docstring 里。

    `executor=` 与 `with_executor=True` **互斥**：前者装上你给的那个，
    后者造一个内置的纸面执行端。两个一起给会直接报错，见下面的注释。
    """
    config = config or Config.from_env()
    config.ensure_dirs()

    problems = config.validate()
    for p in problems:
        log.warning("! 配置问题: %s", p)

    # 策略：显式传入 > 配置里的路径 > 内置示例
    if strategy is None:
        strategy = load_strategy(config.strategy_path or "demo")
    elif isinstance(strategy, str):
        strategy = load_strategy(strategy)

    # 日历：显式传入 > 在线交易日历。
    # ★ 加载失败**不退化成「按工作日判断」**，这是刻意的（见 calendar.py「教训一」）：
    #   那样会在法定节假日产生信号，比「整天不动」更危险。
    #   代价是失败后 `is_trading_day` 恒为 False ⇒ **一个任务都不会跑**，
    #   于是这条日志必须把「后果」和「怎么修」都写出来 —— 否则它就是那种
    #   「进程活着、心跳还在、唯独永远没有信号」的形状，人会去怀疑策略和券商。
    if calendar is None:
        calendar = TradingCalendar()
        if not calendar.load():
            log.error(
                "◇ 交易日历加载失败，本进程将**把所有日期都判成非交易日** —— "
                "也就是说从今天起一个任务都不会跑（进程会一直活着）。"
                "最常见的原因是没装 akshare：`pip install akshare`。"
                "也可以显式传入 calendar=（离线可用 weekday_calendar(start, end)，"
                "但它会在法定节假日产生信号，只适合演示）。"
                "失败原因见上一行日历自己的报错。")

    # 行情：显式传入 > 在线（重试 + 缓存）
    if market is None:
        from .marketdata.cache import CachedMarketData
        from .marketdata.online import OnlineMarketData, RetryingMarketData
        market = CachedMarketData(
            RetryingMarketData(OnlineMarketData(timeout=config.http_timeout)),
            cache_dir=config.cache_dir, calendar=calendar,
            max_stale_days=config.max_stale_days,
        )

    # 传输后端：显式传入 > 配置（内置名或仓库外路径）。
    # ★ 这一句可能**抛异常** —— 取值不认识时不会悄悄退回文件后端，见 load_transport。
    if transport is None:
        transport = load_transport(config)
    runner = build_runner(
        config, strategy, market, transport, calendar,
        notifier=notifier or _build_notifier(config, console),
        schedule=schedule,
    )
    if clock is not None:
        runner.clock = clock

    # 执行端：显式传入 > with_executor 造一个纸面执行端 > 不装。
    #
    # 两个都给 = 两种指定方式冲突，**直接报错、不猜**。这与
    # 「策略文件里有多个 Strategy 子类时必须显式指定」是同一条纪律：
    # 静默选中一个的代价是「你以为跑的是 A，实际跑的是 B」—— 而它不报错，
    # 表现上跟「今天没有信号」长得一模一样。
    if executor is not None and with_executor:
        raise ValueError(
            "executor= 与 with_executor=True 同时给了 —— 这是两种互斥的指定方式，"
            "框架不替你猜。想用内置纸面执行端，就只给 with_executor=True。")
    if executor is None and with_executor:
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
    executor: Executor | None = None,
) -> Chain:
    """**零凭据、零网络**的链：文件后端 + 假行情 + 纸面执行器。

    `demo.py`、绝大多数测试、以及「想先看看这东西长什么样」的人，都走这条路。
    它跑通就说明链路本身是完整的 —— 与「有没有配好账号」无关。

    传 `executor=` 可以换掉那个纸面执行端，其余一律不变 ——
    这是「接自己的券商执行端」最短的一条验证路径：先用假行情把执行端接上，
    确认它真的被调到了、回执真的写回来了，再去接真行情。

    ★ 本入口**不读环境变量** —— 它直接造 `Config`，不走 `Config.from_env()`，
    这正是「零凭据」承诺的一部分（跟环境里配没配账号无关）。代价是它有一个
    静默面：设了 `SC_STRATEGY_PATH` 又没显式传 `strategy=` 时，跑的是内置示例
    策略。那一种情况会打一条 warning 把「这次谁在跑」说清楚 ——
    **只警告，不采纳**（采纳了它就不再是零凭据入口）。
    """
    config = Config(role="producer", transport="file",
                    runtime_dir=Path(runtime_dir))
    config.ensure_dirs()

    # ★ 本入口不读环境变量（见 docstring），但**不能沉默**。
    #   实测 2026-09-22，同样设 SC_STRATEGY_PATH 指向仓库外的私有策略：
    #       build_chain()       → MY_PRIVATE（环境变量被采纳）
    #       build_file_chain()  → demo_basic（跑的是内置示例，且没有任何输出）
    #   于是「我设了环境变量」被读成「我的策略进来了」，接着他会去怀疑策略或
    #   怀疑框架 —— 而两条都不是问题所在。这条 warning 就是为那一种情况准备的：
    #   只在**设了环境变量、又没显式传 strategy=** 时触发，说清楚这次是谁在跑。
    #   刻意不采纳那个路径：采纳了它就不再是「零凭据」入口了（用户的环境里
    #   SC_TRANSPORT / SC_ROLE 会跟着渗进来，那就不是「跑通即链路完整」了）。
    if strategy is None and os.environ.get("SC_STRATEGY_PATH"):
        log.warning(
            "! SC_STRATEGY_PATH 已设置（%s），但**本入口不读环境变量** —— "
            "build_file_chain 是零凭据入口，只认显式传入的 strategy=。"
            "本次跑的是内置示例策略 demo_basic。要跑你自己的策略就传参："
            'build_file_chain(runtime_dir, strategy="../你的策略.py")',
            os.environ["SC_STRATEGY_PATH"])

    strategy = strategy or load_strategy("demo")
    if isinstance(strategy, str):
        strategy = load_strategy(strategy)

    universe = list(codes or strategy.universe())
    market = (FakeMarketData(universe, drift=drift, halt=halt)
              if fake else None)
    calendar = _calendar_between(start, end)

    return build_chain(
        config, strategy=strategy, market=market, calendar=calendar,
        executor=executor,
        # 没给就用内置纸面执行端；给了就绝不覆盖它。
        with_executor=executor is None,
        initial_cash=initial_cash, console=console,
        schedule=[],
    )


def _calendar_between(start: str, end: str) -> TradingCalendar:
    from datetime import date as _date
    return weekday_calendar(
        _date.fromisoformat(start), _date.fromisoformat(end))
