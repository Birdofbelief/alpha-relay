# -*- coding: utf-8 -*-
"""策略侧契约：`decide` 必须是纯函数，以及策略怎么被加载。

## 为什么「纯函数」要写成可执行的测试

纯函数这条要求，写在文档里就是一句「请不要」。而它一旦被违反，后果是
**悄无声息**的：策略里藏一次网络请求，当天跑起来完全正常，只是
「回放同一天的行情得不到同一个结果」—— 于是「那天为什么卖」永远只能靠猜。

所以这里不靠自觉，用一道绊线：`decide` 执行期间，只要调用栈里出现
`strategies/` 目录下的帧（内置示例在 `signalchain/strategies/`），
就说明是**策略自己**在做 I/O，直接判失败。
（框架自己是可以做 I/O 的 —— 取行情、读缓存都是它的活。契约约束的是策略。）
"""
from __future__ import annotations

import builtins
import socket
import sys
from datetime import datetime
from pathlib import Path

import pytest

from signalchain.marketdata.base import Quote
from signalchain.marketdata.fake import FakeMarketData
from signalchain.strategy import (
    Strategy,
    StrategyContext,
    load_strategy,
)
from signalchain.strategies.demo_basic import DemoBasicStrategy

from .conftest import CLEAN_CODES, FIXED_NOW

#: `load_strategy` 给仓库外策略模块起的名字前缀（见 `signalchain/strategy.py`）。
EXTERNAL_MODULE_PREFIX = "signalchain_user_strategy_"


def _is_strategy_frame(frame) -> bool:
    """这一帧是不是**策略**的代码。

    ★ 判据只能是「文件在哪」，不能是「有没有发生 I/O」——
    取行情、读缓存都是框架的活，它们也做 I/O。

    两种策略代码，两种识别法：
      - 内置示例：文件路径里有 `strategies/` 这一段目录（现在在包内，
        `signalchain/strategies/`；它曾经在仓库根的 `strategies/` 下，
        这条判据两次都成立，所以不用跟着搬）
      - **仓库外加载的**：`load_strategy` 把它注册成
        `signalchain_user_strategy_<stem>`，从模块全局名认

    第二条是重点，不是补丁：**仓库外的私有策略才是这套框架的主要用法**。
    只认 `strategies/` 目录的话，这条绊线对真正要保护的那个策略恰好失明。
    """
    name = frame.f_globals.get("__name__", "")
    if name.startswith(EXTERNAL_MODULE_PREFIX):
        return True
    parts = Path(frame.f_code.co_filename.replace("\\", "/")).parts
    return "strategies" in parts[:-1]


def _strategy_frame_on_stack() -> bool:
    """调用栈里有没有策略的帧。"""
    frame = sys._getframe(1)
    while frame is not None:
        if _is_strategy_frame(frame):
            return True
        frame = frame.f_back
    return False


@pytest.fixture
def tripwire(monkeypatch):
    """拦下策略发起的 I/O，记下来而不是直接炸。

    记下来而不是 raise：这样断言失败时能看到**具体是哪一次**调用，
    而不是一个光秃秃的异常。排查一个「策略偷偷联网」的问题，
    「它 open 了哪个文件」比「它违规了」有用得多。
    """
    hits: list[str] = []
    real_open = builtins.open
    real_connect = socket.socket.connect

    def guard_open(file, *args, **kwargs):
        if _strategy_frame_on_stack():
            hits.append(f"open({file!r})")
        return real_open(file, *args, **kwargs)

    def guard_connect(self, address, *args, **kwargs):
        if _strategy_frame_on_stack():
            hits.append(f"socket.connect({address!r})")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    return hits


def _context(now: datetime = FIXED_NOW, codes=None) -> StrategyContext:
    codes = codes or list(CLEAN_CODES)
    market = FakeMarketData(codes, drift=0.002)
    market.now = now
    return StrategyContext(now=now, calendar=_cal(), market=market)


def _cal():
    from datetime import date

    from signalchain.calendar import weekday_calendar

    return weekday_calendar(date(2026, 9, 1), date(2026, 9, 30))


# ------------------------------------------------------------
# 纯函数
# ------------------------------------------------------------
def test_decide_performs_no_io(tripwire):
    """`decide` 期间不许有策略发起的文件/网络操作。

    这条测试就是「纯函数」这句要求的可执行版本。它拦的是**策略**发起的调用，
    框架的取数不受影响 —— 界限看的是调用栈，不是「有没有发生 I/O」。
    """
    strategy = DemoBasicStrategy()
    ctx = _context()
    ctx.load_quotes(strategy.universe())
    list(strategy.decide(ctx))
    assert tripwire == [], f"策略在 decide 里做了 I/O：{tripwire}"


