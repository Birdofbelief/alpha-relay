# -*- coding: utf-8 -*-
"""线上数据结构（wire format）与其序列化。

**这一层的字段名、格式、大小写都是冻结的**，见 `docs/WIRE_CONTRACT.md`。

为什么冻结：链路的两端可能由不同的人、不同的语言、不同的部署节奏维护。
一旦序列化格式漂移，症状是「信号发出去了但执行端读不懂」——**静默失败**，
往往要等一笔该成交的单没成交才发现。所以这里宁可啰嗦，也要把格式钉死，
并用 golden-file 测试守住（`tests/test_wire_contract.py`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

# ------------------------------------------------------------
# channel 名。同时是传输后端里的「文件名」，改了就是破坏兼容。
# ------------------------------------------------------------
CH_SIGNALS = "signals"
CH_HEARTBEAT = "heartbeat"
CH_POSITIONS = "positions"
CH_EXECUTIONS = "executions"

ALL_CHANNELS = (CH_SIGNALS, CH_HEARTBEAT, CH_POSITIONS, CH_EXECUTIONS)

# 各 channel 在传输后端里的落盘文件名。
# Gist 后端用它当 gist 内的 file name；文件后端用它当文件名。
CHANNEL_FILENAMES = {
    CH_SIGNALS: "trade_signals.json",
    CH_HEARTBEAT: "heartbeat.json",
    CH_POSITIONS: "positions.json",
    CH_EXECUTIONS: "executions.json",
}

ACTION_BUY = "buy"
ACTION_SELL = "sell"

# 执行回执的状态取值。前两个是终态，只有终态才允许把信号从队列里清掉。
EXEC_SUBMITTED = "submitted"
EXEC_FILLED = "filled"
EXEC_CANCELED = "canceled"
EXEC_FAILED = "failed"
EXEC_SKIPPED = "skipped"

TERMINAL_EXEC_STATUSES = frozenset({EXEC_FILLED, EXEC_CANCELED})


# ------------------------------------------------------------
# 代码格式转换
#
# 内部一律用「带交易所后缀」的形式（六位数字 + `.XSHG`/`.XSHE`），因为执行端
# 就是这么认的。这套后缀规则来自聚宽（JQ）约定，为兼容既有执行端而保留。
#
# 这里刻意不举具体代码当例子：示例里的六位数字最容易被复制走，
# 而真实标的池的成员正是靠「有没有出现在公开仓库里」这种最低级的方式泄漏的。
# （扫描器抓过这条 —— 初版这里写的就是一个真代码。）
# ------------------------------------------------------------
def to_jq(code: str) -> str:
    """6 位数字代码 → 带交易所后缀。已是后缀形式则原样返回（幂等）。"""
    code = str(code).strip()
    if "." in code:
        return code
    # 上交所：5/6/9 开头（含 5xx 基金、6xx 股票、9xx B股）；其余归深交所。
    suffix = "XSHG" if code.startswith(("5", "6", "9")) else "XSHE"
    return f"{code}.{suffix}"


def to_bare(code: str) -> str:
    """带后缀 → 6 位裸代码。"""
    return str(code).strip().split(".")[0]


def to_pt(code: str) -> str:
    """带后缀 → 券商端形式（`.SS`/`.SZ`）。**这是 `positions` channel 的线格式。**

    ★ 同一条链上，两个 channel 用**两套不同的后缀**，这是实测出来的：

        signals / executions 的 code  → `.XSHG`/`.XSHE`（聚宽形式）
        positions 的 key              → `.SS`/`.SZ`（券商端形式，无转换直写）

    `positions` 之所以是券商端形式，是因为执行端是**把自己的持仓字典原样
    序列化写出去**的 —— 那些 key 用的是它自己 SDK 的写法。而 `executions`
    侧每个写点都显式过了一次转换。

    **不要「顺手统一」这两套后缀。** 统一看起来是整洁，实际是让其中一端
    认不出另一端的输出。本仓库已经因为「同一标的两套代码写法」栽过一次。
    """
    code = str(code).strip()
    if code.endswith((".SS", ".SZ")):
        return code
    return to_jq(code).replace(".XSHG", ".SS").replace(".XSHE", ".SZ")


def pt_to_jq(code: str) -> str:
    """券商端形式（`.SS`/`.SZ`）→ 聚宽形式（`.XSHG`/`.XSHE`）。

    与 `to_jq` 的区别：`to_jq` 是**从裸代码推**（靠首位字符判断交易所），
    本函数是**从已有后缀翻**（靠查表）。两条路都要有，因为两种输入都可能出现：
    内部构造的代码是裸的，而从 `positions` 读回来的已经带后缀了。
    """
    code = str(code).strip()
    return code.replace(".SS", ".XSHG").replace(".SZ", ".XSHE")


def fmt_dt(dt: datetime) -> str:
    """`signals.time` / `executions.time` / `positions.update_time` 的时间格式。

    **就是 `str(datetime)`** —— 线上这三处写的都是 `str(current_dt)`。

    不要改成 `isoformat()`：后者用 `T` 分隔，与既有执行端的解析和历史信号
    不兼容。这三个字段的消费方式只有两种：取前 10 个字符比日期（`[:10]`）、
    以及按字典序排序 —— 两者对「有没有微秒」都不敏感，
    所以这里与线上保持一致即可，多一个保证是：`str()` 的输出**恒为定宽前缀**，
    字典序必然等于时间序。
    """
    return str(dt)


def fmt_dt_secs(dt: datetime) -> str:
    """`heartbeat.last_time` 的时间格式 —— **与 `fmt_dt` 不同，这是刻意的。**

    ★ 两个格式化函数并存不是冗余，是必须的。理由是**有一个硬解析器**：

        线上监控端   datetime.strptime(last_time, '%Y-%m-%d %H:%M:%S')

    线上产生端在心跳里写的是 `current_dt.strftime("%Y-%m-%d %H:%M:%S")`，
    **显式截断到秒**。若这里改用 `str(dt)`，只要时钟带微秒，
    输出就变成 `2026-09-20 09:50:56.878982`，而 `strptime` 会抛
    `ValueError: unconverted data remains: .878982`。

    抛了会怎样？被上层 `except` 吞掉 → `last_dt = None` →
    `_stale_checkpoint()` 走 fail-open 分支返回 None →
    **心跳滞后检测静默失效**。不报错、不告警，日志里只是少一行。

    这正是「格式写错会导致静默降级」的那一类，所以它必须有一个
    **微秒非零**的测试来钉（`FIXED_NOW` 的微秒恰好是 0，
    不特意构造就永远测不出来 —— 本仓库初版就是这么漏掉的）。
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Signal:
    """一条待执行的交易信号。

    `amount` 对 buy 可能为 0 —— 这是有意的：买入份数由执行端按账户可用资金
    与限价反推，信号端不掌握账户余额，硬填一个数是错的。
    """

    code: str
    action: str
    amount: int = 0
    price: float = 0.0
    signal_type: str = "normal"
    created_at: datetime | None = None
    sig_id: str | None = None
    processed: bool = False

    def __post_init__(self) -> None:
        self.code = to_jq(self.code)
        if self.action not in (ACTION_BUY, ACTION_SELL):
            raise ValueError(f"action 必须是 {ACTION_BUY}/{ACTION_SELL}，收到 {self.action!r}")
        self.amount = int(self.amount)
        self.price = round(float(self.price), 4)
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.sig_id is None:
            self.sig_id = self.make_id()

    def make_id(self) -> str:
        """信号 ID = 时间戳 + 代码 + 动作。

        刻意做成**确定性**的：同一个时点对同一标的同一动作只会生成同一个 ID。
        执行端拿它做幂等键 —— 重发同一条信号不会重复下单。若这里掺入随机数或
        自增序号，「重试」就会变成「重复下单」。
        """
        return f"{self.created_at.strftime('%Y%m%d%H%M%S')}_{self.code}_{self.action}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为线上格式。**键的顺序与既有实现一致**，便于 golden 对比。"""
        return {
            "id": self.sig_id,
            "code": self.code,
            "action": self.action,
            "amount": self.amount,
            "price": self.price,
            "time": fmt_dt(self.created_at),
            "signal_type": self.signal_type,
            "processed": self.processed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        raw_time = d.get("time")
        created = None
        if raw_time:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    created = datetime.strptime(str(raw_time), fmt)
                    break
                except ValueError:
                    continue
        return cls(
            code=d.get("code", ""),
            action=d.get("action", ""),
            amount=d.get("amount", 0),
            price=d.get("price", 0.0),
            signal_type=d.get("signal_type", "normal"),
            created_at=created,
            sig_id=d.get("id"),
            processed=bool(d.get("processed", False)),
        )


@dataclass
class Execution:
    """执行端回写的成交/下单回执。链路据此判断信号是否已被消化。

    ## ★ 线上有**两种记录形状**，字段集互斥 —— 别把它们拼成一份

    实测（线上两处构造点）：

      **非终态**（`submitted`/`skipped`/`failed`）—— 由 `_record()` 统一构造
          `signal_id, code, action, status, reason, time`
          **没有** `order_id` / `price` / `amount`

      **终态**（`filled`/`canceled`）—— 线上三处**直接构造**
          `signal_id, code, action, status, order_id, price, amount, time`
          **没有** `reason`

    所以 `reason` 与 `order_id`/`price`/`amount` **是互斥的**，各自只属于一种形状。

    ⚠️ **不要照线上执行端那个写回执函数的 docstring 写解析代码。**
    那份 docstring 把两种形状拼成了一份「完整」字段表，而且**列了一个
    全代码库都不存在的 `limit_price`**（全文件检索：只出现在那一行注释里）。
    本仓库初版正是照它建的模型 —— 于是凭空多出一个字段、又漏掉了真正的 `price`，
    后果是产生端的成交播报读 `rec.get('price')` 时恒为 `None`。
    **文档比代码更容易被信任，这一条是活例子。**

    读的时候要宽容（两种形状都能解），写的时候要严格（按 status 决定形状）。
    """

    signal_id: str
    code: str
    action: str
    status: str
    reason: str = ""
    order_id: str = ""
    #: 成交价（`filled` 用真实成交价，`canceled` 用委托价）。**仅终态有。**
    price: float = 0.0
    #: 成交/委托份额。**仅终态有。**
    amount: int = 0
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """按 status 输出对应形状。键序也与线上构造点逐条对齐（顺序是契约）。"""
        if self.is_terminal:
            return {
                "signal_id": self.signal_id,
                "code": self.code,
                "action": self.action,
                "status": self.status,
                "order_id": self.order_id,
                "price": round(float(self.price), 4),
                "amount": int(self.amount),
                "time": fmt_dt(self.created_at or datetime.now()),
            }
        return {
            "signal_id": self.signal_id,
            "code": self.code,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "time": fmt_dt(self.created_at or datetime.now()),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Execution":
        """读的时候**必须宽容**：两种形状都可能出现，且要容忍将来多出的字段。

        缺字段一律用 `.get` 取默认值 —— 解析方比生成方晚升级是常态
        （执行端先上新字段，产生端还没跟上），抛 `KeyError` 会让整条链路
        因为一个无关紧要的新字段而停摆。
        """
        return cls(
            signal_id=d.get("signal_id", ""),
            code=d.get("code", ""),
            action=d.get("action", ""),
            status=d.get("status", ""),
            reason=d.get("reason", ""),
            order_id=d.get("order_id", ""),
            price=float(d.get("price", 0.0) or 0.0),
            amount=int(d.get("amount", 0) or 0),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_EXEC_STATUSES


class WireFormatError(ValueError):
    """线格式无法识别。

    ★ 它**必须是个异常，不能退化成空快照**。理由见 `PositionSnapshot.from_dict`：
    「读不懂」和「确认空仓」在风控上是**相反**的两个结论 ——
    前者必须降级（禁买），后者可以放心开新仓。把它们合并，
    就会在「读不懂」时表现得像「确认空仓」，进而放心买入 → 变成双持仓。
    """


@dataclass
class PositionSnapshot:
    """执行端回写的**账户真实持仓**。

    这是整套设计里最重要的一个语义：**执行端的持仓是唯一真相源**。
    信号端自己记的持仓只是缓存，随时可能因为「卖了但没成交」「被人工操作过」
    而与真实情况不一致。因此对账失败时必须降级（见 gates.PositionsSyncedGate）。

    ## 代码形式：内部归一到聚宽形式，对外写成券商端形式

    | 位置 | 形式 |
    |---|---|
    | 本对象的 `positions` 的 key（内部） | `.XSHG`/`.XSHE` |
    | 落盘/上 gist 的 key（线格式） | `.SS`/`.SZ` |

    内部统一成聚宽形式，是因为 `Signal.code` 也是聚宽形式 —— 全仓库只留一种
    内部写法，才不会重演「同一标的两套代码格式」那个坑（`marketdata/base.py`
    与 `strategy.py` 各记过一次）。线格式则必须是券商端形式，因为线上就是那么写的。
    两者的转换收在 `to_dict`/`from_dict` 这一对里，别的地方不碰。
    """

    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_value: float = 0.0
    cash: float = 0.0
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """写成线格式。**key 转成券商端形式**（`.SS`/`.SZ`），见类文档。"""
        return {
            "update_time": fmt_dt(self.updated_at or datetime.now()),
            "total_value": self.total_value,
            "cash": self.cash,
            "positions": {to_pt(k): v for k, v in self.positions.items()},
        }

    @classmethod
    def from_dict(cls, d: Any) -> "PositionSnapshot":
        """读线格式。**三种形态都认**，认不出就抛 `WireFormatError`。

        实测线上存在**三种**形态，不是两种：

          A 包装 dict  `{"update_time":…, "total_value":…, "cash":…, "positions":{…}}`
          B 裸 dict    `{code: {...}}`                     ← 早期版本写的，线上有存量
          C 旧的 list  `[{code, amount, …}, …]`            ← 执行端老版本遗留

        形态 C 的存在本身就说明**线上确实曾经是另一种格式**，所以兼容不是
        「防御性编程」，是在处理真实的历史包袱。

        ★ **认不出时抛异常，绝不返回空快照。** 线上对这种情况的处理是标记
        **持仓未同步** —— 判对账失败，也就是**禁买**。本仓库初版这里
        `return cls()`，把「读不懂」变成了「确认空仓」，正好是失效方向反了：
        该降级的时候反而放行买入，而双持仓是这套系统里最贵的错误。

        注意形态 A 里 `positions` 字段存在但不是 dict 时，**同样是失败**，
        不能当成空仓。
        """
        if isinstance(d, list):
            # 形态 C：`[{code, amount, …}]` → `{code: {...}}`。取不到 code 就跳过该条。
            positions = {}
            for p in d:
                if isinstance(p, dict) and p.get("code"):
                    positions[str(p["code"])] = p
            return cls(positions=cls._normalize(positions))

        if not isinstance(d, dict):
            raise WireFormatError(
                f"持仓结构无法识别（{type(d).__name__}），"
                f"按对账失败处理而不是当成空仓")

        if "positions" in d:
            # 形态 A。`positions` 不是 dict → 失败（与线上一致，不退化）。
            raw = d["positions"]
            if not isinstance(raw, dict):
                raise WireFormatError(
                    f"positions 字段不是 dict（{type(raw).__name__}），按对账失败处理")
            return cls(
                positions=cls._normalize(raw),
                total_value=float(d.get("total_value", 0) or 0),
                cash=float(d.get("cash", 0) or 0),
                updated_at=cls._parse_time(d.get("update_time")),
            )

        # 形态 B：顶层就是 {code: {...}}。
        # 但「一个 dict 里一个持仓都没有」也可能是「格式不认识」——
        # 这里用「所有值都不是 dict」来区分：值为空的 dict 仍然按空仓处理，
        # 因为那是一个合法的空持仓表。
        if d and not any(isinstance(v, dict) for v in d.values()):
            raise WireFormatError(
                "顶层 dict 里没有任何持仓条目，既不像裸持仓表也不像包装形态，"
                "按对账失败处理")
        return cls(positions=cls._normalize(d))

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """key 归一到聚宽形式，值只留 dict（非 dict 的值是脏数据，丢掉）。"""
        return {pt_to_jq(str(k)): dict(v)
                for k, v in raw.items() if isinstance(v, dict)}

    @staticmethod
    def _parse_time(raw: Any) -> datetime | None:
        """`update_time` 解析。**只在形态 A 下有意义** —— 形态 B/C 下它是空的。

        这不是缺陷而是兼容层的真实代价：线上用 `positions_data.get('update_time','')`，
        形态 B/C 下恒为空串，于是由它回填的 `buy_date` 会退化成「今天」。
        契约里必须写明，否则「为什么持仓买入日期全是今天」
        会变成一个查不出来的问题。
        """
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(str(raw), fmt)
            except ValueError:
                continue
        return None

    @property
    def bare_codes(self) -> list[str]:
        """6 位裸代码 —— 对账用的规范形式（线上对账前也是先归一到这个再比）。"""
        return [to_bare(c) for c in self.positions]

    def held_codes(self) -> list[str]:
        """只保留 amount > 0 的标的 —— 与既有语义一致，剔除挂零的残留记录。

        返回的是**聚宽形式**（内部规范形式），因为调用方通常要拿它和
        `Signal.code` / 策略标的池比。要比数字的话用 `bare_codes`。
        """
        return [c for c, p in self.positions.items()
                if int(p.get("amount", 0) or 0) > 0]


@dataclass
class Heartbeat:
    """心跳。`status` 是**严重度阶梯**而非普通枚举：

        alive < positions_stale < error

    只升不降（当天内），跨日重置。监控端按它决定发不发告警 ——
    所以「取最严重」这个语义是接口的一部分，不是实现细节。
    """

    last_time: datetime | None = None
    last_task: str = ""
    status: str = "alive"

    SEVERITY = {"alive": 0, "positions_stale": 1, "error": 2}

    def to_dict(self) -> dict[str, Any]:
        return {
            # ★ 这里必须是 `fmt_dt_secs`（截断到秒），不是 `fmt_dt`。
            # 这是全仓库唯一一个「格式化方式写错会**静默**降级」的字段：
            # 带微秒的输出会让监控端的 strptime 抛异常 → 被吞 → fail-open →
            # 心跳滞后检测无声失效。详见 fmt_dt_secs 的说明。
            "last_time": fmt_dt_secs(self.last_time or datetime.now()),
            "last_task": self.last_task,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Heartbeat":
        if not isinstance(d, dict):
            return cls()
        raw = d.get("last_time")
        when = None
        if raw:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    when = datetime.strptime(str(raw), fmt)
                    break
                except ValueError:
                    continue
        return cls(last_time=when, last_task=d.get("last_task", ""),
                   status=d.get("status", "alive"))

    def escalate(self, status: str) -> None:
        """严重度只升不降。降级会让监控漏报已经发生过的异常。"""
        if self.SEVERITY.get(status, 0) > self.SEVERITY.get(self.status, 0):
            self.status = status


def signals_to_payload(signals: Iterable[Signal]) -> list[dict[str, Any]]:
    return [s.to_dict() for s in signals]


def payload_to_signals(payload: Any) -> list[Signal]:
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if isinstance(item, dict) and item.get("code") and item.get("action"):
            out.append(Signal.from_dict(item))
    return out


def executions_to_index(payload: Any, keep_latest: bool = True) -> dict[str, Execution]:
    """把回执列表转成 `{signal_id: Execution}`。

    同一个 signal_id 会有多条回执（submitted → filled）。`keep_latest=True`
    取靠后的那条（默认，也是绝大多数调用方要的）；`keep_latest=False` 取**最早**的那条
    ——「这笔单最终变成什么」看最后一条，「这笔单最初是怎么下的」看第一条。

    依赖「列表按时间追加」这一约定 —— 它同样写进了 WIRE_CONTRACT。
    """
    if not isinstance(payload, list):
        return {}
    index: dict[str, Execution] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        sid = item.get("signal_id")
        if not sid:
            continue
        # ★ 这个 continue 不能省。初版这里两个分支写的是同一句
        # `index[sid] = exec_`，于是 `keep_latest=False` 会被静默忽略 ——
        # 参数等于不存在，调用方拿到的是最新一条却以为是最早一条。
        # 参数被静默忽略是比崩溃更坏的一种 bug：它给出的是错误答案，不是失败。
        if not keep_latest and sid in index:
            continue
        index[sid] = Execution.from_dict(item)
    return index
