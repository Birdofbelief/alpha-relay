# -*- coding: utf-8 -*-
"""单一写入者强制 —— 把一条约定从「文档里的君子协定」变成运行期约束。

这组测试守的不是某个函数，而是整套设计的地基：Gist 的写是 read-modify-write，
两端同时写同一个 channel 会静默丢信号（双方日志都显示成功）。
所以「谁能写哪个 channel」必须有测试保证，不能靠人记得。
"""
from __future__ import annotations

import pytest

from signalchain.models import (
    CH_EXECUTIONS,
    CH_HEARTBEAT,
    CH_POSITIONS,
    CH_SIGNALS,
    ALL_CHANNELS,
)
from signalchain.transport.base import (
    CHANNEL_OWNERS,
    OwnershipError,
    Role,
    SingleWriterGuard,
    TransportError,
)
from signalchain.transport.file import FileTransport
from signalchain.transport.null import NullTransport

PRODUCER_OWNS = (CH_SIGNALS, CH_HEARTBEAT)
EXECUTOR_OWNS = (CH_POSITIONS, CH_EXECUTIONS)


def test_channel_owners_are_complete_and_disjoint():
    """四个 channel 必须**恰好**各有一个主人。

    漏一个 → 没人能写；多一个主人 → 竞态。这张表是权限的唯一定义，
    所以它本身要被测试钉住：将来加 channel 时忘了登记，这里会红。
    """
    assert set(CHANNEL_OWNERS) == set(ALL_CHANNELS)
    assert set(CHANNEL_OWNERS.values()) == {Role.PRODUCER, Role.EXECUTOR}


@pytest.mark.parametrize("channel", EXECUTOR_OWNS)
def test_producer_cannot_write_executor_channels(tmp_path, channel):
    """产生端写 positions/executions → 必须抛 OwnershipError。

    这两个 channel 是执行端的，产生端写它们会覆盖执行端刚报上来的真实持仓 ——
    而对账是整套风控的支点，它一旦被污染，闸门会基于假数据放行买入。
    """
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    with pytest.raises(OwnershipError):
        guard.write(channel, {})


@pytest.mark.parametrize("channel", PRODUCER_OWNS)
def test_executor_cannot_write_producer_channels(tmp_path, channel):
    """执行端写 signals/heartbeat → 必须抛 OwnershipError。

    执行端**不能**回写 signals 去标记 processed，这是单一写入者约定最直接的代价，
    也是它最容易被绕过的地方（「我只是改一个布尔字段」）。绕过的后果是
    产生端下一次 read-modify-write 会把这次修改整体覆盖掉。
    """
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.EXECUTOR)
    with pytest.raises(OwnershipError):
        guard.write(channel, [])


@pytest.mark.parametrize("channel", ALL_CHANNELS)
def test_read_is_allowed_for_every_channel(tmp_path, channel):
    """**读不受所有权限制** —— 两端都要读全部四个 channel。

    所有权约束的是写。若这里也拦，执行端就读不到信号、产生端就读不到持仓，
    整条链直接断掉。所以「写错要炸」和「读全都行」是一组必须同时成立的性质。
    """
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    assert guard.read(channel) is None       # 文件不存在 → None，不是异常


def test_owner_can_actually_write(tmp_path):
    """别只测「不该写的写不了」—— 也要测「该写的写得成」。

    否则一个「永远抛异常」的实现也能让上面所有测试通过。
    """
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    assert guard.write(CH_SIGNALS, [{"id": "s1"}]) is True
    assert guard.read(CH_SIGNALS) == [{"id": "s1"}]


def test_unknown_channel_is_rejected_on_both_paths(tmp_path):
    """不认识的 channel 读写都要炸。

    名字打错（比如把 signals 写成 signal）在「按文件名存」的后端上会**静默成功**
    —— 它只是创建了一个没人读的文件。必须在这里拦住。
    """
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    for bad in ("signal", "", "SIGNALS"):
        with pytest.raises(TransportError):
            guard.read(bad)
        with pytest.raises(TransportError):
            guard.write(bad, [])


def test_guard_preserves_backend_identity_and_role(tmp_path):
    """describe() 要同时暴露后端身份和角色 —— 排障时这两个都要看。

    一次「为什么写不进去」的排查，第一个要问的就是「这个进程自认为是谁」。
    """
    inner = FileTransport(tmp_path)
    guard = SingleWriterGuard(inner, Role.EXECUTOR)
    assert guard.role is Role.EXECUTOR
    assert guard.inner is inner
    assert "executor" in guard.describe()
    assert inner.describe() in guard.describe()


def test_guard_propagates_cas_support():
    """`supports_cas` 必须透传。

    它决定上层能不能走「原子比较并交换」那条路。被 guard 吞掉的话，
    将来接上支持 CAS 的后端会被当成不支持 —— 功能静默降级。
    """
    class CasBackend(NullTransport):
        supports_cas = True

    assert SingleWriterGuard(CasBackend(), Role.PRODUCER).supports_cas is True
    assert SingleWriterGuard(NullTransport(), Role.PRODUCER).supports_cas is False


def test_ownership_error_is_a_transport_error(tmp_path):
    """`OwnershipError` 是 `TransportError` 的子类，且**不能被降级吞掉**。

    调用方通常会 `except TransportError` 然后降级重试。若所有权错误混在其中，
    「配置写错了」就会表现为「网络不好，再试几次」——
    一个永远不会自愈的错误被当成瞬时故障重试，症状是最难查的那种。
    """
    assert issubclass(OwnershipError, TransportError)
    guard = SingleWriterGuard(FileTransport(tmp_path), Role.PRODUCER)
    with pytest.raises(OwnershipError):      # 必须能按更具体的类型抓到
        guard.write(CH_POSITIONS, {})
