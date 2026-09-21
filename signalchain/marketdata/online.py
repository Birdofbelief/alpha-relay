# -*- coding: utf-8 -*-
"""在线行情源：腾讯日线 + 新浪快照。

## 命名说明

项目目录叫「akshare」，但实际取数**不走 akshare** —— 历史日线走腾讯财经
（`web.ifzq.gtimg.cn`），实时快照走新浪（`hq.sinajs.cn`）。
akshare 只在交易日历那一处用到（见 `calendar.py` 的默认 loader）。

这个事实对使用者是好消息：**本仓库的核心路径只需要 `requests` + `pandas`**，
akshare 是可选依赖。所以本模块叫 `online` 而不是 `akshare_*`。

## 三个反直觉的实测事实（都写进了代码）

### 1. 腾讯返回 `day`（未复权）不是错误

腾讯只在「确有除权/折算需要修正」时才返回 `qfqday`；**不需要修正的标的只返回 `day`**。
实测 21 只里有 8 只如此。

所以 `day` 不能拒收 —— 拒收会直接废掉 38% 的候选池，而且废掉的恰好是
「从来没除过权」的那批标的（往往是最稳的那批）。代码里接受两者，
但**记录每只的来源**，因为来源一旦变化就说明复权基准变了。

### 2. 新浪对停牌标的返回的是**完整字段 + 价格全 0**

形如 `永赢500,0.000,1.026,0.000,...` —— 字段一个不少，价格是 0。
而真正取不到的代码是**整行都不出现**。

这两者含义相反，绝不能合并：
  - 整行缺失 → 数据管道坏了 → **整批不可信**
  - 整行在、价格 0 → **停牌**，很正常 → 只跳过这一只

### 3. 空串要当 0，NaN 要当「失效」

上游对「没这个值」和「这个值是脏的」用了不同的表示：
  - 空串 / 无法解析 → `0.0`，交给停牌判断处理（沿用实盘行为）
  - `nan` / `inf`     → `None`，视为**行情失效**，触发严格全停

把 `nan` 也归一成 0 的话，一次上游脏值就会伪装成「全市场停牌」——
信号静默消失，而且不报错。
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from typing import Sequence

import pandas as pd
import requests

from ..models import to_jq  # noqa: F401 - 线格式的真相源在 models，此处仅再导出
from .base import MarketDataError, Quote, QuoteBatch

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_QUOTE = "https://hq.sinajs.cn/list={codes}"


def to_sina(code: str) -> str:
    """6 位代码 → 腾讯/新浪用的**带市场前缀**形式。

    与 `models.to_jq` 是同一套「5/6/9 开头属上交所」规则的两个方向：
    一个给北向的 URL 用（`sh600519`），一个给线格式用（`600519.XSHG`）。
    规则必须一致 —— 不一致会让「沪市的标的按深市去查」，而腾讯对不存在的
    代码返回空节点，表现为「这只标的数据拿不到」，排查时极易误判成网络问题。
    """
    return f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"


def parse_sina_num(text) -> float | None:
    """新浪字段 → 数值。见模块文档「事实 3」。"""
    if text is None or text == "":
        return 0.0
    try:
        value = float(text)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return None
    return value


class OnlineMarketData:
    """实时抓取（无持久缓存）。

    本类**只做一次请求**，不重试、不管时间预算 ——
    重试与截止时间属于调度层的职责（见 `RetryingMarketData` 与 `runner.py`）。
    把两者分开，才能单独测试「解析是否正确」与「重试策略是否合理」。

    类内有一个**进程内的快照缓存**（`snapshot_ttl` 秒）。它与持久缓存的区别很关键：
    它只活在进程里，重启即消失，且有**年龄上限** —— 见 `_snapshot_or_none`。
    """

    def __init__(self, timeout: float = 15.0, snapshot_ttl: float = 300.0,
                 max_snapshot_age: float = 300.0) -> None:
        self.timeout = timeout
        self.snapshot_ttl = snapshot_ttl
        self.max_snapshot_age = max_snapshot_age
        self._snapshot: tuple[datetime, pd.DataFrame] | None = None
        #: 每只标的最近一次的复权来源：`qfqday`（已复权）/ `day`（未修正）
        self.sources: dict[str, str] = {}
        self.stats = {"quote_calls": 0, "history_calls": 0, "stale_snapshot_drops": 0}

    # ---------------- 实时快照 ----------------

    def quotes(self, codes: Sequence[str]) -> QuoteBatch:
        self.stats["quote_calls"] += 1
        codes = list(codes)
        df = self._snapshot_or_none()
        if df is None:
            df = self._fetch_snapshot(codes)
        batch = QuoteBatch(fetched_at=datetime.now())
        if df is None or df.empty:
            # 整体取不到 → 全部算缺失。调用方（闸门）会据此整体停手。
            batch.missing = list(codes)
            return batch

        present = set(df["code"])
        for code in codes:
            if code not in present:
                # 整行缺失 = 管道异常（**不是**停牌，见模块文档）→ 交给闸门决定
                batch.missing.append(code)
                continue
            row = df[df["code"] == code].iloc[-1]
            quote = Quote(
                code=code,
                name=str(row.get("name", "")),
                price=row.get("price"),
                prev_close=row.get("prev_close"),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
            )
            batch.quotes[code] = quote
            # 数值不可信 ⇒ invalid（脏值）；数值可信但为 0 ⇒ 停牌，不算 invalid。
            if not quote.is_finite:
                batch.invalid.append(code)
        return batch

    def _snapshot_or_none(self) -> pd.DataFrame | None:
        """取出未过期的快照；过期则返回 None。

        ★ **过期快照一律丢弃，绝不降级使用。**

        旧实现用 `timedelta.seconds`（不跨天）且没有年龄上限，于是会把**昨天**的
        行情当成今天的实时价。用这种价格选股、再把它写进信号的限价字段，
        会让执行端挂出成交不了或明显偏离的单 —— 而这种单看起来完全正常，
        只是永远不成交，或者以坏价格成交。

        拿不到新价就**承认拿不到**，由上层按重试预算兜住；重试也失败就不发信号。
        """
        if self._snapshot is None:
            return None
        at, df = self._snapshot
        age = (datetime.now() - at).total_seconds()
        if age < self.snapshot_ttl:
            return df
        if age > self.max_snapshot_age:
            self.stats["stale_snapshot_drops"] += 1
            log.warning("🚫 现有行情快照已过期（%.0f 秒前，上限 %.0f），不使用",
                        age, self.max_snapshot_age)
            self._snapshot = None
        return None

    def _fetch_snapshot(self, codes: Sequence[str]) -> pd.DataFrame | None:
        url = SINA_QUOTE.format(codes=",".join(to_sina(c) for c in codes))
        headers = dict(HEADERS, Referer="https://finance.sina.com.cn/")
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - 网络失败是预期内情况
            log.warning("获取实时行情失败: %s", e)
            return None
        if resp.status_code != 200:
            log.warning("实时行情 HTTP 异常: %s", resp.status_code)
            return None

        # 新浪返回 GBK 编码的 JS 变量赋值，必须显式指定编码，否则中文名乱码
        resp.encoding = "gbk"
        rows = []
        for line in resp.text.split("\n"):
            line = line.strip()
            if not line or "=" not in line or '"' not in line:
                continue
            try:
                var_part = line.split("=")[0].strip()
                value_part = line.split('"')[1]
                if not value_part:
                    continue
                fields = value_part.split(",")
                if len(fields) < 6:
                    continue
                rows.append({
                    "code": var_part.replace("var hq_str_", "")[2:],
                    "name": fields[0],
                    "open": parse_sina_num(fields[1]),
                    "prev_close": parse_sina_num(fields[2]),
                    "price": parse_sina_num(fields[3]),
                    "high": parse_sina_num(fields[4]),
                    "low": parse_sina_num(fields[5]),
                })
            except Exception:  # noqa: BLE001 - 单行解析失败跳过，不影响其余
                continue

        if not rows:
            log.warning("实时行情返回空内容")
            return None

        df = pd.DataFrame(rows)
        self._snapshot = (datetime.now(), df)
        return df

    # ---------------- 历史日线 ----------------

    def history(self, code: str, bars: int) -> pd.DataFrame:
        """取前复权日线。取不到抛 `MarketDataError`（**不返回空表**）。

        返回空表会被调用方当成「这只标的历史很短」，从而静默走错分支 ——
        比如把一个刚成立的标的当成数据故障，或反过来把故障当成短历史。
        """
        self.stats["history_calls"] += 1
        # 多取 30 根：校验会丢掉一些行（停牌、异常），留出余量才能凑够 bars 根
        datalen = bars + 30
        params = {"param": f"{to_sina(code)},day,,,{datalen},qfq"}
        try:
            resp = requests.get(TENCENT_KLINE, params=params,
                                headers=HEADERS, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise MarketDataError(f"{code}: 历史日线请求失败: {e}") from e

        if resp.status_code != 200:
            raise MarketDataError(f"{code}: 历史日线 HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise MarketDataError(f"{code}: 历史日线返回非 JSON（可能被限流页拦截）") from e

        node = (payload or {}).get("data", {}).get(to_sina(code)) or {}
        # ★ 优先 qfqday；没有则接受 day —— 见模块文档「事实 1」
        if node.get("qfqday"):
            kline, source = node["qfqday"], "qfqday"
        elif node.get("day"):
            kline, source = node["day"], "day"
        else:
            raise MarketDataError(f"{code}: 历史日线无数据节点（键={sorted(node)}）")

        prev = self.sources.get(code)
        if prev is not None and prev != source:
            # 复权基准变了 —— 新旧序列不能拼接。缓存层会据此丢弃旧缓存。
            log.warning("📐 %s 复权来源发生变化：%s → %s，历史序列不可拼接",
                        code, prev, source)
        self.sources[code] = source

        rows = []
        for item in kline:
            if len(item) < 6:
                continue
            try:
                rows.append({
                    "date": item[0],
                    "open": float(item[1]),
                    "close": float(item[2]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "volume": float(item[5]),
                })
            except (TypeError, ValueError):
                continue
        if not rows:
            raise MarketDataError(f"{code}: 历史日线解析后为空（源={source}）")

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def describe(self) -> str:
        return (f"online(腾讯日线+新浪快照, 超时{self.timeout:.0f}s, "
                f"快照{self.stats['quote_calls']}次)")


class RetryingMarketData:
    """给任意 `MarketData` 套上重试 + 硬截止。

    两个设计点：

    **一、缺行也要重试，而且要先把残表丢掉。**
    「整体失败」和「少几只」是同一个后果（这一轮没有完整行情），所以走同一套重试。
    但如果源内部有快照缓存，重试会拿回**同一份残表** —— 于是必须显式丢弃它，
    否则重试只是空转。这里通过重新发起请求前调用 `inner.quotes()` 实现
    （源自身的 TTL 逻辑决定是否真的重取）。

    **二、重试必须有硬截止。**
    关键成交窗口只有几分钟（如 14:45~14:55），一次没有上限的重试会把窗口耗光，
    结果是「为了拿到行情而错过了能用行情的时刻」。截止时间一到就停，
    **带着当前结果返回** —— 让上层按 fail-closed 处理，而不是卡在这里。
    """

    def __init__(self, inner, attempts: int = 2, interval: float = 5.0,
                 deadline=None) -> None:
        self.inner = inner
        self.attempts = attempts
        self.interval = interval
        self.deadline = deadline  # callable -> datetime | None
        self.stats = {"retries": 0, "aborted": 0}

    def _deadline(self) -> datetime | None:
        return self.deadline() if callable(self.deadline) else self.deadline

    def quotes(self, codes) -> QuoteBatch:
        batch = self.inner.quotes(codes)
        if batch.ok:
            return batch

        for attempt in range(1, self.attempts + 1):
            deadline = self._deadline()
            if deadline is not None and datetime.now() + \
                    timedelta(seconds=self.interval) >= deadline:
                self.stats["aborted"] += 1
                log.error("⛔ 已到行情重试截止时间（%s），停止重试，用当前结果继续",
                          deadline.strftime("%H:%M"))
                break
            log.warning("⚠️ 实时行情不完整（%s），%.0f 秒后重试（第 %d/%d 次）",
                        batch.problems(), self.interval, attempt, self.attempts)
            time.sleep(self.interval)
            self.stats["retries"] += 1
            batch = self.inner.quotes(codes)
            if batch.ok:
                return batch

        log.error("🚫 重试 %d 次后行情仍不完整：%s", self.attempts, batch.problems())
        return batch

    def history(self, code: str, bars: int) -> pd.DataFrame:
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return self.inner.history(code, bars)
            except MarketDataError as e:
                last = e
                if attempt < self.attempts:
                    log.warning("⚠️ %s 历史获取失败，%.0f 秒后重试: %s",
                                code, self.interval, e)
                    time.sleep(self.interval)
                    self.stats["retries"] += 1
        raise MarketDataError(f"{code}: 重试 {self.attempts} 次后仍失败: {last}")

    def describe(self) -> str:
        return (f"retry({self.inner.describe()}, 试{self.attempts}次/"
                f"间隔{self.interval:.0f}s/重试{self.stats['retries']})")
