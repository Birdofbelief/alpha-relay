# 架构

## 一、分层与依赖方向

依赖**只向下**，没有环。这不是靠自觉 —— 它是可核对的：
下面每一行都由「这个模块 `from ..` 导入了谁」机械得出，改坏了立刻看得出来。

| 层 | 模块 | 依赖谁 |
|---|---|---|
| **L0 叶子**（零包内依赖） | `models` `config` `calendar` `notify` `executor` `marketdata/base` `transport/base` | —— |
| **L1 零件** | `gates` `store` | `models`（+ `transport/base`） |
| **L1 后端实现** | `marketdata/{cache,online,fake}` `transport/{file,gist,null}` | `base` 家族 |
| **L1 后端加载** | `transport/__init__`（`load_transport`） | `base` 家族 + `config` |
| **L2 角色** | `strategy` `paper` | `calendar` / `gates` / `marketdata/base` / `models` / `store` |
| **L3 调度** | `runner` | `strategy` `store` `gates` `notify` `transport` |
| **L4 装配** | `chain` | **全部** |

> `chain` 是唯一允许「什么都知道」的模块，因为它**只做装配**，不含业务判断。
> 这条界线是这套分层是否成立的判据：一旦 `chain` 里出现 `if 行情坏了 then ...`，
> 判断就跑到装配层去了，分层开始漏水。

### `models` 与 `config` 为什么必须在最底下

它们被所有层引用。**一旦它们反向依赖任何东西，环就不可避免。**
所以 `models` 里只有数据结构与序列化，`config` 里只有配置读取 —— 两者都不知道
「策略」「闸门」「传输」是什么。

---

## 二、模块地图

```
signalchain/
├── models.py        四个 channel 的数据结构 + JSON 序列化（★ 线格式的唯一真相源）
├── config.py        配置（环境变量 → Config；含凭据读取）
│
├── calendar.py      交易日历（静态表 / 星期推算 / 可选 akshare 刷新）
│
├── marketdata/
│   ├── base.py      MarketData 协议 + Quote + 后缀归一化
│   ├── online.py    在线行情（akshare）
│   ├── cache.py     ★ 装饰器：本地缓存 + 新鲜度门槛 + 复权异常检测
│   └── fake.py      FakeMarketData（演示与全部测试用，零网络）
│
├── transport/
│   ├── __init__.py  ★ load_transport —— 按配置装配后端（内置名 / 仓库外 .py 路径）
│   ├── base.py      ★ Transport 协议 + CHANNEL_OWNERS + SingleWriterGuard
│   ├── file.py      FileTransport（本地/共享盘，演示与测试默认）
│   ├── gist.py      GistTransport（对齐现网行为）
│   └── null.py      NullTransport（dry-run，只打日志）
│
├── gates.py         ★ 三个 fail-closed 闸门 + GateChain
├── strategy.py      ★ Strategy 基类 + StrategyContext + 仓库外策略加载
├── store.py         四个 channel 的读写门面（含回执索引、队列清理）
├── notify.py        通知（Console / ServerChan / 多路复用 / 节流）
├── executor.py      ★ Executor 协议 —— 接自己执行端的落点（只含契约，无实现）
├── paper.py         ★ PaperExecutor —— 本地纸面成交，让闭环在零券商下成立
├── runner.py        ★ 调度、时间预算、失败上限、心跳严重度
└── chain.py         装配（build_chain / build_file_chain）

strategies/demo_basic.py      示例策略（**明确非实盘**，只证明框架与策略无关）
tools/sanitize_check.py       脱敏门禁（装成 pre-commit 钩子，见 SANITIZATION.md）
tests/                        含 golden 线格式、传输契约、端到端接缝测试
```

★ = 值得优先读的几个。

---

## 三、一轮的完整时序

`chain.step()` 是一个完整的「嘀嗒」。**顺序不能反**，理由见下。

```
 chain.step(now)
   │
   ├─ ① executor.publish_positions(now)     执行端先报持仓
   │                                         ★ 必须在决策之前 —— 否则对账闸门拿不到数据，
   │                                           会禁买（这恰恰是它该做的事，但会每轮都禁）
   │
   ├─ ② runner.run_round(now)
   │     │
   │     ├─ build_context(now)
   │     │    ├─ 取行情  → 失败则 ctx.data.quote_ok = False
   │     │    ├─ _sync_positions()  → 读不到则 positions_synced = False（**未知 ≠ 空仓**）
   │     │    ├─ _load_aux()        → 逐项取，成败汇总进 ctx.data
   │     │    └─ strategy.on_start(ctx)
   │     │
   │     ├─ strategy.decide(ctx)           ★ 纯函数：只能读 ctx，只能返回 Signal
   │     ├─ gates.apply(signals, ctx.data) → (放行, 否决理由)
   │     ├─ stores.signals.append(sig)     逐条投递
   │     ├─ stores.signals.prune(...)      清理队列（三条规则：终态删、跨日卖删、跨日买留）
   │     ├─ strategy.on_finish(ctx, result)
   │     ├─ _persist_strategy_state()
   │     └─ finally: _report_quality() + beat(严重度)
   │
   └─ ③ executor.step(now)                  执行端消化信号 → 写回执 + 新持仓
```

