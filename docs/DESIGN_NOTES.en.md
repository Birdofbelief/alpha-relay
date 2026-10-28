# Design Notes

**This file records "why it's built this way," and "what happens if you don't."**

The wire format and the APIs live elsewhere ([`WIRE_CONTRACT.md`](WIRE_CONTRACT.md),
[`ARCHITECTURE.md`](ARCHITECTURE.md)). For what this thing is even *for*, see
[`README.md`](../README.md). What's kept here are the judgments
**bought from live trading** — every one of them cost something, so every one of them
says what it cost.

Translated from [`DESIGN_NOTES.md`](DESIGN_NOTES.md), starting at commit `3d8c9d7`.
The two files are always committed together, and
[`tests/test_docs_tripwire.py`](../tests/test_docs_tripwire.py) mechanically checks that
they name the same components — so "the English one quietly rotted" is a test failure,
not something a reader has to notice.

> ★ **The Chinese version is authoritative.** Where the two disagree, it is right.
> This file exists so that readers who don't read Chinese can skim the reasoning —
> not to be a second source of truth.

---

## 1. Failure direction: the criterion is "which side hurts more," not "which side is safer"

This is the most important rule in the repo, and the easiest one to get wrong.

### The claim

When data is incomplete, the default action is "stop," not "carry on."
**But *which half* stops has to follow from the asymmetry of the costs.**

### Three gates, three directions

| Gate | Triggers on | Stops | If you reversed it |
|---|---|---|---|
| `QuoteIntegrityGate` | any symbol's quote missing / NaN / non-positive | **all emission** | "skip just that one": every other symbol's decision is still built on bad data — and **a sell is just as wrong**; you might sell a position that was healthy |
| `PositionsSyncedGate` | reconciliation with the executor didn't complete | **buys; sells go through** | "stop everything": if you can't read positions you can't sell, which stops **your stop-losses too** and leaves the exposure on |
| `AuxDataGate` | key auxiliary data missing entirely | **no new positions; existing ones still managed** | "stop everything": auxiliary data only picks new symbols; without it, take-profit / stop-loss on what you already hold is unaffected |

### ★ The counter-intuitive part

**"Stop everything" looks safest. It isn't.** It treats three problems with wildly
different costs as if they were one.

Look at the second row: when positions can't be read, a wrong buy costs you
"possibly opened a duplicate position" (real money, irreversible); a wrong sell costs
"missed one sell" (you can sell again next round). **Those are orders of magnitude
apart — why would they stop together?** So the direction here is
**block buys, allow sells**, not "when in doubt, stop everything."

The first row is the opposite. When quotes are bad, "skip only the bad symbol" looks
more *precise*, but bad quotes contaminate **the decision itself**, not one symbol.
A sell can also be a decision made at a wrong price.

> **The criterion has to be written down, or someone will later "fix" it as a bug.**
> A gate that blocks buys but allows sells looks a lot like a typo to someone reading
> only the code — so every gate in `gates.py` ends its docstring with
> "what happens if you reverse this."

### The cost

Getting the direction wrong costs far more than "doing one less thing." This one was
bought with **real money lost on a live account**, not derived from design principles.

---

## 2. Idempotency: "naturally idempotent" instead of dedup

### The claim

A signal carries a deterministic id, and that id *is* the idempotency key:

```
id = strftime("%Y%m%d%H%M%S") + "_" + code + "_" + action
```

**Same market state → same id → the consumer can recognize "I already handled this."**

### ★ That uniqueness has a precondition, and the precondition isn't under this framework's control

The producer **does not deduplicate**. So the precondition for id uniqueness is:

> within one second, for one code, in one direction, published only once.

That holds in a single process, single thread. **It is not a global uniqueness
guarantee** — the moment you have multiple processes emitting concurrently, two
different orders share one idempotency key, and the executor treats the second as a
duplicate and **silently drops it**.

One layer of mitigation: **the timestamp comes from `ctx.now`, not the wall clock.**
This isn't only about reproducibility — mix in the wall clock and replaying the same
day's quotes generates a batch of brand-new ids, "have I handled this?" forever answers
"no," and the idempotency key stops working entirely.

### ★★ On retry the id must not change: anchor to the **planned** time

The timestamp inside that id comes from `ctx.now` — the moment this round **actually
ran**. Once tasks retry, that's a trap:

