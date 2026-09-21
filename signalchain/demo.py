# -*- coding: utf-8 -*-
"""端到端演示：`py -m signalchain.demo`

全程**零凭据、零网络、零券商**，用假行情和文件后端把整条链跑一遍，
每一步都打印它在证明什么。

这个演示的意义不是「能跑」，而是**能验证**：它把几个平时看不见的性质
变成了看得见的输出 —— 信号长什么样、闸门拦下了什么、幂等有没有生效、
限价单是成交还是挂住、持仓怎么回到产生端。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .chain import Chain, build_file_chain
from .models import ACTION_BUY, ACTION_SELL, Signal, to_bare

BANNER = "=" * 74


def _hr(title: str) -> None:
    print(f"\n{BANNER}\n  {title}\n{BANNER}")


def ctx_signal(chain: Chain, now: datetime, code: str, action: str = "buy",
               min_shares: int = 100):
    """造一条**时间戳固定**的演示信号。

    时间戳必须显式给 `now`。若让它取墙上时钟，同一份演示每次跑出来的
    信号 id 都不同 —— 那就演示不了幂等，也说明不了「可回放」。
    """
    batch = chain.market.quotes([code])
    quote = batch.quotes.get(code)
    price = (quote.price if quote and quote.price else 1.0)
    return Signal(code=code, action=action, amount=min_shares,
                  price=price, created_at=now)


def _show(chain: Chain, channel: str) -> None:
    """打印一个 channel 的内容。**这是观察链路状态的唯一窗口。**

    文件名走 `CHANNEL_FILENAMES`，不要硬写 —— 落盘名是**线格式的一部分**，
    与 Gist 后端共用同一张表。硬写会在改名时静默打印「文件不存在」。
    """
    from .models import CHANNEL_FILENAMES

    name = CHANNEL_FILENAMES.get(channel, f"{channel}.json")
    path = Path(chain.config.channel_dir) / name
    if not path.exists():
        print(f"  ({name} 尚不存在)")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > 1400:
        text = text[:1400] + "\n  ...（截断）"
    print(text)


def _feed_three_probes(chain: Chain, now: datetime) -> list[str]:
    """往队列里喂三条信号，让执行端的三种判定**各发生一次**。

    ★ 为什么不「让策略自己产出」：那是碰运气。上一轮策略只产出一条，
    而**回执的两种形状（终态 / 非终态）都该在演示里被看见一次** ——
    靠运气覆盖不到的路径，等于没有演示。所以这里直接构造。

    三条用**不同的标的**：同一秒、同一代码、同一方向会撞上同一个幂等键，
    第二条会被队列当成重复丢掉 —— 那正是第 5 步要单独演示的性质，
    不该在这里意外发生。

    返回实际喂进去的说明文字（供打印，也让调用方能断言条数）。
    """
    uni = list(chain.strategy.universe())
    held = next(iter(chain.executor.account.positions), "") if chain.executor else ""
    # ★ 账户里的 key 是**带后缀**的（执行端从券商侧原样拿到，如 `510050.XSHG`），
    # 而策略的标的池是**不带后缀**的（`510050`）—— 同一个标的、两种写法。
    # market 接口按调用时给的形式回 key，所以查价格前统一归一化。
    held_bare = to_bare(held) if held else ""

    # 卖出那条**必须挑账户里没有的**：挑错了它会真的成交，
    # 那就不是「跳过」那条分支了 —— 顺带说明为什么这里不写死代码。
    not_held = [c for c in uni if c != held_bare]
    assert len(not_held) >= 2, (
        f"演示需要至少两只『没持有』的标的来分别演示挂单与跳过，"
        f"而池里只有 {uni}、账户持有 {held!r}")
    buy_wait, sell_none = not_held[0], not_held[1]

    market = {c: chain.market.quotes([c]).quotes[c].price for c in uni}

    probes: list[tuple[Signal, str]] = [
        # 买：限价压到市价之下 → 不追价，挂单等待（非终态）
        (Signal(code=buy_wait, action=ACTION_BUY, amount=100,
                price=round(market[buy_wait] * 0.9, 4), created_at=now),
         f"买入 {buy_wait}，限价 = 市价的 90%  → 挂单等待"),
        # 卖：账户里没有它 → 跳过（执行端在这里兜住闸门放行的卖单）
        (Signal(code=sell_none, action=ACTION_SELL, amount=100,
                price=0, created_at=now),
         f"卖出 {sell_none}，但账户里没有它    → 跳过"),
    ]
    if held:
        # 买：限价在市价之上 → 成交（终态）
        probes.append(
            (Signal(code=held, action=ACTION_BUY, amount=100,
                    price=round(market[held_bare] * 1.1, 4), created_at=now),
             f"买入 {held}（已持有），限价 = 市价的 110% → 成交"))

    labels = []
    for sig, label in probes:
        chain.runner.stores.signals.append(sig)   # signals 归产生端写
        labels.append(label)
        print(f"    · {label}")
    return labels


def run_demo(keep: bool = False, quiet: bool = False) -> int:
    runtime = Path(tempfile.mkdtemp(prefix="signalchain-demo-"))
    print(f"运行目录：{runtime}")

    _hr("1. 装配链路")
    chain = build_file_chain(runtime, drift=0.002)
    print(f"  {chain.describe()}")
    print("\n  说明：行情是 **假数据**（确定性），传输是 **本地文件**，"
          "\n        执行端是 **纸面账户**。整条链在这里不碰网络、不要凭据。")
    print(f"  同时注意 role={chain.config.role}：产生端身份的 guard "
          f"**写不了 positions/executions**，")
    print("  即使纸面执行器就在同一个进程里 —— 所有权绑在角色上，不是绑在进程上。")

    # ★ 上面那句话**真的试一次**，而不是只打印出来。
    # 一段只会打印「我会拦住」的演示，和一个真的会拦住的实现，在终端上
    # 长得一模一样 —— 这正是本仓库反复在防的那种「声明冒充证据」。
    from .transport.base import OwnershipError

    try:
        chain.guarded_transport.write("positions", {})
    except OwnershipError as e:
        print(f"\n  → 实测：产生端试写 positions channel ⇒ {type(e).__name__}")
        print(f"     {e}")
    else:
        # 走到这里说明核心不变量已经失效 —— 这条演示的前提不成立了。
        # 不继续演下去：在一堆正常输出里混一句「其实没拦住」，没人会看见。
        raise RuntimeError(
            "单一写入者约束失效：产生端身份竟然写进了 positions channel。"
            "这是本仓库的核心不变量，必须立刻查 signalchain/transport/base.py")

    now = datetime(2026, 9, 18, 14, 45, 0)

    _hr("2. 第一轮：执行端先报持仓 → 产生端决策")
    print("  顺序很关键：执行端**必须先报持仓**，产生端才可能完成对账。")
    print("  如果跳过这一步，你会看到下面那条买入信号被闸门拦下。")
    result, fills = chain.step(now)
    print(f"\n  → {result.summary()}")
    print(f"  → 执行端：{fills or '无信号可处理'}")
    print("\n  写进 signals channel 的内容：")
    _show(chain, "signals")

    _hr("3. 第二轮：三种回执形态（成交 / 挂单 / 跳过）")
    print("  纸面执行端的判定顺序：停牌? → 限价是否允许成交? → 资金/持仓是否够?")
    print("  这一轮**直接喂三条信号**，让三种判定各发生一次 ——")
    print("  让策略自然产出三种结果是碰运气的：上面那一轮只产出了一条。")
    # ★ 用第二轮的**时刻**造这三条信号，而不是复用第一轮的 now。
    # 幂等键 = 秒 + 代码 + 方向，复用第一轮的秒会让第三条撞上第一轮那条真信号
    # 的 id，被队列当成重复静默丢掉 —— 演示会「少一条回执」而看不出原因。
    later = now.replace(hour=15)
    labels = _feed_three_probes(chain, later)
    result2, fills2 = chain.step(later)
    print(f"\n  → {result2.summary()}")
    print(f"  → 执行端：{fills2}")

    # ★ 演示自己检查自己：三种判定**都发生了**才算演示成功。
    # 只打印「3 条喂入」而不核对结果，就等于又给了读者一句无证据的声明 ——
    # 本文件第 1 步刚讲过为什么那不行。
    #
    # ★★ 所以这里必须和第 1 步一样 **raise**，不能只 print。
    # 初版是 print 一句 ⛔ 就往下走，而 `run_demo` 照旧 `return 0` —— 于是
    # 「自检失败了」和「根本没有自检」在退出码上完全一样，CI 跑 demo 看到的
    # 是绿灯。那条 ⛔ 还会混在几十行正常输出里，没人会注意到。
    # 判据不是「更严格」，是**前提没成立时不该让演示继续往下演**：
    # 后面每一行的预期结果都建立在「三种判定都覆盖到了」之上。
    want = {"filled", "submitted", "skipped"}
    got = set(fills2)
    if not want <= got:
        raise RuntimeError(
            f"演示前提没成立：三种判定里少了 {sorted(want - got)} —— "
            f"喂入 {len(labels)} 条、回执 {fills2}。"
            f"上面每行的预期结果都不可信，所以这里直接停，不把剩下的戏演完。")
    print(f"  → ✅ 三种判定都发生了（喂入 {len(labels)} 条，回执 "
          f"{sum(fills2.values())} 条）")

    print("\n  executions channel（回执）—— 注意两种形状的**字段集不一样**：")
    _show(chain, "executions")
    print("  ★ 终态（filled / canceled）带 order_id / price / amount，**没有 reason**；")
    print("    非终态（submitted / skipped）带 reason，**没有 order_id / price / amount**。")
    print("    同一个数组里的两种互斥形状，按 status 分辨 —— 取错字段不会报错，")
    print("    只会静默拿到 None 或 0（见 docs/WIRE_CONTRACT.md 第五节）。")

    _hr("4. 持仓回流")
    print("  执行端把成交后的持仓写回 positions channel，产生端下一轮就能对账：")
    _show(chain, "positions")

    _hr("5. 幂等有两层，分开看")
    print("  队列里那条信号已被终态回执清理掉了，所以下面直接验**第一层**：")
    print("  拿同一个 Signal 对象往队列里追加两次。")
    probe = chain.runner.stores.signals
    sig = chain.strategy.universe()[0]
    s1 = ctx_signal(chain, now, sig)
    n0 = len(probe.raw())
    probe.append(s1)
    n1 = len(probe.raw())
    probe.append(s1)          # 同一个 id，再写一次
    n2 = len(probe.raw())
    print(f"\n  追加第 1 次：{n0} → {n1}；追加第 2 次：{n1} → {n2}")
    print(f"  信号 id = {s1.sig_id}")
    print("  → 第二次没有增长，说明**队列按 id 去重**。这就是「重试 ≠ 重复下单」的第一层。")
    print("\n  第二层在执行端：它按 `signal_id` 查历史回执，已成交的静默跳过；")
    print("  另外还会查「有没有挂着未终态的单」，那是重启后防重复下单的第二道闸。")
    print("\n  （上面这条人造信号会留在队列里 —— 它没有回执，按清理规则本就不该删。）")

    _hr("6. 闸门：三个闸门的失效方向**故意不一样**")
    print("  这是整个仓库最值得单独看的一段。闸门与策略无关，而且不靠策略自觉 ——")
    print("  下面**直接**把一条买入信号喂给闸门链，看不同数据质量下各自的判定。")
    from .gates import DataQuality

    buy = ctx_signal(chain, now, chain.strategy.universe()[0], min_shares=100)
    sell = ctx_signal(chain, now, chain.strategy.universe()[0],
                      action="sell", min_shares=100)
    # 每个 case **只制造一个问题**，其余项显式置为正常 ——
    # 否则多个闸门会同时触发，读者分不清是哪一道拦下的。
    cases = [
        ("一切正常", DataQuality(quote_ok=True, positions_synced=True, aux_data_ok=True)),
        # 这里的代码要跟 DEMO_UNIVERSE 保持一致 —— 演示里出现的每个代码
        # 都会被脱敏扫描器按「是不是真标的池成员」检查一遍（见 tools/sanitize_check.py）。
        ("行情失效（缺失/NaN/非正）",
         DataQuality(quote_ok=False, quote_failed_codes=["510050"],
                     positions_synced=True, aux_data_ok=True)),
        ("未完成持仓对账",
         DataQuality(positions_synced=False,
                     positions_sync_reason="读不到执行端持仓")),
        ("辅助数据整体缺失",
         DataQuality(positions_synced=True,
                     aux_data_ok=False, aux_reason="溢价率接口超时")),
    ]
    for label, dq in cases:
        allowed, vetoes = chain.runner.gates.apply([buy, sell], dq)
        # 注意变量名：这里**别**叫 keep —— 外层有个同名的 `keep` 参数管着
        # 「演示完要不要保留运行目录」。同名会把它覆盖成一个非空集合，
        # 于是 `if keep:` 永远为真、临时目录永远不清理。
        # （本文件初版就是这么写的，跑出来才发现。）
        passed = {s.action for s in allowed}
        verdict = ["买入✅" if "buy" in passed else "买入⛔",
                   "卖出✅" if "sell" in passed else "卖出⛔"]
        note = f"  （{vetoes[0].reason}）" if vetoes else ""
        print(f"  {label:26s} → {'  '.join(verdict)}{note}")
    print("\n  （闸门链刻意**不短路**：一条信号同时触发多道时，理由会全部记下 ——"
          "\n    上面每个 case 只制造一个问题，是为了让对应关系清楚。）")
    print("\n  读法：**行情失效时买卖全停**（价格不可信，任何决策都没意义）；")
    print("  而**对账失败/辅助数据缺失时只禁买、卖出照常** —— 因为卖错了执行端会")
    print("  「无该标的持仓」兜住，买错了却会直接变成双持仓。")
    print("  判据是「哪边错了更疼」，不是「哪边更保守」。一律全停看着安全，")
    print("  实际会让你在数据小毛病时错过该有的止损。")

    _hr("7. 心跳与最终状态")
    hb = chain.runner.stores.heartbeat.read()
    print(f"  心跳：last_task={hb.last_task!r} status={hb.status!r}")
    print("  status 是**严重度阶梯**（alive < positions_stale < error），只升不降、"
          "\n  跨日复位。监控端按它决定发不发告警，所以「不降级」是接口语义。")
    print("\n  严重度的映射（见 runner._severity_for）：")
    print("    行情失效 → error（价格不可信，必须人来处理）")
    print("    未对账 / 辅助数据缺失 → positions_stale（功能受限但没裸奔）")
    print("    正常 → alive（只报平安，不改变严重度）")

    print(f"\n  最终目录内容：")
    for p in sorted(Path(chain.config.channel_dir).iterdir()):
        print(f"    {p.name}  ({p.stat().st_size} 字节)")

    if keep:
        print(f"\n已保留运行目录：{runtime}")
    else:
        shutil.rmtree(runtime, ignore_errors=True)
        print("\n运行目录已清理（用 --keep 可保留）")

    print(f"\n{BANNER}")
    print("  闭环完成：策略 → 闸门 → 信号 → 执行 → 回执 → 持仓回流")
    print(BANNER)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalchain.demo",
        description="signal-chain 端到端演示（零凭据、零网络）")
    parser.add_argument("--keep", action="store_true",
                        help="保留临时运行目录以便查看产物")
    parser.add_argument("--quiet", action="store_true",
                        help="只输出演示内容，不输出日志")
    args = parser.parse_args(argv)

    if not args.quiet:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)-7s %(message)s")
    else:
        import logging
        logging.disable(logging.CRITICAL)
    return run_demo(keep=args.keep, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
