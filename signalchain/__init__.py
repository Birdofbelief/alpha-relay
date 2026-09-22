# -*- coding: utf-8 -*-
"""alpha-relay —— 策略与信号链路解耦的可插拔框架。

## 它在解决什么

大多数「量化脚本」长成同一个样子：一个几千行的单文件，把三件毫不相干的事
焊在一起 —— 策略逻辑、数据管道、投递与风控。后果是：
改一个阈值要读完整份代码；换个投递方式要动策略；而风控散落在各处，
谁也不知道一共有几道。

本框架把这条链切成分层的几块，每块只有一个职责，且**都能单独替换**：

    Strategy     ← 你的 alpha。只实现 decide(ctx)，不碰任何 I/O
    MarketData   ← 行情。可换成假数据、加缓存、加重试
    Transport    ← 投递。内置 文件 / Gist / 空实现；你自己的后端放仓库外，
                   让 SC_TRANSPORT 指过去（或 build_chain(transport=...)）即可
    Executor     ← 执行端。把信号变成委托 —— 本仓库只带一个纸面占位
    Gate         ← 风控。fail-closed，且与策略无关
    Runner       ← 调度。时间预算、失败上限、心跳

## 三条贯穿全库的主张

1. **策略是纯函数。** `decide(ctx)` 不发请求、不读文件、不写状态。
   它只从 `ctx` 拿数据，只返回 `Signal`。链路负责其余一切。
   好处不只是整洁 —— 它让策略**可以离线回放**，这是能验证策略的前提。

2. **失效方向必须朝「不做」。** 数据不全时默认动作是「停」，不是「照做」。
   具体每个闸门停哪一半，见 `gates.py` —— 三个闸门的方向**故意不一样**，
   判断依据是「哪边错了更疼」而不是「哪边更保守」。

3. **单一写入者不是风格偏好，是当前唯一能消除竞态的手段。**
   凡是靠 read-modify-write 实现「往列表里追加」的后端都不原子 ——
   内置的 `gist` 后端正是这样一个例子。两个写者同时改同一份文档，
   后写的会**静默**覆盖先写的，而两边日志都显示成功。
   所以 channel 的所有权写在代码里（`transport/base.py` 的 `CHANNEL_OWNERS`），
   写错直接抛异常，而不是打个警告。

## 最快上手

想先看它跑起来：`python -m signalchain.demo`
（零凭据、零网络、零券商；跑完把四个 channel 的真实字节都打出来）

作为库用：

    from signalchain import build_file_chain, setup_logging

    setup_logging()                         # 入口第一行：掰 UTF-8 + 配日志
    chain = build_file_chain("./runtime")   # 文件后端 + 假行情 + 纸面执行器
    result, fills = chain.step()            # 执行端先报持仓 → 决策 → 闸门 → 投递 → 回执
    print(result.summary())

★ `setup_logging()` 该**由你的入口调用**，库本身不会在 import 时动你的流。
  不调也能跑（`emit()` 永不抛异常），代价是重定向出去的日志不是 UTF-8、
  而 `logging` 遇到编不出的字符会**静默丢掉那一行**并打一段噪音。
  理由见 `signalchain/console.py`。

换成你自己的策略 —— **策略文件放仓库外，永不入库**：

    chain = build_file_chain("./runtime", strategy="./my_private.py")

★ 上面的模块名与函数名由 `tests/test_docs_tripwire.py` 逐个 import 验证。
文档里写一个不存在的 API，代价不是「读者看不懂」，而是**读者以为框架坏了**。
"""
from __future__ import annotations

__version__ = "0.1.0"

# ------------------------------------------------------------
# ★ 依赖缺失时先说人话
# ------------------------------------------------------------
# 这是别人 clone 下来会撞上的**第一种**失败：照 README 敲第一条命令，拿到的是一条
# 十几行的 traceback，最后一行 `ModuleNotFoundError: No module named 'pandas'`。
# 它说了「缺什么」，但没说「怎么办」—— 而对刚上手的人，Traceback 本身就是劝退。
#
# 实测（2026-09-22，全新 venv、零第三方包、从仓库根跑 `python -m signalchain.demo`）：
#     退出码 1，输出 14 行，末行就是上面那句。
#
# ⇒ 换成一条带**可直接粘贴的安装命令**的报错。
#   两个包都在这儿挡，因为它们在**导入期**就是硬依赖：`marketdata/base.py` 与
#   `marketdata/online.py` 都是模块级 import。已用 meta_path 探针逐个实测 ——
#   假装没有 requests 也一样在 import 时就炸，不是「只有用到网络才需要」。
#
# ★ 只认这两个名字，别的一律原样抛出 —— 把真正的 bug 也包装成「去装依赖」会把人带沟里。
#
# ★ 刻意抛 `ImportError` 而不是 `print + SystemExit`：库在 import 期自杀会让调用方
#   **抓不住**（`try: import signalchain / except ImportError` 拦不到 SystemExit），
#   而且会在别人的进程里直接掐死进程。`from None` 把原来那条 traceback 的上下文去掉，
#   输出就只剩「这条消息 + 几行栈」，不再是十几行。
# ★ 消息只用 **GBK 里有的符号**（中文标点都在，避开 `⇒`/`✓` 这类）：这条报错是在
#   `setup_logging()` 之前发生的，此时 stderr 在重定向场景下就是 cp936 ——
#   用了 GBK 没有的字符，读者看到的会是转义序列。同一条纪律见 console.py。
try:
    from importlib import import_module as _import_module

    _import_module("pandas")
    _import_module("requests")
    del _import_module
