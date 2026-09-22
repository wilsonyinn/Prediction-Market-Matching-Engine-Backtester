# Prediction-Market Matching Engine & Backtester — Project Spec

## Overview

Build, in Python, a limit order book matching engine modeled on a prediction-market exchange (Polymarket's CLOB). Then:

1. Prove it correct with a rigorous automated test suite.
2. Benchmark it, optimize it, and report measured before/after results.
3. Record real Polymarket market data, replay it deterministically, and backtest a simple market-making strategy.

This is a portfolio project for SWE internship recruiting. Correctness, readable code, and documented design decisions matter more than feature count. The author must be able to explain every design decision in an interview.

## Working agreements (for Claude Code)

- Work one phase at a time, in order. Stop at the end of each phase for review. Do not start the next phase until all tests pass and the author approves.
- Write tests alongside features, not after.
- Keep `NaiveBook` permanently as the reference implementation. Never delete it.
- Prefer clarity over cleverness. Docstrings should explain *why*, not just *what*.
- Record every non-obvious decision in `docs/DESIGN.md`: the decision, alternatives considered, and the reason.
- Ask before adding any dependency not listed in the tech stack.
- Do not guess Polymarket API details. Before writing the recorder, consult Polymarket's official API documentation and confirm endpoints, message schemas, tick sizes, and rate limits.
- Read-only project: never place real orders, and never handle wallets, private keys, or API secrets.

## Tech stack

- Python 3.11+
- pytest, hypothesis, pytest-cov
- ruff (lint + format), mypy (type checking)
- `websockets` (Phase 3 recorder)
- Optional, ask first: `sortedcontainers` (TreeBook), `matplotlib` (charts)
- Benchmarking uses the standard library: `time.perf_counter_ns`, `tracemalloc`, `gc`, `cProfile`
- GitHub Actions for CI

## Repository layout

```
predmarket-engine/
  pyproject.toml
  README.md
  Makefile
  docs/DESIGN.md
  src/engine/
    types.py        # enums, Order, events, reject reasons
    book.py         # OrderBook protocol
    naive_book.py   # O(n) reference implementation
    array_book.py   # bounded-price array implementation
    tree_book.py    # optional: sorted-map implementation
    engine.py       # MatchingEngine: validation, sequencing, clock, matching
  src/data/
    recorder.py     # Polymarket websocket recorder
    normalize.py    # raw messages -> normalized events
    replay.py       # deterministic replay
  src/backtest/
    fill_model.py
    strategy.py
    metrics.py
    run_backtest.py
  bench/
    workloads.py
    run_bench.py
  tests/
    unit/
    property/
    differential/
  results/          # benchmark + backtest outputs
```

## Core requirements

### Prices and quantities

- Prices are integer ticks, never floats. Tick size is configurable per market (e.g., 0.01 → valid prices 1..99; 0.001 → valid prices 1..999).
- Valid price range excludes 0 and $1.00: `1 <= price <= max_tick - 1`.
- Quantities are positive integers in base units. Conversion from the exchange's decimal sizes happens only at the data-ingestion boundary, using a fixed, documented scale factor.
- Engine timestamps are integer milliseconds.

### Order fields

`order_id` (unique str), `side` (BUY/SELL), `price` (int ticks), `quantity` (int), `order_type` (GTC/GTD/FOK/FAK), `expires_at` (int ms; GTD only), `timestamp` (int ms), `owner_id` (optional str, used by the backtester).

### Order types

- **GTC**: rests until filled or canceled.
- **GTD**: rests until filled, canceled, or the engine clock reaches `expires_at`, at which point it expires.
- **FOK**: must fill completely and immediately against resting liquidity. Otherwise it is killed with zero trades and no book change. Never rests.
- **FAK**: fills as much as possible immediately. The remainder is killed. Never rests.

### Matching rules

- Price-time priority: best price first; within a price level, FIFO by arrival sequence.
- An incoming order crosses when buy price >= best ask, or sell price <= best bid.
- Execution price is always the resting (maker) order's price.
- An incoming order may match multiple resting orders across multiple price levels, until it is filled or no longer crosses.
- Any remaining GTC/GTD quantity rests on the book.
- The engine is single-threaded and strictly sequential. Every input event gets a monotonically increasing sequence number. There is no concurrency inside the engine.
- Time comes only from input event timestamps, never the wall clock. This guarantees deterministic replay.
- Input timestamps must be non-decreasing. Reject events with an earlier timestamp than the engine clock.
- Expiry semantics: before processing any event with timestamp `t`, expire every GTD order with `expires_at <= t`. An order expiring at `t` is not matchable at `t`. Also expose `advance_time(t)`.

### Validation

Rejections return an explicit reason code (enum) and cause no state change. Reject when:

- price is out of range or not an integer
- quantity <= 0
- `order_id` is a duplicate
- side or order type is unknown
- GTD has no `expires_at`, or `expires_at <= clock`
- `expires_at` is set on a non-GTD order
- timestamp is earlier than the engine clock

Canceling an unknown or already-finished order returns `CancelRejected` with a reason.

### Output events

Every engine call returns an ordered list of immutable events (frozen dataclasses with `slots=True`), each carrying `seq` and `timestamp`:

`OrderAccepted`, `OrderRejected(reason)`, `Trade(trade_id, maker_order_id, taker_order_id, price, quantity, taker_side)`, `OrderRested(remaining)`, `OrderFilled`, `OrderCanceled`, `OrderExpired`, `OrderKilled(killed_qty)`, `CancelRejected(reason)`.

### Engine API

```python
engine = MatchingEngine(book=ArrayBook(max_tick=100))
engine.submit(order) -> list[Event]
engine.cancel(order_id, timestamp) -> list[Event]
engine.advance_time(timestamp) -> list[Event]
engine.best_bid() -> int | None
engine.best_ask() -> int | None
engine.snapshot() -> BookSnapshot   # per side: [(price, total_qty, order_count)]
```

### Book implementations

Matching logic lives once, in `MatchingEngine`. Book implementations differ only in data structure and all satisfy the same `OrderBook` protocol (add, remove by ID, best price per side, front order at a price level, reduce quantity, lookup by ID, iterate levels).

1. **NaiveBook**: one flat list per side. Best price and FIFO order are found by linear scan, so operations are O(n). This is the reference implementation.
2. **ArrayBook**: a fixed-size array indexed by price tick. Each level is a FIFO queue supporting O(1) removal by ID (an `OrderedDict` or an intrusive doubly linked list). A dict maps `order_id` to its location. Best bid/ask indices are tracked incrementally; when a level empties, scan to the next non-empty level (bounded by array size).
3. **TreeBook** (stretch, ask first): a sorted map of price levels (`sortedcontainers.SortedDict`). This is the general-exchange approach, used as a benchmark comparison.

`docs/DESIGN.md` must include a complexity table for each implementation: add, cancel, best price, and match.

---

## Phase 0 — Setup

- [ ] Initialize the repo with `pyproject.toml` and a `src/` layout
- [ ] Configure ruff, mypy, pytest, and coverage
- [ ] Add a GitHub Actions workflow that runs lint, type check, and tests on push
- [ ] Create skeletons for `README.md` and `docs/DESIGN.md`

**Done when:** CI is green with a placeholder test.

## Phase 1 — Engine and tests

- [ ] `types.py`: enums, `Order`, events, reject reasons
- [ ] `book.py`: `OrderBook` protocol
- [ ] `NaiveBook`
- [ ] `MatchingEngine`: validation, sequencing, clock, matching, all four order types, cancel, expiry
- [ ] `ArrayBook`
- [ ] Parametrize the whole test suite so every test runs against every book implementation

### Unit tests (each behavior gets its own test)

- [ ] Price priority: the better-priced resting order fills first
- [ ] FIFO priority: at the same price, the earlier order fills first
- [ ] Partial fills: incoming order larger than resting, and smaller than resting; remainders are correct
- [ ] Multiple fills: one incoming order sweeps several orders and several price levels
- [ ] Execution happens at the maker's price
- [ ] A non-crossing order rests without trading
- [ ] Boundary: buy price exactly equal to best ask trades
- [ ] Cancellation: resting order, partially filled order, unknown ID, canceling twice
- [ ] Expiration: GTD expires at `expires_at`, is not matchable afterward, and GTD with a past expiry is rejected
- [ ] FOK success: a full fill, including one that needs multiple levels
- [ ] FOK failure: zero trades and the book is unchanged
- [ ] FAK: partial execution with the remainder killed; FAK against an empty side; FAK never rests
- [ ] Empty book: best bid/ask are `None`; orders against an empty side behave correctly
- [ ] Invalid orders: one test per rejection reason
- [ ] Same-timestamp events are processed strictly in sequence order
- [ ] The book is never crossed after any event

### Property-based tests (Hypothesis)

Generate random sequences of submits, cancels, and time advances, then check after every event:

- [ ] Conservation: for each order, filled + remaining + canceled + expired + killed == original quantity
- [ ] Trade symmetry: total quantity bought == total quantity sold
- [ ] No overfills and no negative quantities
- [ ] The book is never crossed
- [ ] Every trade price is within both orders' limits
- [ ] FOK is all-or-nothing
- [ ] FOK and FAK orders never appear in the book
- [ ] Determinism: the same input sequence run twice produces an identical event stream

### Differential tests

- [ ] Feed the same random sequences (thousands of examples) to engines backed by `NaiveBook` and `ArrayBook`; the event streams must be identical

**Done when:** all tests pass in CI, coverage of `src/engine` is at least 90%, and `DESIGN.md` documents the matching rules, clock/expiry semantics, and rejection rules.

## Phase 2 — Benchmark and optimize

- [ ] `bench/workloads.py` with seeded, deterministic generators:
  - **balanced**: a configurable mix of passive adds, cancels, and marketable orders
  - **deep book**: pre-populate N resting orders (N = 1k, 10k, 100k), then measure; this should show O(n) vs O(1) divergence
  - **sweep-heavy**: many orders that cross multiple levels
  - **replay**: recorded Polymarket data (hook added in Phase 3)
- [ ] `bench/run_bench.py`: CLI to run any workload against any implementation
- [ ] Metrics:
  - events/sec
  - trades/sec
  - per-event latency p50 / p95 / p99 / max, using `perf_counter_ns`
  - peak memory via `tracemalloc`, measured in a separate run because tracemalloc slows execution
- [ ] Methodology:
  - include warmup runs
  - repeat runs and report the median
  - document any GC handling
  - record Python version, CPU, and OS with every result
- [ ] Write results to `results/*.json` and auto-generate a markdown table; optionally chart latency vs. book depth
- [ ] Profile `ArrayBook` with cProfile and optimize. Log each optimization in `DESIGN.md` with before/after numbers (e.g., `__slots__`, fewer per-event allocations, incremental best-price tracking)
- [ ] Optional: add `TreeBook` for the general-exchange comparison

**Done when:** the README has a results table comparing implementations at multiple book depths, with honest methodology notes. Do not use the phrase "low-latency"; report measured numbers and speedups only.

## Phase 3 — Real data, replay, backtest

### Recorder

- [ ] Verify Polymarket's public market-data websocket in the official docs: endpoint, subscription message, message types, and whether auth is required
- [ ] `recorder.py` CLI:
  - subscribe to a configurable list of markets
  - write raw messages to append-only gzipped JSONL, each with a local receive timestamp
  - use one file per market per day
- [ ] Reconnect with exponential backoff and log gaps; detect sequence gaps or hash mismatches if the feed provides them
- [ ] Respect rate limits; stay strictly read-only
- [ ] Record several days across a few markets with different liquidity levels

### Normalize and replay

- [ ] Convert raw messages into a normalized event stream (snapshots, level updates, trades) with integer ticks and quantities
- [ ] Confirm whether the public feed is price-level aggregated (L2) or per-order (L3). If L2, replay reconstructs a level-based book and individual queue positions are unknown; document this
- [ ] Deterministic replay: the same file produces the same reconstructed book states on every run, with a test proving it
- [ ] Validate the reconstruction against periodic snapshots from the feed

### Backtest

- [ ] Strategy orders live in a simulated layer on top of the replayed market book. They never alter the replayed market; document this as a limitation (no market impact)
- [ ] Fill model v1 (conservative): a resting strategy buy at price `p` fills when the market trades below `p`, or at `p` once the estimated queue ahead (the level's size when the order was placed) is consumed. Starting with trade-through-only is acceptable if documented
- [ ] Configurable latency between a strategy decision and the order's arrival
- [ ] Strategy v1: a market maker that
  - quotes a bid and ask around the mid with a configurable half-spread and size
  - enforces a maximum inventory
  - skews quotes against its current inventory
  - cancels and replaces quotes when the mid moves
- [ ] Baselines: do nothing, and fixed quotes without inventory skew
- [ ] Metrics:
  - realized PnL and mark-to-mid PnL
  - inventory over time
  - fill count and fill rate
  - max drawdown
  - spread captured per fill
  - adverse selection: mid move N seconds after each fill
- [ ] Resolution risk: report exposure; if a market resolves inside the recorded window, settle inventory at $1 or $0
- [ ] Parameter sweep over half-spread and inventory limit, with a results table and PnL chart

**Done when:** one command reproduces the full backtest report from recorded data, and the README has a limitations section.

## Phase 4 — Polish

- [ ] README contents:
  - one-paragraph pitch
  - Mermaid architecture diagram
  - how to run each part
  - results tables
  - summary of design decisions
  - limitations and future work
- [ ] Complete `DESIGN.md`:
  - complexity table
  - integer ticks
  - bounded-price array rationale
  - maker-price execution
  - event-time clock
  - L2 replay limitations
  - fill model assumptions
- [ ] Makefile targets: `make test`, `make bench`, `make record`, `make backtest`
- [ ] A small sample dataset (committed, or fetched by a download script) so reviewers can run the backtest themselves
- [ ] Clean git history with meaningful commits per phase

## Out of scope (list as future work in README)

- Complementary YES/NO matching (Polymarket's real CLOB can match across both outcome tokens; v1 models a single outcome token's book)
- Self-trade prevention
- Fees
- A networked exchange server with multiple clients
- Rewriting the hot path in C++ or Rust
- Real trading of any kind
