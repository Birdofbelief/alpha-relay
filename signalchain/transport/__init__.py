# -*- coding: utf-8 -*-
"""传输层 —— 「投递后端可拔插」的实现处。

随包发的后端：

    FileTransport   本地/共享盘。**零凭据**，演示与测试的默认
    NullTransport   什么都不做，只记日志。演练与「干跑」用
    GistTransport   GitHub Gist。提取自实盘，行为逐条对齐

它们都实现同一个 `Transport` 协议，并且都必须通过
`tests/test_transport_contract.py` 里的**同一套契约用例**。
那是「可拔插」的证据，不是口号。

## 「可拔插」到哪一步为止

上面三个名字是**随包发的**，不是**你能用的全部**。后端一共两处可插：

    SC_TRANSPORT=../my_private_objectstore.py    # ① 配置文件里给一条路径
    build_chain(config, transport=obj)           # ② 装配时直接把对象传进去

①指向的是**仓库外**的一个 `.py` 文件，里面写你自己的 `Transport` 子类
（对象存储、自建 HTTP、网络盘——每一家券商终端够得着的东西都不一样，
框架替不了你）。②是同一个东西的另一条路：显式传入优先于配置，
和策略的 `strategy=` 是同一套优先级。

两条路都只是**装配期的一件事**：策略、闸门、调度、执行端一行不动。
契约测试里那套用例，你的后端也应当能过一遍 —— 它们是可执行的最低要求。

⚠️ 按路径加载 = 执行该文件里的任意代码。只指向你自己的文件，
绝不要指向下载来的、别人给的路径。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from ..config import Config
from .base import (
    BUILTIN_TRANSPORTS,
    CHANNEL_OWNERS,
    OwnershipError,
    Role,
    SingleWriterGuard,
    Transport,
    TransportError,
)
from .file import FileTransport
from .null import NullTransport

log = logging.getLogger(__name__)

__all__ = [
    "Transport", "TransportError", "OwnershipError",
    "Role", "CHANNEL_OWNERS", "SingleWriterGuard",
    "FileTransport", "NullTransport", "GistTransport",
    "BUILTIN_TRANSPORTS", "load_transport",
]


def __getattr__(name: str):
    """`GistTransport` 惰性导入 —— 让离线测试不必走 requests 那条路径。"""
    if name == "GistTransport":
        from .gist import GistTransport
        return GistTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ============================================================
# 按配置造后端
# ============================================================

def load_transport(config: Config) -> Transport:
    """按 `config.transport` 造后端。三种取值：

        "file" / "gist" / "null"        随包发的（`BUILTIN_TRANSPORTS`）
        "../my_objectstore.py"          仓库外文件路径 → 里面找 `Transport` 子类
        其它                             **抛错**

    第三条是刻意的：**不认识的取值不许悄悄退回文件后端。**
    退回的后果不是「少了个功能」，是**两端写到了两处不同的存储**，
    而两边日志都显示成功 —— 信号不丢不报错，只是永远送不到。
    这跟 `build_chain` 在 `executor=` 与 `with_executor=True` 撞车时直接报错、
    以及策略文件里有多个候选类时必须显式指定，是同一条纪律：**不猜**。

    唯一的例外是 gist 缺凭据 —— 那一条**保留降级**（打到 console 报 error），
    因为它的成因是「凭据没配好」而不是「你写错了名字」，
    且在无人值守的场景里，能跑但降级好过起不来。
    """
    kind = config.transport
    # 内置名不区分大小写（`FILE` / `Gist` 都认）；**路径则原样**。
    # 这个区别是必要的：路径一旦被 lower()，在大小写敏感的文件系统上
    # `../MyStore.py` 会变成「文件不存在」，而报错会指向一个用户没写过的路径。
    lowered = kind.lower() if isinstance(kind, str) else kind
    if lowered in BUILTIN_TRANSPORTS:
        return _build_builtin(lowered, config)
    return _load_external_transport(kind)


def _build_builtin(kind: str, config: Config) -> Transport:
    """随包发的三个后端。"""
    if kind == "gist":
        from .gist import GistTransport
        if not (config.gist_id and config.github_token):
            log.error("× 配置为 gist 后端但凭据不全，**降级为文件后端**（不带着空凭据发请求）")
            return FileTransport(config.channel_dir)
        return GistTransport.from_env()
    if kind == "null":
        return NullTransport()
    return FileTransport(config.channel_dir)


def _load_external_transport(target: str) -> Transport:
    """从**仓库外**的 `.py` 文件加载一个后端。

    写法与 `strategy.load_strategy` 的按路径加载一致 ——
    仓库里「从外面装一个类进来」这件事只有这一种形状，不该有第二种。
    """
    path = Path(target)
    if not path.exists():
        path = Path(target + ".py")
    if not path.exists():
        raise FileNotFoundError(
            f"找不到传输后端 {target!r}（既不是内置名 {'/'.join(BUILTIN_TRANSPORTS)}，"
            f"也不是存在的文件路径）")

    path = path.resolve()
    module_name = f"signalchain_user_transport_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载传输后端文件: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    transport = _find_transport(module, path)
    log.info("已加载外部传输后端 %s（来自仓库外路径 %s）", transport.describe(), path)
    return transport


def _find_transport(module, path: Path) -> Transport:
    """在模块里找后端对象。

    和策略那边同一套规则，包括**为什么**要有这套规则：

      - 一个文件里放辅助类是常事，所以候选可以有多个；
      - 但**有且只有一个**候选时才自动选它 —— 多个就必须显式指定，
        因为静默选中一个的代价是「你以为在写 A 存储，实际写的是 B」，
        而它不报错，表现上跟「今天没信号」长得一模一样。

    显式指定的方式，二选一：

        TRANSPORT = MyObjectStorage                 # 类 → 由框架无参构造
        TRANSPORT = MyObjectStorage.from_env        # 零参工厂 → 需要配置时用这个

    第二种是给「构造要参数」的后端留的（先例是内置的 `GistTransport.from_env()`，
    它要读凭据）。工厂返回的东西仍然会被检查是不是 `Transport`。
    """
    explicit = getattr(module, "TRANSPORT", None)
    if explicit is not None:
        return _instantiate(explicit, path)

    candidates = [
        obj for name, obj in vars(module).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and issubclass(obj, Transport)
        and obj is not Transport
        and obj.__module__ == module.__name__
    ]
    if not candidates:
        raise ImportError(
            f"{path}: 没找到 Transport 子类。若你的后端类在辅助模块里，"
            f"请在本文件里加一行 `TRANSPORT = 你的类`")
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise ImportError(
            f"{path}: 找到多个 Transport 子类（{names}），无法判断该用哪个。"
            f"请加一行 `TRANSPORT = 你要用的那个类`")
    return _instantiate(candidates[0], path)


def _instantiate(explicit, path: Path) -> Transport:
    """把 `TRANSPORT = X` 那个 X 变成一个装得进去的对象。"""
    if isinstance(explicit, type) and issubclass(explicit, Transport):
        return explicit()
    if callable(explicit):
        obj = explicit()
        if not isinstance(obj, Transport):
            raise TypeError(
                f"{path}: TRANSPORT 是工厂，但它返回 {type(obj).__name__}，"
                f"不是 Transport 的实例")
        return obj
    raise TypeError(
        f"{path}: TRANSPORT 必须是 Transport 的子类，"
        f"或一个返回 Transport 的零参函数，当前是 {explicit!r}")
