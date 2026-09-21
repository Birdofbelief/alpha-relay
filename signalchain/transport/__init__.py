# -*- coding: utf-8 -*-
"""传输层 —— 「Gist 可拔插」的实现处。

后端清单：

    FileTransport   本地/共享盘。**零凭据**，演示与测试的默认
    NullTransport   什么都不做，只记日志。演练与「干跑」用
    GistTransport   GitHub Gist。提取自实盘，行为逐条对齐

它们都实现同一个 `Transport` 协议，并且都必须通过
`tests/test_transport_contract.py` 里的**同一套契约用例**。
那是「可拔插」的证据，不是口号。
"""
from .base import (
    CHANNEL_OWNERS,
    OwnershipError,
    Role,
    SingleWriterGuard,
    Transport,
    TransportError,
)
from .file import FileTransport
from .null import NullTransport

__all__ = [
    "Transport", "TransportError", "OwnershipError",
    "Role", "CHANNEL_OWNERS", "SingleWriterGuard",
    "FileTransport", "NullTransport", "GistTransport",
]


def __getattr__(name: str):
    """`GistTransport` 惰性导入 —— 让离线测试不必走 requests 那条路径。"""
    if name == "GistTransport":
        from .gist import GistTransport
        return GistTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
