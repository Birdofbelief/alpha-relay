# -*- coding: utf-8 -*-
"""历史缓存装饰器。

行情层最容易被低估的一层。它要回答三个问题，而三个答案都不是显然的：

## 一、缓存落后几个交易日还可以用？

分三档，而不是「新鲜/不新鲜」两档：

| 落后 | 判定 | 处理 |
|---|---|---|
| 0 个交易日 | 健康 | 直接用 |
| 1 个交易日 | 可疑但可用 | **告警**后用 —— 通常是上游当天还没更新 |
| ≥2 个交易日 | 失效 | **跳过这一只**，不参与决策 |

为什么中间要留「1 天」这一档：免费行情源当天日线经常要到收盘后才补齐，
此时缓存天然落后一天。若把这天判成失效，每个交易日的前半程都会整体跳过 ——
而策略需要的恰恰是「截至昨收」的序列，昨收就是**完整的**。判死它等于每天上午瘫痪。

反过来，落后 ≥2 天就真的说明上游出问题了，此时**跳过这一只**（而不是整批停），
因为没有它的信号是拿旧数据算出来的，而其它标的可能还好好的。

## 二、复权口径变了怎么办？

同一个标的的历史序列，在不同时间点取，**复权口径可能不一样**。
两份口径不同的序列拼在一起，会在拼接处凭空造出一个巨大缺口 ——
策略会把它读成趋势破位。所以缓存要记下「这份是用什么口径存的」，
口径变了就**整份丢弃重取**，绝不在旧数据后面追加新口径的尾巴。

## 三、当天那根 K 线要不要算？

不要。盘中取回来的「今天」是一根**还在变的** K 线。
用它算出来的指标，上午十点和下午两点结果不同 —— 于是同一天不同时刻跑出的信号
不可比，你自己那套回测也对不上。本模块在读取时**恒定丢弃日期 ≥ 今天的行**，
让「历史」在任何时刻都只包含已收盘的完整 K 线。

副作用是收盘后也拿不到当天那根。这是刻意的：**确定性比那一点点新鲜度更值钱**。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..calendar import TradingCalendar
from .base import (
    MarketDataError,
    QuoteBatch,
    detect_adjustment_jump,
    validate_history,
)

log = logging.getLogger(__name__)

#: 新鲜度三档（落后交易日数 → 是否可用）
FRESH = 0
STALE_WARN = 1


class CachedMarketData:
    """给任意 `MarketData` 套上本地历史缓存。

    读取顺序：**先试在线，失败退缓存**。
    这个顺序不能反 —— 反了就等于「有缓存就永远不更新」，
    缓存会退化成一份越来越旧、却从不报错的快照。

    但退缓存时**必须过新鲜度闸门**：能退，不代表退回来的东西能用。
    一个退回到两周前数据的缓存，比直接失败危险得多，因为它不报错。
    """

    def __init__(
        self,
        inner,
        cache_dir: str | Path,
        calendar: TradingCalendar,
        adjust: str = "qfq",
        max_stale_days: int = STALE_WARN,
    ) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calendar = calendar
        self.adjust = adjust
        self.max_stale_days = max_stale_days
        self.stats = {"hit": 0, "miss": 0, "stale_used": 0, "stale_skip": 0,
                      "refetch": 0, "fallback": 0, "rejected": 0}

    # ---------------- 实时行情：不缓存 ----------------

    def quotes(self, codes) -> QuoteBatch:
        """实时行情**直接透传，不缓存**。

        给快照加缓存是危险的：进程重启后拿到一份「看起来正常」的陈旧报价，
        而所有闸门都会判它合格（数值确实都合法），于是拿昨天的价格下单。
        快照的新鲜度无法从数据本身判断，所以这一层不做缓存。
        """
        return self.inner.quotes(codes)

    # ---------------- 历史 ----------------

    def history(self, code: str, bars: int) -> pd.DataFrame:
        now = datetime.now()
        target = self.calendar.previous_closed_trading_day(now)

        cached, cache_problems = self._load(code)
        behind = None
        if cached is not None and target is not None:
            behind = self.calendar.trading_days_behind(
                self._last_date(cached), target)

        if cached is None or behind is None or behind > 0:
            fresh = self._fetch_and_store(code, bars)
            if fresh is not None:
                if behind and behind > 0:
                    self.stats["refetch"] += 1
                return fresh
            if cached is None:
                raise MarketDataError(
                    f"{code}: 在线获取失败且无可用缓存"
                    + (f"（缓存问题：{'; '.join(cache_problems)}）" if cache_problems else ""))
            self.stats["fallback"] += 1
            log.warning("◆ %s 在线获取失败，退回缓存（落后 %s 个交易日）", code, behind)

        # 走到这里 = 用了缓存。必须过新鲜度闸门。
        return self._accept_cached(code, cached, behind)

    def _fetch_and_store(self, code: str, bars: int) -> pd.DataFrame | None:
        """在线取一份新的。失败返回 None（**不抛**）—— 调用方要能退缓存。"""
        try:
            raw = self.inner.history(code, bars)
        except Exception as e:  # noqa: BLE001 - 取失败是预期内情况
            log.warning("◆ %s 在线历史获取失败: %s", code, e)
            return None

        frame, problems = validate_history(raw)
        if frame.empty:
            log.warning("◆ %s 在线历史全部非法，丢弃：%s", code, "; ".join(problems))
            return None

        jump = detect_adjustment_jump(frame)
        if jump:
            # 不缓存、不返回 —— 一份未复权的序列会造出凭空的趋势破位。
            self.stats["rejected"] += 1
            log.error("◆ %s 复权异常，拒用该数据：%s", code, jump)
            return None

        # ★ 先丢掉未收盘的那根，**再**落盘。
        # 顺序反了会留下一个很难发现的错误：盘中取回来的「今天」是一根还在变的
        # K 线，把它存下来，到了明天它就被当成「已收盘的今天」正常读出来用 ——
        # 而且用的是上午十点的那个版本。这正是本模块文档「三」要避免的事，
        # 只是它在**落盘**这一步被漏掉了（读取时丢弃不能补救已经写进磁盘的脏行）。
        closed = self._drop_unclosed(frame, datetime.now())
        self._store(code, closed)
        return closed

    def _accept_cached(self, code: str, frame: pd.DataFrame,
                       behind: int | None) -> pd.DataFrame:
        """缓存能不能用，只看这一个闸门。"""
        if behind is None:
            # 日历不可用 → 无从判断新旧 → 保守跳过这只，而不是赌它新。
            self.stats["stale_skip"] += 1
            raise MarketDataError(f"{code}: 交易日历不可用，无法判断缓存新鲜度，跳过")

        if behind > self.max_stale_days:
            self.stats["stale_skip"] += 1
            raise MarketDataError(
                f"{code}: 缓存落后 {behind} 个交易日（阈值 {self.max_stale_days}），跳过该标的")

        if behind == STALE_WARN:
            self.stats["stale_used"] += 1
            log.warning("◆ %s 使用落后 %d 个交易日的缓存（可用但需留意）", code, behind)
        else:
            self.stats["hit"] += 1
        return self._drop_unclosed(frame, datetime.now())

    # ---------------- 磁盘 ----------------

    def _path(self, code: str) -> Path:
        safe = code.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe}.csv"

    def _meta_path(self, code: str) -> Path:
        return self._path(code).with_suffix(".meta.json")

    @staticmethod
    def _last_date(frame: pd.DataFrame) -> date:
        v = frame["date"].iloc[-1]
        return v.date() if hasattr(v, "date") else v

    def _load(self, code: str) -> tuple[pd.DataFrame | None, list[str]]:
        """读缓存。任何问题都返回 `(None, 原因)` —— **绝不上抛**。

        缓存损坏不该让整轮停摆，它只该让这一只走在线路径。
        """
        path, meta_path = self._path(code), self._meta_path(code)
        if not path.exists():
            return None, ["无缓存文件"]
        try:
            frame = pd.read_csv(path)
        except Exception as e:  # noqa: BLE001
            return None, [f"缓存读取失败: {e}"]

        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}

        # ★ 复权口径变了 → 整份丢弃。绝不拼接口径不同的两段序列。
        if meta.get("adjust") not in (None, self.adjust):
            return None, [f"复权口径已变（缓存 {meta.get('adjust')} → 现在 {self.adjust}）"]

        frame, problems = validate_history(frame)
        if frame.empty:
            return None, problems or ["缓存内容为空"]
        return frame, problems

    def _store(self, code: str, frame: pd.DataFrame) -> None:
        """落盘。先写临时文件再 `os.replace`，避免读到写了一半的表。"""
        path, meta_path = self._path(code), self._meta_path(code)
        tmp = path.with_suffix(".csv.tmp")
        try:
            frame.to_csv(tmp, index=False)
            os.replace(tmp, path)
            meta = {
                "code": code,
                "adjust": self.adjust,
                "bars": len(frame),
                "last_date": str(self._last_date(frame)),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
            meta_tmp = meta_path.with_suffix(".json.tmp")
            meta_tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(meta_tmp, meta_path)
        except Exception as e:  # noqa: BLE001 - 写缓存失败不影响本轮
            log.warning("◆ %s 缓存写入失败（不影响本轮）: %s", code, e)

    # ---------------- 未收盘 K 线 ----------------

    @staticmethod
    def _drop_unclosed(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
        """丢弃日期 ≥ 今天的行。见模块文档「三」。

        给整份序列一个**与运行时刻无关**的定义：任何时刻读到的「历史」，
        都恰好是「截至最近一个已收盘交易日」。今天早上跑和今天下午跑结果一样。
        """
        if frame.empty:
            return frame
        today = now.date()
        dates = frame["date"]
        if hasattr(dates.iloc[0], "date"):
            mask = dates.apply(lambda v: v.date() < today)
        else:
            mask = pd.to_datetime(dates).dt.date < today
        dropped = int((~mask).sum())
        if dropped:
            log.debug("◆ 丢弃 %d 根未收盘 K 线", dropped)
        return frame[mask].reset_index(drop=True)

    # ---------------- 观测 ----------------

    def describe(self) -> str:
        return (f"cached({self.inner.describe()}, 口径={self.adjust}, "
                f"目录={self.cache_dir.name}, "
                f"命中{self.stats['hit']}/留存{self.stats['stale_used']}/"
                f"跳过{self.stats['stale_skip']})")
