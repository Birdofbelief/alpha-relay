# -*- coding: utf-8 -*-
"""调度与主循环 —— 把各部分接起来的那一层。

这里集中了**无人值守场景下最容易出事**的几处逻辑，每一条都是从「某天没跑」
或「某天跑了两次」这类事故里提炼的：

## 1. 用 `>=` 匹配，而不是精确匹配分钟

主循环 30 秒醒一次，进程还可能因为重启、卡顿、系统休眠而错过整点。
精确匹配 `HH:MM` 会让「14:57:00 那一秒不在循环里」变成**当天不执行**。
用 `>=` 之后，晚到几分钟仍会补执行 —— 这一条把「错过」变成「延迟」。

## 2. 补执行必须有上限（`catchup_minutes`）

但「晚到就补」不能无限：收盘后启动时补执行 14:57 的任务，等于拿收盘前的
判断去下一个早就该作废的单。所以超过窗口就跳过 —— **并且要把它标记成
「已执行」**，否则每 30 秒都会重新判断一次、重复打印一堆跳过日志。

## 3. 跳过和失败都要持久化，否则重启就重来

`executed_tasks` 按天持久化。这不是优化，是正确性：
进程重启后若忘了「今天已经跳过/执行过这个任务」，会重新执行一遍 ——
而重复执行在交易上是真的会重复下单的。

## 4. 同一任务连续失败有上限（`task_fail_limit`）

一个确定性异常（比如某个字段改名了）会每 30 秒抛一次，一天抛 2880 次。
日志被刷满之后，**真正的告警会淹在里面**。所以连续失败到上限就放弃当天、
把心跳置为 error 等人工处理。放弃是显式的，不是静默的。

## 5. 主循环兜底：任何异常都不许杀进程

最外层 `except` 捕获一切并继续循环。听上去粗暴，但对比一下：
    - 崩掉 → 当天所有剩余任务都不会执行，而且**没有心跳了**
    - 继续 → 至少心跳还在，监控端能看见，人来得及介入
孤独运行的程序，**带着错误活着**远比干脆死去有价值。
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from .calendar import TradingCalendar
from .config import Config
from .gates import DataQuality, GateChain, Veto
from .models import TERMINAL_EXEC_STATUSES
from .notify import NotifyThrottle, NullNotifier
from .store import Stores
from .strategy import AuxResult, Strategy, StrategyContext, StrategyState
from .transport import Role, SingleWriterGuard, Transport
from .transport.base import OwnershipError

log = logging.getLogger(__name__)

#: 默认任务表 = A 股的**四个交易时段边界**。这四个时刻由交易所规则固定，
#: 不是「挑」出来的，因此与任何策略、任何人的执行习惯都无关。
#: ★ 要按自己的节奏跑，就往这个表里追加 (时刻, 名字) —— 默认值刻意不含
#: 任何「收盘前 N 分钟」这类常见调仓时刻，因为那种数字属于使用者的策略，不属于框架。
DEFAULT_SCHEDULE: list[tuple[str, str]] = [
    ("09:15", "open_auction"),      # 开盘集合竞价开始
    ("11:30", "morning_close"),     # 上午收市
    ("13:00", "afternoon_open"),    # 下午开市
    ("14:57", "close_auction"),     # 收盘集合竞价开始
]

#: 超过这个时长就不再补执行（收盘后补执行毫无意义）
DEFAULT_CATCHUP_MINUTES = 30

#: 单任务当天连续失败上限
DEFAULT_TASK_FAIL_LIMIT = 3

#: 主循环心跳间隔
LOOP_INTERVAL_SECONDS = 30

#: 「日历根本不可用」这条告警的最小间隔（秒）。
#: 它是一个**持续故障**，会一直成立；但不该每 30 秒刷一行把日志淹掉。
CALENDAR_DEAD_ALERT_SECONDS = 3600

#: 启动时刷新历史的最低时间预算：低于它就跳过刷新，优先保证定时任务不被拖过窗口
STARTUP_MIN_BUDGET_SECONDS = 60


@dataclass
class RoundResult:
    """一轮任务的执行结果。"""

    task: str
    at: datetime
    emitted: int = 0        # 策略产出的信号数
    allowed: int = 0        # 过闸门后放行的
    written: int = 0        # 实际投递成功的
    vetoes: list[Veto] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"[{self.task}] 失败: {self.error}"
        parts = [f"产出{self.emitted}", f"放行{self.allowed}", f"投递{self.written}"]
        if self.vetoes:
            parts.append(f"拦下{len(self.vetoes)}")
        parts.append(f"{self.elapsed:.1f}s")
        return f"[{self.task}] " + "、".join(parts)


class _AtomicState:
    """按天持久化的运行状态。写入用 tmp + replace，避免半截 JSON。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as e:
            # 状态文件损坏时**不抛** —— 丢掉状态重启，好过让进程起不来。
            # 代价是当天可能重复执行任务，这个代价在日志里明确记下来。
            log.error("! 状态文件损坏，将以空状态启动（当天任务可能重复执行）: %s", e)
            return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("状态保存失败（不影响本轮）: %s", e)

    # ---- 便捷视图 ----

    def executed(self, day: str) -> set[str]:
        raw = self.data.setdefault("executed_tasks", {})
        return set(raw.get(day, []))

    def mark_executed(self, day: str, task: str) -> None:
        raw = self.data.setdefault("executed_tasks", {})
        tasks = set(raw.get(day, []))
        tasks.add(task)
        raw[day] = sorted(tasks)
        # 只保留最近 7 天，避免状态文件无限增长
        for old in sorted(raw)[:-7]:
            raw.pop(old, None)
        self.save()


