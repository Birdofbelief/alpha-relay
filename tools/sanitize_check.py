# -*- coding: utf-8 -*-
"""公开前的脱敏扫描器 —— 本仓库的「门禁」。

设计前提：**看不见的检查比没有检查更坏**。
所以这个脚本有三个特点：
  1. 只报「文件名:行号:命中类别」，**绝不打印命中的实际值**。
     命中值一旦被打进 CI 日志或终端记录，脱敏本身就失败了。
  2. 宁可误报（要求人工确认）也不放过 —— 只扫描文本，不做「大概是示例」的智能豁免。
  3. **自身出问题时必须大声。** 终端上「什么都没打印」和「检查通过了」
     长得一模一样 —— 本脚本在这上面连踩三次，见文件末尾的注释。

★ **本文件不许有 shebang 行**（`#!...`）。原因见文件末尾。用 `py tools/sanitize_check.py`
或 `py -m tools.sanitize_check` 调用。

用法：
    py tools/sanitize_check.py            # 扫描被 git 跟踪的文件（发布前跑这个）
    py tools/sanitize_check.py --all      # 扫描工作区全部文件（含未跟踪）
    py tools/sanitize_check.py --staged   # 只扫描暂存区（pre-commit 钩子用）

退出码：0 = 干净；1 = 有命中；**2 = 结果无效**（扫描器自己出错 ——
不是「通过」，也不是「命中」，别把 2 读成好消息）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

# ------------------------------------------------------------
# 输出编码：**必须抢在任何 print 之前**，否则本脚本会在 Windows 上静默变红。
#
# Windows 上不带 `PYTHONUTF8=1` 时，stdout 的编码是控制台代码页（简体中文机器上是
# GBK，CI 的 windows-latest 上是 cp1252），而本脚本要打印 ℹ️ / ⚠️ / ✅。
# 这些字符不在 GBK 里 ⇒ `print()` 抛 UnicodeEncodeError ⇒ 进程死在 main() 里。
#
# ★ 实测（2026-09-21，本机 `py -X utf8=0 tools/sanitize_check.py --all`）：
#     UnicodeEncodeError: 'gbk' codec can't encode character 'ℹ'
#     GATE_EXIT=2
#   同一原因让 `tests/test_sanitize_rules.py` 里那条「文档里的调用方式必须有输出」
#   的回归测试也变红（它起子进程调本脚本）。**一个根因，两个症状。**
#   而 3 个 ubuntu job 全绿 —— 因为 Linux 默认就是 UTF-8，本地开发机也设了
#   PYTHONUTF8=1 ⇒ 这个坑只在「别人 clone 下来在 Windows 上跑」时现形。
#
# 强制 UTF-8：真控制台上 Python 走 WriteConsoleW 渲染，与代码页无关；
# 重定向到管道/文件时，日志也因此是 UTF-8 而不是 GBK。
# errors="replace" 是兜底 —— 宁可把一个 emoji 打成 "?"，**也绝不因为「打印不出来」而崩**。
# 这跟文件末尾那条纪律是同一条：检查器不许有安静的失效路径。
# ------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # 不是 TextIOWrapper（被别的工具包过 / 已关闭）
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------
# 第一类：凭据形状。命中的是「像凭据的东西」，与它是否真的可用无关。
# ------------------------------------------------------------
CREDENTIAL_RULES = [
    ("GitHub classic token", re.compile(r"\bghp_[A-Za-z0-9]{16,}")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("GitHub OAuth/other token", re.compile(r"\bgh[osur]_[A-Za-z0-9]{16,}")),
    ("ServerChan sendkey", re.compile(r"\bSCT[A-Za-z0-9]{10,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer literal", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    # 32 位十六进制字符串——GitHub Gist ID 正好是这个形状。
    # 用它来兜「换了一枚 Gist 但你忘了删」的情况，代价是偶尔误报 UUID/哈希。
    ("Gist-ID-like hex32", re.compile(r"\b[0-9a-f]{32}\b")),
]

# 赋值语句里出现「看起来是真值」的凭据字段。
# 允许空串、占位符、以及从环境变量读取的写法。
PLACEHOLDER_OK = re.compile(
    r"""^\s*(""|''|None|null|\.\.\.|"""
    r"""["']?(CHANGE_?ME|YOUR[_A-Z]*|XXX+|<[^>]*>|PLACEHOLDER|TODO|REDACTED|EXAMPLE)["']?)\s*$""",
    re.IGNORECASE,
)
SECRETISH_NAMES = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|SENDKEY|SEND_KEY|APIKEY|API_KEY|GIST_ID|"
    r"CLIENT_SECRET|ACCESS_KEY|PRIVATE_KEY)",
    re.IGNORECASE,
)
#: 抓 `名字` + `其余部分`，引号用反向引用配平（`"k": v` 是 JSON，`k = v` 是 Python）。
_NAME_AND_REST = re.compile(
    r"""^\s*(?:export\s+)?(?P<q>["']?)(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P=q)\s*(?P<rest>.*)$"""
)


