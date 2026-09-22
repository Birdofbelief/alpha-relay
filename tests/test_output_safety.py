# -*- coding: utf-8 -*-
"""输出安全 —— 「别人 clone 下来在中文 Windows 上跑，不许崩、也不许丢日志」。

## 为什么单独有这一条

这个病**在作者的开发机上是看不见的**：那里设了 `PYTHONUTF8=1`，
`print("⛔")` 一切正常。而在别人的简体中文 Windows 上（没有那个变量），
stdout 是 GBK —— `print()` 抛 `UnicodeEncodeError`，进程当场退出、退出码 1。
README 让人敲的第一条命令就是那个演示。**「在我机器上是好的」在这里不是掉以轻心，
是环境本身就不同。**

而两类输出的失效方式又不一样：

    print()    → 抛异常 → 进程死
    logging    → Handler.handleError 吞掉异常 → 进程照跑，但**那一行日志永久丢失**，
                 再把一段 `--- Logging error ---` + traceback 噪音打到 stderr

第二种更坏：它不中断，只在回头对账时才发现少了几行。

## 三层断言，缺一层就有盲区

1. **静态**（AST）：包内不得出现裸 print / 直写 stdout —— 唯一出口是 `console.emit`。
2. **静态**（AST）：非 docstring 的字符串常量必须 GBK 可编。这一条把
   「日志图标只用 GBK 里有的符号」从约定变成护栏 —— 否则今天修好，明天
   有人加个 `log.info("🎉 完成")` 又回去了。
3. **动态**（子进程）：真的把控制台编码按成 GBK / cp1252 跑一遍，
   断言**退出码 0 + 输出非空 + 不含 `UnicodeEncodeError` / `Logging error`**。

★ 为什么第 1、2 条必须按**名字枚举**而不能抽样：抽样能证明「我读过的这几处是干净的」，
  证明不了「没有第 46 处」。而「计数类/全称类断言只能来自按名字枚举」是本仓库
  已经踩过的坑（见 docs/DESIGN_NOTES.md 里「探针范围 vs 断言范围」那条）。

★ 第 3 条不能省，因为前两条都只是**语法层**的证据：它们看不见
  `emit` 自己是不是真的不抛、handler 到底抓住了哪个流。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from signalchain.console import emit

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "signalchain"

# 唯一允许碰输出流的模块（它自己就是那个出口）。
EMITTER = PACKAGE_ROOT / "console.py"

# 控制台编码的两端：简体中文 Windows（GBK）与 CI 的 windows-latest（cp1252）。
# 后者连中文都编不出来 —— 所以「GBK 能编」并不蕴含「cp1252 能编」，
# 中文提示语在 cp1252 下一定会退化成 "?"，这是**知道的取舍**：
# 真正的失败条件是「崩掉」和「丢日志」，不是「非中文控制台上中文变问号」。
CONSOLE_ENCODINGS = ["gbk", "cp1252"]


def _package_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _docstring_ids(tree: ast.AST) -> set[int]:
    """标出属于 docstring 的 Constant 节点。

    ★ 排除 docstring 不是偷懒：docstring 永远不会被写进任何输出流，
      把它算进来就是**探针范围大于断言范围** —— 会得出一堆假阳性，
      然后逼着人去改注释，改完一行代码也没修好。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _bare_output_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """列出绕开 emit 的输出调用：(行号, 写法)。

    按名字枚举三个逃生口，不是抽样：
      print(...)                 —— 最常见的那个
      builtins.print(...)        —— 换个名字而已，同一个坑
      sys.stdout.write(...)      —— 更底层，编码错一样抛
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "print":
            found.append((node.lineno, 'print(...)'))
            continue
        if isinstance(fn, ast.Attribute) and fn.attr == "print":
            found.append((node.lineno, f"{ast.unparse(fn)}(...)"))
            continue
        if isinstance(fn, ast.Attribute) and fn.attr == "write" \
                and isinstance(fn.value, ast.Attribute) \
                and fn.value.attr in ("stdout", "stderr"):
            found.append((node.lineno, f"{ast.unparse(fn)}(...)"))
    return found


def test_package_never_writes_to_a_stream_directly():
    """包内唯一的输出出口是 `console.emit`。

    这不是风格洁癖：`emit` 是那个「永不因编码抛异常」的地方。绕过它 = 绕过它。
    """
    offenders: list[str] = []
    for path in _package_files():
        if path == EMITTER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, shape in _bare_output_calls(tree):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}  {shape}")
    assert not offenders, (
        "包内出现了绕开 console.emit 的输出调用。\n"
        "改成 `from .console import emit` 再用 `emit(...)` —— "
        "裸 print 在中文 Windows 上（GBK 控制台）会抛 UnicodeEncodeError，\n"
        "而 logging 会把这行日志静默吞掉。\n  " + "\n  ".join(offenders)
    )


def test_user_visible_strings_are_gbk_encodable():
    """非 docstring 的字符串常量必须能被 GBK 编码。

    这条把「日志图标只用 GBK 里有的符号」固化成护栏：
    否则今天修好明天还能加回去，而**加了之后在本机是看不出来的**。

    用 GBK 当判据而不是 cp1252：主要受众是简体中文 Windows，
    而 cp1252 下连中文都编不出来，那种情况下变问号是可接受的降级（见模块头）。
    """
    offenders: list[str] = []
    for path in _package_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docs:
                continue
            bad = []
            for ch in node.value:
                try:
                    ch.encode("gbk")
                except UnicodeEncodeError:
                    bad.append(ch)
            if bad:
                uniq = "".join(sorted(set(bad)))
                detail = ", ".join(f"U+{ord(c):04X} {c}" for c in sorted(set(bad)))
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}  "
                    f"含 {uniq}  ({detail})")
    assert not offenders, (
        "这些字符串会被写进输出流，但 GBK 里没有它们 —— 中文 Windows 用户会看到\n"
        "`--- Logging error ---` 噪音并且**丢掉那一行**。换成 GBK 里有的符号\n"
        "（√ × ! ◆ ◇ ○ ● △ ▲ ▼ →）。\n  " + "\n  ".join(offenders)
    )


def test_emit_survives_a_stream_that_rejects_the_text():
    """`emit` 的承诺是「永不抛异常」—— 拿一个 GBK 专属的流当场验一遍。

    这是**构造出来的**最坏情形：流掰不动（不是 TextIOWrapper）、又编不出那些字符。
    两害相权：把一个字符打成 `?` << 让调用方的进程死掉。
    """
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="gbk", newline="")
    try:
        emit("badge: ⛔ done ✅", file=stream)   # ⛔ / ✅ —— GBK 里没有
        stream.flush()
    finally:
        stream.detach()
    written = buf.getvalue()
    assert written, "一个字符都没写出去 —— 那不是「降级」，那是「静默丢输出」"
    assert b"?" in written, (
        "预期编不出的字符退化成 `?`（errors='replace'）。"
        f"实际写出去的是 {written!r}")


def _run_in_a_console(args: list[str], encoding: str, timeout: int = 300):
    """起一个子进程，把它的控制台编码按成指定值。

    ★ 必须**显式摘掉** `PYTHONUTF8` / `PYTHONIOENCODING` 再由我们设回 ——
      本机开发环境里前者是设着的（`settings.json` 的 env 段），
      不摘掉的话子进程照样是 UTF-8 模式，这条测试就成了摆设：
      **它测的是一个别人机器上不存在的环境。**
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONIOENCODING"] = encoding
    # ★ 启动器＝**跑测试的这个解释器**，不挑。理由与 test_end_to_end.py 里那条同源：
    #   启动器不是被测对象，而 `sys.executable` 是唯一被证明装好了依赖的那个。
    #   曾经这里写 `shutil.which("py") or sys.executable` —— 2026-09-22 CI 的
    #   windows runner 上 `py` 存在却指向另一套没装 requests/pandas 的 Python，
    #   于是整批测试红在「缺少运行时依赖：pandas」上：那是**机器属性**，不是代码。
    launcher = sys.executable
    return subprocess.run([launcher, *args], capture_output=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(REPO_ROOT), env=env, timeout=timeout)