```
planned 14:45, first run at 14:45:00 → id suffix 144500
retry at 14:46:30                    → id suffix 144630   ← a new id!
```

The executor uses `signal_id` as its idempotency key, so the second pass looks like
**a brand-new order**. This isn't a theoretical risk: once "a failed round is retried"
is fixed, retrying goes from "barely happens" to "the default path."

So `StrategyContext.signal()` builds the id from:

- `anchor` (the planned time, passed in by the scheduler) — decides `sig_id`
- `created_at` / the wire `time` (the actual moment) — records *when the decision was made*

The two are equal normally and diverge only when a run is late or retried. Calling
`run_round()` by hand (no planned time) leaves `anchor` empty and falls back to the old
behaviour.

> ★ Generalized: **any dimension of an idempotency key that can change on retry has
> already broken idempotency** — even if the changing dimension looks "more truthful."

### Why retries are safe (this one isn't obvious)

`GistTransport._request` retries by **resending the exact same body, and never re-GETs
inside the loop.**

That's safe because this backend's write has **whole-document overwrite** semantics —
replaying the same body is naturally idempotent. **If the write semantics were changed
to "append," this retry becomes a bug immediately**, producing duplicate signals.
(Which is why the `_request` docstring states the precondition. Anyone changing the
write semantics has to read that sentence first.)

### Cost / known gap

- Two **different** orders, same second, same code, same direction → the second is
  treated as a duplicate. The current strategy doesn't produce that sequence, but it's
  a **convention**, not a guarantee.
- Fixing it properly means adding a dedup layer on the producer side; this framework
  deliberately doesn't, because that would introduce new shared state.

---

## 3. Time budget: three numbers, three kinds of failure

In an unattended program, "missed" and "duplicated" are twin accidents.
Three constants each treat one:

| Constant | Value | Treats |
|---|---|---|
| (matching) `>=` rather than exact | — | **Missed**: in a 30-second loop, the second `14:57:00` most likely isn't inside the loop at all. Exact match = the task never runs that day |
| `catchup_minutes` | 30 | **Far too late**: starting up after the close and back-filling the 14:57 task means placing orders from a decision that expired long ago |
| `startup_min_budget_seconds` | 60 | **Eating your own window**: refreshing the history cache on startup takes so long that the next task gets judged overdue and skipped |
| `task_fail_limit` | 3 | **Log flooding**: one deterministic exception thrown every 30 seconds is 2880 throws a day; the real alerts drown |

### ★ Three details that are easy to miss

1. **A skip must still be marked "executed."** Otherwise it's re-evaluated every
   30 seconds and the log fills up with the same "skipped."
2. **The execution record must be persisted per day — that's correctness, not
   optimization.** Forget "already ran today" across a restart and it **runs again**;
   in trading, running again really does place the order again.
3. **`>=` turns "missed" into "late," but late has to be bounded.** These two come as a
   pair; either one alone is broken:
   only `>=` without `catchup_minutes` → still back-filling after the close;
   only `catchup_minutes` without `>=` → 30 seconds late means the whole day is skipped.

### ★★ A failure must count as a failure: `RoundResult.error` is one too

The round runner (`Runner.run_round`) **does not kill the process over a single round's
exception**: it records the exception into `RoundResult.error` and returns. That's right —
one task failing shouldn't take the others down.

But the scheduler (`run_due`) **has to notice that return value.** It used to look only at
"did it raise," so on the default path:

- the task failed every day while `executed_tasks` still recorded it ⇒ **no retry that day**
- the heartbeat still said alive ⇒ monitoring never fired
- the "consecutive failure limit" never triggered ⇒ those three log lines never appeared

Together: **the process is alive, the heartbeat is healthy, the task fails every day, and
the terminal shows no trace of it.** The fix: a non-empty `result.error` counts as a
failure — don't mark it executed, bump the fail count, escalate the heartbeat to error;
only at `task_fail_limit` do we explicitly give up for the day.

★ This fix **has to ship together with "anchor to the planned time"** above. Retrying
re-runs `decide`; without the anchor that means duplicate orders. Two patches, one unit.

### ★★ A known sharp edge: the consecutive-failure window is only 90 seconds

`task_fail_limit = 3` × `LOOP_INTERVAL_SECONDS = 30` ⇒
**three consecutive failures of the same task (about 90 seconds) give up on that task
for the day and set the heartbeat to error.**

