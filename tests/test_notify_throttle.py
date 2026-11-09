# -*- coding: utf-8 -*-
"""告警节流的语义：**按日一次**、**发成功才记账**、**跨日复位**。

这条线在姊妹项目里出过事故，症状是：**长驻进程只响第一天，之后永久静默**。
两种成因都值得钉住：

- 「发失败也记账」⇒ 一次网络抖动让当天的告警再也发不出去；
- 「当日一次的位不跨日复位」⇒ 第二天起永远静默。

★ 本仓库的正确性**不依赖** key 里带日期：`notify_once` 存的是「上次是**哪一天**发的」，
日期对不上就重新放行。所以 `reset()` 只负责**内存**（账本按 key 长），
不负责正确性 —— 这条区别很容易被写反，测试里两条都钉住。
"""
from __future__ import annotations

import logging
from datetime import date

from signalchain.notify import NotifyThrottle


def test_serverchan_exception_log_redacts_the_sendkey(caplog, monkeypatch):
    """★ requests 的异常文本里带完整 URL，而 URL 里就是 sendkey。

    原实现直接 `log.warning("...: %s", e)`，一次网络抖动就把 key 永久写进
    日志文件 —— 与「凭据不许进日志」那条铁律直接冲突。
    """
    import requests
    import signalchain.notify as notify

    key = "SCT-test-key-123"

    def _raise(url, **kwargs):
        raise requests.ConnectionError(f"连接失败: {url}")

    monkeypatch.setattr(requests, "post", _raise)
    notifier = notify.ServerChanNotifier(key)

    with caplog.at_level(logging.WARNING, logger="signalchain.notify"):
        assert notifier.send("标题", "正文") is False

    assert caplog.records, "发送失败却没有留下任何日志"
    leaked = [r.getMessage() for r in caplog.records if key in r.getMessage()]
    assert not leaked, f"sendkey 进了日志：{leaked}"


class RecordingNotifier:
    """记录发过什么；`results` 控制前几次发送的成败。"""

    def __init__(self, results: list[bool] | None = None) -> None:
        self._results = list(results or [])
        self.sent: list[str] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append(title)
        return self._results.pop(0) if self._results else True


def test_same_key_same_day_is_sent_once():
    notifier = RecordingNotifier()
    throttle = NotifyThrottle(notifier)

    assert throttle.notify_once("quote_fail:2026-09-18", "t", "b",
                                date(2026, 9, 18)) is True
    assert throttle.notify_once("quote_fail:2026-09-18", "t", "b",
                                date(2026, 9, 18)) is False
    assert len(notifier.sent) == 1


def test_failed_send_is_not_recorded():
    notifier = RecordingNotifier([False, True])
    throttle = NotifyThrottle(notifier)

    # 第一次发失败 —— 不记账，所以下一轮还有机会
    assert throttle.notify_once("k", "t", "b", date(2026, 9, 18)) is False
    assert throttle.notify_once("k", "t", "b", date(2026, 9, 18)) is True
    assert len(notifier.sent) == 2


def test_keys_embedding_the_day_rearm_by_themselves():
    notifier = RecordingNotifier()
    throttle = NotifyThrottle(notifier)
    d1, d2 = date(2026, 9, 18), date(2026, 9, 19)

    assert throttle.notify_once(f"quote_fail:{d1.isoformat()}", "t", "b", d1)
    assert throttle.notify_once(f"quote_fail:{d2.isoformat()}", "t", "b", d2)

    assert len(notifier.sent) == 2, "日期一变 key 就变，不需要 reset 也该重新响"


def test_a_key_without_a_day_is_still_rearmed_by_the_day_comparison():
    """不含日期的 key **不会**永久静默 —— 节流自己存着「上次是哪天」。

    这条是刻意留下的反直觉断言：容易把「当日一次」误解成「key 里必须编日期」。
    真正的失效形状是「位只置不复位」，不是「key 里没有日期」。
    """
    notifier = RecordingNotifier()
    throttle = NotifyThrottle(notifier)

    assert throttle.notify_once("positions_unsync", "t", "b", date(2026, 9, 18))
    assert throttle.notify_once("positions_unsync", "t", "b", date(2026, 9, 18)) is False
    assert throttle.notify_once("positions_unsync", "t", "b", date(2026, 9, 19)) is True

    assert len(notifier.sent) == 2


def test_reset_clears_the_bookkeeping():
    throttle = NotifyThrottle(RecordingNotifier())
    throttle.notify_once("k", "t", "b", date(2026, 9, 18))
    assert throttle._sent  # noqa: SLF001 - 这里就是要看内部账本有没有变大

    throttle.reset()
    assert throttle._sent == {}
