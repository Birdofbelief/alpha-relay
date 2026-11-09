# -*- coding: utf-8 -*-
"""`Config` 的**显式构造**路径。

`from_env()` 之外的另一种装配方式（文档里推荐的、测试里到处在用的
`Config(runtime_dir=tmp_path / "rt")`）不该因为参数写法不同而在运行期
抛出一句离原因很远的 `TypeError`。这三条都是「手写 dataclass」时
很自然、但在原实现里会静默/爆炸的形状。
"""
from __future__ import annotations

from pathlib import Path

from signalchain.config import Config

from .conftest import SYNTHETIC_SH, SYNTHETIC_SZ


def test_a_string_runtime_dir_is_coerced_to_a_path(tmp_path):
    """`runtime_dir=str(...)` 原来会让 `cache_dir` 的 `/` 运算抛 TypeError。"""
    cfg = Config(runtime_dir=str(tmp_path / "rt"))

    assert isinstance(cfg.runtime_dir, Path)
    assert cfg.state_path == cfg.runtime_dir / "state.json"
    cfg.ensure_dirs()
    assert cfg.cache_dir.is_dir()


def test_role_is_normalized_like_from_env_does():
    """大小写不该算配置错误：`from_env` 已经 lower+strip，直接构造也应一致。"""
    assert Config(role=" Producer ").role == "producer"
    assert Config(role="EXECUTOR").role == "executor"


def test_limit_20_codes_accepts_a_comma_separated_string():
    """环境变量那条路接受 `"a,b"`，直接构造时传同一个字符串也应当能解析。"""
    cfg = Config(limit_20_codes=f"{SYNTHETIC_SH}, {SYNTHETIC_SZ}")

    assert cfg.limit_20_codes == frozenset({SYNTHETIC_SH, SYNTHETIC_SZ})