The intent is right (not giving up = 2880 log lines), but the window means:
**a transient fault lasting 90 seconds (a network blip, one 5xx from the data source,
one restart) can destroy that task for the day.** With the fix above, this window now
**actually applies** — before, it was mostly decoration.

This is **deliberately kept**, not an oversight — giving up is **explicit** (it logs and
sets the heartbeat to error for a human), not silent. But the user has to know the number.
If it feels short, raise `SC_TASK_FAIL_LIMIT`: multiply it by 30 seconds to get the total
wait before giving up.

> ★ Which yields a more general lesson: **"it's wrapped in a try" ≠ "it's safe."**
> The outermost `except` in the main loop does keep the process alive — and it also turns
> **"crashed" into "gave up."** Crashes are loud; giving up is silent.
> Troubleshooting that only confirms "the process is still running" will miss this entire class.

### Why live with the error

The outermost loop catches everything and continues. Compare:
- **Crash** → none of the day's remaining tasks run, and **there's no heartbeat**, so
  monitoring sees nothing at all
- **Continue** → at least the heartbeat is there, a human can see it, there's time to intervene

A program running alone is worth far more **alive with an error** than dead.
The one exception: `OwnershipError` (writing the wrong channel) **must be allowed to be
loud** — that's a programming error, and swallowing it hides an architectural violation.

---

## 4. Network resilience: telling "worth retrying" from "retrying only makes it worse"

### The claim

Retrying isn't "try a few more times." **Classify first, back off second, count third.**

```python
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
```

**401 / 403 / 404 are deliberately excluded.** They mean "token expired," "gist deleted,"
"wrong ID" — that's **misconfiguration**, not a transient fault. Retrying only:
1. burns the quota (Gists allow 5000/hour with a token)
2. **delays you finding out about the misconfiguration** — and the most expensive thing
   about that class of error is finding it late

### Four concrete practices

| Practice | Reason |
|---|---|
| **Timeouts must have a value** (default 15s) | A **stuck** request is harder to diagnose than a **failed** one, and it eats straight into the trading window's time budget |
| **Exponential backoff + jitter** (`1.5·2^n + U(0,0.5)`) | Jitter is for de-synchronizing — without it, multiple processes retry together and fail together |
| **`Retry-After` takes priority** | On 429 the server tells you exactly how long to wait; your own guessed backoff sequence is only worse |
| **Exhausted retries return the last response** | Let the caller take its own degradation branch, rather than throwing an exception up here that it can't act on |

### Reads are deliberately not cached

`GistTransport.read()` sends a real HTTP request every time — **no caching or reuse of
the whole gist.**

This isn't an optimization left undone; it's an optimization **refused**: caching would
create a time gap between "what you read" and "what's actually stored," and on a trading
path that gap shows up as **"made a new decision from stale positions"** — the most
expensive class of error there is. Quota for correctness; here that's a good trade.

### Quota discipline

Anonymous Gists allow 60 requests/hour, 5000 with a token. So the backend **reads and
writes only the one file it needs each time**, never the whole gist — the PATCH body
contains just that one file, and the gist API replaces only the files explicitly given.

### Credentials never go into logs

`describe()` returns `gist:<sha256(id)[:8]>` — **distinguishable, but not usable for
access**. Neither the gist_id nor the token appears in a log line.

> A secret gist **is not private** — it's "not indexed," not "not readable."
> Anyone who knows the ID can read it. So the Gist ID itself is treated as a credential.

### ★ Bare requests inside third-party libraries: the timeout isn't ours to set

The "timeouts must always have a value" rule above only covers requests **this repository
sends itself**. The nastier trap lives in someone else's code: `akshare`'s
`tool_trade_date_hist_sina()` makes its own HTTP call **without a timeout**, and requests
has **no default upper bound** when none is passed.

The result is not a crash but a **frozen main loop**: the process is alive, heartbeats keep
being written, and not a single scheduled task runs that day — the log just stops on its
last line. That shape sends you hunting through strategies and brokers, not the calendar.

Two conclusions:

1. **`socket.setdefaulttimeout()` does not work on requests.** It only applies when the
   argument is omitted entirely, while requests explicitly stuffs that value into its own
   call — a prescription that looks completely right and is measurably useless.