def _assigned_value(line: str) -> tuple[str, str] | None:
    """从一行里取出 `(字段名, 值)`；不是赋值行则返回 `None`。

    ★ 这里**故意不写成一条大正则**去把类型标注「顺手吃掉」。初版就是那么写的，
    结果 `gist_id: str,` 报了假警：可选标注组匹配失败后会**回退成空**，
    于是冒号被当成赋值号、`str` 被当成凭据值 —— 每一处函数签名都成了命中。
    正则的回退是它的固有行为，不是调参能修掉的。

    改成两步：先看名字有没有被引号包着，再决定那个冒号是什么。
      - **带引号**（`"gist_id": "abc"`）→ 冒号是 JSON/字典的键值分隔符
      - **不带引号**（`gist_id: str = ""`）→ 冒号开头的这一段是 Python 类型标注，
        **值必须出现在 `=` 之后**；整个 rest 里没有 `=` 就不是赋值行
        （这一条同时把手写的文档行 `gist_id: gist 的 ID…` 排除掉了）
    """
    m = _NAME_AND_REST.match(line)
    if not m:
        return None
    name, rest, quoted = m.group("name"), m.group("rest"), bool(m.group("q"))

    if quoted:
        if rest[:1] in (":", "="):
            return name, rest[1:]
        return None

    if rest.startswith(":"):
        # 类型标注区。没有 `=` 就说明这行的冒号后不是值。
        if "=" not in rest:
            return None
        return name, rest.split("=", 1)[1]
    if rest.startswith("="):
        return name, rest[1:]
    return None

# ------------------------------------------------------------
# 第二类：自定义指纹（**规则由使用者提供，本文件里不留任何真指纹**）
#
# ★ 这一类刻意**不内置任何规则**。原因不是「不方便」，而是**内置规则本身就是
#   泄漏点**：一条指纹规则必须写明它要抓什么，于是「核心公式」「多周期结构」
#   「阈值表」「冷却期语义」这些词就跟着规则一起进了公开仓库 —— 门禁把它要
#   保护的东西印在了自己身上。这与 `INTERNAL_DOCS` 那条「不要写平台名」是同
#   一个道理，只是那条泄漏的是一个名字，这条泄漏的是整段结构。
#
# ★★ 更要紧的一点：**按关键词过滤天生不完备。** 它只认识「我提前想到的写法」，
#   而真正会泄漏的是**结构** —— 一个字段叫什么名字、一个参数是什么形状。
#   实测：本文件早期那套关键词规则，对「一个私有辅助数据的**键名**被写进
#   公开 docstring」这种残留**一条都没命中**，而它就是一个确凿的私有字段名。
#   所以第一道防线永远是「结构上就不存在」（私有策略整段重写、
#   公开代码里不留私有字段名），这一类规则只是**兜底的绊线**，不是防线。
#
#   格式（每行一条，**制表符**分隔）：
#       正则<TAB>类别标签
#   `#` 开头是注释；`# expect: N` 声明本文件应有多少条规则（见下）。
#   文件被 `.gitignore` 挡住 —— 一个为了保密而存在的文件，绝不能自己成为
#   泄漏源（同第三类）。格式示例见 `.sanitize-markers.example.txt`。
#
# ★ 三条硬错误一律**整轮作废（退出码 2）**，绝不跳过那一行：
#     1. 某行没有制表符
#     2. 正则编译不过
#     3. `# expect: N` 与实际条数对不上
#   第 1 条不是假想：制表符被打成空格时，正则**照样编译得过**（空格在正则里
#   只是普通字符），只是永远匹配不到任何东西 —— 一个完美的静默失效。
#   跳过一条读不懂的规则，和「那条规则没命中」，在终端上长得一模一样。
# ------------------------------------------------------------

