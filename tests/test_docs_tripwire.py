# -*- coding: utf-8 -*-
"""文档里承诺的 API，必须真的存在。

## 为什么值得单独一组

README 和 docstring 里的示例代码是**读者唯一会照着敲的东西**。它们写错的方式
只有一种，而且完全静默：**没有人执行它们**。

代价比「读者看不懂」大得多。读者照着敲，拿到 `ImportError`，第一反应不是
「文档过时了」，而是「这框架是坏的」—— 而这是最贵的一种误解：它发生在
**第一次接触**，此后再没有第二次机会。

本仓库真的踩过：`signalchain/__init__.py` 的最快上手写的是

    from signalchain import build_file_chain, DemoStrategy
    chain = build_file_chain("./runtime")
    chain.run_once()

三个名字**全是坏的** —— `DemoStrategy` 的真名是 `DemoBasicStrategy`；
`build_file_chain` 当时没被导出；`Chain` 上根本没有 `run_once()` 这个方法。
三处都得靠执行才发现，而没有任何测试在执行它。

## 这一组做两件事

1. **逐个 import** 文档里 `from X import a, b` 形式的每一行 —— 这是机械的，
   不需要维护一份「文档里提过的名字」清单（那种清单自己就会过期）。
2. **断言快速上手的那几行真的能跑** —— 真的装一条链、真的跑一轮。
   `import` 过了不等于会用：`build_file_chain(...)` 的**签名**也可能变。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import pkgutil
import re
import subprocess
import sys

import pytest

import signalchain
from signalchain import ACTION_BUY, Strategy

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _import_lines(text: str) -> list[str]:
    """从任意文本里挑出 `from X import ...` 语句（去掉缩进，多行括号拼成一行）。

    只认 `from ... import ...`：`import x` 不带名字，没有「名字写错」的余地。

    ★ 括号续行必须拼回来，并把括号本身去掉。文档里合法地写着

        from signalchain.models import (
            EXEC_CANCELED, Execution,
        )

    只看第一行会得到 `from ... import (`，把 `(` 当成一个要 import 的名字，
    于是**报一个假的红** —— 而示例代码因为行宽限制换行，不是文档的错。
    （2026-09-22 实测：直接放宽检查面时，`docs/EXECUTOR_GUIDE.md` 这一行
    就是这么被误报的。是先量后断言救回来的。）
    """
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not (line.startswith("from ") and " import " in line):
            continue
        # 把括号续行拼到配平为止
        while line.count("(") > line.count(")") and i < len(lines):
            line += " " + lines[i].strip()
            i += 1
        line = " ".join(line.replace("(", " ").replace(")", " ").split())
        out.append(line)
    return out


def _resolve_import_lines(text: str, where: str) -> list[str]:
    """`text` 里解析不了的 `from X import a, b`，每行一条说明；全好则空列表。

    ★ 收集**全部**失败再一次性报出来，不在第一条停下 —— 一次修完，
    比「跑一次、修一行、再跑一次」少几个来回。
    """
    failures: list[str] = []
    for line in _import_lines(text):
        module, symbols = _named_modules_and_symbols(line)
        try:
            mod = __import__(module, fromlist=symbols)
        except ImportError as exc:
            failures.append(f"{where}: `{line}` —— 模块 `{module}` 导不进来：{exc}")
            continue
        missing = [n for n in symbols if not hasattr(mod, n)]
        if missing:
            failures.append(f"{where}: `{line}` —— `{module}` 里没有 `{missing}`")
    return failures


def _named_modules_and_symbols(line: str) -> tuple[str, list[str]]:
    """`from a.b import c, d` → `("a.b", ["c", "d"])`。"""
    module, _, names = line.partition(" import ")
    module = module[len("from "):].strip()
    symbols = [n.strip().split(" as ")[0].strip()
               for n in names.split(",") if n.strip()]
    return module, symbols


# ------------------------------------------------------------
# 包的 docstring
# ------------------------------------------------------------
def test_package_docstring_import_lines_all_resolve():
    """`signalchain/__init__.py` 的 docstring 里每一行 `from ... import` 都要能解析。

    ★ 这条是那个「三个名字全是坏的」缺陷的回归测试。
    """
    text = signalchain.__doc__ or ""
    assert _import_lines(text), "包 docstring 里一行 import 示例都没有 —— 这条测试就白跑了"

    failures = _resolve_import_lines(text, "signalchain/__init__.py 的 docstring")
    assert not failures, (
        "包 docstring 里的 import 行，读者抄下来会失败：\n" +
        "\n".join(f"  {f}" for f in failures))


def test_public_all_matches_reality():
    """`__all__` 里列的每个名字都必须真的存在于模块上。

    反过来（模块上有名字但没进 `__all__`）不作断言 —— 那是风格选择。
    但 `__all__` 里有一个不存在的名字，`from signalchain import *` 就会炸。
    """
    for name in signalchain.__all__:
        assert hasattr(signalchain, name), f"__all__ 里的 `{name}` 不存在"


def test_exported_names_are_sorted_within_their_groups():
    """`__all__` 的分组与组内顺序要稳定 —— 免得它退化成随手追加。

    不作强断言（不检查具体分组），只要求：无重复。
    重复项说明有人在两处分别加过，而 `import *` 时会静默取后者。
    （反引号包住，免得这里的星号被当成转义序列 —— 这条注释本身刚因此报过
    一个 DeprecationWarning，而它是那种「今天只是警告、将来是错误」的东西。）
    """
    names = list(signalchain.__all__)
    assert len(names) == len(set(names)), \
        f"__all__ 有重复项：{[n for n in names if names.count(n) > 1]}"


# ------------------------------------------------------------
# ★ 快速上手必须真的能跑
# ------------------------------------------------------------
def test_the_quickstart_actually_runs(tmp_path):
    """★ 照文档写的那三行，真的装一条链、真的跑一轮。

    `import` 能过不等于会用：签名（位置参数 / 关键字名 / 返回值形状）都可能变，
    而那三样在 import 层面全都看不出来。

    在 `tmp_path` 里跑，不碰仓库目录 —— 传 `./runtime` 会往当前工作目录写东西，
    而测试的当前目录是仓库根。
    """
    from signalchain import build_file_chain

    chain = build_file_chain(tmp_path / "runtime", console=False)
    result, fills = chain.step()          # 文档写的就是这两个返回值

    assert result.ok, result.error
    # 四个 channel 都该有东西 —— 否则「跑通了」只是「没报错」
    for channel in ("signals", "heartbeat", "positions", "executions"):
        assert chain.guarded_transport.read(channel) is not None, \
            f"跑完一轮后 {channel} 还是空的 —— 文档说的「一轮走完闭环」不成立"


def test_the_quickstart_strategy_argument_is_the_documented_keyword(tmp_path):
    """文档里 `build_file_chain(..., strategy="...")` 那个关键字名得是对的。

    文档正文提了一句「换成你自己的策略」，读者会照着写 `strategy=`。
    关键字名改了的后果不是报错 —— 是 `TypeError: unexpected keyword argument`，
    而读者会去怀疑自己的策略文件。
    """
    from signalchain.strategies.demo_basic import DemoBasicStrategy

    from signalchain import build_file_chain

    chain = build_file_chain(tmp_path / "runtime", strategy=DemoBasicStrategy(),
                             console=False)
    assert chain.step()[0].ok


def test_documented_run_command_module_exists():
    """文档说「想先看它跑：`python -m signalchain.demo`」—— 那个模块得真的在。"""
    import importlib

    mod = importlib.import_module("signalchain.demo")
    assert callable(getattr(mod, "run_demo", None)), \
        "文档里的 `python -m signalchain.demo` 需要模块级 run_demo()"


# ------------------------------------------------------------
# ★ README 里那段示例策略
# ------------------------------------------------------------
class _ReadmeExample(Strategy):
    """README「换成你自己的策略」一节里那段代码，逐字搬进来 —— 并真的跑。

    ★ 为什么值得单独搬一份：示例代码是**最容易被复制走、也最容易腐烂**的
    一类文本。它没有调用点，因此没有任何测试会碰到它；而它一旦写错，
    受害的正是「照着你写的抄」的那批人（也就是所有人）。

    下面这段与 README 里的差异仅在于：这里把生成器写成了 `yield`（README 里
    也是 `yield`）。改动 README 时必须同步改这里，否则这条测试就失去意义 ——
    而它失去意义的方式是**继续变绿**。
    """

    name = "readme_example"

    def universe(self):
        return ["510050", "159919"]

    def warmup_bars(self):
        return 60

    def decide(self, ctx):
        if not ctx.data.quote_ok:
            return []
        for code in self.universe():
            if ctx.price(code) is None:
                continue
            hist = ctx.try_history(code, 30)
            if hist is None:
                continue
            closes = hist["close"].astype(float).tolist()
            if closes[-1] > closes[0]:
                yield ctx.signal(code, ACTION_BUY, amount=1000)


def test_the_readme_example_strategy_actually_runs_and_emits(tmp_path):
    """★ README 的示例策略要真的产生信号，不只是「跑起来没报错」。

    只断言 `result.ok` 是不够的：一个 `decide()` 里 `return []` 的策略也会 ok，
    而它会把 README 里「这段能选出标的」的暗示证伪。所以要么断言 `emitted > 0`，
    要么就别在文档里举这个例子。

    用 `drift=0.002` 的假行情（单一上行趋势），所以示例里那个
    「末价 > 首价」的判据应当对两只都成立。
    """
    from signalchain import build_file_chain

    chain = build_file_chain(tmp_path / "runtime", strategy=_ReadmeExample(),
                             console=False)
    result, fills = chain.step()

    assert result.ok, result.error
    assert result.emitted > 0, (
        "README 的示例策略一条信号都没发出来 —— 文档在暗示「这段能选出标的」，"
        f"而实际 {result.summary()}")


# ------------------------------------------------------------
# ★ 文档里提到的「构件名」必须真的存在
# ------------------------------------------------------------
#: 这些后缀能认出「文档在指一个具体的类」。
_COMPONENT_SUFFIXES = (
    "Transport", "Gate", "Strategy", "MarketData",
    "Notifier", "Executor", "Store", "Source",
)

_COMPONENT_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:" + "|".join(_COMPONENT_SUFFIXES) + r"))\b")

#: **刻意虚构**的名字：只在示例代码/伪代码里出现，不该存在。
#: 这份清单要尽量短 —— 每加一条都是在放弃一次检查。
#: 下面那条测试会验证清单里的名字**仍然真的出现在文档里**，
#: 免得它退化成一张「什么都放行」的白名单。
_HYPOTHETICAL = {"MyStrategy", "MyExecutor"}

#: 要检查的文档。代码块的 ASCII 图也算 —— 这正是它被写出来的理由。
_DOC_FILES = ("README.md", "README.en.md", "docs/ARCHITECTURE.md",
              "docs/DESIGN_NOTES.md", "docs/DESIGN_NOTES.en.md",
              "docs/EXECUTOR_GUIDE.md", "docs/WIRE_CONTRACT.md",
              "docs/SANITIZATION.md", "docs/MIGRATION.md")

#: (原文, 译本) —— 下面那条测试要求同一条目里两边提到的构件名**完全一致**。
#:
#: ⚠️ 说清它能挡什么：**只挡「改了标识符集合」**。把两个闸门名字**对调**
#: （名字都对、归属反了）这种**重绑定**它挡不住 —— 集合相等即通过。
#: README 那对尤其如此（两个版本都点名了三个内置传输，所以「哪个是默认后端」
#: 被译反时集合照样相等）。**散文里的事实性描述仍然只有人看得见。**
_TRANSLATION_PAIRS = (("docs/DESIGN_NOTES.md", "docs/DESIGN_NOTES.en.md"),
                      ("README.md", "README.en.md"))


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _reachable_names() -> set[str]:
    """包内**任何位置**可达的名字（包根 + 所有子模块）。

    刻意不用 `hasattr(signalchain, name)`：`PaperExecutor`、`GistTransport`
    这些住在子模块里是合理设计，不该被这条测试逼着全导到包根。
    要抓的是「**哪里都没有**」。
    """
    names = set(dir(signalchain))
    for info in pkgutil.walk_packages(signalchain.__path__, "signalchain."):
        try:
            names |= set(dir(importlib.import_module(info.name)))
        except Exception:            # noqa: BLE001 - 可选依赖缺失不该让这条测试红
            continue
    return names


def _documented_components() -> dict[str, list[str]]:
    """{构件名: [出现它的文档]}。

    ★ 文档**缺了就直接报错**，不静默跳过 —— 否则删掉一份文档就等于
    悄悄关掉这条检查，而终端上什么都不会变。（这个仓库吃过这个亏，
    见 `docs/SANITIZATION.md` 第五节。）
    """
    found: dict[str, list[str]] = {}
    root = _repo_root()
    for rel in _DOC_FILES:
        path = root / rel
        assert path.exists(), (
            f"`{rel}` 不存在 —— 它在 `_DOC_FILES` 里，说明它该被检查。"
            f"真的删了它，就把它从这个列表里一起删掉。")
        for name in set(_COMPONENT_RE.findall(path.read_text(encoding="utf-8"))):
            found.setdefault(name, []).append(rel)
    return found


def test_every_doc_import_line_resolves():
    """★ `_DOC_FILES` 里**每一份**文档的**每一行** `from X import a, b` 都要能解析。

    ★ 为什么值得单独一条：本文件开头的 docstring 从一开始就写着
    「**逐个 import** 文档里 `from X import a, b` 形式的每一行」，而实现
    只把 `signalchain.__doc__` 喂了进去 —— 真正的示例代码住在
    `_DOC_FILES` 那些文件里，一行都没被检查过。

    这个缺口有实际代价：README 的「换成你自己的执行端」一节写着

        from signalchain import Config, build_chain

    而 `Config` 当时**没有**从包根导出（它住在 `signalchain.config`）。
    读者照着敲必得 `ImportError`，第一反应不是「文档过时」而是「这框架是坏的」。
    （同一个坑 `Strategy` 先踩过一次，见 `__all__` 里那条注释。）

    ★ 范围按 `_DOC_FILES` **枚举**，不按「我记得读过哪几处」——
    后者会随记忆漏，而枚举不会。
    """
    root = _repo_root()
    failures: list[str] = []
    checked = 0
    for rel in _DOC_FILES:
        path = root / rel
        assert path.exists(), (
            f"`{rel}` 不存在 —— 它在 `_DOC_FILES` 里，说明它该被检查。"
            f"真的删了它，就把它从这个列表里一起删掉。")
        text = path.read_text(encoding="utf-8")
        checked += len(_import_lines(text))
        failures += _resolve_import_lines(text, rel)

    assert checked, (
        f"`_DOC_FILES` 那 {len(_DOC_FILES)} 个文件里一行 import 都没扫到 —— "
        f"正则或文档列表坏了，这条测试白跑")
    assert not failures, (
        "文档里这些 import 行，读者抄下来会失败：\n" +
        "\n".join(f"  {f}" for f in failures) +
        "\n要么把名字导出到包根，要么改文档。**不要让读者抄到一行必炸的代码。**")


def test_every_component_named_in_the_docs_actually_exists():
    """★ 文档里写成构件的名字，必须真的能在包里找到。

    ★ 这条测试的来历：`docs/ARCHITECTURE.md` 与 README 的架构图里
    都画着一个 `HttpTransport`，而**全仓库搜不到它** —— 它在计划里被写成
    「只留接口与 TODO」，那个接口从来没被写出来。

    为什么原有的绊线抓不到：上面那条只解析 `from X import a, b` 形式的行，
    而这个名字出现在**一张 ASCII 架构图**里。图的杀伤力不比示例代码小 ——
    读者看架构图，是为了知道「这个系统由哪些块组成」；图上多一个不存在的块，
    他会在代码里找，找不到，然后怀疑整个文档。
    """
    reachable = _reachable_names()
    documented = _documented_components()

    assert documented, "一个构件名都没扫到 —— 正则或文档列表坏了，这条测试白跑"

    missing = {n: w for n, w in documented.items()
               if n not in reachable and n not in _HYPOTHETICAL}
    assert not missing, (
        "文档里提到这些构件，但包里哪里都没有：\n" +
        "\n".join(f"  {n}  ← {', '.join(w)}" for n, w in sorted(missing.items())) +
        "\n要么实现它，要么从文档里删掉。**不要让读者去找一个不存在的东西。**")


def test_the_hypothetical_allowlist_has_not_rotted():
    """豁免清单里的名字必须**仍然真的出现在文档里**。

    ★ 一张只增不减的白名单会把上面那条测试慢慢变成摆设：
    文档改过之后，旧名字留在清单里，而新名字又多豁免一个。
    所以这里反向验证 —— 不再被提到的豁免项必须删掉。
    """
    documented = set(_documented_components())
    stale = _HYPOTHETICAL - documented
    assert not stale, (
        f"豁免清单里的 {sorted(stale)} 已经不在任何文档里了 —— "
        f"删掉它们，否则这张清单会退化成一张「什么都放行」的白名单")


# ------------------------------------------------------------
# ★ 译文不许悄悄漂移
# ------------------------------------------------------------
def _components_in(rel: str) -> set[str]:
    text = (_repo_root() / rel).read_text(encoding="utf-8")
    return set(_COMPONENT_RE.findall(text))


@pytest.mark.parametrize("source,target", _TRANSLATION_PAIRS)
def test_a_translation_names_the_same_components_as_its_source(source, target):
    """★ 译文与原文提到的构件名必须**完全一致**。

    这条防的是「改了中文忘了英文」的漂移，而它之所以静默，是因为两边**都读得通**：
    中文版把 `FooGate` 换成了 `BarGate`，英文版还写着 `FooGate` —— 不会有任何东西变红
    （上面那个「构件必须存在」的测试甚至会**放行**，因为 `FooGate` 可能还活着），
    而英文读者会照着一份过期文档去理解当前行为。

    ★ 断言取**相等**而不是「译文 ⊆ 原文」：多出来的构件名同样有害 ——
    那意味着译文替原文做了一个原文没做的承诺。
    """
    src = _components_in(source)
    dst = _components_in(target)
    assert src, f"{source} 里一个构件名都没扫到 —— 正则或文件坏了，这条测试白跑"

    missing = sorted(src - dst)
    extra = sorted(dst - src)
    assert not missing and not extra, (
        f"`{source}` 与 `{target}` 提到的构件名对不上：\n"
        f"  原文有、译文没跟上：{missing}\n"
        f"  译文有、原文里没有：{extra}\n"
        f"两边都要改 —— 或者把这一对从 `_TRANSLATION_PAIRS` 里摘掉"
        f"（摘之前先确认它是否还称得上「译文」）。")


# ------------------------------------------------------------
# ★ .env.example 与代码读的变量必须一一对上
# ------------------------------------------------------------
_ENV_VAR_RE = re.compile(r"\b(SC_[A-Z0-9_]+)\b")


def _vars_read_by_code() -> set[str]:
    """包源码里以字符串字面量出现的 `SC_*` 名字（= 真的会被读的那些）。"""
    names: set[str] = set()
    for path in (pathlib.Path(signalchain.__file__).parent).rglob("*.py"):
        names |= set(_ENV_VAR_RE.findall(path.read_text(encoding="utf-8")))
    return names


def _vars_in_template() -> set[str]:
    path = _repo_root() / ".env.example"
    assert path.exists(), (
        "`.env.example` 不见了 —— `transport/gist.py` 缺凭据时的报错信息里"
        "指的就是它，README 也链它")
    text = path.read_text(encoding="utf-8")
    # 模板里注释掉的也算「提到了」——`# SC_ROLE=producer` 是有效的文档。
    return set(_ENV_VAR_RE.findall(text))


def test_every_env_var_the_code_reads_is_documented():
    """★ 代码读的每个 `SC_*` 都要出现在 `.env.example` 里。

    ★ 为什么值得单独钉：这些变量**在代码里读得到、在文档里找不到**是
    最难自查的一类缺失 —— 你手上有代码，所以你不会去别处找；
    而用户只有文档，所以他会以为「没有这个开关」。
    实际发生过：`SC_CATCHUP_MINUTES` / `SC_MAX_STALE_DAYS` /
    `SC_TASK_FAIL_LIMIT` / `SC_HTTP_TIMEOUT` / `SC_SNAPSHOT_TTL`
    五个调节旋钮，代码全读、模板一个都没写。
    """
    undocumented = _vars_read_by_code() - _vars_in_template()
    assert not undocumented, (
        f"这些变量代码会读，但 `.env.example` 里没写：{sorted(undocumented)}\n"
        f"用户只看得到模板 —— 没写就等于「不存在」。请补进模板（连默认值一起）。")


def test_the_template_documents_nothing_that_does_not_exist():
    """反向：模板里也不该出现代码根本不读的变量名。

    ★ 反方向同样有害，而且更隐蔽 —— 用户照着模板设了一个**没人读**的变量，
    它当然「不生效」，于是他会去查自己的配置、查环境、查 shell，
    而真相是这个变量从来不存在。**一个不存在的旋钮比没有旋钮更坏。**
    """
    phantom = _vars_in_template() - _vars_read_by_code()
    assert not phantom, (
        f"`.env.example` 里写了这些变量，但**代码从来不读**：{sorted(phantom)}\n"
        f"照着设的人会发现它不生效，然后去查自己的配置 —— 而它根本不存在。")


# ============================================================
# ★ 缺依赖时，读者必须拿到「照着装就行」，而不是一条裸 traceback
# ============================================================
# 这守的是**第一次接触**：别人 clone 下来敲的第一条命令。
# 实测（2026-09-22，全新 venv、零第三方包、从仓库根跑）：
#     python -m signalchain.demo
#     → 退出码 1，14 行输出，末行 `ModuleNotFoundError: No module named 'pandas'`
# 那句只说了「缺什么」，没说「怎么办」—— 而 Traceback 出现在第一次接触时，
# 代价不是「读者看不懂」，是「读者以为这框架坏了」（同本文件开头那条教训）。
#
# ★ 为什么用 meta_path 阻塞，而不是「建个空 venv」：要验的是**导入期**行为，
#   而建空 venv 得下载十几分钟，且测出来的是「那天镜像快不快」。阻塞法还把
#   「缺哪个包」变成参数 —— 两个依赖各测一次，顺便证明它们在导入期都是硬依赖。
_BLOCKED_IMPORT_PROBE = """
import sys

