# -*- coding: utf-8 -*-
"""交易日历。

这里有两条从实盘换来的教训，都是「静默失效」型的 —— 出问题时没有任何异常，
只是某些该发生的动作不再发生。所以本模块的设计围绕**如何不静默**展开。

## 教训一：刷新失败时，保留旧缓存

直觉写法是「刷新失败 → 把缓存清空 → 下次重试」。这在本场景里是**有害的**，
因为清空会让 `is_trading_day` 退化到「按工作日判断」，于是法定节假日会被判成
交易日 —— 程序照常运行并**在休市日产生信号**。

比较两种失效的严重程度：
  - 日历过期（缓存旧）  → 漏掉一个假期，最坏情况是照着上一年的日历跑
  - 日历清空（退化工作日）→ **在休市日交易**，直接产生无效信号

所以：**加载成功但过期 > 加载失败**。刷新失败时明确保留旧缓存并告警。

## 教训二：日历有「保质期」，长驻进程会跨年撞上

行情源的交易日历**只覆盖到当年 12-31**。一个「部署完就一直跑」的进程
（这是常态，不是例外）在跨年之后会发现：次年的任何日期都不在缓存里，
于是 `is_trading_day` 恒为 False，主循环一路 `sleep + continue` ——
**不买不卖不止损，日志里一行异常都没有。**

修复是「进入新的一天时刷新一次」。它不是可有可无的优化，是防止全年静默停摆。

## 教训三：默认 loader 里有一个**没有超时**的第三方请求

`akshare` 的 `tool_trade_date_hist_sina()` 内部自己发 HTTP 请求，而且**不传 timeout**。
requests 在没传 timeout 时**没有默认上限** —— 一条半开的连接可以让它永远阻塞。

⚠️ **不要用 `socket.setdefaulttimeout()` 去兜。** 这是最常见的「看起来对」的药方，
它**对 requests 无效**：requests 会把继承来的默认值显式塞进自己的参数里，把默认值盖掉。
本条不是推演 —— 姊妹项目实测证伪过这个药方，然后量到真正的修法。

对框架来说，正确的位置不是去全局打补丁改 `requests` 的行为（库不该在 import 时
偷偷改变整个进程的语义），而是**在自己这一层套一个有界调用**：
到点就放弃本次刷新 → 保留旧缓存（教训一）→ 告警 → 主循环照常跑。
**并且同一时刻只允许一个加载在飞**，否则每次重试都再挂一个线程，
挂得越久线程越多，原本的「卡死」会变成「卡死 + 泄漏」。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Iterable

log = logging.getLogger(__name__)

#: 默认 loader 的等待上限（秒）。**必须有值**：见模块文档「教训三」。
#: 与 `config.http_timeout`（`SC_HTTP_TIMEOUT`，默认 15）同值 —— 两个都是
#: 「等一个不受我们控制的网络动作」，给两个数只会让人以为有两道防线。
DEFAULT_LOAD_TIMEOUT = 15.0


class CalendarError(RuntimeError):
    pass


class TradingCalendar:
    """基于交易日集合的日历。

    数据来源由 `loader` 提供（返回日期序列）。默认实现走 akshare；
    测试可以直接注入一组日期，不必联网。
    """

    def __init__(self, loader=None,
                 loader_timeout: float | None = DEFAULT_LOAD_TIMEOUT) -> None:
        self._loader = loader
        #: `None` = 不加时限（只给测试与离线注入用）。生产路径**永远不要**传 None。
        self._loader_timeout = loader_timeout
        self._in_flight: threading.Thread | None = None
        self._days: set[date] | None = None
        self._years: set[int] = set()
        self._last_refresh: datetime | None = None
        self._refresh_failures = 0

    # ---------------- 加载 ----------------

    @staticmethod
    def _default_loader() -> list[date]:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        return [d.date() if hasattr(d, "date") else d for d in df["trade_date"]]

    def _bounded_load(self, loader) -> list[date]:
        """有界地调用 loader。超时抛 `CalendarError`（由 `refresh` 转成「保留旧缓存」）。

        为什么是线程 + join，而不是给 akshare 传 timeout：
        **那个 timeout 不在我们手里** —— 它是 akshare 内部的 `requests` 调用，
        我们只能控制「等它多久」，控制不了「它自己等多久」。

        线程无法被强杀，所以超时之后那个线程会一直挂着。这可以接受，
        但**不能每个刷新周期都再挂一个** —— 那会把「卡住」升级成「卡住 + 线程泄漏」。
        `_in_flight` 就是为了这件事：上一次还没回来，这一次直接放弃，不再开新线程。
        """
        if self._loader_timeout is None:
            return list(loader())

        if self._in_flight is not None and self._in_flight.is_alive():
            raise CalendarError(
                "上一次日历加载仍未返回，本次放弃（不叠加新线程）。"
                "若每次都报这一句，说明那次加载永远不会回来 —— "
                "**重启进程**才能恢复日历刷新（旧缓存仍可用，但不会更新）")

        box: dict[str, object] = {}

        def run() -> None:
            try:
                box["days"] = list(loader())
            except BaseException as e:  # noqa: BLE001 - 原样带回主线程再抛
                box["error"] = e

        worker = threading.Thread(target=run, name="calendar-loader", daemon=True)
        self._in_flight = worker
        worker.start()
        worker.join(self._loader_timeout)
        if worker.is_alive():
            raise CalendarError(
                f"日历加载超过 {self._loader_timeout:.0f}s 未返回，放弃本次刷新")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["days"]  # type: ignore[return-value]

    def load(self) -> bool:
        """首次加载。失败返回 False（调用方应据此进入保守模式）。"""
        return self.refresh(initial=True)

    def refresh(self, initial: bool = False) -> bool:
        """刷新日历。

        ★ **失败时保留旧缓存。** 见模块文档「教训一」。
        返回是否刷新成功。
        """
        loader = self._loader or self._default_loader
        try:
            days = self._bounded_load(loader)
        except Exception as e:  # noqa: BLE001 - 加载失败是预期内情况
            self._refresh_failures += 1
            if self._days:
                log.error("◇ 交易日历刷新失败，**保留旧缓存**（%d 天，覆盖到 %s）: %s",
                          len(self._days), max(self._days), e)
            else:
                log.error("◇ 交易日历刷新失败且**无旧缓存可用**，"
                          "is_trading_day 将保守地返回 False（宁可不动，不可乱动）: %s", e)
            return False

        if not days:
            # 空结果与异常同等对待 —— 都是「没拿到」，都不能用来覆盖好数据。
            self._refresh_failures += 1
            log.error("◇ 交易日历刷新返回空，保留旧缓存（%s）",
                      f"{len(self._days)} 天" if self._days else "无")
            return False

        self._days = set(days)
        self._years = {d.year for d in self._days}
        self._last_refresh = datetime.now()
        self._refresh_failures = 0
        if not initial:
            log.info("◇ 交易日历已刷新：%d 天，覆盖 %s",
                     len(self._days), sorted(self._years))
        return True

    def needs_new_year_refresh(self, today: date) -> bool:
        """今天所在年份是否还没被日历覆盖。

        这是跨年静默停摆的**唯一**检出点。返回 True 时调用方必须刷新，
        否则整个新年度都会判成非交易日。
        """
        if self._days is None:
            return True
        return today.year not in self._years

    # ---------------- 查询 ----------------

    @property
    def usable(self) -> bool:
        return bool(self._days)

    def is_trading_day(self, when: date | datetime) -> bool:
        """是否交易日。

        **无日历时返回 False（保守）**，而不是退化成「按工作日判断」。
        宁可整天不动，也不在休市日产生信号 —— 前者看得见（心跳停更、无信号），
        后者要等成交失败或对账异常才暴露。
        """
        if not self._days:
            return False
        d = when.date() if isinstance(when, datetime) else when
        return d in self._days

    def previous_trading_day(self, when: date | datetime) -> date | None:
        """严格早于 `when` 的最近一个交易日。"""
        if not self._days:
            return None
        d = when.date() if isinstance(when, datetime) else when
        probe = d - timedelta(days=1)
        for _ in range(30):  # 最长假期不超过 30 天，兜住死循环
            if probe in self._days:
                return probe
            probe -= timedelta(days=1)
        return None

    def previous_closed_trading_day(self, when: date | datetime) -> date | None:
        """**已收盘**的最近一个交易日。

        注意它和「今天是不是交易日」无关，两种情况答案一样：
          - 今天是交易日（周一盘中）→ 今天的日线还没生成 → 答案是上周五
          - 今天不是交易日（周六）    → 答案是上周五
        两种情况下 `previous_trading_day` 都给出正确答案，所以这里**不分支**。
        写成一个方法只是为了让调用点的意图显式：我要的是「已收盘」的那个交易日。
        """
        return self.previous_trading_day(when)

    def trading_days_behind(self, newest: date, target: date) -> int | None:
        """`newest` 落后 `target` 几个交易日。用于缓存新鲜度分档。"""
        if not self._days:
            return None
        probe, count = newest, 0
        while probe < target and count < 60:
            probe += timedelta(days=1)
            if probe in self._days:
                count += 1
        return count

    def describe(self) -> str:
        if not self._days:
            return "calendar(未加载)"
        return f"calendar({len(self._days)}天/{sorted(self._years)})"


def make_static_calendar(days: Iterable[date]) -> TradingCalendar:
    """用固定日期集合造一个日历，测试与离线演示用。"""
    cal = TradingCalendar(loader=lambda: list(days))
    cal.load()
    return cal


def weekday_calendar(start: date, end: date) -> TradingCalendar:
    """把所有工作日当成交易日。

    ⚠️ **仅供离线演示与测试。** 真实运行时它会在法定节假日产生信号 ——
    正是本模块「教训一」里描述的那种错误。名字里带 weekday 就是为了让它
    在代码里一眼可见、不会被误用到生产路径上。
    """
    days = []
    probe = start
    while probe <= end:
        if probe.weekday() < 5:
            days.append(probe)
        probe += timedelta(days=1)
    return make_static_calendar(days)