### 为什么是这个顺序

**执行端先报持仓，产生端才能完成对账。** 反过来写的话，每轮开始时产生端看到的
都是**上一轮的**持仓，而 `PositionsSyncedGate` 的第一轮必然判「未对账」→ 禁买。
这不是 bug，是两道顺序约束叠在一起的结果。

**`beat()` 在 `finally` 里。** 无论这一轮成功、失败还是抛异常，心跳都必须发出去 ——
心跳本身就是「我还活着」的信号，漏发等于监控端看到的是「死了」。
所以它在 `finally`，而不是主流程末尾。

### ★ 主循环兜底与唯一的例外

`run_round` 捕获一切异常，把 `result.error` 填上并继续 —— 崩溃会带走心跳，带错误活着更好。

**唯一的例外是 `OwnershipError`：它被显式重抛。** 写错 channel 是**编程错误**，
吞掉它等于把架构违规藏起来，而它下次还会犯。

---

## 四、两个角色

| 角色 | 写 | 读 |
|---|---|---|
| **产生端**（producer） | `signals`、`heartbeat` | `positions`、`executions` |
| **执行端**（executor） | `positions`、`executions` | `signals` |

**「谁写」写在代码里**（`transport/base.py` 的 `CHANNEL_OWNERS`），
`SingleWriterGuard` 在运行期**抛异常**拒绝越权 —— 不是打警告。

理由见 [`WIRE_CONTRACT.md`](WIRE_CONTRACT.md) 第一节：**凡是靠 read-modify-write 实现
「往列表里追加」的存储都不原子**（内置的 `GistTransport` 是一个实例），
两个写者同时改同一份文档，后写的**静默**覆盖先写的。
所有权约束因此**不能绑在存储上**，只能绑在角色上 —— 换后端换不掉它。

### ★ 同一个进程可以同时扮演两端

`build_chain(..., with_executor=True)` 时两端跑在同一个进程里，
各自持有**不同的** guard，共用同一个 `Transport` 对象 ——
所以即使同进程，产生端也写不了 `positions`。

这不是演示特例：它证明所有权是**绑在角色上、而不是绑在进程上**的。
真实部署拆成两个进程时，约束一字不改地继续生效。

---

## 五、数据流（四个 channel）

```
      产生端                                          执行端
  ┌──────────────┐                              ┌──────────────┐
  │   Runner     │ ── signals ────────────────▶ │ 消化信号      │
  │   Strategy   │ ── heartbeat ──▶ 监控        │ 下单/成交     │
  │   Gates      │                              │              │
  │              │ ◀────────── positions ────── │ 报持仓        │
  │              │ ◀────────── executions ───── │ 写回执        │
  └──────────────┘                              └──────────────┘
```

四个 channel = **同一处存储里的四个文件**（`models.py` 的 `CHANNEL_FILENAMES`）。
「同一处存储」具体是什么，由 `Transport` 的实现决定：本地文件、共享盘、对象存储、
Gist —— 换存储不影响上面这张图，也不影响 channel 的所有权划分。
**字段级契约**在 [`WIRE_CONTRACT.md`](WIRE_CONTRACT.md) —— 那里才是给消费端看的东西。

### 读入侧必须宽容，写出侧必须严格

- `positions` 要被认成**三种**历史形态（包装 / 裸 dict / 旧 list）；
  认不出的**抛异常**而不是返回空快照 ——「未知」和「确认空仓」合并会开错仓。
- `executions` 有**两种互斥形态**（非终态 / 终态），字段集合不同，不会报错只会静默取到 `None`。
- `heartbeat.last_time` 读取时容忍带微秒的存量值，写出时**必须**不带。

---

## 六、扩展点：想改什么，改哪里