MARKER_FILE_ENV = "SC_MARKER_FILE"
MARKER_FILE_DEFAULT = ".sanitize-markers.txt"
MARKER_EXAMPLE_DEFAULT = ".sanitize-markers.example.txt"
MARKER_SEP = "\t"
DEFAULT_MARKER_LABEL = "自定义指纹"

#: 可选的头指令，声明清单里应当解析出几条规则。
#: 存在的理由：**指纹规则被删掉时，没有任何测试会变红** —— 而「门禁悄悄变松」
#: 在本仓库真实发生过（黑名单规则整条瞎掉却无人察觉）。
MARKER_EXPECT_RE = re.compile(r"^#\s*expect\s*:\s*(\d+)\s*$", re.IGNORECASE)


class MarkerFileError(RuntimeError):
    """指纹清单读不懂或自相矛盾 —— 必须当成失败，不能当成「没有规则」。"""


class MarkerFile(NamedTuple):
    """一份加载好的指纹清单。`declared` 是 `# expect:` 声明的条数（没写则 None）。"""

    path: Path
    rules: list[tuple[str, str]]
    declared: int | None = None


def load_markers(path: Path | None = None) -> MarkerFile:
    """读取自定义指纹清单。文件不存在 → 空清单（不报错，但调用方要如实说不完整）。

    三条硬错误见上（缺分隔符 / 正则编译不过 / expect 对不上），一律抛
    `MarkerFileError` 让整轮结果作废。**降级成「跳过这一条」是本文件反复在防
    的那种失效**：它表现为一次干净通过。
    """
    if path is None:
        path = Path(os.environ.get(MARKER_FILE_ENV, REPO_ROOT / MARKER_FILE_DEFAULT))
    if not path.exists():
        return MarkerFile(path, [])

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise MarkerFileError(f"读不了指纹清单 {path}：{e}") from e

    rules: list[tuple[str, str]] = []
    declared: int | None = None
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        m = MARKER_EXPECT_RE.match(line)
        if m:
            declared = int(m.group(1))
            continue
        if line.lstrip().startswith("#"):
            continue
        if MARKER_SEP not in line:
            raise MarkerFileError(
                f"{path}:{lineno} 这一行没有制表符。格式是「正则<TAB>类别标签」。\n"
                f"    分隔符若被打成空格，正则**照样编译得过**（空格在正则里是普通"
                f"字符），只是永远匹配不到任何东西 —— 这种失效从输出上看不出来，"
                f"所以这里不跳过，直接作废整轮。")
        pattern, label = (part.strip() for part in line.split(MARKER_SEP, 1))
        if not pattern:
            raise MarkerFileError(f"{path}:{lineno} 正则为空（空正则会匹配每一行）")
        try:
            re.compile(pattern)
        except re.error as e:
            raise MarkerFileError(f"{path}:{lineno} 正则编译不过：{e}") from e
        rules.append((pattern, label or DEFAULT_MARKER_LABEL))

    if declared is not None and declared != len(rules):
        raise MarkerFileError(
            f"{path} 声明 `# expect: {declared}`，实际解析出 {len(rules)} 条 ——"
            f"要么有规则被删掉了，要么有一行写坏了。\n"
            f"    规则条数变少不会让任何测试变红，所以这一条只能自己拦。")

    return MarkerFile(path, rules, declared)

