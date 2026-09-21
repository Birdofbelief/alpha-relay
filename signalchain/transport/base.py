# -*- coding: utf-8 -*-
"""传输层抽象 —— 「可拔插」的全部含义就在这个文件里。

## 为什么不是一个普通的 KV 接口

链路要跨两台机器、两个进程、两种部署节奏传递四类文档：

    signals    / heartbeat    ← 信号端写，执行端读
    positions  / executions   ← 执行端写，信号端读

如果把「谁可以写哪个 key」交给使用者的自觉，那么第一个偷懒的实现就会引入竞态。
所以**所有权是接口的一部分**（`CHANNEL_OWNERS`），由 `SingleWriterGuard` 在运行期强制。

## 为什么单一写入者是硬约束，而不是风格偏好

以 Gist 后端为例。「往信号列表里追加一条」必须走 read-modify-write：

    GET  /gists/:id                     → 拿到 trade_signals.json 的内容
    本地 append 一条
    PATCH /gists/:id  {files: {...}}    → 用新内容整体替换该文件

注意 PATCH 只替换你指定的那个文件、不会动 gist 里的其它文件 —— 但**文件内容的
替换是整体覆盖**，而这份内容是在上一步 GET 之上算出来的。两处之间没有原子性：
两端同时跑这段，后到的 PATCH 会覆盖前一个刚写进去的信号，信号凭空消失，
而双方日志都显示「成功」。

因此当前设计只有两个合法选择：
  a) 每个 channel 只有一个写入者（本项目采用，`SingleWriterGuard` 强制）；
  b) 换一个支持原子操作/CAS 的后端（`Transport` 接口已为它预留 `compare_and_swap`）。

选 (a) 的代价是：执行端**不能**回写 signals 去标记 processed ——
所以「信号被消化了」这件事由 `executions` 表达，而不是修改 signals 本身。
这个约束会顺着设计一路传导，别试图绕过它。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from ..models import (
    ALL_CHANNELS,
    CH_EXECUTIONS,
    CH_HEARTBEAT,
    CH_POSITIONS,
    CH_SIGNALS,
)


class Role(str, Enum):
    """本进程在链路里的角色。决定它能写哪些 channel。"""

    PRODUCER = "producer"
    EXECUTOR = "executor"


#: channel → 唯一允许写入它的角色。
CHANNEL_OWNERS: dict[str, Role] = {
    CH_SIGNALS: Role.PRODUCER,
    CH_HEARTBEAT: Role.PRODUCER,
    CH_POSITIONS: Role.EXECUTOR,
    CH_EXECUTIONS: Role.EXECUTOR,
}


class TransportError(RuntimeError):
    """传输层的可预期失败。调用方应当捕获并降级，而不是让它冒泡杀掉主循环。"""


class OwnershipError(TransportError):
    """写了自己不拥有的 channel。这是**配置错误**，必须响亮地失败。

    刻意不降级成 warning：一旦压低成日志，它就会在生产的噪声里消失，
    而后果是两端互相覆盖、信号静默丢失 —— 正是这套设计要根除的故障模式。
    """


class Transport(ABC):
    """链路文档的存储后端。

    实现者只需关心四件事：读、写、探测可用性、自报身份。
    重试、退避、所有权校验、序列化统一由外层处理。
    """

    #: 后端是否支持原子的「比较并交换」。文件/Gist 都是 False；真实数据库可为 True。
    supports_cas: bool = False

    @abstractmethod
    def read(self, channel: str) -> Any | None:
        """读取一个 channel 的内容。

        返回已反序列化的 Python 对象；channel 不存在或为空时返回 None。
        **不要把「不存在」和「读失败」都返回 None** —— 读失败请抛 TransportError，
        因为这两者对调用方的含义完全相反：
          - 不存在 → 首次运行，正常
          - 读失败 → 数据可能丢了，必须降级（参见 fail-closed 闸门）
        """

    @abstractmethod
    def write(self, channel: str, doc: Any) -> bool:
        """整体覆盖一个 channel。返回是否成功。

        覆盖语义（而非追加）是刻意的：它让「重发同一份内容」天然幂等，
        重试不会造成重复。追加语义的重试则会造成重复 —— 对交易信号是致命的。
        """

    def healthcheck(self) -> tuple[bool, str]:
        """后端自检，供启动时打印与心跳使用。默认实现：尝试读一次。"""
        try:
            self.read(CH_SIGNALS)
            return True, "ok"
        except Exception as e:  # noqa: BLE001 - 自检要吞掉一切异常
            return False, f"{type(e).__name__}: {e}"

    @abstractmethod
    def describe(self) -> str:
        """一句话描述本后端（**不得包含凭据**）。会打进日志与心跳。"""


class SingleWriterGuard(Transport):
    """给任意后端套上「单一写入者」强制。

    把这条约定从「文档里的君子协定」变成「运行期会抛异常的约束」，
    是这套设计中性价比最高的一处改动：它让一类静默的数据竞争变成一次响亮的崩溃。

    用法：Runner 与 Executor 各自用自己角色的 guard 包住同一个后端。
    """

    def __init__(self, inner: Transport, role: Role) -> None:
        self._inner = inner
        self._role = role
        self.supports_cas = inner.supports_cas

    @property
    def role(self) -> Role:
        return self._role

    @property
    def inner(self) -> Transport:
        return self._inner

    def read(self, channel: str) -> Any | None:
        if channel not in ALL_CHANNELS:
            raise TransportError(f"未知 channel: {channel!r}（合法值：{ALL_CHANNELS}）")
        return self._inner.read(channel)

    def write(self, channel: str, doc: Any) -> bool:
        if channel not in ALL_CHANNELS:
            raise TransportError(f"未知 channel: {channel!r}（合法值：{ALL_CHANNELS}）")
        owner = CHANNEL_OWNERS[channel]
        if owner != self._role:
            raise OwnershipError(
                f"拒绝写入 channel {channel!r}：它属于 {owner.value}，"
                f"而本进程的角色是 {self._role.value}。"
                f"（单一写入者约定 —— 见 signalchain/transport/base.py 顶部注释）"
            )
        return self._inner.write(channel, doc)

    def describe(self) -> str:
        return f"{self._inner.describe()} [as {self._role.value}]"