class Runner:
    """把策略、行情、闸门、传输、通知串成一轮可执行的流程，并负责定时驱动它。"""

    def __init__(
        self,
        config: Config,
        strategy: Strategy,
        market,
        transport: Transport,
        calendar: TradingCalendar,
        notifier=None,
        schedule: Sequence[tuple[str, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.market = market
        self.calendar = calendar
        self.clock = clock or datetime.now

        self.schedule = list(schedule or DEFAULT_SCHEDULE)
        self.catchup_minutes = config.catchup_minutes
        self.fail_limit = config.task_fail_limit

        # ★ 传输一律过 SingleWriterGuard：写错 channel 直接抛，而不是静默写坏。
        self.transport = SingleWriterGuard(transport, Role(config.role))
        self.stores = Stores(self.transport)
        self.gates = GateChain()

        self.notifier = notifier or NullNotifier()
        self.throttle = NotifyThrottle(self.notifier)
        self.state = _AtomicState(config.state_path)
        self.strategy_state = StrategyState(
            data=dict(self.state.data.get("strategy_state", {})))

        self._fail_count: dict[str, int] = {}
        self._tasks: dict[str, Callable[[datetime], Any]] = {}
        # 「跳过本轮」的日志节流，见 `_log_skip`
        self._last_skip_alert: datetime | None = None
        self._last_skip_day: str = ""

    # ============================================================
    # 任务注册
    # ============================================================

    def register(self, name: str, fn: Callable[[datetime], Any]) -> None:
        """注册任务实现。名字必须出现在 `self.schedule` 里才会被触发。"""
        self._tasks[name] = fn

    def register_defaults(self) -> None:
        """注册默认任务表里的全部任务。

        默认全部指向同一个「跑一轮」流程 —— 这是**正确**的起点：
        策略在 `decide(ctx)` 里根据 `ctx.now` 自己判断此刻该做什么
        （晨间选股、午后调仓、盘中止损），比让框架猜要准得多。
        """
        for _at, name in self.schedule:
            self._tasks.setdefault(name, self.run_round)

    # ============================================================
    # 核心：一轮
    # ============================================================

    def run_round(self, now: datetime, task: str = "manual",
                  planned_at: datetime | None = None) -> RoundResult:
        """跑完整的一轮：预热 → 对账 → 决策 → 闸门 → 投递。

        `now` 是**实际**时刻；`planned_at` 是调度表里那一格的**计划**时刻。
        只有 `run_due` 会传后者 —— 手工调用时两者等同。
        区别只体现在信号 id 的锚上，见 `StrategyContext.signal()`。
        """
        started = time.monotonic()
        result = RoundResult(task=task, at=now)
        ctx = None
        try:
            ctx = self.build_context(now, planned_at=planned_at)
            signals = list(self.strategy.decide(ctx) or [])
            result.emitted = len(signals)
            self._anchor_signals(signals, planned_at)

            allowed, vetoes = self.gates.apply(signals, ctx.data)
            result.allowed, result.vetoes = len(allowed), vetoes

            for sig in allowed:
                if self.stores.signals.append(sig):
                    result.written += 1

            # 顺手清理队列（幂等且便宜），避免队列长期只增不减
            removed = self.stores.signals.prune(
                self.stores.executions.index(), now.strftime("%Y-%m-%d"))
            if removed:
                log.info("○ 队列清理 %d 条", removed)

            self.strategy.on_finish(ctx, result)
            self._persist_strategy_state()
        except OwnershipError:
            # 所有权错误是**编程错误**，必须让它响 —— 吞掉它等于掩盖架构违规
            raise
        except Exception as e:  # noqa: BLE001 - 单轮失败不该杀进程
            result.error = f"{type(e).__name__}: {e}"
            log.error("× 本轮任务失败: %s", result.error, exc_info=True)
        finally:
            result.elapsed = time.monotonic() - started
            if ctx is not None:
                self._report_quality(ctx, task)
                self.beat(task, self._severity_for(ctx))
        return result

    @staticmethod
    def _severity_for(ctx: StrategyContext) -> str:
        """把数据质量问题映射到心跳严重度阶梯。

        映射的依据是「这个问题有多需要人来处理」：

            行情失效       → error           价格不可信，任何决策都建立在假数据上
            未完成对账     → positions_stale 只能卖不能买，功能受限但没在裸奔
            辅助数据缺失   → positions_stale 同上，同样是「少做一件事」
            一切正常       → alive           只报平安，不改变严重度

        注意**没有一档叫「降级」** —— 阶梯只升不降，跨日由 `roll_day` 复位。
        """
        if not ctx.data.quote_ok:
            return "error"
        if not ctx.data.positions_synced or not ctx.data.aux_data_ok:
            return "positions_stale"
        return "alive"

    def build_context(self, now: datetime,
                      planned_at: datetime | None = None) -> StrategyContext:
        """组装策略上下文。**数据质量问题在这里被判定并写进 `ctx.data`。**"""
        ctx = StrategyContext(
            now=now,
            calendar=self.calendar,
            market=self.market,
            state=self.strategy_state,
            data=DataQuality(),
            planned_at=planned_at,
        )
        # 1) 行情
        try:
            ctx.load_quotes(self.strategy.universe())
        except Exception as e:  # noqa: BLE001 - 行情整体拿不到 → 判失效
            ctx.data.quote_ok = False
            ctx.data.quote_failed_codes = list(self.strategy.universe())
            log.error("× 行情获取整体失败: %s", e)

        # 2) 持仓对账（执行端是唯一真相源）
        self._sync_positions(ctx)

        # 3) 辅助数据（由框架取，策略纯读）
        self._load_aux(ctx)

        self.strategy.on_start(ctx)
        return ctx

    def _sync_positions(self, ctx: StrategyContext) -> None:
        """读执行端持仓。**读不到就置 unsynced**，交由闸门「只卖不买」。"""
        snapshot = self.stores.positions.read()
        if snapshot is None:
            ctx.data.positions_synced = False
            ctx.data.positions_sync_reason = "读不到执行端持仓（未知 ≠ 空仓）"
            return

        stale_reason = self._positions_staleness(ctx, snapshot)
        if stale_reason:
            ctx.data.positions_synced = False
            ctx.data.positions_sync_reason = stale_reason
            log.error("× 持仓对账失败：%s —— 当日只卖不买", stale_reason)
            return

        ctx.data.positions_synced = True
        ctx.data.positions_sync_reason = ""
        # 持仓只用于展示与告警；**策略不应拿它替代执行端的真实持仓**，
        # 所以这里不塞进 ctx（策略要持仓应该走信号/回执，而不是猜）。
        log.info("◆ 持仓对账成功：%d 只持仓标的，快照时间 %s",
                 len(snapshot.held_codes()),
                 snapshot.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                 if snapshot.updated_at else "未知")

    def _positions_staleness(self, ctx: StrategyContext, snapshot) -> str:
        """快照是否已经旧到不能当「已对账」用。返回原因字符串，不旧则返回空串。

        ★ **默认查（`SC_MAX_POSITION_STALE_DAYS=0` 表示关闭）。**
        关着的时候行为与以前完全一致 —— 只要能解析就算对账成功。
        打开之后，快照落后超过 N 个交易日（或缺时间戳）⇒ 判未对账。

        为什么值得有：执行端自己挂了、持仓文件三天没更新时，框架仍判「已对账」，
        于是「只卖不买」这道闸不会落下 —— 过期持仓会被当成「还在手上」的证据。
        代价是正常场景可能误伤（执行端本来就不常写持仓），所以**默认关**，
        由使用者按自己执行端的可靠性决定。
        """
        max_age = self.config.max_position_stale_days
        if not max_age:
            return ""
        if snapshot.updated_at is None:
            return "持仓快照没有时间戳，无法确认新鲜度"
        behind = self.calendar.trading_days_behind(
            snapshot.updated_at.date(), ctx.now.date())
        if behind is None:
            return ""      # 日历不可用 → 不额外判；日历那条有自己的闸
        if behind > max_age:
            return (f"持仓快照落后 {behind} 个交易日"
                    f"（上限 {max_age}），执行端可能已停更")
        return ""

    def _load_aux(self, ctx: StrategyContext) -> None:
        """逐项获取辅助数据，把成败汇总成 `DataQuality` 喂给 `AuxDataGate`。"""
        sources = list(self.strategy.aux_sources())
        if not sources:
            return
        results: list[AuxResult] = []
        for src in sources:
            try:
                res = src.fetch(ctx)
            except Exception as e:  # noqa: BLE001 - 取数失败按失败处理，不上抛
                res = AuxResult(name=src.name, error=f"{type(e).__name__}: {e}")
                log.error("× 辅助数据 %s 获取异常: %s", src.name, e)
            results.append(res)
            if res.ok:
                ctx.aux[res.name] = res.values

        bad = [r for r in results if not r.ok]
        ctx.data.aux_data_ok = not bad
        ctx.data.aux_missing = [m for r in bad for m in r.missing]
        ctx.data.aux_reason = "；".join(r.reason() for r in bad if r.reason())

    def _persist_strategy_state(self) -> None:
        if not self.strategy_state.dirty:
            return
        self.state.data["strategy_state"] = self.strategy_state.as_dict()
        self.strategy_state.dirty = False
        self.state.save()

    # ============================================================
    # 质量报告与告警
    # ============================================================

    def _report_quality(self, ctx: StrategyContext, task: str) -> None:
        """把数据质量问题转成告警 + 心跳严重度。

        ★ 告警有节流（同一 key 每天一次）。没有节流的告警系统会死于
        「响得太多以至于没人看」—— 见 `notify.NotifyThrottle`。
        """
        day = ctx.now.strftime("%Y-%m-%d")

        if not ctx.data.quote_ok:
            self.throttle.notify_once(
                f"quote_fail:{day}",
                "行情失效，本轮未产生信号",
                f"标的：{','.join(ctx.data.quote_failed_codes[:10])}\n"
                f"任务：{task}\n时间：{ctx.now:%Y-%m-%d %H:%M:%S}",
                ctx.now.date(),
            )

        if not ctx.data.positions_synced:
            self.throttle.notify_once(
                f"positions_unsync:{day}",
                "未完成持仓对账 → 今日只卖不买",
                f"原因：{ctx.data.positions_sync_reason}\n"
                f"影响：禁止开新仓（卖出/止损照常放行）",
                ctx.now.date(),
            )

        if not ctx.data.aux_data_ok:
            self.throttle.notify_once(
                f"aux_fail:{day}",
                "辅助数据不全 → 今日不开新仓",
                f"原因：{ctx.data.aux_reason}",
                ctx.now.date(),
            )

    def beat(self, task: str, status: str | None = None) -> None:
        """写心跳。**严重度只升不降**，跨日由 `roll_day` 复位。

        监控端按这个阶梯决定发不发微信（alive 不发、error 必发），
        所以「降级」必须由明确的复位动作完成，不能被一次成功顺手抹掉 ——
        否则上午的故障会在下午一次正常心跳后消失，没人知道发生过。

        `status=None` 表示「报个平安」：只更新时刻，**不改变严重度**
        （`escalate("alive")` 在阶梯上不升，所以天然不会降级）。
        """
        try:
            hb = self.stores.heartbeat.read()
            hb.escalate(status or "alive")
            hb.last_task = task
            hb.last_time = self.clock()
            self.stores.heartbeat.write(hb.last_task, hb.status, hb.last_time)
        except Exception as e:  # noqa: BLE001 - 心跳失败绝不能影响主流程
            log.warning("心跳写入失败: %s", e)

    def notify_filled(self) -> int:
        """旁路：扫回执，把**新出现的终态**推一条通知。

        刻意做成旁路（只读 + 异常自吞），因为它是「顺便做的事」：
        通知失败绝不能影响交易主流程。已通知过的信号 id 持久化，
        避免进程重启后把同一批成交再推一遍。
        """
        try:
            index = self.stores.executions.index()
        except Exception as e:  # noqa: BLE001
            log.warning("回执读取失败（旁路，不影响主流程）: %s", e)
            return 0

        seen = set(self.state.data.get("notified_exec", []))
        fresh = [(sid, rec) for sid, rec in index.items()
                 if rec.status in TERMINAL_EXEC_STATUSES and sid not in seen]
        if not fresh:
            return 0

        lines = [f"{rec.status} {rec.code} {rec.action} ×{rec.amount}"
                 + (f"（{rec.reason}）" if rec.reason else "")
                 for _sid, rec in fresh]
        # 成交回执是「每笔都要知道」的信息，**刻意不过每日节流** ——
        # 节流是为告警设计的（同一问题每天一次够了），用到成交上会吞掉真实的成交。
        self.notifier.send(f"成交回执 {len(fresh)} 条", "\n".join(lines[:20]))

        self.state.data["notified_exec"] = sorted(seen | {sid for sid, _ in fresh})[-500:]
        self.state.save()
        return len(fresh)

    # ============================================================
    # 调度
    # ============================================================

    def due_tasks(self, now: datetime) -> list[tuple[str, str, bool]]:
        """返回 `[(计划时刻, 任务名, 是否已超窗)]`。**纯函数，不改状态。**

        拆出来是为了可测：调度逻辑的错法（漏执行、重复执行）都很难从
        日志里看出来，只有把它变成可断言的函数才测得动。
        """
        day = now.strftime("%Y-%m-%d")
        current_hm = now.strftime("%H:%M")
        done = self.state.executed(day)
        out: list[tuple[str, str, bool]] = []
        for at, name in self.schedule:
            if current_hm < at or name in done:
                continue
            target = datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M")
            overdue = (now - target).total_seconds() > self.catchup_minutes * 60
            out.append((at, name, overdue))
        return out

    def seconds_until_next_deadline(self, now: datetime) -> float | None:
        """距**下一个补执行窗口关闭**还有多少秒；今日没有待办窗口则 None。

        这个数用来给启动期的长耗时动作（刷新历史缓存）定预算：
        刷新必须在下一个任务窗口关闭前结束，否则会把那个任务判成超时跳过。
        """
        day = now.strftime("%Y-%m-%d")
        deadlines = []
        for at, _name in self.schedule:
            deadline = datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M") + \
                timedelta(minutes=self.catchup_minutes)
            if now < deadline:
                deadlines.append(deadline)
        if not deadlines:
            return None
        return (min(deadlines) - now).total_seconds()

    def run_once(self, now: datetime | None = None, task: str = "manual") -> RoundResult:
        """跑一轮，不碰调度状态。演示、测试与手工触发用。"""
        self.register_defaults()
        return self.run_round(now or self.clock(), task=task)

    def run_due(self, now: datetime | None = None) -> list[RoundResult]:
        """跑掉所有到点且未执行的任务。主循环每轮调它一次。

        这里实现了模块文档里的四条纪律：`>=` 匹配、补执行窗口、
        **跳过也持久化**、连续失败上限。
        """
        now = now or self.clock()
        self.register_defaults()
        day = now.strftime("%Y-%m-%d")
        results: list[RoundResult] = []

        for at, name, overdue in self.due_tasks(now):
            if overdue:
                log.info("▲ %s（计划 %s）已超过补执行窗口 %d 分钟，跳过",
                         name, at, self.catchup_minutes)
                # ★ 超窗的「跳过」也必须落盘，否则每 30 秒重新判一次、日志刷屏
                self.state.mark_executed(day, name)
                continue

            fn = self._tasks.get(name)
            if fn is None:
                log.warning("任务 %s 没有注册实现，跳过", name)
                self.state.mark_executed(day, name)
                continue

            log.info("△ 触发任务 %s（计划 %s，实际 %s）",
                     name, at, now.strftime("%H:%M"))
            target = datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M")
            try:
                outcome = self._dispatch(name, fn, now, target)
            except Exception as e:  # noqa: BLE001 - 单轮失败不该杀进程
                self._record_failure(day, name, f"{type(e).__name__}: {e}",
                                     exc_info=True)
                continue

            if isinstance(outcome, RoundResult):
                results.append(outcome)
                if outcome.error:
                    # ★ `run_round` 把异常写进 `result.error` 而不是抛出（见它的
                    # except 分支：单轮失败不该杀进程）。所以**这里必须当成失败**。
                    # 不看这一行，就是「任务天天失败、`mark_executed` 照记、心跳照报
                    # alive」—— 连续失败上限与心跳 error 对默认路径全部失效，
                    # 而这种失效在终端上没有任何痕迹。
                    self._record_failure(day, name, outcome.error)
                    continue

            self._fail_count[name] = 0
            self.state.mark_executed(day, name)
            self.beat(name)
            log.info("√ %s 完成", name)
        return results

    def _dispatch(self, name: str, fn, now: datetime, planned_at: datetime):
        """调用一个任务。

        默认任务（`run_round`）需要额外拿到**任务名**与**计划时刻**；
        自定义任务保持公开契约 `fn(now)`，不受影响。

        ★ **按签名判断，不按对象身份判断。** 原来写的是
        `fn.__func__ is Runner.run_round` —— 只要有人**继承** Runner 并重写
        `run_round`，这个判断就为假，`planned_at` 悄悄传不进去，
        信号的幂等锚退回实际时刻，重试又开始换 id。
        「重写一个方法」是再正常不过的事，不能因为它就丢掉安全属性。
        """
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):      # 某些内建/C 可调用对象取不到签名
            return fn(now)
        accepts_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
        kwargs = {}
        if accepts_kw or "task" in params:
            kwargs["task"] = name
        if accepts_kw or "planned_at" in params:
            kwargs["planned_at"] = planned_at
        return fn(now, **kwargs)

    @staticmethod
    def _anchor_signals(signals, planned_at: datetime | None) -> None:
        """给**没有显式 id** 的信号补上「计划时刻」的锚，并重算 `sig_id`。

        为什么必须在这一层做：`ctx.signal()` 会带锚，但策略也可以直接
        `Signal(...)` 构造（它是公开导出的；文档只是建议走 `ctx.signal()`）。
        只要有一条漏网，它就会在重试时换 id ⇒ 执行端去重失效 ⇒ 重复下单。

        显式 id（构造时传了 `sig_id=`，或从线格式读到了 `id`）**绝不改写** ——
        那是别人定的幂等键，改写等于把别人的去重也一起废掉。
        """
        if planned_at is None:
            return
        anchor = planned_at.strftime("%Y%m%d%H%M%S")
        for sig in signals:
            if sig.explicit_id or sig.anchor:
                continue
            sig.anchor = anchor
            sig.sig_id = sig.make_id()

    def _record_failure(self, day: str, name: str, message: str,
                        exc_info: bool = False) -> None:
        """记一次失败。

        ★ **没到上限就不 `mark_executed`** —— 不标记才意味着「下一轮还会重试」。
        到上限才放弃当天并落盘，且**明确写日志**：静默放弃等于没人知道它没跑。
        """
        fails = self._fail_count.get(name, 0) + 1
        self._fail_count[name] = fails
        log.error("× %s 失败（第 %d 次）: %s", name, fails, message,
                  exc_info=exc_info)
        self.beat(f"{name}_ERROR", "error")
        if fails >= self.fail_limit:
            self.state.mark_executed(day, name)
            log.error("× %s 连续失败 %d 次，放弃今日补执行，"
                      "心跳置 error 等人工排查", name, fails)

    def roll_day(self, now: datetime) -> None:
        """跨日复位 + 刷新日历。见 `calendar.py` 的「教训二」：不刷新会全年停摆。

        ⚠️ 刷新失败**不影响当天任何任务** —— 旧缓存仍然可用，
        只是新年度可能缺失（`needs_new_year_refresh` 会在下一天再试）。
        """
        day = now.strftime("%Y-%m-%d")
        if self.state.data.get("current_day") == day:
            return
        self.state.data["current_day"] = day
        self._fail_count.clear()
        # 告警节流的跨日复位。**不是**「当日一次」的实现（那由 notify_once 自己
        # 按日比对保证），只是防 `_sent` 随天数无界增长 —— 见 notify.py 的 reset()。
        self.throttle.reset()
        if self.calendar.needs_new_year_refresh(now.date()):
            log.info("◇ 进入 %s，刷新交易日历（跨年必须刷新，否则会静默停摆）", day)
            self.calendar.refresh()
        self.state.save()

    def _log_skip(self, now: datetime) -> None:
        """记一次「这一轮什么都没跑」—— 并**区分两种成因**。

        ★ 旧写法是 `sleep + continue`，一行日志都不留，于是下面两件事
        产出**完全相同的现象**：

            「今天休市」        → 正常。明天照常。
            「日历根本不可用」  → 故障。**从今往后的每一天都会被跳过。**

        而它们的处置完全相反。日历不可用时进程会一直活着、心跳照旧、
        唯独永远不产出信号 —— 人去怀疑策略、怀疑投递、怀疑券商，
        **不会去怀疑日历**。所以这里必须把话说明白，而不是安静地睡过去。

        节流：正常休市一天只记一行；日历不可用按
        `CALENDAR_DEAD_ALERT_SECONDS` 重复告警（它是持续故障，会一直成立）。
        """
        day = now.strftime("%Y-%m-%d")
        if not self.calendar.usable:
            last = self._last_skip_alert
            if last is None or (now - last).total_seconds() >= CALENDAR_DEAD_ALERT_SECONDS:
                log.error(
                    "× 日历不可用（%s）—— **所有任务都会被跳过**，进程会一直活着"
                    "但永远不产出信号。装 akshare（`pip install akshare`），"
                    "或显式传入一个 calendar= 给 build_chain()。",
                    self.calendar.describe())
                self._last_skip_alert = now
            return
        if day != self._last_skip_day:
            self._last_skip_day = day
            log.info("◇ %s 非交易日（%s），今天不跑任务",
                     day, self.calendar.describe())

    def run_forever(self, max_loops: int | None = None) -> None:
        """主循环。`max_loops` 仅供测试 —— 生产上它是 None（永不退出）。

        ★ 循环体里**没有 `continue`**，是刻意的：计数与 `max_loops` 判断只能有
        一个落点。此前非交易日那条 `continue` 越过了它们，于是
        `run_forever(max_loops=N)` 在日历时**永远不返回**（实测 45 秒不返回，
        被 timeout 杀掉）—— 挂住不报错，比抛异常更难查。
        """
        loops = 0
        while True:
            try:
                now = self.clock()
                self.roll_day(now)

                if self.calendar.is_trading_day(now):
                    for r in self.run_due(now):
                        log.info("◆ %s", r.summary())

                    self.notify_filled()
                else:
                    self._log_skip(now)
            except Exception as e:  # noqa: BLE001
                # 【最外层兜底】任何意外都不得杀进程。带着错误活着 > 干脆死去，
                # 因为前者还有心跳、还能被人看见。见模块文档第 5 条。
                log.error("× 主循环兜底异常（已捕获，继续运行）: %s", e, exc_info=True)
                self.beat("mainloop_ERROR", "error")

            loops += 1
            if max_loops is not None and loops >= max_loops:
                return
            time.sleep(LOOP_INTERVAL_SECONDS)


def build_runner(config: Config, strategy: Strategy, market, transport,
                 calendar: TradingCalendar, notifier=None,
                 schedule: Sequence[tuple[str, str]] | None = None) -> Runner:
    """装配一个 Runner。默认任务表全部指向同一轮流程（见 `register_defaults`）。"""
    runner = Runner(config, strategy, market, transport, calendar,
                    notifier=notifier, schedule=schedule)
    runner.register_defaults()
    return runner
