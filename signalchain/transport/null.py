# -*- coding: utf-8 -*-
"""空后端：把一切写入吞掉，只留日志。

用途是 **dry-run**：想验证「今天的信号会是什么」而不想让任何东西真的发出去时，
把后端换成它。它比 `--dry-run` 标志位更好的地方在于：调用方的代码路径完全不变，
所以它验证的是**真实路径**，而不是一条专门为演练写的旁路。

它同时是 `TransportContract` 测试的一个实现 —— 「能不能只换后端、不换其它代码」
这件事由测试来证明，而不是靠文档承诺。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import Transport

log = logging.getLogger(__name__)


class NullTransport(Transport):
    """只记录、不落盘。可选地保留内存副本，便于测试断言。"""

    supports_cas = False

    def __init__(self, keep_in_memory: bool = True, label: str = "null") -> None:
        self._keep = keep_in_memory
        self._memory: dict[str, Any] = {}
        self._label = label
        #: 写入历史，供测试检查「写了什么、写了几次」
        self.write_log: list[tuple[str, Any]] = []

    def read(self, channel: str) -> Any | None:
        if not self._keep:
            return None
        value = self._memory.get(channel)
        # 深拷贝一份再返回，避免调用方改到内部状态 —— 真实后端每次都是新解析出来的
        # 对象，测试里如果因为共享引用而「碰巧通过」，就失去了意义。
        return json.loads(json.dumps(value, ensure_ascii=False)) if value is not None else None

    def write(self, channel: str, doc: Any) -> bool:
        self.write_log.append((channel, doc))
        if self._keep:
            self._memory[channel] = json.loads(json.dumps(doc, ensure_ascii=False))
        log.info("[null-transport] 丢弃写入 %s（%d 条）",
                 channel, len(doc) if isinstance(doc, (list, dict)) else 1)
        return True

    def describe(self) -> str:
        return f"null:{self._label}(dry-run)"