def test_the_io_tripwire_actually_fires(tripwire, tmp_path):
    """★ **测这道绊线本身** —— 上面那条测试证不了它自己有效。

    一个从不触发的绊线，和一条通过了的测试，在终端上长得一模一样
    （本仓库的脱敏扫描器就有过三个这样的失效，见 `test_sanitize_rules.py`）。

    这里故意造一个**真的会做 I/O** 的策略，而且特意走 `load_strategy`
    从仓库外加载 —— 顺带验证第二种识别法（模块名）也真的有效。
    """
    probe = tmp_path / "dirty_probe.py"
    probe.write_text(
        "from signalchain.strategy import Strategy\n"
        "class Dirty(Strategy):\n"
        "    name = 'dirty'\n"
        "    def universe(self):\n"
        "        return ['510050']\n"
        "    def decide(self, ctx):\n"
        "        with open(__file__, 'r', encoding='utf-8') as fh:\n"
        "            fh.read()\n"
        "        return []\n",
        encoding="utf-8",
    )
    strategy = load_strategy(str(probe))
    ctx = _context()
    ctx.load_quotes(strategy.universe())
    strategy.decide(ctx)

    assert tripwire, (
        "绊线没逮住仓库外策略里的 open() —— 它对**主要用法**失明了，等于没有")
    assert any("dirty_probe.py" in h for h in tripwire), tripwire


def test_decide_is_deterministic_given_the_same_context():
    """同一份 ctx → 同一批信号（id 也要一样）。

    这是「可回放」的全部含义。id 一致尤其关键：它是执行端的幂等键，
    回放时若 id 变了，「这条处理过没有」就永远答「没有」，重放会变成重复下单。
    """
    strategy = DemoBasicStrategy()
    runs = []
    for _ in range(2):
        ctx = _context()
        ctx.load_quotes(strategy.universe())
        runs.append([s.to_dict() for s in strategy.decide(ctx)])
    assert runs[0] == runs[1]
    assert runs[0], "演示策略在这组输入下应当产生信号（否则这条测试什么也没验到）"


def test_signal_timestamps_come_from_ctx_not_the_wall_clock():
    """信号时间戳取自 `ctx.now`，**不是**墙上时钟。

    若取墙上时钟，回放同一天的行情会生成一批全新的 signal id ——
    这正是上面那条「确定性」被破坏的具体形状，所以单独钉一条。
    """
    strategy = DemoBasicStrategy()
    ctx = _context()
    ctx.load_quotes(strategy.universe())
    signals = list(strategy.decide(ctx))

    assert signals
    stamp = FIXED_NOW.strftime("%Y%m%d%H%M%S")
    assert all(s.sig_id.startswith(stamp) for s in signals)
    assert all(s.created_at == FIXED_NOW for s in signals)


def test_decide_tolerates_halted_symbols():
    """有标的停牌时，策略跳过它而不是整体崩掉。

    停牌是**正常**事件。把它当成异常会让策略在任意一只标的停牌时瘫痪，
    而停牌恰恰是那种「一年会遇到好几次」的日常。
    """
    strategy = DemoBasicStrategy()
    market = FakeMarketData(list(CLEAN_CODES), halt={CLEAN_CODES[1]}, drift=0.002)
    market.now = FIXED_NOW
    ctx = StrategyContext(now=FIXED_NOW, calendar=_cal(), market=market)
    ctx.load_quotes(strategy.universe())

    signals = list(strategy.decide(ctx))            # 不该抛异常
    assert all(CLEAN_CODES[1] not in s.code for s in signals)


def test_decide_stops_when_quotes_are_incomplete():
    """行情整体不可信时，策略主动停手（它比闸门更保守是允许的）。

    注意方向：**允许**策略比闸门保守，但**不允许**反过来依赖策略来保证安全
    —— 闸门必须在策略忘记检查时也生效（那由 test_gates.py 负责）。
    """
    strategy = DemoBasicStrategy()
    market = FakeMarketData(list(CLEAN_CODES), missing={CLEAN_CODES[0]}, drift=0.002)
    market.now = FIXED_NOW
    ctx = StrategyContext(now=FIXED_NOW, calendar=_cal(), market=market)
    ctx.load_quotes(strategy.universe())

    assert ctx.data.quote_ok is False
    assert list(strategy.decide(ctx)) == []


# ------------------------------------------------------------
# 代码写法不敏感
# ------------------------------------------------------------
def test_quote_lookup_accepts_both_code_styles():
    """`ctx.quote/price` 对代码写法不敏感。

    裸代码 `510050` 与带后缀的 `510050.XSHG` 是同一个标的。按字符串取键会让
    同一个标的出现两个条目 —— 行情源那层已经因为这件事栽过一次
    （策略按 1.041 下单、执行端看到 1.0630 所以不成交）。
    上下文若不归一化就会把同一个坑再挖一遍，而且更难查：策略拿到的价格
    干脆是 `None`，它会以为是停牌。
    """
    ctx = _context()
    ctx.load_quotes([CLEAN_CODES[0]])            # 用裸代码预热

    bare = ctx.quote(CLEAN_CODES[0])
    suffixed = ctx.quote(f"{CLEAN_CODES[0]}.XSHG")
    assert bare is not None
    assert suffixed is not None
    assert bare.price == suffixed.price
    assert ctx.price(f"{CLEAN_CODES[0]}.XSHG") == ctx.price(CLEAN_CODES[0])


