# -*- coding: utf-8 -*-
"""脱敏扫描器**自己的**测试。

## 为什么一个检查器需要被检查

`tools/sanitize_check.py` 的职责是「报出泄漏」。它的失效方式只有一种：
**该报的时候没报** —— 而那时它的输出和「通过」是同一个样子（都是零命中、
都是绿色）。所以它自己必须有一个反向的、可执行的检查。

这不是假想的风险。本文件覆盖的三条规则，在写出来的当天**全都是瞎的**：

  1. `Finding = tuple` 把 `Finding` 绑定到了内建 `tuple` 本身
     → `Finding(a, b, c)` 抛 TypeError → 唯一用它构造的那条规则
     （自备黑名单）**从加上去到被发现，一次都没成功报出过命中**。
     而它只在**命中时**才崩，所以空跑、静态看，一切正常。

  2. 一条大正则把 Python 类型标注「顺手吃掉」→ `gist_id: str,` 被当成
     「凭据字段被赋了值 `str`」→ 每一处函数签名都成了命中。

  3. 未 git init 时 `git ls-files` 返回空 → 脚本打印
     「✅ 共 0 个文件，零命中」—— 一个漂亮的假绿灯。

三条的共同点是：**报「通过」的那条路是通的，报「命中」的那条路是断的。**
只测「干净输入 → 零命中」永远发现不了它们。所以下面每一条规则都要
**两个方向各测一次**：干净输入不报，以及**脏输入必须报**。

## ★ 那些「诱饵」字符串为什么是拼出来的

本文件要写一堆「应该被扫描器抓到」的示例。但如果它们以完整形态出现在源码里，
**这个文件自己就会成为扫描器的命中项** —— 而它必须通过同一道门禁
（`py tools/sanitize_check.py --all`）。

所以所有诱饵都由 `_bait()` 在**运行期**拼出来，源码里没有任何一个完整形态。
这不是取巧：它顺带证明了「扫描器扫的是文本，不是意图」——
它不会因为一句话出现在测试里就网开一面。
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bait(*parts: str) -> str:
    """把片段拼成一个**运行期**字符串，源码里看不到完整形态。

    ★ 分片不是随便切的，规则有两条，少一条就漏：

      1. **相邻的两个片段，拼起来才是那个词。** 比如把 `ghp_` 和后面
         那串字符分成两个片段，源码里就不会出现一个完整的 token 形状。
      2. **凡是被 `.*` 连接的规则，两半都要切。** 这一条是踩出来的：
         只在词中间切片是不够的 —— 一条形如「两个完整词并列」的参数列表，
         源码文本里两半都在、顺序也对，而 `.*` 能跨过引号和逗号把它们连上，
         于是**这行参数自己成了命中项**。两半都切碎之后，规则要的那个
         字面量在源码里根本不存在，才算真的安全。

    ★ 连**注释和文档字符串都算**。本函数的初版在文档里举了个「错误示范」，
    把两个词原样写了出来 —— 那一行于是成了命中项。**讲解陷阱的文本本身
    也会踩中陷阱**，因为扫描器只认文本、不认意图。所以下面的示例一律用
    占位词（`前半`/`后半`）代替真词，右侧注释也只写类别不写原文。
    """
    return "".join(parts)


def _load():
    """按路径加载扫描器（它不在包 `signalchain` 里，也不是一个包）。"""
    path = REPO_ROOT / "tools" / "sanitize_check.py"
    spec = importlib.util.spec_from_file_location("_sanitize_check_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


san = _load()


# ============================================================
# 第零条：检查器本身得是能跑的
# ============================================================
def test_finding_is_a_real_namedtuple():
    """★ `Finding` 必须真的是个 NamedTuple。

    初版写的是 `Finding = tuple`。那不是类型别名 —— 它把 `Finding` 绑定到
    内建 `tuple` 本身，于是 `Finding(a, b, c)` 变成 `tuple(a, b, c)` 并抛
    `TypeError: tuple expected at most 1 argument`。

    危险之处在于**字面量写法 `(a, b, c, "")` 全都正常**，只有唯一一处用
    `Finding(...)` 构造的地方会崩 —— 而那是自备黑名单规则，且**命中时才崩**。
    所以这条测试要断言的是「能构造」，不是「类型名对不对」。
    """
    f = san.Finding("a.py", 1, "类别")
    assert isinstance(f, tuple)
    assert f.path == "a.py" and f.lineno == 1
    assert f.snippet == "", "命中值必须为空 —— 见下面 test_reports_never_carry_the_value"


def test_scanner_survives_a_clean_input():
    """干净输入 → 零命中。这是**必要性**检查，不是充分性检查。"""
    assert san.scan_text("ok.py", "x = 1\ny = 'hello'\n") == []


# ============================================================
# 规则一：凭据形状
# ============================================================
_CRED_BAITS = [
    ("ghp",     _bait("ghp_", "A" * 24)),
    ("pat",     _bait("github_pat_", "B" * 24)),
    ("oauth",   _bait("gho_", "C" * 24)),
    ("sendkey", _bait("SCT", "1" * 14)),
    ("slack",   _bait("xoxb-", "2" * 14)),
    ("aws",     _bait("AKIA", "Q" * 16)),
    ("pkcs",    _bait("-----BEGIN ", "RSA ", "PRIVATE KEY-----")),
    ("bearer",  _bait("Bearer ", "z" * 26)),
    ("gistid",  _bait("d", "e" * 31)),      # 正好 32 位 —— 多一位就不再是 Gist ID 的形状
]


@pytest.mark.parametrize("label,text", _CRED_BAITS,
                         ids=[label for label, _ in _CRED_BAITS])
def test_credential_shapes_are_caught(label, text):
    """每一类凭据形状都必须被**真的报出来**。

    参数化而不是一条大断言：一条大断言在某一类失效时只告诉你「红了」，
    而这里会直接指出是哪一类瞎了 —— 检查器的排障成本要低，
    否则坏了也没人去修。
    """
    findings = san.scan_text("leak.py", f'KEY = "{text}"\n')
    assert findings, f"{label} 这一类形状没被抓到：{text!r}"


def test_clean_prose_does_not_trip_credential_shapes():
    """正常文本不许误报 —— 误报会训练人加 `--no-verify`，那等于关掉整个检查。"""
    prose = (
        "token 这个名字出现在文档里是正常的\n"
        "Authorization: token <你的token>\n"
        "gist 的 ID 是一串 32 位十六进制\n"
    )
    assert san.scan_text("doc.md", prose) == []


def test_reports_never_carry_the_value():
    """★ 命中报告里**不许出现命中值本身**。

    报告若把值打出来，就等于把泄漏从「文件里」搬到了「日志/终端/CI 输出」——
    而后者往往更容易被看到、被留存、被转发。所以 `snippet` 一律为空。
    """
    bait = _bait("ghp_", "S" * 24)
    findings = san.scan_text("leak.py", f'TOKEN = "{bait}"\n')
    assert findings
    for f in findings:
        assert f.snippet == ""
        assert bait not in repr(f)


# ============================================================
# 规则二：凭据字段被赋了非占位值 —— 这里是最容易出假警的地方
# ============================================================
@pytest.mark.parametrize("line", [
    "GITHUB_TOKEN = \"" + "R" * 24 + "\"",
    "\"GITHUB_TOKEN\": \"" + "R" * 24 + "\"",
    "SERVERCHAN_SENDKEY = \"" + "R" * 24 + "\"",
    "api_key = \"" + "R" * 24 + "\"",
])
def test_real_looking_assignments_are_caught(line):
    assert san.scan_text("cfg.py", line + "\n"), f"没抓到：{line!r}"


@pytest.mark.parametrize("line", [
    # 类型标注 —— 初版在这里报了一大片假警
    "gist_id: str = \"\"",
    "    token: str,",
    "def f(gist_id: str, token: str = \"\") -> None:",
    # 占位符
    "GITHUB_TOKEN = \"\"",
    "GITHUB_TOKEN = ''",
    "GITHUB_TOKEN = None",
    "GITHUB_TOKEN = \"<你的 token>\"",
    "GITHUB_TOKEN = \"YOUR_TOKEN_HERE\"",
    "GITHUB_TOKEN = \"CHANGE_ME\"",
    "GITHUB_TOKEN = \"TODO\"",
    # 从环境变量读 —— 这是**推荐写法**，绝不能报
    "GITHUB_TOKEN = os.environ[\"SC_GITHUB_TOKEN\"]",
    "GITHUB_TOKEN = os.getenv(\"SC_GITHUB_TOKEN\")",
    # 行尾注释不该影响判断
    "GITHUB_TOKEN = \"\"   # 留空则关闭",
    # 文档行：冒号后面是说明，不是值
    "gist_id: gist 的 ID",
])
def test_annotations_placeholders_and_env_reads_are_not_flagged(line):
    """★ 这一组全部必须**不报**。每一行都对应一次真实的误报或一次刻意的允许。

    尤其是类型标注那三行：初版的正则把 `gist_id: str,` 里的 `str` 当成了
    凭据值，于是**每一处函数签名都成了命中**。误报的代价不是「吵」，
    是让人对整个检查失去信任、进而绕过它。
    """
    assert san.scan_text("cfg.py", line + "\n") == [], f"误报：{line!r}"


def test_assigned_value_parser_handles_the_two_colon_cases():
    """`_assigned_value` 的两步解析：引号决定那个冒号是什么。

    带引号 → JSON/字典的键值分隔符；不带引号 → Python 类型标注，值在 `=` 之后。
    单独测这个函数，是因为它的行为**无法从端到端结果反推**：
    一条不报的断言既可能是「解析对了」也可能是「整条规则瞎了」。
    """
    assert san._assigned_value('"gist_id": "abc"') == ("gist_id", ' "abc"')
    assert san._assigned_value('gist_id: str = ""') == ("gist_id", ' ""')
    assert san._assigned_value("GITHUB_TOKEN = 'x'") == ("GITHUB_TOKEN", " 'x'")
    # 不带引号、冒号后没有 `=` → 不是赋值行
    assert san._assigned_value("gist_id: gist 的 ID") is None
    # 完全不是赋值行
    assert san._assigned_value("# 只是一行注释") is None
    assert san._assigned_value("") is None


# ============================================================
# 规则三：自定义指纹（**清单由使用者提供，仓库里不留真指纹**）
# ============================================================
# 这一段原来钉的是「内置指纹表抓不抓得到」，而那张表本身就不该在公开
# 仓库里 —— 规则要写明它抓什么，写明了就等于公布它保护的结构。
# 因此诱饵数据与依赖它的那几条测试一并移除，本仓库只留机制层的测试。

# ============================================================
# 规则四：内部文档（按文件名拦）
# ============================================================
def test_internal_doc_filenames_are_blocked(tmp_path):
    """内部文档**按文件名**就拦下，不必读内容。"""
    for bad in san.INTERNAL_DOCS[:4]:
        findings = san.scan_paths([f"{bad}.md"])
        assert any(f.lineno == 0 for f in findings), f"{bad} 没被拦下"


# ============================================================
# 规则五：自备黑名单（那条曾经完全瞎掉的规则）
# ============================================================
def test_denylist_hits_are_actually_reported():
    """★ 黑名单命中的**方向**必须真的通 —— 这条规则曾经一次都没报出过。

    它曾经因为 `Finding = tuple` 而在**命中时**抛 TypeError。
    所有「干净文件 → 零命中」的测试都是绿的，因为干净文件根本走不到
    构造 `Finding` 那一行。所以这里必须喂一个**会命中**的输入。
    """
    findings = san.scan_denylist("f.py", "一行\n这里有 某某标的 字样\n", ["某某标的"])
    assert len(findings) == 1
    assert findings[0].lineno == 2
    assert findings[0].snippet == ""


def test_denylist_is_literal_not_regex():
    """黑名单按**字面量**处理，不做正则解释。

    让使用者去写转义只会引入静默失配（写错一个 `\\` 就等于没检查），
    而静默失配正是这个文件要消灭的东西。
    """
    assert san.scan_denylist("f.py", "字面量 a.b\n", ["a.b"])
    assert san.scan_denylist("f.py", "字面量 axb\n", ["a.b"]) == []


def test_denylist_reports_each_term_once_per_file():
    """同一个词在同一文件只报一次 —— 否则一个高频词会刷满屏。"""
    text = "词\n" * 50
    assert len(san.scan_denylist("f.py", text, ["词"])) == 1


def test_denylist_file_itself_is_skipped():
    """★ 黑名单文件自己必须被跳过 —— 它按定义装满了不该出现的东西。

    `--all` 会遍历到它，所以这条跳过是**必需**的，不是优化。
    """
    deny = REPO_ROOT / san.DENY_FILE_DEFAULT
    assert san.scan_paths([san.DENY_FILE_DEFAULT], ["随便一个词"]) == []
    if deny.exists():                     # 本机有这份文件时才真正验到东西
        terms = san.load_denylist()
        assert terms, "黑名单存在却不该是空的"
        assert san.scan_paths([san.DENY_FILE_DEFAULT], terms) == []


def test_load_denylist_handles_comments_and_missing_file(tmp_path):
    """黑名单文件的格式：`#` 注释与空行跳过；文件不存在时返回空表而不是报错。"""
    f = tmp_path / "deny.txt"
    f.write_text("# 注释\n\n词A\n 词B \n", encoding="utf-8")
    assert san.load_denylist(f) == ["词A", "词B"]
    assert san.load_denylist(tmp_path / "不存在.txt") == []


def _run_main(monkeypatch, capsys, argv, no_denylist=True):
    """跑一遍 `main()`，拿到 `(退出码, stdout, stderr)`。

    黑名单走 `SC_DENY_FILE` 指向一个不存在的路径 —— 这是**唯一**能可靠地
    「让黑名单为空」的办法：默认值会去仓库根找真文件，而本机那份是有的。

    ★ stderr 也要收：本文件里好几条断言的对象**只在 stderr 上**（失败原因、
    「这不是通过」这类话）。只看 stdout 的话，那些措辞退化了也测不出来。
    """
    if no_denylist:
        monkeypatch.setenv(san.DENY_FILE_ENV, str(REPO_ROOT / "不存在的黑名单.txt"))
    monkeypatch.setattr(san.sys, "argv", ["sanitize_check.py", *argv])
    rc = san.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_missing_denylist_is_reported_as_incomplete_not_as_pass(monkeypatch, capsys, tmp_path):
    """★ 没加载到黑名单时，结论必须标明**不完整**。

    「零命中」有两种含义：真的干净，或者**根本没检查那一类**。
    两者在终端上长得一样，所以后者必须被显式说出来 —— 否则一个忘了放
    黑名单文件的人会以为自己被完整保护着。

    ★ 这里断言的是**终端上的措辞**，不是某个变量的值。理由：门禁的产物就是
    那几行字，而最后一行是唯一会被记住的。措辞退化（比如把「⚠️ 不完整」
    改回「✅ 通过」）在代码层面看不出任何区别，只在这一层拦得住。
    """
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")

    rc, out, _ = _run_main(monkeypatch, capsys, ["--path", str(clean)])
    assert rc == 0, out
    assert "不完整" in out, f"缺黑名单时必须明说不完整，实际输出：\n{out}"
    assert "✅" not in out, f"不能打绿勾 —— 那是「完整通过」才配用的符号：\n{out}"

    # 反向：有黑名单时同一个文件必须报「完整」通过
    deny = tmp_path / "deny.txt"
    deny.write_text("绝不出现的词\n", encoding="utf-8")
    monkeypatch.setenv(san.DENY_FILE_ENV, str(deny))
    rc, out, _ = _run_main(monkeypatch, capsys, ["--path", str(clean)], no_denylist=False)
    assert rc == 0, out
    assert "✅" in out and "不完整" not in out, f"有黑名单时应当报完整通过：\n{out}"


def test_zero_targets_is_a_failure_not_a_pass(monkeypatch, capsys):
    """★ 待扫文件数为 0 时退出码必须是 2，**不是 0**。

    这是三条「看不见的失效」里最坏的一条的回归测试：仓库还没 `git init` 时
    `git ls-files` 返回空，脚本报「✅ 共 0 个文件，零命中」——一个亮在
    「代码全写好了、检查一次没跑过」那一刻的假绿灯。
    """
    monkeypatch.setattr(san, "_staged_files", lambda: [])
    rc, out, err = _run_main(monkeypatch, capsys, ["--staged"])
    assert rc == 2, f"零文件必须是失败，实际退出码 {rc}，输出：\n{out}"
    assert "这不是通过" in err, f"失败原因必须写在 stderr 上：\n{err}"


def test_unreadable_named_file_is_a_failure_not_a_pass(monkeypatch, capsys, tmp_path):
    """★ `--path` **点名**的文件读不到时，退出码必须是 2，**不能是个绿勾**。

    与上一条同一个病：`scan_paths` 遇到读不到的文件是 `continue`
    （对 `--all` 是对的 —— 那是遍历途中文件恰好消失的竞态），
    但 `--path` 是调用方亲口点名的文件，读不到就不是通过。

    ★ 由实测而来，不是假想：同一个文件，路径用**双引号**经 Git Bash 传进来时
    程序读不到，扫描器照样报「✅ 指定文件共 1 个，零命中」；换成**单引号**
    就正常报红。也就是说路径写法错一点，输出与「真扫过且干净」逐字相同。
    本文件存在的全部理由是「没检查的输出和通过一样」，它不该自己踩这一条。

    ★ 两边都要断言。只测「不存在的路径返回 2」的话，一个**恒返回 2** 的
    实现也能通过 —— 那正是本仓库反复吃过的那种测试（断言写反了，测试就一直
    替 bug 背书）。所以必须配一条真实存在的文件做对照。
    """
    ghost = tmp_path / "并不存在.md"
    rc, out, err = _run_main(monkeypatch, capsys, ["--path", str(ghost)])
    assert rc == 2, f"点名的文件读不到必须是失败，实际退出码 {rc}，输出：\n{out}\n{err}"
    assert "读不到" in err, f"必须说清是哪个文件读不到：\n{err}"
    assert "✅" not in out, f"读不到文件绝不能打绿勾：\n{out}"

    # 对照：同一个调用形态、只把路径换成真文件 → 必须回到 0
    #（这里不要求绿勾：`_run_main` 默认让黑名单为空，那种情况**本来就该**
    # 报「不完整」而不是 ✅ —— 上面那条测试专门盯这个区别。）
    real = tmp_path / "真实存在.md"
    real.write_text("x = 1\n", encoding="utf-8")
    rc, out, err = _run_main(monkeypatch, capsys, ["--path", str(real)])
    assert rc == 0, f"存在的文件应当通过，实际退出码 {rc}，输出：\n{out}\n{err}"
    assert "读不到" not in err, f"文件明明读得到，不该报读不到：\n{err}"


def test_the_documented_invocation_actually_prints_something(tmp_path):
    """★ 用文档里写的那条命令调它，必须**有输出**。

    这是 shebang 那个坑的回归测试，也是本文件最该有的一条 —— 那个坑的形状是：

        首行若有 `#!/usr/bin/env python`，Windows 的 `py` 启动器会去解析它，
        然后**零输出、退出码 49** 地直接死掉。不报错，什么都不说。
        （`python.exe` 调用和 `py -X faulthandler` 都正常 —— 所以极易被当成偶发。）

    而 `py` 恰恰是本项目调 Python 的标准方式 —— 也就是说**按文档调用它的每一次，
    结果都是零输出**，而零输出和「通过」在终端上长得一模一样。

    这条测试只断言「有话说」和「退出码是约定的那几个」：不检查内容，
    因为要拦的就是「静默」本身。
    """
    py = shutil.which("py")
    if py is None:
        pytest.skip("本机没有 py 启动器 —— 而这正是那个坑的触发条件，改用 doc 里的另一条路")

    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")
    proc = subprocess.run(
        [py, "tools/sanitize_check.py", "--path", str(clean)],
        capture_output=True, text=True, cwd=str(REPO_ROOT))

    assert proc.returncode == 0, (
        f"退出码 {proc.returncode} —— 49 就是 shebang 坑复发了。\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}")
    assert proc.stdout.strip(), (
        "零输出 = 和「通过」无法区分。检查器不许有安静的失效路径。")


# ============================================================
# 规则六：自身豁免的**范围**（豁免给宽一点，就是静默漏检）
# ============================================================
def test_self_exemption_covers_rules_but_not_the_denylist():
    """★ 扫描器豁免自己，但**豁免范围只能覆盖规则类，不能连黑名单一起**。

    初版是整体 `continue`，后果具体而荒唐：本文件注释里举的示例代码里
    恰好有一个真标的池成员，而**唯一该发现它的检查正好被豁免掉了**。

    测试用「扫描器源码里确实存在的词」当黑名单词，两个方向各验一次：
      - 空黑名单 → 零命中（证明规则类确实被豁免了）
      - 带该词的黑名单 → 必须报（证明黑名单没被一起豁免）
    """
    self_path = "tools/sanitize_check.py"
    assert san.scan_paths([self_path], []) == [], "规则类应当被豁免"

    term = "SELF_EXEMPT"                  # 这个词就在扫描器源码里
    findings = san.scan_paths([self_path], [term])
    assert findings, (
        "自身豁免把黑名单也一起豁免了 —— 这正是初版那个让真标的池成员漏检的 bug")
    assert all(f.lineno > 0 for f in findings)


# ============================================================
# 规则七：文件清单拿不到时**不许**判通过
# ============================================================
def test_file_listing_failure_raises_instead_of_returning_empty(monkeypatch):
    """★ `git` 失败时必须抛错，**不能返回空表**。

    返回空表会一路走到 `main` 打印「✅ 共 0 个文件，零命中」——
    在仓库还没 git init 的时候（本仓库初建时就是这样）报一个漂亮的假绿灯，
    而那恰好是最危险的时刻：代码全在，检查没跑，输出说通过。
    """
    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(a, returncode=128, stdout="", stderr="not a repo")

    monkeypatch.setattr(san.subprocess, "run", fake_run)
    with pytest.raises(san.ScanInputError):
        san._git_files(["ls-files", "-z"])


def test_all_files_skips_vendored_and_cache_dirs():
    """`--all` 不该走进缓存/依赖目录 —— 那里面的命中没有意义。"""
    files = san._all_files()
    for f in files:
        assert "__pycache__" not in f
        assert not f.startswith(".git/")
        assert ".pytest_cache" not in f


def test_binary_files_are_skipped():
    """二进制文件跳过（读文本会抛 UnicodeDecodeError，不该让整轮挂掉）。"""
    assert ".png" in san.BINARY_EXT
    assert san.scan_paths(["some.png"]) == []


# ============================================================
# 规则八：门禁的**投递方式**也得有人守
# ============================================================
def test_the_pre_commit_hook_invokes_the_scanner_the_documented_way():
    """★ 钩子在，且用文档规定的那种写法调扫描器。

    ★ 这条的来历：README 与 `docs/SANITIZATION.md` 都写着「扫描器装成 git
    pre-commit 钩子」，而 2026-09-20 实测 **`core.hooksPath` 是空的** ——
    钩子文件躺在 `.githooks/` 里，从没挂上过。于是文档声明了一道根本不存在的
    门禁：提交照过，扫描器一次没跑，终端上没有任何异常。
    **这是本仓库反复在防的「声明冒充证据」，只不过这次冒充的是脱敏门禁自己。**

    根因是那句 `git config core.hooksPath .githooks` 只写在**钩子文件内部**，
    没有任何一份文档告诉读者要执行它。

    ## 为什么只做静态断言

    真跑一次钩子要有一个 `sh`，而 pytest 在 Windows 上由 `py` 启动，
    `sh` 只在 PATH 恰好带着 Git 的 `usr/bin` 时才解析得到。
    **一个「有时跳过」的测试等于一个假绿灯** —— 正是本文件在防的东西，
    所以宁可断言得弱一点，也不让它时绿时红。

    真跑一次的验证方法（手工，写在 `docs/SANITIZATION.md`）：往暂存区放一个
    命中项，`git commit` 必须被拒。
    """
    hook = REPO_ROOT / ".githooks" / "pre-commit"
    assert hook.exists(), (
        "`.githooks/pre-commit` 不见了 —— README 与 `docs/SANITIZATION.md` 都"
        "声称扫描器「装成 git pre-commit 钩子」，文件没了那句话就是假的")

    text = hook.read_text(encoding="utf-8")

    assert "py tools/sanitize_check.py --staged" in text, (
        "钩子没有按文档规定的写法调扫描器。\n"
        "★ 必须是 `py tools/sanitize_check.py --staged`：写成 `./tools/...`、"
        "或给扫描器加 shebang，都会让这个钩子**每次都拦住提交并指控有凭据泄漏**"
        "（见 `docs/SANITIZATION.md` 与 `tools/sanitize_check.py` 末尾）。")

    # 命中时必须真的**拒绝**。只打印警告然后放行，门禁就是装饰品。
    assert re.search(r"exit\s+1", text), (
        "钩子里没有 `exit 1` —— 扫描命中时提交不会被拒绝。"
        "一个只会打印警告的钩子在终端上和拦住的钩子长得一样，"
        "而代价是把泄漏放进 git 历史。")

    # 没有出口的门禁会被整体关掉（`--no-verify` 全局化），出口要写在明处。
    assert "--no-verify" in text, (
        "钩子没写「确认误报时怎么绕过」—— 没有出口的门禁，"
        "用户的应对方式是永久绕过它，那时它连误报都拦不住了")


def test_line_endings_are_pinned_to_lf():
    """★ 换行符必须钉成 LF —— 这直接决定那道门禁在别的机器上还活不活着。

    本机 `core.autocrlf=true`。**没有 `.gitattributes` 的话**，`git clone`
    会把 `.githooks/pre-commit` 检出成 CRLF；带 CRLF 的 shell 脚本会被
    `$'\\r'` 干扰，表现出来**不是报错，而是钩子静默失效或每次都拦** ——
    两种都和「钩子没挂上」一样糟，而后者正是上面那条测试刚在防的事。

    `tests/golden/*.json` 同理：那是与券商端比对的线格式样本，
    换行不一致会让「逐字节兼容」在别的平台上**假红**，而假红的常见下场
    是有人把断言放宽，于是真正的漂移以后也拦不住了。

    ★ 这里只能守住「声明还在」+「当前工作区那份确实是 LF」。守不住
    「git 在别的机器上真的照做了」 —— 那种事只有 clone 一次才知道。
    （`.gitattributes` 的每文件规则**优先于** `core.autocrlf`，所以
    `eol=lf` 是能赢的；这一点是它值得写的原因。）
    """
    attrs = REPO_ROOT / ".gitattributes"
    assert attrs.exists(), (
        "`.gitattributes` 不见了 —— 本机 `core.autocrlf=true`，没有它的话"
        "新 clone 出来的 `.githooks/pre-commit` 是 CRLF，脱敏门禁会坏掉")

    text = attrs.read_text(encoding="utf-8")
    assert re.search(r"^\s*\*\s+text=auto\s+eol=lf", text, re.M), (
        "`.gitattributes` 里没有 `* text=auto eol=lf` —— 换行符没被钉住")
    assert ".githooks/" in text, (
        "没有单独为 `.githooks/` 声明 LF —— shell 脚本不该交给 `text=auto` 去判断")

    hook = (REPO_ROOT / ".githooks" / "pre-commit").read_bytes()
    assert b"\r\n" not in hook, (
        "`.githooks/pre-commit` 的工作区副本里已经出现 CRLF —— "
        "shell 脚本带 CRLF 会坏，而它正是脱敏门禁")

    # 线格式样本同样不能带 CRLF（跨平台比对会假红）。
    for golden in sorted((REPO_ROOT / "tests" / "golden").glob("*.json")):
        assert b"\r\n" not in golden.read_bytes(), (
            f"`tests/golden/{golden.name}` 带 CRLF —— "
            f"它是与券商端比对的线格式样本，换行不一致会假红")


# ============================================================
# 收尾：整个工作区必须过
# ============================================================
def test_whole_repo_passes_the_scanner():
    """★ 最后一条：真扫一遍整个工作区。

    前面每一条都在测「某条规则对不对」；这一条测的是**整体结论**。
    它也是唯一能发现「新加的文件里有东西」的测试 ——
    而新加的文件正是最容易带进私货的地方。
    """
    paths = san._all_files()
    assert len(paths) > 20, f"文件数太少（{len(paths)}），扫描可能没生效"
    findings = san.scan_paths(paths, san.load_denylist())
    assert findings == [], (
        "工作区里有命中：\n" +
        "\n".join(f"  {f.path}:{f.lineno} {f.category}" for f in findings))