except ModuleNotFoundError as _exc:
    _missing = (_exc.name or "").split(".")[0]
    if _missing not in ("pandas", "requests"):
        raise
    raise ImportError(
        f"alpha-relay 缺少运行时依赖：{_missing}\n"
        "\n"
        "装一次就行（运行时只需要这两个包）：\n"
        "\n"
        "    python -m pip install requests pandas\n"
        "\n"
        "Windows 上如果 `python` 打不开、或把你送进微软商店，\n"
        "就把上面的 python 换成 py。\n"
        "（这条报错是刻意换掉的：原本这里是一条 ModuleNotFoundError traceback，\n"
        "  只说缺什么、没说怎么办。见本文件顶部。安装说明在 README 的「快速上手」。）"
    ) from None

from .calendar import TradingCalendar, make_static_calendar, weekday_calendar
from .chain import Chain, build_chain, build_file_chain
from .config import Config
from .console import emit, force_utf8, setup_logging
from .executor import Executor
from .gates import (
    AuxDataGate,
    DataQuality,
    Gate,
    GateChain,
    PositionsSyncedGate,
    QuoteIntegrityGate,
    Veto,
)
from .models import (
    ACTION_BUY,
    ACTION_SELL,
    Execution,
    Heartbeat,
    PositionSnapshot,
    Signal,
    to_bare,
    to_jq,
)
from .notify import (
    ConsoleNotifier,
    MultiNotifier,
    Notifier,
    NotifyThrottle,
    NullNotifier,
)
from .store import Stores
from .strategy import (
    AuxResult,
    AuxSource,
    Strategy,
    StrategyContext,
    StrategyState,
    load_strategy,
)
from .transport import (
    CHANNEL_OWNERS,
    FileTransport,
    NullTransport,
    Role,
    SingleWriterGuard,
    Transport,
    TransportError,
)

__all__ = [
    "__version__",
    # 模型
    "Signal", "Execution", "PositionSnapshot", "Heartbeat",
    "to_jq", "to_bare", "ACTION_BUY", "ACTION_SELL",
    # 装配 —— `Config` 与 `Strategy` 是同一回事：README 里写着
    # `from signalchain import Config`，而读者照着敲会得到 ImportError。
    # 换投递后端（`SC_TRANSPORT` 指到仓库外的 .py）那条路必须先造一个 Config，
    # 所以它和策略一样属于**扩展点**，必须能从包根拿到。
    "Config", "Chain", "build_chain", "build_file_chain",
    # 执行端契约 —— 接自己的券商执行端时要 `from signalchain import Executor`
    "Executor",
    # 策略侧 —— 这是框架的**主要扩展点**，必须能从包根拿到。
    # （它曾经没被导出：README 写 `from signalchain import Strategy`，
    #   而读者照着敲会得到 ImportError。tests/test_docs_tripwire.py 抓到的。）
    "Strategy", "StrategyContext", "StrategyState", "load_strategy",
    "AuxSource", "AuxResult",
    # 日历
    "TradingCalendar", "make_static_calendar", "weekday_calendar",
    # 闸门
    "Gate", "GateChain", "Veto", "DataQuality",
    "QuoteIntegrityGate", "PositionsSyncedGate", "AuxDataGate",
    # 传输
    "Transport", "TransportError", "Role", "CHANNEL_OWNERS",
    "SingleWriterGuard", "FileTransport", "NullTransport",
    # 其余
    "Stores", "Notifier", "NullNotifier", "ConsoleNotifier",
    "MultiNotifier", "NotifyThrottle",
    # 输出 —— 入口第一行该调的那个
    "setup_logging", "force_utf8", "emit",
]