def test_quote_returns_none_for_unknown_code():
    ctx = _context()
    ctx.load_quotes([CLEAN_CODES[0]])
    assert ctx.quote("999999") is None


def test_price_returns_none_for_halted():
    """`price()` 对停牌返回 `None`，而 `quote()` 仍然给得出一条 Quote。

    两者不同是有意的：调用方需要能区分「没有这只标的」和「这只停牌了」。
    """
    market = FakeMarketData([CLEAN_CODES[0]], halt={CLEAN_CODES[0]})
    market.now = FIXED_NOW
    ctx = StrategyContext(now=FIXED_NOW, calendar=_cal(), market=market)
    ctx.load_quotes([CLEAN_CODES[0]])

    quote = ctx.quote(CLEAN_CODES[0])
    assert isinstance(quote, Quote) and quote.is_halted
    assert ctx.price(CLEAN_CODES[0]) is None
    assert ctx.pct_change(CLEAN_CODES[0]) is None      # 停牌不是 -100%


def test_quote_before_load_raises_a_framework_error():
    """没预热就查行情 → 报错要说清「这是框架的调用顺序问题，不是策略的错」。

    错误信息里带上这句话，是为了让排查的人第一时间去查框架而不是去改策略。
    """
    ctx = _context()
    with pytest.raises(RuntimeError) as e:
        ctx.quote(CLEAN_CODES[0])
    assert "预热" in str(e.value)


# ------------------------------------------------------------
# 策略状态
# ------------------------------------------------------------
def test_state_only_marks_dirty_on_real_change():
    """状态只在**真的变了**时标脏。

    否则每一轮都会把状态写一遍盘 —— 在 gist 后端上那是白白消耗一次 PATCH 配额，
    而配额是按小时计的。
    """
    from signalchain.strategy import StrategyState

    state = StrategyState()
    state.set("holding", ["a"])
    assert state.dirty is True

    state.dirty = False
    state.set("holding", ["a"])          # 同样的值
    assert state.dirty is False
    state.set("holding", ["b"])          # 变了
    assert state.dirty is True


# ------------------------------------------------------------
# 加载策略
# ------------------------------------------------------------
def test_load_builtin_demo_strategy():
    strategy = load_strategy("demo")
    assert isinstance(strategy, DemoBasicStrategy)
    assert strategy.universe()


def test_load_strategy_from_path_outside_the_repo(tmp_path):
    """★ 仓库外的策略文件可以加载 —— 这是本仓库存在的核心理由之一。

    你的真策略对着这套框架开发、本地跑，**永远不会进入这个仓库**。
    """
    path = tmp_path / "my_private.py"
    path.write_text(
        "from signalchain.strategy import Strategy\n"
        "class Mine(Strategy):\n"
        "    name = 'mine'\n"
        "    def universe(self):\n"
        "        return ['510050']\n"
        "    def decide(self, ctx):\n"
        "        return []\n",
        encoding="utf-8",
    )
    strategy = load_strategy(str(path))
    assert strategy.name == "mine"


def test_multiple_candidates_require_explicit_choice(tmp_path):
    """一个文件里多个 Strategy 子类时必须显式指定，**不许猜**。

    静默选中错误的那个，表现是「策略没生效」—— 而策略没生效和「今天没有信号」
    长得一模一样，属于最难发现的一类问题。
    """
    path = tmp_path / "two.py"
    path.write_text(
        "from signalchain.strategy import Strategy\n"
        "class A(Strategy):\n"
        "    def universe(self): return []\n"
        "    def decide(self, ctx): return []\n"
        "class B(Strategy):\n"
        "    def universe(self): return []\n"
        "    def decide(self, ctx): return []\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportError) as e:
        load_strategy(str(path))
    assert "STRATEGY" in str(e.value)          # 错误信息要告诉人怎么修

    path.write_text(
        path.read_text(encoding="utf-8") + "\nSTRATEGY = B\n", encoding="utf-8")
    assert type(load_strategy(str(path))).__name__ == "B"


def test_missing_strategy_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_strategy(str(tmp_path / "nope.py"))


def test_base_strategy_requires_universe():
    """基类不提供默认池 —— 逼着作者显式写出池子。

    池子的大小直接决定要拉多少行情、缓存多长，给一个「默认池」会让人
    在没想过这件事的情况下跑起来。
    """
    class Bare(Strategy):
        def decide(self, ctx):
            return []

    with pytest.raises(NotImplementedError):
        Bare().universe()
