# -*- coding: utf-8 -*-
"""日历加载必须**有界** —— 第三方库里的裸请求不能把主循环一起挂死。

对应 `calendar.py` 的「教训三」。三种后果各一条断言：

1. 挂住 → 按时放弃，**不把整个进程拖住**；
2. 挂住但有旧缓存 → 旧缓存**原样保留**（「教训一」）；
3. 挂住之后再刷新 → **不叠加新线程**（否则「卡死」会升级成「卡死 + 泄漏」）。

为什么这些测试值得单独存在：挂住的进程**不崩、不报错、日志停在最后一行**。
没有一条会变红的测试，这个修复就只是一段看起来对的代码。
"""
from __future__ import annotations

import threading
import time
from datetime import date

from signalchain.calendar import TradingCalendar

DAY = date(2026, 9, 18)


def _hang_forever():
    time.sleep(30)          # 远大于测试里用的 0.2s 时限
    return [DAY]


def test_hanging_loader_gives_up_on_time():
    cal = TradingCalendar(loader=_hang_forever, loader_timeout=0.2)

    started = time.monotonic()
    assert cal.load() is False
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"应当很快放弃，实际 {elapsed:.1f}s"
    assert cal.usable is False


def test_hang_keeps_the_previous_cache():
    state = {"hang": False}

    def loader():
        if state["hang"]:
            time.sleep(30)
        return [DAY]

    cal = TradingCalendar(loader=loader, loader_timeout=0.2)
    assert cal.load() is True

    state["hang"] = True
    assert cal.refresh() is False

    # 教训一：加载成功但过期 > 加载失败。旧缓存必须原样在。
    assert cal.usable is True
    assert cal.is_trading_day(DAY) is True


def test_timed_out_load_is_not_stacked():
    # 时限取 0.5s、阈值取 0.3s：留够 CI 机器被抢的余量，同时仍能把
    # 「又等了一个完整时限」区分出来（那会是 ≥0.5s）。
    cal = TradingCalendar(loader=_hang_forever, loader_timeout=0.5)
    assert cal.load() is False          # 第一个线程还挂在 sleep(30) 里

    threads_before = threading.active_count()
    started = time.monotonic()
    assert cal.refresh() is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, "上一次还没回来时应当立刻放弃，而不是再等一个时限"
    assert threading.active_count() == threads_before, "不许再叠一个加载线程"


def test_loader_exception_still_degrades_to_failure():
    def boom():
        raise RuntimeError("akshare 炸了")

    cal = TradingCalendar(loader=boom, loader_timeout=0.2)

    assert cal.load() is False
    assert cal.usable is False


def test_fast_loader_still_works_with_the_timeout_on():
    cal = TradingCalendar(loader=lambda: [DAY], loader_timeout=5.0)

    assert cal.load() is True
    assert cal.is_trading_day(DAY) is True


def test_timeout_can_be_disabled_for_offline_wiring():
    """`loader_timeout=None` 只给离线注入用：同步调用，不开线程。"""
    cal = TradingCalendar(loader=lambda: [DAY], loader_timeout=None)
    threads_before = threading.active_count()

    assert cal.load() is True
    assert threading.active_count() == threads_before
