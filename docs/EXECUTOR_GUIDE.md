# 接你自己的执行端

**适用对象**：信号已经能产生出来，现在要把它变成券商那边真实的委托。

先说一件可能反直觉的事：**这一步不用改产生端一行代码。** 换掉 `build_chain(executor=...)`
那一个参数就完了 —— 产生端照常决策、照常投递、照常读你的回执与持仓去对账。
本文件教的就是怎么写那个 `executor=`。

---

## 一、契约只有三个方法

`signalchain/executor.py` 里的 `Executor` 是个**结构化类型**（`typing.Protocol`）——
**长成这个样子就算数**：不必继承任何东西，也不必 import 那个模块。

| 方法 | 什么时候被调用 | 你必须做到 |
|---|---|---|
| `publish_positions(now) -> bool` | 每一轮**最先**，在决策之前 | 把**券商那边的真实持仓**写进 `positions` |
| `step(now) -> dict[str, int]` | 每一轮**最后**，在决策与投递之后 | 消化信号 → 下单 → 写 `executions` 回执；返回 `{状态: 条数}` |
| `describe() -> str` | 打日志 / 发心跳 | 一句话自报身份，**不得含凭据** |

`PaperExecutor` 上另外那几个（`poll_and_execute` / `open_order_ids` / `_try_fill`）
**不在契约里** —— 它们是纸面实现自己的拆法，别照着抄。

### 为什么是 Protocol，不是一个能继承的基类

`Transport` 是 ABC，**要求**显式继承 —— 因为后端是一族要被同一套用例逐条扫过的实现，
写错继承关系本身就是错误。执行端不一样：真实用法是你写一个类去接券商终端，
而**那个类通常已经有基类了**（SDK 的适配层、你自己项目里的客户端类）。
框架没理由逼你再继承一个。

**代价写在明处**：`@runtime_checkable` 的 `isinstance` **只数方法名在不在，
不比对签名**。它是顺手的自检，**不是保证**。真正的保证是
[`tests/test_executor_contract.py`](../tests/test_executor_contract.py)：一个
**不继承任何东西**的最小执行端，真的被装进链、跑完一轮、它写的东西真的被产生端读到。

---

## 二、构造时必须自己包的两行

协议管不到构造函数，而这里恰好有一个**必须遵守**的约定：

```python
self.transport = SingleWriterGuard(transport, Role.EXECUTOR)
self.stores = Stores(self.transport)
```

第一行是**单一写入者**：执行端只准写 `positions` / `executions`，
写 `signals` 会**当场抛** `OwnershipError`。

**别绕过它去拿裸后端。** 绕过之后两端互相覆盖会重新变成静默故障 ——
而这条约定存在的全部理由就是根除它：产生端比你先写、你后写，两边日志都显示成功，
只有收盘对账时才发现「信号怎么少了几条」。**它是抛异常，不是打警告**，
所以「跑起来了」这件事本身不能证明你没绕过它。

第二行是四个 channel 的读写门面（`stores.positions` / `stores.executions` /
`stores.signals`）。

---

## 三、骨架

```python
from signalchain import Role, SingleWriterGuard, Stores
from signalchain.models import (
    EXEC_CANCELED, EXEC_FILLED, EXEC_SKIPPED, EXEC_SUBMITTED,
    Execution, PositionSnapshot,
)


class MyExecutor:
    """接券商终端的执行端。★ 不继承任何东西。"""

    def __init__(self, config, market, transport):
        self.config = config
        self.transport = SingleWriterGuard(transport, Role.EXECUTOR)
        self.stores = Stores(self.transport)
        self.broker = MyBrokerClient(...)          # ← 你自己的券商客户端

    # ---- 1/3：报持仓 ----
    def publish_positions(self, now) -> bool:
        held = self.broker.query_positions()       # ★ 必须来自券商，不是你记的账
        return self.stores.positions.write(PositionSnapshot(
            positions={c: {"amount": n, "cost": 0.0, "enable_amount": n}
                       for c, n in held.items()},
            total_value=0.0, cash=0.0, updated_at=now))

    # ---- 2/3：消化信号 ----
    def step(self, now) -> dict[str, int]:
        settled = self.stores.executions.index()
        open_ids = {sid for sid, rec in settled.items()
                    if rec.status == EXEC_SUBMITTED}          # 第二道防线
        counts: dict[str, int] = {}

        for sig in self.stores.signals.signals():
            prior = settled.get(sig.sig_id)
            if prior is not None and prior.status in (EXEC_FILLED, EXEC_CANCELED):
                continue                                       # 第一道防线：幂等键
            if sig.sig_id in open_ids:
                continue                                       # 已经挂着了，不重下

            status, order_id, reason = self._place(sig)         # ← 你调券商 API 的地方
            self.stores.executions.append(Execution(
                signal_id=sig.sig_id, code=sig.code, action=sig.action,
                status=status, reason=reason, order_id=order_id,
                price=float(sig.price or 0.0), amount=int(sig.amount or 0),
                created_at=now))
            counts[status] = counts.get(status, 0) + 1

        if counts:
            self.publish_positions(now)                        # 成交了，持仓变了
        return counts

    # ---- 3/3：自报身份 ----
    def describe(self) -> str:
        return "mine(券商终端)"
```