# 内部文档 —— 整份都不该出现在公开仓库里（含实盘阈值与事故细节）。
#
# 这份名单只对**文件名**生效（见 `scan_files`），所以只需要覆盖文档的通用名。
#
# ★ 刻意**不写具体平台 / 渠道的名字**。这个文件是公开的，而 `SELF_EXEMPT`
#   让扫描器扫不到自己 —— 也就是说：往名单里写一个平台名，等于由「唯一该拦下
#   那个名字的程序」亲手把它发布出去，而且永远不会被自己发现。
#   那类名字的正确归宿是 `.sanitize-deny.txt`：它被 `.gitignore` 挡住，
#   **自己不入库**（同一个道理，也写在下面第三类的注释里）。
#   所以这里用「备忘」这种通用词兜底，而不是把平台名拼进去。
INTERNAL_DOCS = [
    "备忘", "开发日志", "V5_REPAIR_PLAN", "网络韧性审计",
    "TOKEN轮换工单", "上云工单", "credential", "cloud-computer",
]

# ------------------------------------------------------------
# 第三类：作者自备黑名单（**可选，但强烈建议**）
#
# 上面两类的根本局限：它们只认识**我提前想到的**模式。像「我的标的池里
# 具体是哪 21 个代码」这种事，扫描器不可能凭空知道 —— 而它恰恰是最该被检查的
# 一类泄漏：单独一个标的代码看起来完全无害，21 个凑齐就是策略的核心资产。
# （这里刻意**不举具体的六位数字当例子** —— 那样示例本身就成了泄漏点。）
#
# 解法是让**使用者**提供黑名单，而不是让扫描器去猜。黑名单文件本身被
# `.gitignore` 挡住，所以「池子」不会因为它而被抄进公开仓库 —— 这一点是关键：
# 一个为了保密而存在的文件，绝不能自己成为泄漏源。
#
#   用法：在仓库根放 `.sanitize-deny.txt`，一行一条待检查的字面量。
#        用 `SC_DENY_FILE` 环境变量可以指向别处。
#        兼容 `#` 注释与空行。
#
# 不做正则解释 —— **全部按字面量处理**。黑名单是「我确定这些东西不该出现」，
# 让使用者去写转义只会引入静默失配（写错一个 `\` 就等于没检查）。
# ------------------------------------------------------------

DENY_FILE_ENV = "SC_DENY_FILE"
DENY_FILE_DEFAULT = ".sanitize-deny.txt"


def load_denylist(path: Path | None = None) -> list[str]:
    """读取自备黑名单。文件不存在时返回空列表（不报错）。"""
    if path is None:
        path = Path(os.environ.get(DENY_FILE_ENV, REPO_ROOT / DENY_FILE_DEFAULT))
    if not path.exists():
        return []
    terms: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    except OSError:
        return []
    return terms


def scan_denylist(path: str, text: str, terms: list[str]) -> list[Finding]:
    """在文本里查黑名单字面量。

    ★ **只报行号，不报命中的字面量本身。** 这条与凭据规则同一个理由：
    报告若是把命中值打出来，它就把泄漏从「文件里」搬到了「日志/终端/CI 输出」——
    而后者往往比前者更容易被看到、被留存。
    """
    out: list[Finding] = []
    if not terms:
        return out
    lines = text.splitlines()
    for term in terms:
        if not term:
            continue
        for i, line in enumerate(lines, start=1):
            if term in line:
                out.append(Finding(path, i, "自备黑名单命中（值不显示）"))
                break          # 同一词在同一文件只报一次，避免刷屏
    return out

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
              ".whl", ".pyc", ".so", ".dll", ".exe", ".woff", ".woff2"}

# 自身豁免：扫描器要写规则，规则里必然出现这些词。
SELF_EXEMPT = {"tools/sanitize_check.py"}

