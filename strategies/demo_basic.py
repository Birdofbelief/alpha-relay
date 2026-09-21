# -*- coding: utf-8 -*-
"""演示策略：N 日收益率排序取前几名。

    ⚠️ 这是一个**示例**，不是、也不该被用作实盘策略。

它唯一的作用是证明一件事：**框架对策略内容一无所知**。
它只用了「取历史收盘价 → 算个收益率 → 排名 → 生成信号」这套最朴素的逻辑。

## 为什么这里要刻意写得「笨」

一个演示策略如果写得很聪明，读者会分不清「哪些是框架提供的、
哪些是这个策略自己的本事」。所以这里的判断条件全是废话级的直白 ——
**它的构造与本仓库作者实际在用的任何策略无关**，也不含任何值得复用的思路。

## 标的池说明

下面那几个代码只是**占位用的宽基 ETF**，与任何人的实际标的池无关。
真正的策略应该把它换成自己的池子，并**从仓库外加载**（见 `strategies/__init__.py`）。
"""
from __future__ import annotations

# ──────────────────────────────────────────────────────────────
# 直接运行本文件时的友好出口
# ──────────────────────────────────────────────────────────────
# 这个文件是「策略模块」，不是可执行脚本 —— 被 import 时它只定义一个类。
# 但直接运行它（`py strategies/demo_basic.py`）会先炸在下面那行 import 上：
# 工作目录是 strategies/，仓库根不在 sys.path 里，于是找不到 `signalchain`。
# 那个 traceback 对新手没有任何信息量，所以在这里给一个说人话的出口。
#
# ★ 这个块必须待在 signalchain 的 import **之前**。放到文件末尾是没用的 ——
#   import 先失败，根本走不到那儿。（第一版就写在了末尾，被实测打回。）
if __name__ == "__main__":
    raise SystemExit(
        "这个文件是「策略模块」，不是可执行脚本。\n"
        "\n"
        "要跑演示，请先切到仓库根目录，然后执行：\n"
        "\n"
        "    py -m signalchain.demo\n"
        "\n"
        "把它当库用：\n"
        "\n"
        "    from signalchain import build_file_chain\n"
        "    chain = build_file_chain('./runtime')\n"
        "\n"
        "把**你自己的**策略接进来（策略放仓库外，不进公开历史）：\n"
        "\n"
        "    chain = build_file_chain('./runtime', strategy='./my_private.py')\n"
    )

from typing import Iterable, Sequence

from signalchain.models import ACTION_BUY, ACTION_SELL, Signal
from signalchain.strategy import Strategy, StrategyContext

#: 占位池：三个最主流的宽基指数 ETF（上证50 / 中证500 / 沪深300）。
#: 用意是「谁都不会拿这个当策略」。
#:
#: ★ 这三个代码**必须过仓库自己的脱敏门禁**：扫描器带一份使用者自备的黑名单
#: （`.sanitize-deny.txt`，被 gitignore 挡住），用来查「有没有哪个代码是本该
#: 保密的」。示例里的代码看着无害，但**示例是最容易被复制进别人仓库的东西** ——
#: 所以它和真实代码走同一道门。写 demo 时顺手敲的几个数字，恰好撞上黑名单，
#: 是真实发生过的事。
#:
#: 顺带让三个后缀覆盖两种情况（510* → .XSHG，159* → .XSHE），
#: 这样演示里能同时看到两种线格式代码。
DEMO_UNIVERSE = ["510050", "510500", "159919"]

LOOKBACK = 20      # 看多少天的收益
HOLD_TOP = 1       # 持有排名前几
LOT_CASH = 10000   # 每次买入用多少钱（示例值）


class DemoBasicStrategy(Strategy):
    name = "demo_basic"

    def universe(self) -> Sequence[str]:
        return list(DEMO_UNIVERSE)

    def warmup_bars(self) -> int:
        # 多看 10 根，留出校验丢行的余量 —— 否则「要 20 根只给 18 根」时
        # 指标会在序列开头悄悄失真。
        return LOOKBACK + 10

    def decide(self, ctx: StrategyContext) -> Iterable[Signal]:
        # 数据质量是本策略**能看到**的（框架另有闸门兜底，见 gates.py）。
        # 这里主动停手是演示「策略可以比闸门更保守」；但请注意：
        # **不能反过来依赖策略来保证安全** —— 闸门必须在策略忘记检查时也生效。
        if not ctx.data.quote_ok:
            ctx.log.warning("演示策略：本轮行情不完整，主动跳过")
            return []

        scores: dict[str, float] = {}
        for code in self.universe():
            if ctx.price(code) is None:
                # 停牌或价格不可信 → 本轮不参与排名。
                # （停牌是很正常的事，只跳过这一只，不影响其它标的。）
                continue
            hist = ctx.try_history(code, self.warmup_bars())
            if hist is None or len(hist) <= LOOKBACK:
                continue
            closes = hist["close"].astype(float).tolist()
            first, last = closes[-LOOKBACK - 1], closes[-1]
            if first <= 0:
                continue
            scores[code] = last / first - 1.0

        if not scores:
            ctx.log.warning("演示策略：无可用标的，本轮无信号")
            return []

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        target = [code for code, _ in ranked[:HOLD_TOP]]
        ctx.log.info("演示策略：排名 %s → 目标持仓 %s",
                     [(c, f"{s:+.2%}") for c, s in ranked], target)

        held = set(ctx.state.get("holding", []))
        signals: list[Signal] = []

        # 先卖后买：卖出腾出的资金才够买入。顺序反了会在资金不足时白跑一趟。
        for code in sorted(held - set(target)):
            price = ctx.price(code)
            if price is None:
                # 停牌 → 卖不掉是客观事实，跳过。**但要留痕**：
                # 悄悄跳过会让人以为「策略没想卖」，而实际是「想卖卖不了」。
                ctx.log.warning("演示策略：%s 想卖出但无有效价格（停牌？），本轮跳过", code)
                continue
            # 用 ctx.signal 而不是 Signal(...) —— 时间戳取自 ctx.now，
            # 这样同一份行情回放出的信号 id 完全相同（幂等的前提）。
            signals.append(ctx.signal(code, ACTION_SELL, amount=0,
                                      price=price, signal_type="demo_exit"))

        for code in target:
            if code in held:
                continue
            price = ctx.price(code)
            if not price:
                continue
            shares = int(LOT_CASH / price / 100) * 100  # 取整到 100 股
            if shares <= 0:
                continue
            signals.append(ctx.signal(code, ACTION_BUY, amount=shares,
                                      price=price, signal_type="demo_entry"))

        ctx.state.set("holding", target)
        return signals

    def on_finish(self, ctx: StrategyContext, results) -> None:
        ctx.log.info("演示策略：本轮 %s", results.summary() if results else "无结果")
