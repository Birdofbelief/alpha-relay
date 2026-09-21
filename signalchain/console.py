# -*- coding: utf-8 -*-
"""输出出口 —— 本包**唯一**允许写 stdout / stderr 的地方。

## 这个模块在防什么

Windows 上 Python 的 stdout 编码是**控制台代码页**：简体中文机器上是 GBK（cp936），
CI 的 windows-latest 上是 cp1252 —— 除非进程里设了 `PYTHONUTF8=1`。

于是同一份代码在不同机器上表现不同：

    print("⛔ 行情失效")        # 开发机（有 PYTHONUTF8=1）：正常
                                # 别人的中文 Windows（GBK）：UnicodeEncodeError，进程退出码 1

**而作者看不见这个错**，因为他自己的机器设了那个变量。这就是「在我机器上是好的」
最典型的一种：被测的东西和上线的东西不是同一个环境。

## 三条纪律

1. **包内不出现裸 `print`。** 所有输出走 `emit()` —— 它是「永不抛异常」的那个出口。
   这条由 `tests/test_output_safety.py` 用 AST 逐个名字枚举来盯着，不靠自觉。

2. **用户可见的字符串不出现 GBK 编不出的字符。** 日志与提示里的图标一律用
   GBK 里本来就有的符号（`√ × ! ◆ ◇ ○ ● △ ▲ ▼ →`）。
   这样**中文 Windows 用户零配置**就能看到一行不丢的日志 ——
   而不是每条带图标的日志都被 Python 换成一段 `--- Logging error ---` 噪音。
   （这条同样有测试盯着：非 docstring 的字符串常量必须 GBK 可编。）

3. **库不在 import 时劫持宿主的 stdout。** `force_utf8()` 只由**入口**显式调用。
   一个被 import 的库擅自改掉宿主的流编码，是比原问题更坏的毛病。

## 为什么 `setup_logging()` 还要管「你已经装好的 handler」

`force_utf8()` 只管 `sys.stdout` / `sys.stderr` 这两个**当前**的流。
而你自己装的 handler 可能抓着一个我们根本不知道的流 ——
一个没给 `encoding` 的 `FileHandler` 在 Windows 上默认走系统代码页（GBK），
写日志时碰上编不出的字符就**丢掉那一行**。所以本函数会遍历已有 handler，
把它们手里的流也掰成 UTF-8。

★ 一条被实测推翻的说法（2026-09-21）。我原先写的是「handler 在构造时就把
  `sys.stderr` 抓走了，所以先 basicConfig 再 force_utf8 是白忙」。
  **实测不成立**：`reconfigure()` 改的是**同一个对象**，handler 抓的那个引用
  没有换 —— 两种顺序对 stdout/stderr 等价。
  `tools/mutcheck_output.py` 的 M4b 刻意留着这条「预期为绿」的变异来守住结论：
  哪天它变红了，说明 Python 的 stdio 行为变了，这段解释得重写。
  真正需要遍历 handler 的是**上一条理由**（handler 手里可能是另一个流），与顺序无关。
  本函数仍然把顺序固定下来，只是省得调用方每次都要想这件事。
"""
from __future__ import annotations

import logging
import sys
from typing import TextIO

__all__ = ["emit", "force_utf8", "setup_logging"]

DEFAULT_LOG_FORMAT = "%(levelname)-7s %(message)s"


def force_utf8(*streams: TextIO) -> None:
    """把流掰成 UTF-8（`errors="replace"` 兜底）。不传就用 stdout + stderr。

    真控制台上 Python 走 `WriteConsoleW` 渲染，与代码页无关 —— 所以这一步
    不会让中文变乱码，只是让「写出去的那串字节」在重定向到文件时是 UTF-8。

    `errors="replace"` 是**故意的兜底**：宁可把一个字符打成 `?`，
    也绝不因为「打印不出来」而让调用方的进程崩掉。
    """
    for stream in (streams or (sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            # 不是 TextIOWrapper（被别的工具包过 / 已关闭）—— 掰不动就算了，
            # 后面 emit() 还有一层兜底。
            pass


def emit(*args: object, sep: str = " ", end: str = "\n",
         file: TextIO | None = None, flush: bool = False) -> None:
    """`print()` 的安全版：**永不因编码抛异常**。

    先按正常路径写；只有当流因为编码拒收时，才退化成「编不出的字符打成 `?`」。
    两害相权：丢一个字符 << 让调用方的进程死掉。
    """
    stream = file if file is not None else sys.stdout
    text = sep.join(str(a) for a in args) + end
    try:
        stream.write(text)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(enc, "replace").decode(enc, "replace"))
    except ValueError:
        # 流已关闭（比如解释器正在退出）—— 输出丢了就丢了，别在这里抛。
        return
    if flush:
        stream.flush()


def setup_logging(level: int = logging.INFO,
                  fmt: str = DEFAULT_LOG_FORMAT) -> None:
    """入口该调的那一行：**掰流 + 配日志**，合并成一次调用。

    用法（你自己脚本的第一行）::

        from signalchain import setup_logging
        setup_logging()

    两种情形都覆盖：

    - 根 logger 还没有 handler → 按本函数给的方式配置一个（写到 stderr）。
    - 你已经自己装过 handler → **不重复添加**，改为把那些 handler 手里的流
      也掰成 UTF-8 —— 它们可能是我们不知道的流（比如一个没给 encoding 的
      文件 handler）。
    """
    force_utf8()
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
        return
    for handler in root.handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None:
            force_utf8(stream)
    root.setLevel(level)