2. **The right place is our own layer.** Wrap the loader in a bounded call
   (`calendar.py`, `_bounded_load`): give up on time → keep the old cache → alert →
   the main loop keeps going. And **only one load may be in flight at a time**, otherwise
   every refresh cycle parks another thread and "stuck" becomes "stuck plus leaking."

Residual risk, stated honestly: this bound covers *how long we wait*, **not DNS resolution**
(`getaddrinfo` has no upper bound).

### Failover sources: whole-table swap is the only acceptable shape

Live quotes are single-source with same-source retry today (`RetryingMarketData`). If a
second quote vendor is ever added, the five items below are **constraints**, not
suggestions — each one maps to a real incident:

| Constraint | Why |
|---|---|
| **Swap the whole table, never mix per symbol** | mixing puts two vendors' prices side by side in **the same comparison** ⇒ silently changes "which one to buy," unreproducible from logs afterwards |
| **A fallback must issue its own request, never reuse a source that serves another purpose** | reuse couples two jobs' failure semantics; they also run at different cadences ⇒ you use the price "as of that moment," older the longer it runs |
| **The fallback path must have a test that actually walks it** | an "unreachable" branch doesn't error — it just stays fail-closed forever; that is exactly why `RetryingMarketData` got tests |
| **A failed fallback is still fail-closed — never backfill from cache or a stale snapshot** | same reason as §5: bad data is more dangerous than no data |
| **Ship a one-switch rollback** | parameters and conventions are measured, not documented; until they are verified you must be able to fall back to single-source immediately |

**One reverse conclusion on top: do not add fallbacks for historical daily bars.**
Adjustment only rewrites history, not the current bar; splicing two vendors' adjustment
conventions manufactures **a trend break out of nothing** — the same reason §5 gives for
never splicing series from different sources.

---

## 5. Quote cache: better unusable than wrong

Every check in the market-data layer follows one rule:
**bad data is more dangerous than no data.**

| Check | Action | Why it can't be let through |
|---|---|---|
| adjustment jump (`detect_adjustment_jump`) | **don't cache, don't return** | an unadjusted series manufactures **a trend break out of nothing** |
| adjustment basis changed (cached `qfq` → now `hfq`) | **discard the whole thing** | never splice two series with different bases — the seam invents a fake gap |
| cache behind > `max_stale_days` (default 1) | **skip that symbol** | the decision would be made on an old price |
| cache behind == `STALE_WARN` (1 trading day) | use it, but count it | one day behind is usually fine, but it **has to leave a trace** (the `stale_used` counter goes into the log) |

`describe()` exposes hit / kept / skipped so that "how many decisions today were made on
old data" is answerable — **an unobservable degradation is no degradation at all.**

### Limit-up / limit-down: exchange rules belong to the chain, not the strategy

"Which symbol to buy" is the strategy's business; "can this price actually fill" isn't.
Limit-up/limit-down is the latter:

| Situation | What the paper executor should do | Why |
|---|---|---|
| price ≥ limit-up, buying | `submitted` (order waits) | you're at the back of the queue at the board; filling it hands you a buy point for free |
| price ≤ limit-down, selling | `submitted` | same, other direction |
| previous close missing / zero | **don't judge** (old behaviour) | unknown ≠ no limit; treating "unknown" as 0 would block every trade |

`submitted` is deliberately a third state: neither filled nor failed — the same meaning as
"halted, order waiting." Judging it `failed` makes people think the order was rejected;
judging it `filled` invents a fill. **Either one makes the record lie.**

Rates differ by instrument: default ±10%, `588` prefix (STAR-market ETFs) ±20%, and any
other 20% symbols listed explicitly in `SC_LIMIT_20_CODES` (plain six-digit codes are
fine; the framework normalizes them). **The framework deliberately ships no built-in
"which codes are 20%" list** — that list goes stale as funds launch and rules change, and
hard-coding it would bake one person's universe into the framework.