def _assert_clean_run(proc, what: str, encoding: str,
                      require_stdout: bool = True) -> None:
    both = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"{what} 在控制台编码 {encoding} 下退出码 {proc.returncode}。\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    assert (proc.stdout.strip() if require_stdout else both.strip()), (
        f"{what} 在 {encoding} 下零输出 —— 和「通过」无法区分。")
    assert "UnicodeEncodeError" not in both, (
        f"{what} 在 {encoding} 下抛了 UnicodeEncodeError：\n{both}")
    assert "Logging error" not in both, (
        f"{what} 在 {encoding} 下丢了日志行（logging 吞掉异常并打了噪音）。\n"
        f"这比崩掉更坏：进程照跑，只有回头对账时才发现少了几行。\n{both}")


@pytest.mark.parametrize("encoding", CONSOLE_ENCODINGS)
@pytest.mark.parametrize("quiet", [False, True], ids=["带日志", "quiet"])
def test_documented_command_survives_a_non_utf8_console(encoding, quiet):
    """README 让人敲的第一条命令，在别人的控制台上必须活下来。

    两种模式都跑，因为它们覆盖**不同的代码路径**：

    - `带日志`：`setup_logging()` + 一路 logging 输出（`emit` 与 handler 都在场）。
    - `quiet`：`logging.disable()`，只剩 `emit`。

    只测其中一种，另一种的失效就没人管 —— 这正是上一轮的教训：
    当时那条入口测试用的是 `run_demo(quiet=True)`，把打印关掉了，
    于是**护栏把失败条件一起关掉了**，崩了整轮也没红。
    """
    args = ["-m", "signalchain.demo"] + (["--quiet"] if quiet else [])
    proc = _run_in_a_console(args, encoding)
    _assert_clean_run(proc, "python -m signalchain.demo", encoding)


