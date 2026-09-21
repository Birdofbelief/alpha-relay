# signal-chain

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab.svg)](https://www.python.org/)
[![tests](https://github.com/Birdofbelief/signal-chain/actions/workflows/tests.yml/badge.svg)](https://github.com/Birdofbelief/signal-chain/actions/workflows/tests.yml)
[![Runtime deps](https://img.shields.io/badge/runtime%20deps-requests%20%2B%20pandas-informational.svg)](pyproject.toml)
[![Credentials](https://img.shields.io/badge/credentials-zero%20literals-brightgreen.svg)](docs/SANITIZATION.md)

**把「策略」和「把策略的信号安全送到券商」拆开的框架。**

策略只需要实现一个 `decide(ctx)`；取数、数据质量校验、风控闸门、投递、重试、
回执、持仓对账、调度、心跳、告警，全部由链路负责。

投递后端可拔插（Gist / 本地文件 / 空实现），
策略可从**仓库外路径**加载 —— 你的 alpha 永远不会进这个仓库。

```
零凭据、零网络、零券商也能完整跑通：py -m signalchain.demo
```

> **English** — *signal-chain* decouples a trading strategy from the plumbing that
> delivers its signals safely. Your strategy implements one pure function, `decide(ctx)`;
> the framework owns market data, data-quality gates, risk gates, delivery, retries,
> execution receipts, position reconciliation, scheduling, heartbeats and alerting.
> Delivery backends are pluggable (GitHub Gist / local file / null), and **strategies are
> loaded from paths outside this repository — your alpha never enters it.**
> The chain runs end to end with **zero credentials, zero network and zero broker**:
> `py -m signalchain.demo`. Design notes and reference docs are written in Chinese;
> this README is the entry point.

---

## 目录

- [为什么会有这个东西](#为什么会有这个东西)
- [它解决什么问题](#它解决什么问题)
- [一分钟看懂](#一分钟看懂)
- [三条贯穿全库的主张](#三条贯穿全库的主张)
- [快速上手](#快速上手)
- [跑测试](#跑测试)
- [关于「零凭据」](#关于零凭据)
- [现状与边界](#现状与边界)
- [文档](#文档)
- [环境](#环境)
- [License](#license)

---

## 为什么会有这个东西

这不是一个「我想写个框架」的产物。它是一条**真实在跑的实盘链路**的提取物 ——
原始形态是一个单文件脚本，经 Gist 投递信号、券商端执行。

把它拆出来的理由只有一条：**基础设施可以公开，alpha 不行。**

而在拆的过程中，真正被沉淀下来的其实不是代码，是几个**用实盘亏损换来的判断**：

- 数据出错时该停哪一半 → 按「哪边错了更疼」定，**不按「哪边更保守」**；
- 两个写者同时改同一个 Gist 会怎样 → 后写的**静默**覆盖先写的；
- 重试会不会变成重复下单 → 幂等键必须能从回放里复现。

> 如果「保守」就能得出正确答案，那就不需要判断了。
> 需要判断的地方，恰恰是「保守」会给出错答案的地方。

**所以这个仓库里最值钱的东西不在 `signalchain/` 里，在
[`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md) 里。**

---

## 它解决什么问题

大多数「量化脚本」最后都长成同一个样子：一个几千行的单文件，把三件毫不相干
的事焊在一起 —— **策略逻辑**、**数据管道**、**投递与风控**。后果是连锁的：

- 改一个阈值要读完整份代码；
- 换个投递方式要动策略；
- 风控散落在各处，**谁也不知道一共有几道**，更不知道哪几道是 fail-open 的。

本框架把这条链切成几块，每块一个职责，每块都能单独替换：

| 层 | 职责 | 可换成什么 |
|---|---|---|
| `Strategy` | 你的 alpha。**只实现 `decide(ctx)`，不碰任何 I/O** | 任何策略，包括仓库外的私有策略 |
| `MarketData` | 行情与历史 | 在线接口 / 本地缓存装饰器 / 假数据 |
| `Transport` | 投递与读取 | Gist / 本地文件 / 空实现 |
| `Gate` | 风控（与策略无关） | 三个内置闸门，可增删 |
| `Runner` | 调度、时间预算、失败上限、心跳 | —— |
| `PaperExecutor` | 本地纸面成交（无券商） | 真券商客户端（见[现状与边界](#现状与边界)） |

### 一分钟看懂

```
        ┌──────────────── signal-chain ────────────────┐
私有策略 │                                              │
┌──────┐│   Runner ──┬── Scheduler（任务表 / 补执行窗口）  │
│      ││            ├── TradingCalendar                │
│decide││            └── Heartbeat（严重度只升不降）      │
│  ()  ││                    │                          │
└──┬───┘│                    ▼                          │
   │    │            GateChain（fail-closed 闸门）       │
   │    │                    │                          │
   └───▶│                    ▼                          │
 Signal │            Transport（★ 可拔插）               │
        │             ├─ GistTransport                  │
        │             ├─ FileTransport   ← 演示/测试默认  │
        │             └─ NullTransport   ← dry-run       │
        │                    │                          │
        │     回执 ◀─────────┘                          │
        └──────────────────────────────────────────────┘
                             ▲
                    PaperExecutor（纸面成交，闭环）
```

---

## 三条贯穿全库的主张

### 1. 策略是纯函数

`decide(ctx)` **不发网络请求、不读文件、不写状态**。它只能从 `ctx` 拿数据
（`now` / `calendar` / `market` / `state` / 数据质量标志），只返回 `Signal`。

好处不只是整洁 —— **它让策略可以离线回放**。而「能回放」是「能验证」的前提。
这一条不是靠自觉维持的：`tests/test_strategy_contract.py` 里有一个
**I/O 绊线**，策略一旦碰了文件和网络，测试当场变红。

### 2. 失效方向必须朝「不做」，但**每个闸门朝哪边不一样**

数据不全时默认动作是「停」，不是「照做」。但**「停哪一半」要按「哪边错了更疼」
来定，不是按「哪边更保守」**：

| 闸门 | 触发条件 | 动作 | 为什么是这个方向 |
|---|---|---|---|
| `QuoteIntegrityGate` | 任一标的行情缺失/NaN/非正 | **全部信号停发** | 行情坏了，任何判断都建立在错的价格上。此时卖出同样是错的 —— 你可能卖掉一只本来健康的持仓 |
| `PositionsSyncedGate` | 未能与执行端完成持仓对账 | **禁买，卖出照常** | 读不到持仓时买入 = 可能重复建仓（真金白银的错）；而卖出的风险是「少卖一次」，可恢复 |
| `AuxDataGate` | 关键辅助数据整体缺失 | **禁开新仓，存量照管** | 辅助数据用来挑新标的，缺了它就挑不出，但不影响已有持仓的止盈止损 |

判据是**代价不对称**，不是**保守程度**。这一条是从实盘亏钱里换来的。

### 3. 单一写入者不是风格偏好，是当前唯一能消除竞态的手段

GitHub Gist 的更新是 **read-modify-write，非原子**。两个写者同时改同一个 gist，
后写的会静默覆盖先写的 —— 而「静默」是关键：两边都以为自己写成功了。

所以 channel 的**所有权写在代码里**（`transport/base.py` 的 `CHANNEL_OWNERS`）：

```
signals    ← 只有产生端能写
heartbeat  ← 只有产生端能写
positions  ← 只有执行端能写
executions ← 只有执行端能写
```

`SingleWriterGuard` 包住任意后端，**写错 channel 直接抛异常**，而不是打个警告。
这条约束在运行时被强制，不是靠注释约定。

---

## 快速上手

### 先看它跑

```bash
py -m signalchain.demo
```

零凭据、零网络、零券商。跑完会打印四个 channel 的**真实落盘字节**，
包括执行端回写的持仓与成交回执。

> **Windows 用户：也可以直接双击 `demo.cmd`。**
>
> 双击一个 `.py` 只会让黑框闪一下就没了 —— 窗口在进程退出时立刻关闭，
> 里面打印的东西（不管成功还是报错）你根本来不及看。`demo.cmd` 就是为这个
> 存在的：它跑完会**停住等你按键**，失败时还会把常见原因列出来。
>
> 顺带一提，直接双击 `strategies/demo_rotation.py` 看到的那个黑框也是同一个
> 问题，且那时它其实在报 `ModuleNotFoundError` —— 那个文件是**策略模块**不是
> 脚本，它在这里就等着告诉你该跑哪个命令。

### 作为库用

```python
from signalchain import build_file_chain

chain = build_file_chain("./runtime")   # 文件后端 + 假行情 + 纸面执行器
result, fills = chain.step()            # 执行端先报持仓 → 决策 → 闸门 → 投递 → 回执
print(result.summary())
```

### 换成你自己的策略

策略文件放在**仓库外**，用路径加载：

```python
chain = build_file_chain("./runtime", strategy="./my_private.py")
```

或设环境变量 `SC_STRATEGY_PATH`。`.gitignore` 另外兜了一层
（`strategies/private*`）—— 但真正的保障是**它根本不在这个仓库里**。

一个策略的最小形态（与 [`strategies/demo_rotation.py`](strategies/demo_rotation.py) 同结构，
`tests/test_strategy_contract.py` 里有一份等价的实现被真的跑过）：

```python
from signalchain import Strategy, ACTION_BUY

class MyStrategy(Strategy):
    name = "my_strategy"

    def universe(self):
        return ["510050", "159919"]        # 不带后缀，链路自己补

    def warmup_bars(self):
        return 60                          # 链路据此决定历史缓存拉多长

    def decide(self, ctx):
        if not ctx.data.quote_ok:          # 数据质量是策略**能看到**的
            return []
        for code in self.universe():
            if ctx.price(code) is None:    # 停牌 → 只跳过这一只
                continue
            hist = ctx.try_history(code, 30)
            if hist is None:               # 取不到历史 → 同样只跳这一只
                continue
            closes = hist["close"].astype(float).tolist()
            if closes[-1] > closes[0]:
                yield ctx.signal(code, ACTION_BUY, amount=1000)
```

几个值得注意的点：

- `ctx.history()` 返回 **DataFrame**，取不到会抛异常；`ctx.try_history()` 是
  「取不到算 `None`」的版本。**不要吞掉那个异常** —— 一只标的取不到历史和
  「本轮没有信号」长得一模一样，吞掉就成了一次静默的漏判。
- `ctx.signal()` 而不是自己 `Signal(...)`：它把时间戳取成 `ctx.now` 而不是墙上时钟。
  这一点直接决定**可回放性** —— 信号 id 是幂等键，掺了墙上时钟的话，
  回放同一天的行情会生成一批全新 id，「这条处理过没有」就永远答「没有」。
- `decide()` 可以返回列表，也可以是生成器。

### 它实际跑起来是什么样

下面是 `py -m signalchain.demo` 的真实输出（只做了两处删减：临时目录路径、
与本节无关的重复轮次）。四段值得注意的地方：

**① 所有权绑在「角色」上，不是绑在「进程」上。** 纸面执行器就在同一个进程里，
但产生端身份的 guard 依然写不了 `positions`：

```
  role=producer | 策略=demo_rotation(池3只/预热30根) | 行情=fake(3只) | 传输=file:…\channels

  → 实测：产生端试写 positions channel ⇒ OwnershipError
     拒绝写入 channel 'positions'：它属于 executor，而本进程的角色是 producer。
```

**② 回执有两种互斥形状，同一个数组里按 `status` 分辨。** 取错字段**不会报错**，
只会静默拿到 `None` 或 `0`：

```json
[
  {
    "signal_id": "20260918144500_510050.XSHG_buy",
    "code": "510050.XSHG", "action": "buy",
    "status": "filled",
    "order_id": "paper-20260918144500_510050.XSHG_buy",
    "price": 1.034, "amount": 9600,
    "time": "2026-09-18 14:45:00"
  },
  {
    "signal_id": "20260918154500_510500.XSHG_buy",
    "code": "510500.XSHG", "action": "buy",
    "status": "submitted",
    "reason": "市价 1.1040 高于限价 0.9936，挂单等待",
    "time": "2026-09-18 15:45:00"
  }
]
```

> ★ 终态（`filled` / `canceled`）带 `order_id` / `price` / `amount`，**没有 `reason`**；
> 非终态（`submitted` / `skipped`）带 `reason`，**没有 `order_id` / `price` / `amount`**。
> 详见 [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md)。

**③ 三个闸门的失效方向故意不一样。** 同一条买入信号，在不同数据质量下的判定：

```
  一切正常                       → 买入✅  卖出✅
  行情失效（缺失/NaN/非正）            → 买入⛔  卖出⛔
  未完成持仓对账                    → 买入⛔  卖出✅
  辅助数据整体缺失                   → 买入⛔  卖出✅
```

闸门链刻意**不短路**：一条信号同时触发多道时，理由会全部记下。

**④ 闭环的终点是真实字节。** 跑完之后目录里躺着的是：

```
    executions.json  (1038 字节)
    heartbeat.json  (88 字节)
    positions.json  (216 字节)
    trade_signals.json  (953 字节)
```

完整输出有 182 行，每次跑都会打印 —— 直接跑一遍最快。`--keep` 可以保留运行目录。

---

## 跑测试

```bash
py -m pytest              # 238 项，全部离线，不需要任何凭据
```

同一套测试也跑在 CI 上（Ubuntu + Windows × Python 3.10 / 3.12 / 3.13，
见 [`.github/workflows/tests.yml`](.github/workflows/tests.yml)）——
顶部那个徽章就是它的**实时结果**，不是手写的。

CI 里还会多跑一遍脱敏扫描：本地那个 pre-commit 钩子依赖 `core.hooksPath`，
而那是**本机配置、不随 clone 带走** —— 也就是说别人克隆下来默认是不设防的，
所以服务端补一道。

> 上面那个 **238** 是手工写的，会随代码变化。**以实测为准**：
> `py -m pytest` 看退出码（0 = 全过）。

测试覆盖面（值得单说的几组）：

| 文件 | 在守什么 |
|---|---|
| `test_transport_contract.py` | 同一套用例跑遍所有后端 —— 这是「可拔插」的**证据**，不是口号 |
| `test_wire_contract.py` | **线格式冻结**：字段名、后缀、时间格式与既有券商端逐字节兼容 |
| `test_strategy_contract.py` | 策略纯度绊线（碰 I/O 即红）+ 从仓库外加载策略 |
| `test_end_to_end.py` | 接缝：读磁盘上的**真实字节**，不复用解析器 |
| `test_sanitize_rules.py` | **脱敏扫描器自己的测试** —— 一个只会静默失效的检查器 |
| `test_docs_tripwire.py` | 文档里承诺的 API 真的存在（并且真的能跑） |
| `test_fake_determinism.py` | 假行情必须**逐值钉死**，而且**换进程、换 hash 种子也一样** —— 否则「回放」是在掷骰子 |

---

## 关于「零凭据」

这个仓库**不含任何凭据字面量**，且这不是靠人工检查维持的：

- `tools/sanitize_check.py` —— 一个脱敏扫描器，覆盖凭据形状、私有策略指纹、
  内部文档、以及一份**你自己提供的黑名单**（用来查「我的标的池代码有没有泄漏」，
  这是模式匹配猜不到、却最该查的一类）。
- 装成 **git pre-commit 钩子**，命中即拒绝提交。**克隆后要手动开一次**
  （`core.hooksPath` 是本机配置，不随克隆带走）：

  ```bash
  git config core.hooksPath .githooks
  ```

  没开的话扫描器**一次都不会跑**，而提交看起来完全正常 —— 正是下面说的
  那种「失效和通过长得一模一样」。
- 它的每一条规则都有**反向测试**（`test_sanitize_rules.py`）：不只测「干净输入
  不报」，还测「脏输入必须报」。

> ★ 为什么检查器需要被检查：它的失效方式和它的通过方式**长得一模一样**
> （都是零命中、都是绿色）。本项目的扫描器在写出来的当天，三条规则**全是瞎的** ——
> 详见 [`docs/SANITIZATION.md`](docs/SANITIZATION.md)。

`test_end_to_end.py` 里还有一条可执行证明：**清空所有相关环境变量后，
整条链仍然端到端跑通**。

### 凭据从哪来

源码里一个都没有，全部从环境变量读。模板见 [`.env.example`](.env.example)
（**它自己必须永远是空的** —— 它会被提交进公开仓库）：

```bash
cp .env.example .env      # 然后填自己的值；.env 已被 .gitignore 挡住
```

`SC_GIST_ID` / `SC_GITHUB_TOKEN` / `SC_SERVERCHAN_KEY` / `SC_STRATEGY_PATH`
等变量的含义都写在那个模板里。不需要 Gist 后端的话一个都不用填。

---

## 现状与边界

写清楚这个项目**现在是什么**，比写它能做什么更重要。

| 项 | 说明 |
|---|---|
| **设计来源** | 这套分层、闸门方向、单一写入者约定，来自一个**真实在跑的实盘系统**（单文件形态，经 Gist 投递、券商端执行）。文档里那些「为什么」不是推演出来的。 |
| **本仓库的状态** | ⚠️ **它是从上面那套系统里独立提取出来的，尚未替换线上。** 也就是说：**设计经过实战，这份提取产物本身还没有在生产上跑过。** 请按「可用的框架 + 未经实战的组装」看待它。 |
| **测试证明了什么** | 238 项全离线，验证的是**链路的正确性**（幂等、闸门方向、线格式、所有权）。它**不验证任何策略的收益**。 |
| **没有的东西** | 没有券商客户端 —— 仓库里只有 `PaperExecutor`；没有仓库内的 HTTP 投递后端（该方案被否决过，且**没有留空壳接口**：一个不能用的类不是扩展点，是噪音。要加只需实现 `Transport` 的三个方法）；没有 Web UI、没有数据库、没有回测引擎。 |
| **单进程假设** | 「单一写入者」是**编码约定 + 运行时断言**，不是分布式锁。真要多进程并发写同一后端，得自己在外面加锁。 |
| **已知缺口** | 产生端不去重（依赖「同秒同码同向只推一次」，在单进程下成立）。**缺口是写进 [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md) 的，不是悄悄补上的。** |

### 如果只能记一条

> **每个「失败时怎么办」都要问一遍「反过来做会怎样」，并把这个判断写进代码旁边。**

本仓库所有 fail-closed 的方向都不是从「保守」推出来的，是从**「哪边错了更疼」**
推出来的。两者的结论在三个闸门里**全都不一样** —— 这正是这条规则值得单独存在的理由：

> 如果「保守」就能得出正确答案，那就不需要判断了。
> 需要判断的地方，恰恰是「保守」会给出错答案的地方。

更多取舍见 [`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md)。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 分层、依赖方向、一轮的完整时序 |
| [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md) | **线格式契约**：四个 channel 的字段、后缀、时间格式，以及那些「看起来该统一、实际不能统一」的地方 |
| [`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md) | 设计主张与取舍：失效方向、幂等、时间预算、网络韧性 |
| [`docs/SANITIZATION.md`](docs/SANITIZATION.md) | 脱敏清单 + 扫描器用法 + **黑名单怎么建** |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | 从「单文件脚本」迁到本框架的路径；也写了**什么时候不该迁** |

## 环境

- Python ≥ 3.10
- 运行时依赖只有 `requests` + `pandas`
- `akshare` 是**可选**依赖（只有 `TradingCalendar.refresh()` 需要；不装可用静态日历）
- 使用方式是「clone 下来直接跑」，**不需要 `pip install`**（见 [`pyproject.toml`](pyproject.toml)）

## License

MIT —— 见 [`LICENSE`](LICENSE)。
