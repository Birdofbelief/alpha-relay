# -*- coding: utf-8 -*-
"""版本一致性检查：**一个版本号只有一个来源**，且 tag / CHANGELOG 跟得上。

    python tools/check_version.py

退出码（与 `tools/sanitize_check.py` 同一套约定）：

    0 = 全部一致
    1 = 有违反（打印每一条）
    2 = 结果无效 —— **按失败处理**（比如 pyproject 解析不出来、git 状态读不到）

## 三条不变式

1. `signalchain.__version__` 是**唯一手写**的版本号；
   `pyproject.toml` 必须是 `dynamic = ["version"]`，不能再写一份 `version = "..."`。
2. 本机装了包时，`importlib.metadata.version("signal-chain")` 必须与 `__version__` 相等
   —— 否则「源码里是 A、装出来的是 B」，而症状是 `pip show` 和人看到的代码对不上。
3. **HEAD 正好在 `vX.Y.Z` 标签上 ⇒ `__version__` 必须是 X.Y.Z，且 CHANGELOG 有
   `## vX.Y.Z` 标题；HEAD 不在标签上 ⇒ CHANGELOG 顶部必须仍是「未发布」。**

## 为什么第 3 条值得单独存在

本仓库踩过「标签没推上去」（`交接.md`）。也踩过反过来的：CHANGELOG 写了一个版本标题，
但代码里还停在旧号。两种错都不会让程序崩，只会让「这一版到底改了什么」失去边界 ——
而这种边界一旦丢了，回滚时就不知道回滚到哪儿。

★ 本脚本**只检查，不自动打 tag、不自动发 Release** —— 发布是用户的动作。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_UNRELEASED = "未发布"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dunder_version(root: Path) -> str | None:
    text = _read(root / "signalchain" / "__init__.py")
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _project_block(text: str) -> str | None:
    """`[project]` 段落的原文。没有这段就返回 None（结构读不懂）。"""
    m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text,
                  re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def _pyproject_problems(root: Path) -> list[str]:
    text = _read(root / "pyproject.toml")
    block = _project_block(text)
    if block is None:
        raise ValueError("pyproject.toml 里找不到 [project] 段")
    problems = []
    if re.search(r"^version\s*=", block, re.MULTILINE):
        problems.append(
            "[project] 里又写了一份 version —— 版本号只能有一个来源"
            "（signalchain.__version__）；请改成 dynamic = [\"version\"]")
    m = re.search(r"^dynamic\s*=\s*\[(.*?)\]", block, re.MULTILINE | re.DOTALL)
    dynamic = m.group(1) if m else ""
    if '"version"' not in dynamic and "'version'" not in dynamic:
        problems.append(
            "[project] 的 dynamic 里没有 \"version\" —— 打包时版本号无处可取")
    return problems


def _installed_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:      # pragma: no cover - 3.8 以下不会走到
        return None
    try:
        return version("signal-chain")
    except PackageNotFoundError:
        return None


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(root),
                          capture_output=True, encoding="utf-8",
                          errors="replace")
    return proc.returncode, (proc.stdout or "").strip()


def _changelog_headings(root: Path) -> list[str]:
    out = []
    for line in _read(root / "CHANGELOG.md").splitlines():
        if line.startswith("## "):
            out.append(line[3:].strip())
    return out


def _tag_problems(root: Path, version: str) -> list[str]:
    """HEAD 与 tag / CHANGELOG 的关系。不是 git 仓库时跳过（装包场景没有 .git）。"""
    rc, _ = _git(root, "rev-parse", "--git-dir")
    if rc != 0:
        print("ℹ️ 不是 git 仓库 —— 跳过 tag 检查（装成包之后本来就没有 .git）")
        return []

    headings = _changelog_headings(root)
    if not headings:
        raise ValueError("CHANGELOG.md 里一个 `## ` 标题都没有")

    problems = []

    # ★ **每一个** vX.Y.Z 标签都要在 CHANGELOG 里有对应标题，不只是 HEAD 那个。
    #   只查 HEAD 会漏掉「历史上有过 v0.1.0，但 CHANGELOG 被重写时弄丢了」——
    #   那种情况下别人翻旧版本根本查不到那一版改了什么。
    for t in _version_tags(root):
        if not any(h.startswith(t) for h in headings):
            problems.append(
                f"标签 {t} 存在，但 CHANGELOG.md 里没有 `## {t}` 标题")

    rc, tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
    at_tag = rc == 0 and tag.startswith("v")

    if at_tag:
        expected = f"v{version}"
        if tag != expected:
            problems.append(
                f"HEAD 在标签 {tag} 上，但 __version__ 是 {version}"
                f"（应为 {expected[1:]}）")
    else:
        if _UNRELEASED not in headings[0]:
            problems.append(
                f"HEAD 不在版本标签上，但 CHANGELOG.md 顶部是「{headings[0]}」"
                f" —— 未发布的改动必须先落在「{_UNRELEASED}」段。"
                f"**如果这是发版提交，那是下一步没做完：先创建并推送 tag**"
                f"（顺序见 docs/RELEASING.md；tag 要单独 `git push origin vX.Y.Z`）")
    return problems


_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _version_tags(root: Path) -> list[str]:
    """所有 `vX.Y.Z` 形状的标签。其它形状（试验性的、别名的）不参与检查。"""
    rc, out = _git(root, "tag", "--list", "v*")
    if rc != 0:
        return []
    return [t.strip() for t in out.splitlines() if _TAG_RE.match(t.strip())]


def check(root: Path = ROOT) -> list[str]:
    """跑完全部不变式，返回问题列表（空 = 通过）。结构读不懂时抛 `ValueError`。"""
    version = _dunder_version(root)
    if not version:
        raise ValueError("signalchain/__init__.py 里找不到 `__version__ = \"...\"`")

    problems = _pyproject_problems(root)

    installed = _installed_version()
    if installed is not None and installed != version:
        problems.append(
            f"已安装的包版本是 {installed}，源码里是 {version}"
            " —— `pip show` 和人看到的代码不是同一版")

    problems.extend(_tag_problems(root, version))
    return problems


def main() -> int:
    try:
        problems = check()
    except (OSError, ValueError) as e:
        print(f"★ 结果无效：{e}")
        return 2
    if problems:
        print("★ 版本一致性检查未通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✅ 版本一致：{_dunder_version(ROOT)}"
          f"（唯一来源 signalchain.__version__）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