class Finding(NamedTuple):
    """一条命中：`(文件, 行号, 类别, 命中值)`。

    ★ 这里**必须**是 NamedTuple，不能写 `Finding = tuple`。
    后者不是类型别名 —— 它把 `Finding` 绑定到内建 `tuple` 本身，于是
    `Finding(a, b, c)` 会变成 `tuple(a, b, c)` 并抛
    `TypeError: tuple expected at most 1 argument`。
    （本文件初版就是这么写的：字面量写法 `(a, b, c, "")` 全都正常，
    只有唯一一处用 `Finding(...)` 构造的地方 —— 自备黑名单规则 —— 会崩。
    而它是**命中时**才崩，所以静态看、空跑都发现不了：那条规则从加上去
    到被发现，一次都没成功报出过命中。检查器自己成了它警告的那种东西。）

    `lineno=0` 表示「命中的是文件名本身」，行号无意义。

    ★ **构造一律走 `Finding(...)`，不许写成字面量 `(path, i, cat, "")`。**
    字面量 tuple 与 `Finding` **相等**（NamedTuple 是 tuple 的子类），所以
    `findings == []`、`len(findings)`、`for a, b, c, _ in findings` 全都照常工作 ——
    唯独 `f.lineno` 会 AttributeError。于是「类型标注写 `list[Finding]`、
    实际给的是 tuple」这种谎言能一直不被发现，直到有人真的去读字段。
    （本文件三个构造点曾经全是字面量，就是被测试文件读 `.lineno` 才暴露的。）
    """

    path: str
    lineno: int
    category: str
    snippet: str = ""      # 一律置空：命中值不打印，打印即等于二次泄漏


class ScanInputError(RuntimeError):
    """连「要扫哪些文件」都拿不到 —— 必须当成失败，不能当成「没东西要扫」。"""


def _git_files(args: list[str]) -> list[str]:
    """跑一条 git 命令取文件列表。git 不可用/不在仓库里 → 抛错，**不返回空表**。

    ★ 这里刻意不「失败就返回 []」。返回空表会一路走到 `main` 打印
    「✅ 共 0 个文件，零命中」—— 在**仓库还没 git init** 的时候（本仓库初建时
    就是这样）它会报一个漂亮的假绿灯，而这恰好是最危险的时刻：
    代码全在，检查没跑，输出说通过。
    """
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT,
                             capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ScanInputError(f"找不到 git 命令：{exc}") from exc
    if out.returncode != 0:
        raise ScanInputError(
            f"`git {' '.join(args)}` 返回 {out.returncode}：{out.stderr.strip()[:200]}"
            f"（若仓库尚未 git init，请改用 --all）")
    return [p for p in out.stdout.split("\0") if p]


def _tracked_files() -> list[str]:
    return _git_files(["ls-files", "-z"])


def _staged_files() -> list[str]:
    return _git_files(["diff", "--cached", "--name-only",
                       "--diff-filter=ACMR", "-z"])


def _all_files() -> list[str]:
    result = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache",
                                                ".mypy_cache", ".ruff_cache", "node_modules"}]
        for f in files:
            full = Path(root) / f
            result.append(str(full.relative_to(REPO_ROOT)).replace(os.sep, "/"))
    return result


def _is_json_with_hex32(path: str) -> bool:
    """lock 文件、哈希清单之类含 32 位 hex 是正常的，降级为提示而非拦截。"""
    return Path(path).suffix in {".lock", ".sum", ".json"} and "lock" in Path(path).name


def scan_text(path: str, text: str,
              markers: Sequence[tuple[str, str]] = ()) -> list[Finding]:
    """扫描一段文本。`markers` 是 `(正则, 类别标签)` 列表，默认为空。

    ★ 默认空是**故意的**：指纹规则全部来自外部清单（见第二类），
    所以这个函数的默认行为里没有任何指纹检查。想检查指纹就把清单传进来 ——
    显式传参，而不是让函数偷偷去读磁盘上的某个文件。
    """
    findings: list[Finding] = []
    lines = text.splitlines()

    for i, line in enumerate(lines, start=1):
        for name, rx in CREDENTIAL_RULES:
            if rx.search(line):
                if name == "Gist-ID-like hex32" and _is_json_with_hex32(path):
                    continue
                findings.append(Finding(path, i, f"凭据形状: {name}"))

        parsed = _assigned_value(line)
        if parsed and SECRETISH_NAMES.search(parsed[0]):
            name, value = parsed
            value = value.strip().rstrip(",")
            # 去掉行尾注释再判断，避免把 "TOKEN = ''  # 留空则关闭" 误报
            value_wo_comment = re.sub(r"#.*$", "", value).strip() if "#" in value else value
            if value_wo_comment and not PLACEHOLDER_OK.match(value_wo_comment):
                # 从环境变量读取是允许的
                if not re.search(r"os\.(environ|getenv)|getenv\(|ENV\[", value):
                    findings.append(Finding(path, i, f"凭据字段被赋了非占位值: {name}"))

    findings.extend(scan_markers(path, text, markers))
    return findings