@pytest.mark.parametrize("encoding", CONSOLE_ENCODINGS)
def test_library_path_does_not_need_setup_logging_to_be_callable(encoding):
    """调用方**忘了**调 `setup_logging()` 时，库自己也不许崩。

    这一条测的是另一种用法：不是敲命令，而是把本包当库 import 进自己的脚本。
    那种脚本的作者不会读我们的 README —— 所以 `ConsoleNotifier` 这类
    「库代码里直接写输出」的地方必须自带兜底。

    代码串刻意写成纯 ASCII（emoji 用 `chr()` 现造）：命令行参数里的非 ASCII
    会经过一层系统代码页转换（见 docs 里「参数编码」那条），
    纯 ASCII 就从源头绕开了那个变量 —— **这条测试要测的是输出编码，不是参数编码。**
    """
    # 用 chr() 而不是写转义序列：这段代码要经**命令行参数**递给孩子进程，
    # 而参数里的非 ASCII 会过一层系统代码页转换。用 chr() 让源码串保持纯 ASCII，
    # 从源头绕开那个变量 —— 这条测试要测的是**输出**编码，不是参数编码。
    snippet = "\n".join([
        "from signalchain import ConsoleNotifier",
        "ConsoleNotifier().send(chr(0x26d4) + ' test', "
        "chr(0x6b63) + chr(0x6587) + chr(0x2705))",
        "",
    ])
    proc = _run_in_a_console(["-c", snippet], encoding)
    _assert_clean_run(proc, "ConsoleNotifier 当库用", encoding)


