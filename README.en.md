# alpha-relay

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab.svg)](https://www.python.org/)
[![tests](https://github.com/Birdofbelief/alpha-relay/actions/workflows/tests.yml/badge.svg)](https://github.com/Birdofbelief/alpha-relay/actions/workflows/tests.yml)
[![Runtime deps](https://img.shields.io/badge/runtime%20deps-requests%20%2B%20pandas-informational.svg)](pyproject.toml)
[![Credentials](https://img.shields.io/badge/credentials-zero%20literals-brightgreen.svg)](docs/SANITIZATION.md)

> [中文](README.md)

**Your strategy decides what to buy. alpha-relay makes sure that decision reaches your
execution side — reliably — and that it actually got there.**

**It does not invent your strategy, and it does not place your orders.**

Between "the strategy computed a signal" and "the broker placed an order" there is a
stretch of plumbing that **fails silently, not loudly**: bad data, dropped signals,
retries that duplicate orders, fills nobody recorded, positions that no longer match.
alpha-relay owns that stretch.

```
[Your strategy] --signal--> [alpha-relay] --signal--> [Your executor] --order--> [Broker]
 you write decide()          this repo                  your broker adapter       places the order
 outside this repo           owns the middle            outside this repo
```

Three legs, three owners:

- **Your strategy** decides what to buy or sell. The alpha is yours; the code lives outside this repo.
- **alpha-relay (this repo)** delivers the signal reliably: one format, data/risk gates,
  retries, execution receipts, position reconciliation, scheduling, heartbeats, alerts.
- **Your executor** talks to the broker and turns the signal into an order. It is also outside
  this repo. The only executor here is `PaperExecutor`, a **no-broker placeholder** for demos and tests.

**Where does it run?** Wherever you deploy it: your laptop, a cloud VM, a VPS
(see [the cross-machine section](#across-machines-how-a-signal-gets-from-producer-to-executor)).
It does **not** provide the cloud, and it does not require one. The invariant is:
**your strategy never enters this public repo, and never enters the broker platform.**

### What it owns / what it does not

| It owns | It does not |
|---|---|
| One signal format both ends understand | Invent your strategy (the alpha is yours) |
| Blocking **buys** on bad data or position mismatch (sells still go) | Provide market data |
| Delivery retries; no silent drops | Provide a cloud host or deploy for you |
| Execution receipts (did it actually fill?) | Contain a broker client — **it does not place orders** |
| Position reconciliation (mismatch ⇒ alert / block buys) | Run backtests (a separate concern) |
| Scheduling, heartbeats, alerting | Provide a web UI or a database |

### Why not just one script?

A script can make a signal "work once." Running it unattended every day needs four more things:

1. **fail in the right direction** — when data is bad, blocking the right half matters more than "being conservative";
2. **retries that cannot become duplicate orders** — reproducible idempotency keys;
3. **receipts** — proof of what the execution side actually did;
4. **reconciliation** — when the two sides disagree, block buys rather than build a position twice.

When these four break, they usually **do not raise** — the chain just quietly stops doing its job.
This project's point is to turn those silent failures into alerts, blocks, and records.

> **30-second demo** (no account, no network, no broker):
> ```
> python -m pip install requests pandas
> python -m signalchain.demo
> python -m pytest
> ```

> **Windows:** if `python` does not open, or drops you into the Microsoft Store, replace `python`
> with `py` — or just double-click `demo.cmd`.
>
> ⚠️ **Install and run with the same launcher.** On your machine `python` and `py` may be
> **two different interpreters** — a package installed into one is invisible to the other.
> The symptom looks like this (the message itself tells you what to type):
>
> ```
> ImportError: alpha-relay 缺少运行时依赖：pandas
>     python -m pip install requests pandas
> ```
>
> Measured on GitHub's Windows runner (2026-09-22): `python` had the dependencies, `py` still
> pointed at another interpreter without pandas. `py -0` lists every interpreter registered on your
> machine; to install into the one `py` uses, run `py -m pip install requests pandas`.
> **Double-clicking `demo.cmd` cannot hit this**: it picks one launcher and uses it throughout,
> and the install command it prints on failure is written for that same launcher.
>
> If `python` sends you to the Microsoft Store, that is Windows' **stub**, not Python — it means
> there is no real Python on your `PATH` (re-install and tick *Add python.exe to PATH*). If Python
> is installed but `python` still hits the stub, use `py` to go around it.
>
> **You can also just double-click `demo.cmd`.** Double-clicking a `.py` only flashes a black window
> — the window closes the instant the process exits, so whatever it printed (success or error) is
> gone before you can read it. `demo.cmd` exists for that: it **waits for a keypress** and, on
> failure, lists the usual causes. It prefers `py` and falls back to `python`
> (see the `where py` lines in `demo.cmd`).

---

## Contents

- [Why this exists](#why-this-exists)
- [The problem it solves](#the-problem-it-solves)
- [Thirty-second overview](#thirty-second-overview)
- [Three claims that run through the whole codebase](#three-claims-that-run-through-the-whole-codebase)
- [Quick start](#quick-start)
- [Across machines](#across-machines-how-a-signal-gets-from-producer-to-executor)
- [Running the tests](#running-the-tests)
- [On "zero credentials"](#on-zero-credentials)
- [Status and boundaries](#status-and-boundaries)
- [Docs](#docs)
- [Environment](#environment)
- [License](#license)

---

## Why this exists

This is not the product of "I felt like writing a framework". It is the **extraction of a live
trading chain** — originally a single-file script whose signal-producing and signal-executing halves
ran in two different processes.

There is exactly one reason it was split out: **infrastructure can be public; alpha cannot.**

What actually got preserved while splitting it was not the code — it was a handful of judgements
**paid for with real losses**:

- When data breaks, which half should stop? Decide by **which error hurts more**, *not* by
  "which choice is more conservative".
- Two writers editing the same document? The later one **silently** overwrites the earlier one.
- Can a retry become a duplicate order? Only if the idempotency key cannot be reproduced from a replay.

> If "being conservative" were enough to derive the right answer, no judgement would be needed.
> The places that need judgement are exactly the places where "being conservative" gives the wrong one.

**So the most valuable thing in this repository is not in `signalchain/` — it is in
[`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md)** (Chinese; an English outline lives in
[`docs/DESIGN_NOTES.en.md`](docs/DESIGN_NOTES.en.md)).

---

## The problem it solves

Most "quant scripts" end up looking the same: a few thousand lines in one file, welding three
unrelated concerns together — **strategy logic**, **data plumbing**, and **delivery + risk control**.
The consequences compound:

- changing a threshold means reading the whole file;
- changing the delivery channel means touching the strategy;
- risk checks are scattered, so **nobody knows how many there are**, let alone which ones fail open.

This framework cuts the chain into pieces, one responsibility each, every piece independently
replaceable:

| Layer | Responsibility | Replaceable with |
|---|---|---|
| `Strategy` | your alpha. **Implement `decide(ctx)` and nothing else — no I/O** | any strategy, including a private one outside the repo |
| `MarketData` | quotes and history | online source / local cache decorator / fake data |
| `Transport` | delivery and reading | local file / Gist / null / a store you implement |
| `Gate` | risk control (strategy-independent) | three built-in gates, add or remove |
| `Runner` | scheduling, time budget, failure ceiling, heartbeat | — |
| `Executor` | turns signals into orders | your broker terminal; the built-in `PaperExecutor` is only a **no-broker placeholder** |

### Thirty-second overview

```
        ┌───────────────── alpha-relay ────────────────┐
private │                                              │
strategy│   Runner ──┬── Scheduler (task table / window) │
┌──────┐│            ├── TradingCalendar                │
│      ││            └── Heartbeat (severity only up)   │
│decide││                    │                          │
│  ()  ││                    ▼                          │
└──┬───┘│            GateChain (fail-closed gates)      │
   │    │                    │                          │
   │    │                    ▼                          │
   └───▶│            Transport (★ pluggable)            │
 Signal │             ├─ FileTransport   ← default, zero credentials
        │             ├─ GistTransport   ← cross-machine reference impl
        │             └─ NullTransport   ← dry-run
        │                    │                          │
        │     receipts ◀─────┘                          │
        └──────────────────────────────────────────────┘
                             ▲
                    Executor (★ pluggable: your broker terminal)
                      └─ PaperExecutor ← no-broker placeholder, for loop tests
```

---

## Three claims that run through the whole codebase

### 1. A strategy is a pure function

`decide(ctx)` **makes no network calls, reads no files, writes no state**. It takes everything from
`ctx` (`now` / `calendar` / `market` / `state` / data-quality flags) and returns `Signal`s.

The benefit is not tidiness — **it makes the strategy replayable offline**, and "replayable" is the
precondition for "verifiable". This is not left to discipline: `tests/test_strategy_contract.py`
carries an **I/O tripwire** — the moment a strategy touches a file or the network, the test goes red.

### 2. Failure must lean toward "do nothing" — but **not in the same direction for every gate**

When data is incomplete the default action is "stop", not "carry on". But **which half to stop is
decided by which error hurts more, not by which choice is more conservative**:

| Gate | Triggers when | Action | Why this direction |
|---|---|---|---|
| `QuoteIntegrityGate` | any instrument's quote is missing / NaN / non-positive | **all signals stop** | prices are broken, so every judgement is built on a wrong price. Selling is equally wrong here — you may sell a position that is perfectly healthy |
| `PositionsSyncedGate` | reconciliation with the executor did not complete | **no buys, sells proceed** | buying without positions can open a duplicate position (a real-money error); the cost of a blocked sell is "one missed sell", which is recoverable |
| `AuxDataGate` | key auxiliary data is entirely missing | **no new positions, existing ones still managed** | auxiliary data is used to *pick* new instruments; its absence does not disable stop-loss on what you already hold |

Conservatism is not the criterion; **asymmetric cost** is. This one was paid for in the market.

### 3. A single writer is not a style preference — it is currently the only way to remove the race

**Any store that appends by "read → modify → write back" is not atomic.** The chain's append works
exactly that way (`store.py`'s `append`: read, append one entry, write the whole document back), so
this is not the flaw of one backend — it is the flaw of **that entire class of backends**: two
writers editing the same document, and **the later write silently overwrites the earlier one**.
*Silently* is the important word: both sides believe they succeeded, and both logs say success.

A backend that offers an atomic append or CAS is the only exception — `Transport.supports_cas` is the
flag that marks it. **All three built-in backends are `False` today**, and so is the object store,
network drive or self-hosted HTTP service you swap in: **as long as that read-modify-write is still
there, the bug is still there.** The origin of this hard constraint is in
[`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md).

So channel **ownership lives in the code** (`transport/base.py`, `CHANNEL_OWNERS`):

```
signals    ← only the producer may write
heartbeat  ← only the producer may write
positions  ← only the executor may write
executions ← only the executor may write
```

`SingleWriterGuard` wraps any backend and **raises on a wrong-channel write** rather than logging a
warning. The constraint is enforced at runtime, not agreed in a comment.

---

## Quick start

### Watch it run first

```bash
python -m pip install requests pandas   # only two runtime deps, once
python -m signalchain.demo
```

Zero credentials, zero network, zero broker — dependencies, but nothing for you to fill in. When it
finishes it prints the **real bytes on disk** for all four channels, including the positions and
execution receipts written back by the executor.

### Use it as a library

> ★ The snippet below needs `import signalchain` to work, and this package is **not** in your
> `site-packages`. Either run the script **from the repository root** (import finds the package in
> the current directory) — fine for a quick look — or install it once with
> `python -m pip install .`, after which **any** directory can import it.
> (The error if you skip this is `ModuleNotFoundError: No module named 'signalchain'`. It is not a
> broken framework.)

```python
from signalchain import build_file_chain, setup_logging

setup_logging()                         # first line of an entry point; see "Output and encoding"
chain = build_file_chain("./runtime")   # file backend + fake market data + paper executor
result, fills = chain.step()            # executor reports positions → decide → gates → deliver → receipts
print(result.summary())
```

### Swap in your own strategy

Strategy files live **outside the repository** and are loaded by path:

```python
chain = build_file_chain("./runtime", strategy="../my_private.py")
```

★ Paths resolve against **your working directory**, not the repository root — hence the `../` above.
If you run your script from the repository root, `./my_private.py` would put your alpha *inside* the
repo, which is precisely what this path exists to avoid. (The three `strategies/…` lines in
`.gitignore` **do not cover this case**: a pattern containing `/` is anchored by git to the directory
holding `.gitignore`, so those lines only match **the repository root's `strategies/` directory and
everything below it, at any depth** (when the pattern matches a directory, the whole subtree is
excluded) — it matches **by location**, and the name is merely an additional
condition. There is in fact no top-level `strategies/` directory in this repo at all. The two places
you would actually drop your alpha — `./my_private.py` in the repo root, and anything under
`signalchain/strategies/` — are **not covered**; `git check-ignore -v ./my_private.py` says
`NOT IGNORED`. The real guarantee is that the file is not in the repo at all.)

Or set `SC_STRATEGY_PATH` — note it **only affects `build_chain()`**: `build_file_chain()`
deliberately honours only an explicitly passed `strategy=` and ignores environment variables.
A wrong path does **not** fall back to the built-in strategy silently; it raises `FileNotFoundError`.

The smallest shape of a strategy (same structure as
[`signalchain/strategies/demo_basic.py`](signalchain/strategies/demo_basic.py); an equivalent
implementation in `tests/test_strategy_contract.py` is actually executed):

```python
from signalchain import Strategy, ACTION_BUY

class MyStrategy(Strategy):
    name = "my_strategy"

    def universe(self):
        return ["510050", "159919"]        # no suffix; the chain adds it

    def warmup_bars(self):
        return 60                          # the chain uses this to size the history cache

    def decide(self, ctx):
        if not ctx.data.quote_ok:          # data quality is visible to the strategy
            return []
        for code in self.universe():
            if ctx.price(code) is None:    # suspended → skip only this instrument
                continue
            hist = ctx.try_history(code, 30)
            if hist is None:               # no history → likewise skip only this one
                continue
            closes = hist["close"].astype(float).tolist()
            if closes[-1] > closes[0]:
                yield ctx.signal(code, ACTION_BUY, amount=1000)
```

A few things worth noticing:

- `ctx.history()` returns a **DataFrame** and raises when unavailable; `ctx.try_history()` is the
  "unavailable means `None`" version. **Do not swallow that exception** — "no history for one
  instrument" and "no signal this round" look exactly alike, so swallowing it is a silent miss.
- Use `ctx.signal()` rather than constructing `Signal(...)` yourself: it stamps `ctx.now` instead of
  the wall clock. That single choice decides **replayability** — a signal id is the idempotency key,
  and mixing in the wall clock means replaying the same day's data mints a batch of brand-new ids, so
  "have I handled this one?" answers "no" forever.
- `decide()` may return a list or be a generator.

### Swap in your own delivery backend

Subclass `Transport`, put it in a `.py` file **outside the repository**, and point the config at it:

```bash
# .env
SC_TRANSPORT=../my_objectstore.py
```

Three methods and nothing else (a full example is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
section 6):

```python
from signalchain.transport import Transport


class MyObjectStorage(Transport):
    def read(self, channel):        # -> document, or None when absent
        ...
    def write(self, channel, doc):  # whole-document replace (not append); True on success
        ...
    def describe(self):             # one-line identity (**must not contain credentials**),
        return "objectstore(my bucket)"   # goes into logs and the heartbeat
```

If the file contains exactly **one** `Transport` subclass it is selected automatically; with several
(helper classes are common) you must name one explicitly — **either** form:

```python
TRANSPORT = MyObjectStorage                  # the class itself → constructed with no arguments
TRANSPORT = MyObjectStorage.from_env         # a nullary factory → use when construction needs args
```

> ★ **Need constructor arguments (endpoint / key / bucket name)? Use the second form.** The first
> yields a `TypeError` — and that error now tells you to switch to `from_env`, or to pass the
> instance directly at assembly time: `build_chain(config, transport=MyObjectStorage(endpoint, key))`.
> The factory need not be called `from_env`; any name works. The precedent is the built-in
> `GistTransport.from_env`.

A wrong name **raises — it does not quietly fall back to `file`**. Silent degradation here would not
mean "one feature missing": it would mean the two ends writing to two different stores, with both
logs reporting success.

### Swap in your own executor

The executor contract is **the three `Executor` methods**. It is a structural type (`typing.Protocol`) —
**you need not inherit anything**, because your executor almost certainly already has a base class
(an adapter over a broker SDK). A skeleton and the two lines you must wrap yourself are in
[`docs/EXECUTOR_GUIDE.md`](docs/EXECUTOR_GUIDE.md).

The shortest validation path is to **wire it up against fake market data first**: confirm it is really
called and that receipts really come back, then connect real data. Only the `executor=` argument
changes; the producing side does not move a line:

```python
from pathlib import Path

from signalchain import Config, build_chain
from signalchain.transport import FileTransport

config = Config(role="producer", transport="file",
                runtime_dir=Path("./runtime"))
executor = MyExecutor(config, FileTransport(config.channel_dir))

chain = build_chain(config, strategy="./my_private.py", executor=executor)
```

> ★ `executor=` and `with_executor=True` are **mutually exclusive**: the latter means "build me the
> built-in paper executor", and passing both **raises outright** instead of silently picking one.
> The cost of picking one silently is not an error message — it is that you believe you are running
> A while you are running B, and it looks exactly like "no signals today".

### What it actually looks like

Below is real output from `python -m signalchain.demo` (with two omissions: a temporary directory
path, and repeated rounds irrelevant here). Four things are worth noticing:

**① Ownership is bound to the *role*, not the process.** The paper executor lives in the same
process, and the producer guard still cannot write `positions`:

```
  role=producer | strategy=demo_basic(3 instruments/30 warmup bars) | market=fake(3) | transport=file:…\channels

  → measured: producer tries to write the positions channel ⇒ OwnershipError
     refusing to write channel 'positions': it belongs to executor, but this process's role is producer.
```

**② Receipts come in two mutually exclusive shapes, told apart by `status` in one array.** Reading the
wrong field **does not raise** — it silently yields `None` or `0`:

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
    "reason": "market price 1.1040 above limit 0.9936, order resting",
    "time": "2026-09-18 15:45:00"
  }
]
```

> ★ Terminal states (`filled` / `canceled`) carry `order_id` / `price` / `amount` and **no `reason`**;
> non-terminal states (`submitted` / `skipped`) carry `reason` and **no `order_id` / `price` /
> `amount`**. Details in [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md).

**③ The three gates fail in deliberately different directions.** The same buy signal under different
data-quality conditions:

```
  everything healthy        → buy √   sell √
  quotes broken (missing/NaN/non-positive) → buy ×   sell ×
  positions not reconciled  → buy ×   sell √
  auxiliary data entirely missing → buy ×   sell √
```

The gate chain deliberately **does not short-circuit**: when one signal trips several gates, every
reason is recorded.

**④ The end of the loop is real bytes.** When it finishes, the directory holds:

```
    executions.json  (1038 bytes)
    heartbeat.json  (88 bytes)
    positions.json  (216 bytes)
    trade_signals.json  (953 bytes)
```

The full output is 186 lines (**counting convention**: stdout 174 + stderr 12, the two streams counted
separately — counting stdout alone gives 174) and is printed on every run — running it once is the
fastest way to see it. `--keep` preserves the run directory.

### Output and encoding (read this if you are on a Chinese Windows)

On Windows without `PYTHONUTF8=1`, Python's stdout uses the **console code page** — GBK on a Simplified
Chinese machine. That breaks two kinds of output in **completely different ways**:

| You wrote | What happens on a GBK console |
|---|---|
| `print("⛔")` | raises `UnicodeEncodeError` → **the process exits on the spot** (exit code 1) |
| `log.info("⛔")` | `logging` **swallows** the exception → the process keeps running, but **that line is lost forever**, plus a `--- Logging error ---` noise block |

The second is worse: it does not interrupt anything, and you only discover the missing lines when
reconciling later. Both are **invisible on the author's machine** (which sets `PYTHONUTF8=1`) — they
only surface when someone else clones the repo and runs it on Windows. And the first command this
README tells you to run is that very demo.

So the repository has two hard rules, and **both are watched by tests**
(`tests/test_output_safety.py` forces the console encoding to GBK / cp1252 and runs a real
subprocess):

1. **No bare `print` inside the package** — all output goes through `console.emit()`, which never
   raises on encoding (characters that cannot be encoded degrade to `?`; between losing one character
   and killing the caller's process, the former wins).
2. **No user-visible string contains a character GBK cannot encode** — log icons use only symbols GBK
   already has: `√ × ! ◆ ◇ ○ ● △ ▲ ▼ →`. That is why you see `× gate blocked signal` rather than a
   cross emoji.

**For your own script, one line at the top is enough:**

```python
from signalchain import setup_logging
setup_logging()          # forces UTF-8 and configures logging in one call
```

It also works without it (`emit()` has a fallback), at the cost of redirected logs not being UTF-8
(someone else's editor reads mojibake) and `logging` silently dropping any line it cannot encode. The
library **will not** touch your streams at import time — a library that silently re-encodes its
host's streams is a worse defect than the original problem. The full story is in
[`signalchain/console.py`](signalchain/console.py).

---

## Across machines: how a signal gets from producer to executor

The producer and the executor are usually not on the same machine — the strategy lives on your machine or a
cloud VM (or a machine that crunches data), while the executor sits next to the broker terminal. The two ends
need a place **both can reach** to hold those four channels.

This framework **does not care what that place is**; it only requires that the store implement
`Transport`'s three methods:

```python
read(channel) -> Any | None     # read a channel's document
write(channel, doc) -> bool     # write it whole (not append)
describe() -> str               # one-line identity (no credentials)
```

Pick by scenario:

| Scenario | Use |
|---|---|
| **Both ends on one machine** | the built-in `FileTransport` (`SC_TRANSPORT=file`). Zero credentials, zero network, zero external services — only the two packages `requests` + `pandas`. This is the path `python -m signalchain.demo` takes |
| **Strategy on a cloud VM, executor on the broker-terminal side** | this is the middle leg of the three-leg picture on the first screen: the strategy process produces signals in the cloud, the executor consumes them on your own terminal. The chain **does not require both ends on one machine**; which store to use depends on **what the executor’s machine can read** (see the next two rows) |
| **Cross-machine, you already have object storage** | write a `Transport` subclass (three methods, a few dozen lines) in a `.py` file **outside the repo**, then point `SC_TRANSPORT=../my_objectstore.py` at it — not one line of this framework changes; see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) section 6. Replacing a whole object is atomic (you never read half a JSON), but **lost updates still happen**: two writers at once, the later overwrites the earlier. So it is bound by the ★ below, not exempt from it |
| **Cross-machine, want something ready-made** | the built-in `GistTransport` is a **reference implementation**: it demonstrates wrapping an HTTP service that only does whole-document reads and writes into a `Transport`. It works, but it is neither the only option nor the default recommendation |

> ★ **One thing does not change with the store: a channel may have exactly one writer.** Enforced at
> runtime by `SingleWriterGuard`. Swapping the store does not remove it — it only changes *how* two
> writers break things: a file backend may leave half a JSON, object storage loses updates, Gist
> overwrites the whole document. **All of them silently.**

---

## Running the tests

```bash
python -m pytest              # 367 tests, fully offline, no credentials needed
```

The same suite runs in CI (Ubuntu + Windows × Python 3.10 / 3.12 / 3.13, see
[`.github/workflows/tests.yml`](.github/workflows/tests.yml)) — the badge at the top is its **live
result**, not hand-written.

CI also runs the sanitization scan a second time: the local pre-commit hook depends on
`core.hooksPath`, which is **local configuration and does not travel with a clone** — meaning a fresh
clone is unguarded by default, so the server side adds a check.

> The **367** above is hand-written and will drift. **Trust the measurement**: run
> `python -m pytest` and look at the exit code (0 = all passed).
>
> ⚠️ **In a public clone it is `365 passed, 2 skipped`** — two tests carry a `skipif` whose condition
> is exactly "is the author's private fingerprint list present", so they **must** skip in a public
> clone. They only mean something on the author's machine (they check that the list is
> self-consistent and that this repository does not trip it), so this is by design rather than a
> weakened test — but **do not read `2 skipped` as "everything ran"**.
>
> Both numbers are **measured, not counted** (2026-09-22): a working tree copied from `git ls-files`
> (i.e. the file set a clone receives, with line endings normalized by git's attributes), installed
> into an empty venv holding only `pip` / `setuptools`, plus the two runtime deps and pytest, run with
> `PYTHONUTF8` / `PYTHONIOENCODING` / `PYTHONPATH` / `TZ` **explicitly stripped**. That last part is
> not fussiness: this machine sets `PYTHONUTF8=1`, and without stripping it "green on my machine" is
> **fake green**.

Coverage worth singling out:

| File | What it guards |
|---|---|
| `test_transport_contract.py` | one suite run against every backend — this is the **evidence** for "pluggable", not a slogan |
| `test_transport_plugin.py` | writes a real `.py` backend **outside the repo**, loads it via `SC_TRANSPORT=<path>`, runs a full round and checks it really received bytes; also pins "a wrong name must raise, never quietly fall back to `file`" |
| `test_wire_contract.py` | **wire format frozen**: field names, suffixes and time format are byte-compatible with the existing executor |
| `test_strategy_contract.py` | strategy purity tripwire (any I/O turns it red) + loading a strategy from outside the repo |
| `test_end_to_end.py` | the seam: reads the **real bytes** on disk, without reusing the parser |
| `test_sanitize_rules.py` | **tests for the sanitizer itself** — a checker whose only failure mode is silence |
| `test_docs_tripwire.py` | APIs promised in the docs actually exist (and actually run) |
| `test_fake_determinism.py` | fake market data must be **pinned value by value**, and identical **across processes and hash seeds** — otherwise "replay" is a dice roll |
| `test_output_safety.py` | must not crash, and must not **silently drop log lines**, on someone else's console |

---

## On "zero credentials"

This repository contains **no credential literals**, and that is not maintained by human review:

- `tools/sanitize_check.py` — a sanitization scanner covering credential shapes, internal documents,
  and **two lists supplied by the user**: a **literal denylist** ("did my instrument universe leak?" —
  the class that pattern matching cannot guess but most needs to catch) and a **regex fingerprint
  list** ("has this shape of code flowed back in?"). Both are `.gitignore`d; formats are in
  [`docs/SANITIZATION.md`](docs/SANITIZATION.md) section 4.
- But the scanner is **not a defense, it is a tripwire**. The real defense is **structurally removing
  the strategy**: the public repo contains one from-scratch, minimal example strategy whose `def` and
  signatures are copied from the private one and whose contents are not. **A list only recognizes
  shapes you thought of in advance; what leaks is structure** — this is backed by measurement, see
  [`docs/SANITIZATION.md`](docs/SANITIZATION.md) section 1.
- It installs as a **git pre-commit hook** and rejects the commit on a hit. **You must enable it once
  after cloning** (`core.hooksPath` is local config and does not travel with a clone):

  ```bash
  git config core.hooksPath .githooks
  ```

  Without that, the scanner **never runs**, and the commit looks perfectly normal — exactly the
  "failure looks identical to passing" pattern below.
- The hook **picks its own interpreter** (`py` → `python3` → `python`, and it test-runs it to confirm
  it can execute), so macOS / Linux need not care whether `py` exists. When it cannot find one it says
  "**this check did not run**" rather than falsely accusing you of leaking credentials — three failure
  paths, three distinct messages.
- Every rule has a **reverse test** (`test_sanitize_rules.py`): not just "clean input reports
  nothing" but "dirty input must report".

> ★ Why a checker needs checking: its failure mode and its passing mode **look identical** (both are
> zero hits, both are green). On the day this project's scanner was written, three of its rules were
> **blind** — see [`docs/SANITIZATION.md`](docs/SANITIZATION.md).

`test_end_to_end.py` also carries an executable proof: **with every relevant environment variable
cleared, the whole chain still runs end to end**.

### Where credentials come from

There are none in the source; everything is read from environment variables. The template is
[`.env.example`](.env.example) — and **it must stay empty forever**, because it is committed:

```bash
cp .env.example .env      # then fill in your own values; .env is .gitignore'd
```

**The default backend (local file) needs no variables at all** — you can run it without even creating
`.env`, which is the path `python -m signalchain.demo` takes. Only two things ever need filling in:

- `SC_STRATEGY_PATH` — if your strategy lives outside the repo (recommended, and the default posture);
- `SC_GIST_ID` / `SC_GITHUB_TOKEN` / `SC_SERVERCHAN_KEY` — only if you choose the Gist backend or want
  phone push notifications.

What each variable means is documented in that template.

---

## Status and boundaries

Stating clearly what this project **is right now** matters more than stating what it can do.

| Item | Notes |
|---|---|
| **Where the design comes from** | the layering, the gate directions and the single-writer convention come from a **live trading system** (single-file form, producer and executor separated). The "why"s in the docs are not deduced on paper. |
| **The state of this repo** | ⚠️ **It was extracted from that system and has not replaced it in production.** In other words: **the design has been battle-tested; this extracted artifact has not run in production.** Treat it as a usable framework assembled but not yet field-proven. |
| **What the tests prove** | 367 tests, fully offline (365 + 2 skipped in a public clone). They verify the **chain's correctness** — idempotency, gate directions, wire format, ownership, pluggability at both ends, output that does not crash. They **do not verify the returns of any strategy**. |
| **What is missing** | no broker client — the repo only ships `PaperExecutor`, a **no-broker placeholder**; you wire up the real executor yourself. No web UI, no database. |
| **Delivery backends** | three **examples** built in: `file` / `gist` / `null`. On one machine, `file` is enough; for cross-machine, see the [section above](#across-machines-how-a-signal-gets-from-producer-to-executor). **Any other store (object storage, self-hosted HTTP, network drive) is yours to implement** — every broker terminal can read a different set of stores, and no framework can decide that for you. When you are done, **nothing in this repo changes**: the backend class lives outside it and the config points at a path (`SC_TRANSPORT=../x.py`), or you pass `build_chain(transport=...)` at assembly time. **A wrong name raises instead of quietly falling back to `file`** — that would put the two ends on two different stores while both logs reported success. |
| **No backtesting** | this framework is **orthogonal** to backtesting, not competing with it. Write your judgement as the pure function `decide(ctx)`: for backtests, write a thin adapter that calls it from backtrader / qlib; in production, this framework calls **the same function**. **The strategy body is never written twice — what is written twice is the adapter.** There is no backtest engine here and there will not be one. |
| **Single-process assumption** | "a single writer" is a **coding convention plus a runtime assertion**, not a distributed lock. If you truly need several processes writing one backend, add locking outside. |
| **Known gap** | the producer does not de-duplicate (it relies on "same second, same code, same direction is pushed once", which holds in a single process). **The gap is written down in [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md)** rather than quietly patched. |

### If you remember only one thing

> **For every "what happens when this fails", also ask "what if I did the opposite?" — and write that
> judgement next to the code.**

Every fail-closed direction in this repository was derived from **which error hurts more**, not from
"being conservative". The two answers differ in **all three** gates — which is exactly why the rule
deserves to stand alone:

> If "being conservative" were enough to derive the right answer, no judgement would be needed.
> The places that need judgement are exactly the places where "being conservative" gives the wrong one.

More trade-offs in [`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md) (Chinese; English outline in
[`docs/DESIGN_NOTES.en.md`](docs/DESIGN_NOTES.en.md)).

---

## Docs

| File | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | layering, dependency direction, the full sequence of one round |
| [`docs/EXECUTOR_GUIDE.md`](docs/EXECUTOR_GUIDE.md) | **wiring your own executor**: the three-method skeleton, the two lines you must wrap, and why to validate against fake data first |
| [`docs/WIRE_CONTRACT.md`](docs/WIRE_CONTRACT.md) | **the wire contract**: fields, suffixes and time formats of the four channels, and the places that look like they should be unified but must not be |
| [`docs/DESIGN_NOTES.md`](docs/DESIGN_NOTES.md) | design claims and trade-offs: failure directions, idempotency, time budget, network resilience |
| [`docs/DESIGN_NOTES.en.md`](docs/DESIGN_NOTES.en.md) | English translation of the above (for readers without Chinese; **the Chinese version wins on any discrepancy**) |
| [`docs/SANITIZATION.md`](docs/SANITIZATION.md) | the sanitization checklist + scanner usage + **how to build a denylist** |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | migrating from a single-file script; also **when not to migrate** |
| [`docs/RELEASING.md`](docs/RELEASING.md) | **release checklist**: where the version lives, how to tag, what to push, how to verify afterwards (Chinese) |

The reference docs are written in Chinese. This README and `docs/DESIGN_NOTES.en.md` are the English
entry points.

## Environment

- Python ≥ 3.10
- only two runtime dependencies: `requests` + `pandas`
- `akshare` supplies the **trading calendar**. The zero-dependency demo (`build_file_chain`) ships an
  offline calendar and does not need it, but **the default production assembly `build_chain()` does** —
  without it the calendar is unavailable and the program treats **every day** as a non-trading day:
  the process stays alive, heartbeats continue, and **not a single task runs**.
  Install with `python -m pip install akshare`.
- Usage is "clone and run": **you do not install alpha-relay as a dependency, but you do install the
  two runtime deps** — `python -m pip install requests pandas` (see [`pyproject.toml`](pyproject.toml)).
- **All times are interpreted as local machine time, with Beijing time as the semantic reference**
  (A-share opens, closes and catch-up windows are all written in Beijing time). Scheduling uses **bare
  local time with no notion of time zones at all**: task times are compared as
  `datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M")` against `datetime.now()` (see `due_tasks` and
  `self.clock = clock or datetime.now` in `signalchain/runner.py`).
  ⇒ A wrong machine time zone **does not raise — it just runs at the wrong time**: on a UTC machine a
  09:30 task fires at 17:30 Beijing time. **Set the time zone to `Asia/Shanghai` before running in the
  cloud or in a container.**
- On Windows, if `python` points at the Microsoft Store stub (which produces no output when run), use
  `py` instead — see the note in [Quick start](#quick-start).

## License

MIT — see [`LICENSE`](LICENSE).