def scan_markers(path: str, text: str,
                 markers: Sequence[tuple[str, str]] = ()) -> list[Finding]:
    """只跑**自定义指纹**那一类。

    ★ 单独抽出来是为了自身豁免（见 `scan_paths`）：扫描器自己的源码必须跳过
    凭据类规则（它按定义就得写出 `ghp_` 这类词样），**但指纹类不能跳过** ——
    本文件是公开的，所以一条指纹若命中它自己的注释，那不是误报，是真命中。
    实战验证过一次：本节初版把 `scan_text` 整个用在自身豁免上，其中一条
    `SECRETISH_NAMES = re.compile(...)` 当场被凭据规则报了出来。两个方向都得试。
    """
    out: list[Finding] = []
    lines = text.splitlines()
    for rx, label in markers:
        for i, line in enumerate(lines, start=1):
            if re.search(rx, line, re.IGNORECASE):
                out.append(Finding(path, i, f"自定义指纹: {label}"))
    return out


def resolve(path: str) -> Path:
    """把一个扫描目标解析成实际文件路径。**绝对路径直接采用，相对路径按仓库根解析。**

    ★ 单独抽出来，是因为「这个字符串到底指哪个文件」必须**只有一个定义**：
    `scan_paths` 判「读不读得到」、`main` 判「调用方点名的文件在不在」，
    两处若各写一遍，迟早会分叉 —— 而分叉的表现是「一处说没问题、另一处说不在」，
    最难查的那种。
    """
    p = Path(path.replace(os.sep, "/"))
    return p if p.is_absolute() else REPO_ROOT / p


