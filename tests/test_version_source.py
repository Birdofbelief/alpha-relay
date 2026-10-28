# -*- coding: utf-8 -*-
"""版本号单一来源与 tag / CHANGELOG 的一致性。

## 这一组防的是什么

版本号分叉不会让程序崩。它只会让「这一版到底改了什么」失去边界：

  - `pyproject.toml` 写一份 `0.1.0`、`__init__.py` 写一份 `0.1.1`
    ⇒ `pip show` 和源码对不上，回滚时不知道回滚到哪儿
  - HEAD 在 `v0.2.0` 标签上，但 CHANGELOG 还挂着「未发布」
    ⇒ 别人看 CHANGELOG 完全不知道 0.2.0 是什么

`tools/check_version.py` 在 CI 里盯着这三条。**这里测的是那个检查器本身** ——
一个永远返回 0 的检查器和没有检查器，在输出里长得一模一样。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_version", ROOT / "tools" / "check_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cv = _load()


# ------------------------------------------------------------
# 现状：本仓库自己的文件必须过
# ------------------------------------------------------------
def test_this_repository_passes():
    assert cv.check(ROOT) == []


# ------------------------------------------------------------
# pyproject 不能有第二份版本号
# ------------------------------------------------------------
def _pyproject(tmp_path, body: str) -> pathlib.Path:
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_static_version_in_project_is_a_violation(tmp_path):
    root = _pyproject(tmp_path, '[project]\nname = "x"\nversion = "0.1.0"\n')
    problems = cv._pyproject_problems(root)
    assert any("version" in p for p in problems)


def test_dynamic_without_version_is_a_violation(tmp_path):
    root = _pyproject(tmp_path, '[project]\nname = "x"\ndynamic = ["readme"]\n')
    problems = cv._pyproject_problems(root)
    assert any("dynamic" in p for p in problems)


def test_dynamic_version_is_accepted(tmp_path):
    root = _pyproject(tmp_path, '[project]\nname = "x"\ndynamic = ["version"]\n')
    assert cv._pyproject_problems(root) == []


# ------------------------------------------------------------
# `__version__` 读不出来 = 结果无效，不是「通过」
# ------------------------------------------------------------
def test_missing_dunder_version_raises(tmp_path):
    (tmp_path / "signalchain").mkdir()
    (tmp_path / "signalchain" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cv.check(tmp_path)


# ------------------------------------------------------------
# tag / CHANGELOG 的一致性（用假的 git 回答）
# ------------------------------------------------------------
def _fake_git(monkeypatch, *, at_tag: str | None):
    def git(root, *args):
        if args[0] == "rev-parse":
            return 0, ".git"
        if args[0] == "tag":
            return 0, at_tag or ""
        if args[0] == "describe":
            return (0, at_tag) if at_tag else (1, "")
        return 1, ""
    monkeypatch.setattr(cv, "_git", git)


def _changelog(tmp_path, heading: str):
    (tmp_path / "CHANGELOG.md").write_text(
        f"# 变更记录\n\n---\n\n## {heading}\n\n正文\n", encoding="utf-8")


def test_head_on_a_tag_needs_the_matching_version(tmp_path, monkeypatch):
    _fake_git(monkeypatch, at_tag="v0.2.0")
    _changelog(tmp_path, "v0.2.0 — 2026-10-28")

    problems = cv._tag_problems(tmp_path, "0.1.0")
    assert any("0.2.0" in p and "0.1.0" in p for p in problems)


def test_head_on_a_tag_needs_the_changelog_heading(tmp_path, monkeypatch):
    _fake_git(monkeypatch, at_tag="v0.2.0")
    _changelog(tmp_path, "未发布（`v0.2.0` 之后）")

    problems = cv._tag_problems(tmp_path, "0.2.0")
    assert any("CHANGELOG" in p for p in problems)


def test_head_on_a_tag_with_matching_heading_passes(tmp_path, monkeypatch):
    _fake_git(monkeypatch, at_tag="v0.2.0")
    _changelog(tmp_path, "v0.2.0 — 2026-10-28")

    assert cv._tag_problems(tmp_path, "0.2.0") == []


def test_head_not_on_a_tag_requires_an_unreleased_section(tmp_path, monkeypatch):
    _fake_git(monkeypatch, at_tag=None)
    _changelog(tmp_path, "v0.2.0 — 2026-10-28")

    problems = cv._tag_problems(tmp_path, "0.2.0")
    assert any("未发布" in p for p in problems)


def test_head_not_on_a_tag_with_unreleased_section_passes(tmp_path, monkeypatch):
    _fake_git(monkeypatch, at_tag=None)
    _changelog(tmp_path, "未发布（`v0.1.0` 之后）")

    assert cv._tag_problems(tmp_path, "0.1.0") == []


def test_a_version_ahead_of_the_last_tag_is_allowed_before_release(tmp_path, monkeypatch):
    """★ 发布前的中间状态必须合法：`__version__` 已经是 0.2.0，但最后一个 tag 还是
    v0.1.0、CHANGELOG 也还是「未发布」。

    这一条钉的是**流程**：改版本号与打标签是两步，中间会停留好几天。
    如果检查器只接受「版本号 == 最近 tag」，那这段时间 CI 会一直红，
    红久了就有人把它改为「允许不一致」—— 检查器就此失效。
    """
    _fake_git(monkeypatch, at_tag=None)
    _changelog(tmp_path, "未发布（`v0.1.0` 之后）")

    assert cv._tag_problems(tmp_path, "0.2.0") == []


def _fake_git_with_tags(monkeypatch, tags: list[str], *, at_tag: str | None = None):
    def git(root, *args):
        if args[0] == "rev-parse":
            return 0, ".git"
        if args[0] == "tag":
            return 0, "\n".join(tags)
        if args[0] == "describe":
            return (0, at_tag) if at_tag else (1, "")
        return 1, ""
    monkeypatch.setattr(cv, "_git", git)


def test_every_version_tag_needs_a_changelog_heading(tmp_path, monkeypatch):
    """★ 不只是 HEAD 那个标签 —— **历史上每个版本**都要能在 CHANGELOG 里查到。

    只查 HEAD 会漏掉「CHANGELOG 被重写时弄丢了旧版本标题」：标签还在，
    别人翻那一版却查不到改了什么。
    """
    _fake_git_with_tags(monkeypatch, ["v0.1.0", "v0.2.0"], at_tag=None)
    _changelog(tmp_path, "v0.1.0 — 2026-09-21")

    # 顶部不是「未发布」，所以除了 v0.2.0 缺标题，还会报未发布那条
    problems = cv._tag_problems(tmp_path, "0.2.0")
    assert any("v0.2.0" in p and "CHANGELOG" in p for p in problems)
    assert not any("`## v0.1.0`" in p for p in problems)


def test_non_semver_tags_are_ignored(tmp_path, monkeypatch):
    _fake_git_with_tags(monkeypatch, ["v0.1.0", "experiment", "v0.3"], at_tag=None)
    _changelog(tmp_path, "v0.1.0 — 2026-09-21")

    problems = cv._tag_problems(tmp_path, "0.2.0")
    assert not any("experiment" in p or "v0.3" in p for p in problems)


def test_the_not_on_a_tag_error_points_at_the_release_step(tmp_path, monkeypatch):
    """发版提交那一刻最容易看到这条 —— 报错必须说清「下一步是打 tag」。"""
    _fake_git_with_tags(monkeypatch, [], at_tag=None)
    _changelog(tmp_path, "v0.2.0 — 2026-10-28")

    problems = cv._tag_problems(tmp_path, "0.2.0")
    assert any("tag" in p for p in problems)
