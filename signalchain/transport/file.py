# -*- coding: utf-8 -*-
"""文件后端：一个目录，每个 channel 一个 JSON 文件。

两个用途：
  1. **演示与测试的默认后端** —— 零凭据、零网络、零第三方服务，
     clone 下来就能把整条链路跑通。这是本仓库「完整」而非「半截」的关键。
  2. 单机/共享盘场景下的真实部署选项。

与 Gist 后端的对照很有意思，值得注意：
文件后端用 `tmp + os.replace` 写入，**单文件写是原子的**；
而 Gist 的 read-modify-write 不是。所以「单一写入者」这条约束在文件后端上
并不是正确性所必需的 —— 但本仓库仍然一律强制，因为：
  a) 同一份 Runner 代码要能无差别地切换到 Gist 后端；
  b) 依赖「恰好选了原子后端」的正确性是脆的，换后端就悄悄失效。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..models import CHANNEL_FILENAMES
from .base import Transport, TransportError


class FileTransport(Transport):
    """把每个 channel 存成 `<root>/<channel>.json`。

    参数：
        root:   存放目录，不存在会自动创建。
        indent: JSON 缩进。默认 2，与既有线上格式一致（便于人工 diff / 快照对比）。
    """

    supports_cas = False

    def __init__(self, root: str | os.PathLike, indent: int = 2) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.indent = indent

    # ---------------- 内部工具 ----------------

    def _path(self, channel: str) -> Path:
        # 文件名走 CHANNEL_FILENAMES，与 Gist 后端保持一致 ——
        # 换后端时落盘的文件名不变，人工排查的肌肉记忆可以复用。
        return self.root / CHANNEL_FILENAMES.get(channel, f"{channel}.json")

    # ---------------- Transport 接口 ----------------

    def read(self, channel: str) -> Any | None:
        path = self._path(channel)
        if not path.exists():
            # 「不存在」是正常状态（首次运行），返回 None。
            # 「读失败」必须抛异常 —— 两者含义相反，不能合并。
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise TransportError(f"读取 {path} 失败: {e}") from e
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise TransportError(f"{path} 内容不是合法 JSON: {e}") from e

    def write(self, channel: str, doc: Any) -> bool:
        path = self._path(channel)
        payload = json.dumps(doc, ensure_ascii=False, indent=self.indent)
        # 原子写：先写同目录临时文件再 rename。
        # 必须同目录 —— 跨文件系统的 rename 不是原子操作，而且会失败。
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except BaseException:
                # 失败时清掉临时文件，避免 root 目录里越积越多垃圾
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise TransportError(f"写入 {path} 失败: {e}") from e
        return True

    def describe(self) -> str:
        return f"file:{self.root}"
