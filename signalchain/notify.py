# -*- coding: utf-8 -*-
"""通知出口。

## 为什么通知要有「每日只发一次」的节流

告警系统最常见的死法不是不响，是**响得太多以至于没人看**。
一个每 30 秒重试的失败任务，如果每次都发一条，一晚上能刷出上千条；
用户第二天会直接静音它，从此所有告警都失效。

所以本模块内置 `NotifyThrottle`：同一个 key 每天只允许发一次。
注意它的语义是「**成功发出**才算发过」—— 发送失败时**不**占用当天的名额，
否则一次网络抖动就会让当天的告警永久丢失，等于把告警系统关掉了。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Protocol
from .console import emit

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, title: str, body: str) -> bool:
        ...


class NullNotifier:
    """不发。默认值 —— 让「不配置通知」成为最省事的选项，而不是报错。"""

    def send(self, title: str, body: str) -> bool:
        log.info("[notify:null] %s | %s", title, body.replace("\n", " ")[:200])
        return True


class ConsoleNotifier:
    """打到标准输出。演示与本地调试用。"""

    def send(self, title: str, body: str) -> bool:
        emit(f"\n=== {title} ===\n{body}\n")
        return True


class MultiNotifier:
    """扇出到多个出口。任何一个成功即视为成功。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, title: str, body: str) -> bool:
        ok = False
        for n in self.notifiers:
            try:
                ok = n.send(title, body) or ok
            except Exception as e:  # noqa: BLE001 - 通知失败绝不能影响主流程
                log.warning("通知出口 %s 异常: %s", type(n).__name__, e)
        return ok


class ServerChanNotifier:
    """Server 酱（方糖）微信推送。

    `sendkey` 从构造参数或环境变量 `SC_SERVERCHAN_KEY` 取，**源码里不留字面量**。
    未配置时本类不应该被实例化 —— 用 NullNotifier。
    """

    ENDPOINT = "https://sctapi.ftqq.com/{key}.send"

    def __init__(self, sendkey: str, timeout: float = 10.0) -> None:
        if not sendkey:
            raise ValueError("sendkey 不能为空；未配置请改用 NullNotifier")
        self._key = sendkey
        self._timeout = timeout

    @classmethod
    def from_env(cls, var: str = "SC_SERVERCHAN_KEY", **kwargs) -> "ServerChanNotifier | NullNotifier":
        import os

        key = os.environ.get(var, "").strip()
        if not key:
            log.info("未设置 %s，通知降级为 NullNotifier", var)
            return NullNotifier()
        return cls(key, **kwargs)

    def send(self, title: str, body: str) -> bool:
        import requests

        try:
            resp = requests.post(
                self.ENDPOINT.format(key=self._key),
                data={"title": title, "desp": body},
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 - 通知失败不上抛
            log.warning("ServerChan 发送异常: %s", e)
            return False
        if resp.status_code != 200:
            log.warning("ServerChan 发送失败: HTTP %s", resp.status_code)
            return False
        # ServerChan 用 body 里的 code 表示业务结果，HTTP 200 不代表发成功
        try:
            payload = resp.json()
        except ValueError:
            return False
        if payload.get("code") not in (0, None):
            log.warning("ServerChan 业务失败: %s", payload.get("message"))
            return False
        return True


class NotifyThrottle:
    """同一个 key 每天只放行一次。

    ★ 关键语义：**只有真正发成功才记账**。
    如果发送失败也记账，那么当天这个告警就再也发不出去了 ——
    一次瞬时网络抖动就能让一个严重问题沉默一整天。
    """

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier
        self._sent: dict[str, str] = {}  # key -> 日期字符串

    def notify_once(self, key: str, title: str, body: str,
                    today: date | None = None) -> bool:
        """已发过返回 False（未重复发）；本次发出返回 True。"""
        day = (today or date.today()).isoformat()
        if self._sent.get(key) == day:
            return False
        if not self._notifier.send(title, body):
            # 失败不记账 —— 留机会给下一次重试
            return False
        self._sent[key] = day
        return True

    def reset(self) -> None:
        """跨日重置。由 Runner 在新的一天调用。"""
        self._sent.clear()