def scan_paths(paths: list[str], denylist: list[str] | None = None,
               markers: Sequence[tuple[str, str]] | None = None) -> list[Finding]:
    """扫描一组路径。`paths` 可以是仓库相对路径，也可以是绝对路径。

    ★ **作者自备的两个清单文件自身必须被跳过。** 它们按定义装满了不该出现的
    东西（黑名单是字面量，指纹清单是「正则 + 它要抓什么」的说明），扫它们必然
    满屏命中。`--all` 会走到它们，所以这条跳过是必需的，不是优化。
    """
    findings: list[Finding] = []
    private_names = {Path(n).name for n in (DENY_FILE_DEFAULT, MARKER_FILE_DEFAULT)}
    markers = markers or []

    for rel in paths:
        norm = rel.replace(os.sep, "/")
        if norm in SELF_EXEMPT:
            # 扫描器要写规则，规则里必然出现 `ghp_` 这类词样，扫它自己会满屏误报
            # —— 所以豁免。但**豁免范围必须只覆盖那三类规则，不能连自备清单一起豁免**：
            # 本文件初版是整体 `continue`，结果它自己注释里举的示例代码恰好是个真标的池
            # 成员，而唯一该发现它的检查正好瞎了。豁免给宽一点，就是静默漏检。
            #
            # 指纹清单也照跑，理由是**本文件是公开的**：所以一条指纹若命中了它自己的
            # 注释，那不是误报，是真命中 —— 那段注释就是泄漏。正解是改写注释，
            # 不是给它加豁免。
            self_path = REPO_ROOT / norm
            if self_path.is_file():
                own_text = self_path.read_text(encoding="utf-8")
                findings.extend(scan_denylist(norm, own_text, denylist or []))
                findings.extend(scan_markers(norm, own_text, markers))
            continue
        if Path(norm).name in private_names:
            continue
        if Path(norm).suffix.lower() in BINARY_EXT:
            continue

        # 文件名本身就是内部文档 → 直接拦（不必读内容）
        stem = Path(norm).stem
        for bad in INTERNAL_DOCS:
            if bad.lower() in stem.lower():
                findings.append(Finding(norm, 0, f"内部文档不得入库（匹配「{bad}」）"))
                break

        # 绝对路径直接采用；相对路径按仓库根解析。
        full = resolve(norm)
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(norm, text, markers))
        findings.extend(scan_denylist(norm, text, denylist or []))

    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="公开前脱敏扫描（只报位置，不打印命中值）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="扫描工作区全部文件")
    g.add_argument("--staged", action="store_true", help="只扫描暂存区（pre-commit 用）")
    ap.add_argument("--path", action="append", default=[],
                    metavar="FILE",
                    help="扫描任意文件（可在仓库外，用于复核别人给的产物）；可重复")
    args = ap.parse_args()

    denylist = load_denylist()
    if denylist:
        print(f"ℹ️ 已加载自备黑名单 {len(denylist)} 条（{DENY_FILE_ENV} / {DENY_FILE_DEFAULT}）")
    else:
        print(f"ℹ️ 未发现自备黑名单（{DENY_FILE_DEFAULT}）——"
              f"「我的标的池代码有没有泄漏」这类检查**不会被执行**。")
        print(f"   建议在仓库根建一个（每行一条字面量），该文件已被 .gitignore 挡住。")

    # ★ 清单读不懂 → **本次结果无效**，不是「那就少查几类」。
    #   这是本文件的核心立场：一条读不懂的规则若被静默跳过，它的输出与
    #   「那条规则没命中」逐字相同，而后者看起来就是一次干净通过。
    try:
        markers = load_markers()
    except MarkerFileError as e:
        print("💥 自定义指纹清单读不懂 —— **本次结果无效，不得视为通过**。",
              file=sys.stderr)
        print(f"   {e}", file=sys.stderr)
        return 2
    if markers.rules:
        print(f"ℹ️ 已加载自定义指纹 {len(markers.rules)} 条（{MARKER_FILE_ENV} / "
              f"{markers.path.name}）")
    else:
        print(f"ℹ️ 未发现自定义指纹清单（{MARKER_FILE_DEFAULT}）——"
              f"「这类私有特征有没有被搬进来」的检查**不会被执行**。")
        print(f"   格式见 {MARKER_EXAMPLE_DEFAULT}（每行：正则 + 制表符 + 类别标签）。")

    if args.path:
        targets, label = args.path, "指定文件"
    elif args.all:
        targets, label = _all_files(), "工作区全部文件"
    elif args.staged:
        targets, label = _staged_files(), "暂存区文件"
    else:
        targets, label = _tracked_files(), "git 已跟踪文件"

    if not targets:
        print(f"💥 待扫描文件数为 0（{label}）—— **这不是通过**。", file=sys.stderr)
        print("   零文件等于没检查；而没检查的终端输出和通过是一样的。"
              "请确认仓库已 git init，或改用 --all。", file=sys.stderr)
        return 2

    # ★ 「点名了文件、却一个都读不到」与「一个文件都没给」是同一个病，必须一起拦。
    #
    #   `scan_paths` 遇到读不到的文件是 `continue` —— 这对 `--all`/`--staged` 是对的
    #   （那是遍历途中文件恰好消失的竞态，不该因此中断整轮）。但 `--path` 是
    #   **调用方亲口点名的文件**：读不到就不是通过。否则路径写错一个字符，输出与
    #   「真扫过且干净」**逐字相同** —— 一行绿色的「零命中」。
    #
    #   这不是假想：本条的由来就是实测。`--path 'C:\Users\Public\x\bad.md'` 用双引号
    #   经 Git Bash 传进来时读不到文件，扫描器照样报「✅ 指定文件共 1 个，零命中」，
    #   而同一个文件用单引号传就正常报红。**探针失效与探针通过长得一样**，正是本
    #   文件存在的理由，它不该自己踩这一条。
    if args.path:
        unreadable = [t for t in args.path if not resolve(t).is_file()]
        if unreadable:
            print(f"💥 指定的 {len(unreadable)}/{len(args.path)} 个文件读不到"
                  f" —— **这不是通过**。", file=sys.stderr)
            for t in unreadable:
                print(f"   读不到：{t}", file=sys.stderr)
            print("   路径错一个字符、或写法程序读不懂，结果都和「扫过且干净」一样。",
                  file=sys.stderr)
            return 2

    findings = scan_paths(targets, denylist=denylist, markers=markers.rules)

    if not findings:
        # ★ 通过时必须说清「这次到底检查了什么」。
        # 少一份清单的「✅ 通过」是一句会骗人的话：它看起来和完整通过一样，
        # 而恰恰是「标的池代码有没有泄漏」这件对本仓库最要紧的事没跑。
        # 最后一行是唯一会被记住的那行，所以差别必须写在最后一行上。
        missing = []
        if not denylist:
            missing.append(DENY_FILE_DEFAULT)
        if not markers.rules:
            missing.append(MARKER_FILE_DEFAULT)

        if not missing:
            print(f"✅ 脱敏扫描通过：{label}共 {len(targets)} 个，零命中"
                  f"（含自备黑名单 {len(denylist)} 条、自定义指纹 {len(markers.rules)} 条）。")
        else:
            print(f"⚠️ 脱敏扫描通过（**不完整**）：{label}共 {len(targets)} 个，零命中，"
                  f"但 {' 与 '.join(missing)} 未加载。")
            print("   未加载的那一类检查**没有执行** —— 要算完整通过，"
                  "请在仓库根把这两份清单都建起来（都已被 .gitignore 挡住）：")
            print(f"     {DENY_FILE_DEFAULT}                    每行一条**字面量**"
                  f"（标的池代码、账户名之类的确定字符串）")
            print(f"     {MARKER_FILE_DEFAULT}  每行「正则 + 制表符 + 类别标签」，"
                  f"格式见 {MARKER_EXAMPLE_DEFAULT}")
        return 0

    print(f"❌ 脱敏扫描发现 {len(findings)} 处命中（{label}）：\n")
    for path, lineno, category, _ in sorted(findings):
        loc = f"{path}:{lineno}" if lineno else path
        print(f"  {loc}  →  {category}")
    print("\n命中值一律不打印（打印即等于二次泄漏）。请人工核对这些位置。")
    print("若确认是误报，在 tools/sanitize_check.py 里加精确豁免，不要整体关闭规则。")
    return 1