Prices round to **0.01 with half-up rounding** (`Decimal` + `ROUND_HALF_UP`, not Python's
banker's rounding): `1.05 × 0.9` is `0.9449999999999998` in binary; rounding that with
floats yields 0.94 — one tick below the exchange's 0.95, and that tick decides whether the
order can sell.

### Position-snapshot freshness: **off** by default

The "sell-only, no buys" gate assumes "positions reconciled." The original test was just
"can it be read" — so an executor that died and left a three-day-old holdings file still
counted as reconciled, and stale holdings became evidence that "we still hold it."

`SC_MAX_POSITION_STALE_DAYS` (default `0` = no check) turns on: a snapshot more than N
trading days behind, or one with no timestamp ⇒ not reconciled ⇒ sell-only for the day.

**Why off by default**: executors don't necessarily write positions often, so turning it on
can misfire in healthy setups. The value of this switch depends on your executor's
reliability — the user should decide, not have the framework pick a default that may
misfire every day.

---

## 6. Things deliberately not done

| Not done | Why |
|---|---|
| **A second HTTP transport backend** | The idea was rejected once and doesn't come back in the open-source release. The gain (no longer depending on Gist) doesn't offset "one more delivery path to maintain." **No stub interface was left either** — a class you can't use isn't an extension point, it's noise; to add one you just write a `Transport` subclass in a `.py` file **outside this repo** and point `SC_TRANSPORT` at it (how to wire it up: `ARCHITECTURE.md` §6). ★ Watch the wording: it's **"no second one," not "none"** — the built-in `GistTransport` is itself HTTPS |
| **A broker-specific executor** | Every brokerage terminal is integrated differently; shipping one built-in says "this only supports that one," and everyone outside that one gets nothing. What's actually useful is **a guide** (see [`EXECUTOR_GUIDE.md`](EXECUTOR_GUIDE.md)) — the contract is three methods, and you can attach any of them |
| **A backtest engine** | This framework and backtesting frameworks are **orthogonal**. Write the judgment as a pure function `decide(ctx)`: for backtesting, write a thin adapter that calls it from backtrader / qlib; live, this framework calls **the same function**. **The strategy body is never written twice — what gets written twice is the adapter layer.** Bundling a half-baked backtester only makes people believe they have a backtester |
| **Producer-side dedup** | It would introduce new shared state; today's precondition ("same second, same code, same direction → publish once") holds in a single process. **The known gap is written into [`WIRE_CONTRACT.md`](WIRE_CONTRACT.md) instead of quietly patched** |
| **Read caching** | See §4 — quota for correctness |
| **A unified code suffix** | See [`WIRE_CONTRACT.md`](WIRE_CONTRACT.md) §4: the two suffix schemes coexisting is a **compatibility requirement**, not legacy baggage |
| **Any I/O convenience inside strategies** | It breaks replayability. The only way a strategy gets data is through `ctx`, and `tests/test_strategy_contract.py` has a tripwire on it |

### ★ Why the executor is a *protocol*, and not a base class you can copy

The "no built-in broker executor" row above invites an obvious objection:
**then at least give us a base class to inherit from?**

A skeleton is provided ([`EXECUTOR_GUIDE.md`](EXECUTOR_GUIDE.md)), but it **does not come
from inheritance** — `Executor` is a structural type (`typing.Protocol`), not an
`abc.ABC`. Here's the difference:

- **`Transport` is an ABC and demands explicit inheritance.** Because backends are a
  family of implementations that get swept one by one with the same test suite — getting
  the inheritance relationship wrong is itself an error, and it should be caught on the spot.
- **There is exactly one built-in executor (`PaperExecutor`), and it's a placeholder.**
  The real usage is that you write a class next to it to talk to your brokerage terminal —
  and that class **usually already has a base class** (an SDK adapter layer, your
  project's own `Broker`). Requiring it to also inherit a framework base class trades
  "uniform for uniformity's sake" against a real inheritance conflict — and the usual
  resolution to that conflict is that somebody starts copy-pasting.

**The cost is stated out loud**: `@runtime_checkable`'s `isinstance` **only counts whether
the method names are there; it does not compare signatures.** So it's a handy self-check,
**not a guarantee**. The real guarantee is `tests/test_executor_contract.py`: a minimal
executor that **inherits nothing**, actually installed into a chain, actually run for one
round — with the producer really reading back what it wrote.

---

## 7. If you only remember one thing

**For every "what do we do when this fails," ask "what happens if we reverse it" — and
write that judgment down next to the code.**

Every `fail-closed` direction in this repo was derived not from "be conservative" but
from **"which side hurts more."** The two give **different** answers in all three gates —
which is exactly why this rule deserves to stand on its own:

> If "be conservative" produced the right answer, there'd be no judgment needed.
> Judgment is needed precisely where "be conservative" gives the wrong answer.