@pytest.mark.parametrize("encoding", CONSOLE_ENCODINGS)
def test_redirected_output_is_utf8_not_the_console_codepage(encoding):
    """重定向到文件/管道时，输出必须是 **UTF-8**，不是控制台代码页。

    这条是 `force_utf8()` 在入口存在的**唯一**硬理由（崩溃那部分已经由
    `emit` 兜住了）。为什么值得单列：日志重定向到文件是常规用法，
    而 GBK 字节的日志文件在别人的编辑器里是乱码、在别的工具里直接丢字符。
    ★ 判据用**严格 UTF-8 解码**：解不开就红。这一条对
      「入口忘了掰流」是敏感的 —— 见 `tools/mutcheck_output.py`。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONIOENCODING"] = encoding
    proc = subprocess.run([sys.executable,
                           "-m", "signalchain.demo", "--quiet"],
                          capture_output=True, cwd=str(REPO_ROOT), env=env, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    text = proc.stdout.decode("utf-8")      # 严格：GBK 字节在这里就红
    assert "闭环完成" in text, (
        "重定向出去的字节不是 UTF-8（多半是控制台代码页）。\n"
        f"前 200 字节：{proc.stdout[:200]!r}")


@pytest.mark.parametrize("encoding", CONSOLE_ENCODINGS)
def test_setup_logging_fixes_handlers_the_caller_already_installed(encoding):
    """调用方**先**自己装好了 handler，`setup_logging()` 也得管住它。

    这是 `setup_logging()` 里「遍历已有 handler」那一段的存在理由 ——
    那种 handler 可能抓着一个我们根本不知道的流（比如一个没给 encoding 的
    `FileHandler`，在 Windows 上默认走系统代码页 GBK），而
    `force_utf8(sys.stdout, sys.stderr)` **碰不到它**。

    判据是「那一行真的落进文件且能按 UTF-8 读回来」，
    不是「handler.stream.encoding 等于 utf-8」——后者是在断言实现。
    """
    snippet = "\n".join([
        "import logging, os, tempfile",
        "from signalchain import setup_logging",
        "path = os.path.join(tempfile.mkdtemp(), 'a.log')",
        "handler = logging.FileHandler(path)   # 刻意不给 encoding",
        "logging.getLogger().addHandler(handler)",
        "setup_logging()",
        "logging.getLogger('signalchain').info('对账 %s', chr(0x2705))",
        "handler.flush()",
        "import sys; sys.stdout.write(open(path, 'rb').read().decode('utf-8'))",
        "",
    ])
    proc = _run_in_a_console(["-c", snippet], encoding)
    _assert_clean_run(proc, "调用方自装 FileHandler", encoding)
    assert chr(0x2705) in (proc.stdout or ""), (
        "那一行没按 UTF-8 落进调用方的日志文件 —— "
        "多半是 setup_logging() 没有去管已有的 handler。\n"
        f"文件内容：{(proc.stdout or '')!r}")


@pytest.mark.parametrize("encoding", CONSOLE_ENCODINGS)
def test_logging_path_survives_an_emoji_that_the_programmer_did_not_write(encoding):
    """日志里的**动态内容**（文件名、策略名、拒绝理由）可能带 emoji，也不许丢行。

    静态那两条看不见这件事：它们只知道「源码里的字符串 GBK 编得出」，
    而 `log.info("对账 %s", name)` 里真正写出去的是 `name` ——
    **程序员没写过的那个字符**。`logging` 遇到编不出的字符不抛异常，
    只是丢掉那一行、再往 stderr 打一段 `--- Logging error ---` 噪音。
    代价是「进程照跑、日志少一行」，只有回头对账时才发现。

    ★ 断言直接要求那一行**完整到达**（含 emoji），不是「没报错就行」——
      「没输出」也满足「没报错」。

    ★ 一条被实测推翻的说法（2026-09-21，见 `tools/mutcheck_output.py` 的 M4b）：
      这里原先写的是「`StreamHandler` 构造时就把 `sys.stderr` 抓走了，
      所以先 `basicConfig` 再掰流是白忙」。**不成立** —— `reconfigure()`
      改的是**同一个** `sys.stderr` 对象，两种顺序等价。所以这条测试
      盯的是**结果**（那一行到没到），与 `setup_logging()` 内部的顺序无关。
    """
    snippet = "\n".join([
        "import logging",
        "from signalchain import setup_logging",
        "setup_logging()",
        "logging.getLogger('signalchain').info("
        "'持仓对账成功 %s', chr(0x2705))",
        "",
    ])
    proc = _run_in_a_console(["-c", snippet], encoding)
    _assert_clean_run(proc, "setup_logging + 带 emoji 的日志", encoding,
                      require_stdout=False)
    assert chr(0x2705) in (proc.stderr or ""), (
        "那一行日志没完整到达 —— 说明 handler 手里的流不是 UTF-8。\n"
        f"stderr 实际是：{(proc.stderr or '')!r}")