# ------------------------------------------------------------
# ★★ 关于「看不见的失效」：本脚本连踩三次，三次都是同一个形状
#
# 终端上「什么都没打印」和「检查通过了」看起来完全一样。一个失效的门禁
# 不只是没起作用 —— 它会**冒充一个通过的门禁**，让人据此收工。
#
# 三次实录（都发生在「提取开源仓库」这一轮里）：
#
#   1. `Finding = tuple` —— 见上面 Finding 的注释。把内建 tuple 当成了
#      类型别名，唯一用 `Finding(...)` 构造的地方（自备黑名单规则）一命中就
#      TypeError。空跑正常、静态看正常，只有**真的抓到东西时**才崩。
#      换句话说：它只在最该报出结果的时刻静默。
#
#   2. **本文件不能有 shebang 行。** 首行若是 `#!/usr/bin/env python`，
#      那么 Windows 的 `py` 启动器（`py tools/sanitize_check.py`）会去解析
#      这行 shebang，然后**零输出、退出码 49** 地直接死掉 —— 不是报错，
#      是什么都不说。（`python.exe tools/sanitize_check.py` 与
#      `py -X faulthandler ...` 都正常，所以极易被当成偶发。）
#      这条是真·坑：`py` 是本项目调用 Python 的标准方式，也就是说
#      **所有按文档方式调用它的记录，全是零输出。**
#
#   3. 默认模式依赖 `git ls-files`。仓库还没 `git init` 时它返回空表，
#      脚本于是报「✅ 共 0 个文件，零命中」。这是最坏的一种：一个假绿灯，
#      而且亮在「代码已经全写好了、检查一次都没跑过」的那一刻。
#      → 已由 `ScanInputError` + `targets` 空表判失败修掉。
#
# 三次的共同教训：**检查器的失效路径必须和它的通过路径长得不一样。**
# 所以这里不写 `sys.exit(main())`，而是把自身崩溃也渲染成一条明确输出。
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print("💥 脱敏扫描器**自身**崩溃 —— 本次结果无效，"
              "不得视为通过。", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