| 你想要 | 动哪 | 不用碰 |
|---|---|---|
| 换策略 | 写一个 `Strategy` 子类，或从仓库外路径加载 | 其余全部 |
| 换投递方式 | 写一个 `Transport` 子类放进**仓库外**的 `.py` 文件，再让配置指过去 | 策略、闸门、调度、这个包里的任何文件 |
| 换行情源 | 实现 `MarketData` | 策略、传输 |
| 加一层行情缓存 | 写装饰器包住现有 `MarketData` | 见 `marketdata/cache.py` |
| 加一道风控 | 写一个 `Gate` 加进 `GateChain` | 策略 |
| 加一个定时任务 | 往 `schedule` 里追加 `(时刻, 名字)` | 其余全部 |
| 接真券商 | 实现 `Executor` 的三个方法，从 `build_chain(executor=...)` 装进去 | 产生端一行不动 |

**「不用碰」那一列才是这张表的意义。** 每一行它都能被单独验证 ——
`tests/test_transport_contract.py` 就是「换后端不碰其余」的可执行证据：
同一套用例跑遍所有后端。

### 换投递方式，具体怎么做

写一个 `Transport` 子类，**放在仓库外的任意一个 `.py` 文件里**，然后二选一：

```python
# ① 配置文件里给一条路径（不写代码的那条路）
#    .env:  SC_TRANSPORT=../my_objectstore.py
chain = build_chain(Config.from_env(), strategy="./my_private_strategy.py")

# ② 装配时直接把对象传进去（显式传入优先于配置，与 strategy= / market= 同一条优先级）
chain = build_chain(config, transport=MyObjectStorage(endpoint, key))
```

那个文件长这样 —— 三个方法，其余什么都不用写：

```python
from signalchain.transport import Transport


class MyObjectStorage(Transport):
    def read(self, channel):        # -> 文档对象，没有则 None
        ...
    def write(self, channel, doc):  # 整份替换（不是追加）；成功返回 True
        ...
    def describe(self):             # 一句话自报身份（**不得含凭据**），会进日志与心跳
        return "objectstore(我的桶)"
```

三条必须知道的：

- **文件里只有一个 `Transport` 子类时自动选它**；有多个（辅助类很常见）就加一行
  `TRANSPORT = 你要用的那个类`。构造需要参数的后端，就写
  `TRANSPORT = MyObjectStorage.from_env`（一个零参工厂，先例是内置的
  `GistTransport.from_env()`）。规则与策略那边**完全一致**，包括「不替你猜」。
- **取值写错会报错，不会悄悄退回 `file`。** 这是刻意的：静默退化的后果不是
  「少个功能」，是两端写到了**两处不同的存储**，而两边日志都显示成功。
  唯一的例外是 gist 缺凭据 —— 那一条仍然降级（成因是「凭据没配好」，不是「名字写错了」）。
- **所有权约束跟着你走，不用你实现。** `SingleWriterGuard` 由 Runner / Executor
  自己套在你的后端外面，你只管存字节。

`tests/test_transport_plugin.py` 就是这条路的可执行证据：它在临时目录（= 仓库外）
写一个真实的后端文件，经 `SC_TRANSPORT=<路径>` 装进链、跑完一轮，再检查那个后端
**真的收到了字节**。

---

## 七、已知取舍（写下来，免得被当成 bug）

1. **持仓快照不查新鲜度。** 只要能解析就算对账成功。执行端自己挂了、文件三天没更新时，
   这里仍判「已对账」。「新鲜度」是可观测的（`snapshot.updated_at`），
   纳入判定更保守但会在正常场景误伤 —— **留作使用者按自己执行端的可靠性决定**。
2. **产生端不去重。** 见 [`WIRE_CONTRACT.md`](WIRE_CONTRACT.md) 第二节：id 唯一性的前提
   是「同秒同码同向只推一次」。
3. **连续失败上限的窗口只有 90 秒**（3 次 × 30 秒）。详见
   [`DESIGN_NOTES.md`](DESIGN_NOTES.md) 第三节。
4. **没有第二个 HTTP 投递后端。** 这三个后端就是全部：
   `FileTransport` / `GistTransport` / `NullTransport` ——
   注意 `GistTransport` 本身走的就是 HTTPS，所以「本仓库没有 HTTP 后端」是句错话，
   准确说法是「**没有第二个**」。
   设计早期打算留一个「只有接口、没有实现」的空壳类占位，**它被去掉了** ——
   **一个不能用的类不是扩展点，是噪音**；真正说明「可拔插」的是
   `tests/test_transport_contract.py` 拿同一套用例跑遍**三个真实后端**。
   再写一个 HTTP 投递后端这件事被否决过一次（理由见 `DESIGN_NOTES.md` 第六节），
   要加也不是不行：写一个 `Transport` 子类放进**仓库外**的 `.py` 文件，让
   `SC_TRANSPORT` 指过去（或 `build_chain(transport=...)`）——
   这个包里的任何文件一行都不用动，怎么装见第六节「换投递方式，具体怎么做」。