BLOCKED = (__BLOCKED__,)


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None


sys.meta_path.insert(0, _Blocker())
import signalchain

print("import ok:", ascii(signalchain.__file__))
"""


def _run_import_probe(blocked):
    r"""在子进程里 `import signalchain`，并假装 `blocked` 这个包不存在。

    ★ 探针正文只用 **ASCII** —— 两条独立的理由：
      ① 命令行参数里的非 ASCII 会过一层系统代码页转换（见 docs 里「参数编码」那条）；
      ② **探针自己的 stdout 也会过那一层**。成功那行的 print 原来是中文，
         cp1252 控制台下直接 `UnicodeEncodeError` ⇒ 退出码 1 ⇒ 下面那条对照测试
         报「依赖都在，import 却失败了」。**实际 import 是成功的，是它自己那句
         庆祝的话打不出来** —— 2026-09-22 CI 的 windows-latest 实测。
         假红，而且失败信息把人指向完全错误的方向（去查看门狗的条件）。
         ★ 同一个坑还有**第二个入口**，而且更阴：那句 print 后面跟着
         `signalchain.__file__` —— **路径里带非 ASCII 的机器**（用户名是中文的
         Windows 就是这么个情况）在 cp1252 下照样编不出来，只把字面量改成 ASCII
         并不够。所以这里用 `ascii(...)` 把它转成 `\uXXXX` 转义再打：
         路径仍然看得见（诊断信息一字不少），但输出**保证是纯 ASCII**。
      ★ 本机为什么测不出来 —— **两条独立原因，别只记住第一条**：
        ① 开发机设了 `PYTHONUTF8=1`（下面那个 env 已经摘掉）；
        ② 更根本的一条：**本机代码页是 GBK，而 GBK 编得出中文**。
           那句中文 print 在 GBK 下一点毛病没有，只有 cp1252 才编不出来。
           ⇒ 光摘环境变量**并不能**让本机复现（实测：把 print 换回中文，
             本机照样绿）；要在本机看到它红，得显式把编码按成 cp1252。
           CI 的 windows-latest 是 cp1252 —— 所以它一跑就现形。

    ★★ `BLOCKED` 必须是**元组/集合**（按名字精确比对），不能是字符串。
       这行最初写的是 `BLOCKED = {blocked!r}`（用 `.format()` 塞进去），
       结果 `blocked` 落成了一个**字符串** `'requests'` —— 而 `in` 对字符串是
       **子串匹配**：`"re" in "requests"` 为真，于是解释器去导入 `re` 时被拦，
       报错却出现在 numpy 里（numpy 的 core 导入失败，被它自己的异常处理器
       解释成「你从源码目录导入 numpy」）。**一次假绿、一条牛头不对马嘴的红。**
       现在用 `.replace()` 而不是 `.format()` 塞值：模板里就没有花括号了，
       也就没有「转义 `{{}}`」那一层次要陷阱。
    """
    # ★ 和 test_output_safety.py 里那几处同一条纪律：**显式摘掉** PYTHONUTF8 /
    #   PYTHONIOENCODING，让子进程走**这台机器自己的**控制台编码，而不是开发机
    #   设的那个覆盖值 —— 否则这条探针量的是「我的机器」，不是「别人的机器」。
    #   ⚠️ 但摘掉**不等于本机就能复现** cp1252 那个病：本机代码页是 GBK，
    #   中文照样编得出来（实测：把 print 换回中文，摘掉之后本机仍然绿）。
    #   这条护栏的本机可红性到此为止，剩下那半只有 cp1252 的机器能验 ——
    #   也就是 CI 的 windows runner（2026-09-22：本机 295 全绿，它红 14 条）。
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    code = _BLOCKED_IMPORT_PROBE.replace("__BLOCKED__", repr(blocked))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), env=env, timeout=180)


@pytest.mark.parametrize("blocked", ["pandas", "requests"])
def test_missing_dependency_error_says_what_to_install(blocked):
    """缺依赖时，报错里必须带**可直接粘贴的安装命令**。

    ★ 断言只钉 ASCII 片段，不钉中文原文：这条报错发生在 `setup_logging()`
      之前，重定向场景下 stderr 就是 cp936，钉中文等于把「编码」这个无关变量
      引进断言里（本仓库已有同类教训）。
    """
    proc = _run_import_probe(blocked)
    out = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode != 0, (
        f"假装没有 {blocked}，居然还是 import 成功了 —— 探针没生效？\n{out}")
    assert blocked in out, f"报错里没说是哪个包缺了：\n{out}"
    assert "python -m pip install requests pandas" in out, (
        f"缺 {blocked} 时没给出那条可粘贴的安装命令 ——\n"
        f"读者此刻正卡在第一次接触上，只告诉他「缺什么」等于没帮：\n{out}")
    # ★ 这一条刻意钉得更窄：`ModuleNotFoundError: No module named` 才是「裸报错」
    #   的形状。**不能**写 `"ModuleNotFoundError" not in out` —— 我们自己的友好
    #   消息里就提到了 ModuleNotFoundError（用来解释为什么换掉它），那样写必然假红。
    assert "ModuleNotFoundError: No module named" not in out, (
        f"裸的 ModuleNotFoundError 又跑出来了：\n{out}")


def test_import_is_not_broken_when_the_dependencies_are_present():
    """对照组：依赖都在时，上面那个看门狗**不许**乱开火。

    只测「缺依赖时报错对不对」不够 —— 一个**永远**抛异常的 import 也能过那条。
    这条用同一个探针、只是不阻塞任何包，要求真的 import 成功。
    """
    proc = _run_import_probe("__this_package_does_not_exist_at_all__")
    assert proc.returncode == 0, (
        f"依赖都在，import 却失败了 —— 那个看门狗的条件写错了：\n"
        f"{proc.stdout}{proc.stderr}")
