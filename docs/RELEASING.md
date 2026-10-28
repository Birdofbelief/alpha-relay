# 发布清单

> 这份清单管的是**「把一个版本正式发出去」**这件事：改版本号、打标签、推送、回读验证。
> 它不管日常提交 —— 日常改动直接写进 `CHANGELOG.md` 的「未发布」段、本地提交即可。
>
> ★ **三步是对外动作，必须逐次确认**：`git push`、`git push origin vX.Y.Z`、`gh release create`。
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

> ★ **版本号可以提前提**（本仓库当前就是：`__version__` 已是 `0.2.0`，但没有 tag、
> CHANGELOG 仍是「未发布」）。这个中间状态是合法的，`check_version` 明确接受。
> 提前提号的作用是让打包出来的 wheel 不叫「0.1.0」—— 发版当天只剩改标题和打标签两步。

---

## 1. 发版前检查

```bash
git status                     # 工作区必须干净
python -m pytest               # 必须全绿
py tools/sanitize_check.py --all   # 退出码必须是 0
```

> ★ 脱敏的退出码有三种：**0 = 干净 / 1 = 命中 / 2 = 结果无效**。
> **`2` 按失败处理** —— 它意味着扫描器自己没能得出有效结论，不是「差不多等于通过」。
>
> ⚠️ **这一步不要跑 `check_version`**（除非你已经打好了 tag）。
> 它看到「HEAD 不在标签上、CHANGELOG 顶部却不是未发布」会红 ——
> 那不代表文件错了，只代表**发版还没走完**。顺序见下面 §2–§4。

---

## 2. 改版本号与 CHANGELOG（一个提交）

**分两种起点，做法不同：**

**A. 版本号还没提前提（常规）** —— 三件事同一个提交：

1. `signalchain/__init__.py` 的 `__version__` —— **这是唯一手写的版本号**；
   `pyproject.toml` 是 `dynamic = ["version"]`，不要在那里再写一份。
2. `CHANGELOG.md`：把顶部的 `## 未发布（…）` 改成 `## vX.Y.Z — YYYY-MM-DD`。
3. 提交。

```bash
git add signalchain/__init__.py CHANGELOG.md
git commit -m "发版 vX.Y.Z"
```

**B. 版本号已经提前提好（本仓库当前的 0.2.0 就是这种）** —— 只改 CHANGELOG 标题：

```bash
# 只把「## 未发布（…）」改成「## v0.2.0 — YYYY-MM-DD」
git add CHANGELOG.md
git commit -m "发版 v0.2.0"
```

> ★ **不要**在这个提交里混入功能改动 —— 否则「这一版到底改了什么」失去边界。
>
> ⚠️ **先别急着跑 `check_version`**：此刻 tag 还不存在，它必然报
> 「HEAD 不在版本标签上 / CHANGELOG 顶部不是未发布」。那是**预期的中间态**。

---

## 3. 打标签（紧接着同一个提交）

**用附注标签（annotated tag），说明走文件。**

```bash
# 中文说明不要用 -m，某些终端会把编码搞坏；写成文件再 -F
git tag -a vX.Y.Z -F release-notes.txt
git tag -l "vX.Y.Z"        # 本地确认
```

> ★ 标签名必须与 `__version__` 完全一致（`v` + 版本号）。

---

## 4. 现在再跑一致性检查

```bash
python tools/check_version.py   # 应该退出 0
```

它检查三条：单一版本源、已安装包版本一致、**每个 `vX.Y.Z` 标签都有对应的
`## vX.Y.Z` 标题**。到这一步才可能全绿 —— 因为 tag 刚刚才存在。

---

## 5. 推送（**每一步都要单独确认**）

**先推标签，再推代码**（或者一次推两个 ref）：

```bash
# 推荐：一次推送，CI 在同一个 ref 集合里同时看到 master 和 tag
git push origin master vX.Y.Z

# 也可以分两步，但**必须先推 tag**
git push origin vX.Y.Z
git push origin master
```

> ★ **顺序不能反。** 先推 `master` 会让那次 CI 在远端还看不到 tag 的情况下跑
> `check_version` —— 分支上留下一个**红色 run**，而代码其实没问题。
> 先推 tag 就不存在这个窗口。
>
> ★ **`git push` 默认不推标签。** 本仓库历史上真发生过「代码推了、标签没推」。
> **不要用 `git push --tags`** —— 它会把所有本地标签都推上去，包括试验性的那个。

（可选，对外可见）

```bash
gh release create vX.Y.Z --notes-file release-notes.txt
```

---

## 6. 回读验证

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

## 7. 发完之后

- 在 `CHANGELOG.md` 顶部**新开**一个 `## 未发布（…）` 段（空标题即可）——
  否则下一次改动无处可写，就会有人直接改已发布版本的标题。
- 如果这次发版包含**从本框架回灌到云上**的修复，记进云侧部署映射表
  （`F:\信念鸟\信念鸟本地信号源_akshare\运维备忘.md` 第十二节），
  **云上任何写操作都要单独确认**。
