# 发布清单

> 这份清单管的是**「把一个版本正式发出去」**这件事：改版本号、打标签、推送、回读验证。
> 它不管日常提交 —— 日常改动直接写进 `CHANGELOG.md` 的「未发布」段、本地提交即可。
>
> ★ **三步是对外动作，必须逐次确认**：`git push`、`git push --tags`、`gh release create`。
> 本仓库没有「自动发布」。`tools/check_version.py` 只做检查，**不会替你打标签**。

---

## 0. 什么时候该发版

- 「未发布」段攒到值得单独说一版的程度（通常是行为变化，不是零星错别字）；
- 或者有别人要按版本号依赖它（`pip install signal-chain==X.Y.Z`）。

**版本号规则（0.x 阶段）**

| 改了什么 | 升哪一位 |
|---|---|
| 改公开 API / 改变可观察行为（默认值、闸门方向、失败语义、线格式） | **minor**（0.1.x → 0.2.0） |
| 纯内部修复，对外行为不变 | **patch**（0.1.0 → 0.1.1） |

**不要直接跳 1.0.0**：API 还没冻结，0.x 本身就是「可能变」的承诺。
下一版号在发版当天由仓库所有者确认 —— 清单里不预先写死。

---

## 1. 发版前检查

```bash
git status                     # 工作区必须干净
python -m pytest               # 必须全绿
py tools/sanitize_check.py --all   # 退出码必须是 0
```

> ★ 脱敏的退出码有三种：**0 = 干净 / 1 = 命中 / 2 = 结果无效**。
> **`2` 按失败处理** —— 它意味着扫描器自己没能得出有效结论（读不到清单、路径错等），
> 不是「差不多等于通过」。

---

## 2. 改版本号（一个提交里做完三件事）

1. `signalchain/__init__.py` 的 `__version__` —— **这是唯一手写的版本号**；
   `pyproject.toml` 是 `dynamic = ["version"]`，不要在那里再写一份。
2. `CHANGELOG.md`：把顶部的 `## 未发布（…）` 改成 `## vX.Y.Z — YYYY-MM-DD`。
3. **不要**在这个提交里混入功能改动 —— 否则「这一版到底改了什么」失去边界。

```bash
python tools/check_version.py   # 本地先跑一遍，应为 0
git add signalchain/__init__.py CHANGELOG.md
git commit -m "发版 vX.Y.Z"
```

---

## 3. 打标签

**用附注标签（annotated tag），说明走文件。**

```bash
# 中文说明不要用 -m，某些终端会把编码搞坏；写成文件再 -F
git tag -a vX.Y.Z -F release-notes.txt
git tag -l "vX.Y.Z"        # 本地确认
```

> ★ 标签名必须与 `__version__` 完全一致（`v` + 版本号）。
> `tools/check_version.py` 在 CI 里会比对这一条。

---

## 4. 推送（**每一步都要单独确认**）

```bash
git push origin master        # ① 代码
git push origin vX.Y.Z        # ② 标签 —— 必须单独推
```

> ★ **`git push` 默认不推标签。** 本仓库历史上真发生过「代码推了、标签没推」，
> 结果是远端看起来停在上一版。所以标签单独一条命令，且要用
> `git ls-remote --tags origin` 回读确认。

（可选，对外可见）

```bash
gh release create vX.Y.Z --notes-file release-notes.txt
```

---

## 5. 回读验证

发完不是结束，要**从远端回读**：

```bash
git ls-remote --tags origin              # 标签在不在
git ls-remote origin master              # 代码到没到
```

再看两件事：

- CI（`.github/workflows/tests.yml`）全绿，含 **版本一致性** 那个 job；
- 在一个**全新目录**里 `git clone` + `pip install .`，
  `python -c "import signalchain; print(signalchain.__version__)"` 打出 `X.Y.Z`，
  `python -m signalchain.demo --quiet` 能跑完。

> 「本地装得上」不算验证：本地可能有旧的构建产物或 editable 安装。

---

## 6. 发完之后

- 在 `CHANGELOG.md` 顶部新开一个 `## 未发布（…）` 段（空标题即可）——
  否则下一次改动无处可写，就会有人直接改已发布版本的标题。
- 如果这次发版包含**从本框架回灌到云上**的修复，记进云侧部署映射表
  （版本/提交 → 云上产物 → sha256 → 回滚点 → 验证日期），
  **云上任何写操作都要单独确认**。