上面这份**可运行的版本**在 [`tests/test_executor_contract.py`](../tests/test_executor_contract.py) 里
（叫 `_RecordingExecutor`）：它把 `_place` 换成了「照单全收」，其余形状一模一样，
并且真的被装进一条链跑完了一轮。

### `step()` 里那两道防线，缺一不可

主循环是 30 秒一轮的，`step` 会被**反复调用**；而且进程随时可能重启。

- **第一道：`sig_id` 自带的幂等键。** 已到终态（`EXEC_FILLED` / `EXEC_CANCELED`）
  的不再处理。这是信号带确定性 id 的全部价值所在。
- **第二道：下单前先查「这只单子是不是已经挂在那里了」。**
  执行端记性不好（进程重启、回执丢了）时，靠的是它。

**少了第二道，一次重启就可能变成重复下单** —— 而且它长得像正常工作。
纸面实现里这道叫 `open_order_ids()`（`signalchain/paper.py:162`），可以直接读。

---

## 四、验证顺序：先在假行情上跑通，再换真行情

**这个顺序不能反。** 理由：一个装错了的执行端**不报错**。它最典型的表现是
「链照常跑完，什么都没发生」—— 与「今天没有信号」长得一模一样。

所以先做零成本的那一次（假行情 + 文件后端）：

```python
from signalchain import build_file_chain

chain = build_file_chain("./runtime", executor=MyExecutor(...))
result, fills = chain.step()

print(chain.describe())        # 你的 describe() 必须出现在这里
print(result.emitted, fills)   # 有信号流过吗？有回执写出来吗？
```

**要看到三件事，少一件都说明没接上：**

1. **`chain.describe()` 里有你自己的输出。** 说明链手里的是你，
   而不是被内置纸面执行端顶掉了 —— 顶掉的后果不带任何异常：
   链照常跑、回执照常写，只是写的人不是你，而你以为跑的是自己的券商适配层。
2. **`result.emitted > 0`。** 说明这一轮**真有信号流过**。没有它，第 3 条什么也证明不了 ——
   「执行端没被调用」和「这一轮没信号所以不必调用」在断言上长得一样。
3. **回执是你的。** 去读 `./runtime/` 下的 `executions` channel，
   不要读你自己在内存里记的账 —— 内存里对不对外面看不出来，
   而链路对不上账就是白搭。顺带确认 `positions` channel 里有持仓：
   读不到持仓，产生端的 `PositionsSyncedGate` 会禁买（那是**正确**行为，
   所以它不会报错，只会一直不成交）。

三条都过了再换真行情（`market=` 不传，走在线行情 + 缓存），
此时你唯一改的是券商客户端那一侧。

### 在真行情之前，先回答这五个问题

券商终端之间的差异**恰好都落在这几处**，而每一处的默认答案都可能是错的：

| 问题 | 答错的后果 |
|---|---|
| 下单是同步返回成交，还是返回委托号之后等回报？ | 当成同步 → 把「已委托」记成 `filled`，产生端据此对账，持仓是假的 |
| 拒单怎么表示？抛异常，还是返回一个代码？ | 抛异常没接住 → 整轮 `step` 挂掉，后面的信号一条都不处理 |
| 部分成交算不算成交？ | 算 → 你的账比券商的多；不算 → 一直挂着重下 |
| 撤单失败（其实已经成交了）怎么处理？ | 记成 `canceled` → 幂等键认为到终态了，而这笔其实成交了 |
| 停牌标的的委托，券商收不收？ | 收 → 跟纸面实现一样挂着等；不收 → 你必须产生一条 `EXEC_SKIPPED` 回执 |

**最后一条的反面最坏：静默丢掉。** 丢掉 = 产生端以为这条已经处理过了，
而它一次都没出去。所有失败形态里，能被看见的都是次要的。

---

## 五、三个容易犯的错

| 错法 | 为什么灾难 |
|---|---|
| `publish_positions` 报**你自己记的账** | `PositionsSyncedGate` 就失去意义了 —— 它会以为自己对上了账，而实际对上的是你那份已经漂移的副本 |
| `describe()` 里放账号 / token | 它进日志与心跳。**只许有身份，不许有凭据** |
| `step()` 里吞掉所有异常然后返回 `{}` | 与「这一轮没信号」不可区分。宁可让异常冒出来 —— 主循环会把它记进 `result.error`，心跳照发，人看得见 |

---

## 六、不用你操心的事

- **产生端一行都不用改。**
- **不必继承 `Executor`，也不必 import 它。** `from signalchain import Executor`
  只在你想要一个类型标注时才用得上。
- **`executor=` 与 `with_executor=True` 是互斥的**，两个一起给会直接抛 `ValueError` ——
  框架不替你猜。这是故意的：静默选中一个的代价是「你以为跑的是 A，实际跑的是 B」，
  而它不报错，表现上与「今天没有信号」一模一样。

---

## 七、本仓库刻意不做的事

**不带任何券商的客户端。** 每个券商终端的接入方式都不一样，内置一个就等于宣告
「只支持这一家」，那家之外的人全都用不上。

本框架保证的到 `step()` 为止：**信号被确定性地、幂等地、带风控地送到你手上。**
拿到信号之后怎么下单、怎么处理拒单与部分成交、怎么跟券商对账，是执行端自己的事。

上面那份骨架**照抄能跑**（`tests/test_executor_contract.py` 里跑着），
它到 `self._place(sig)` 那一行就交给你了 —— 那一行是本仓库唯一不打算替你写的东西。
