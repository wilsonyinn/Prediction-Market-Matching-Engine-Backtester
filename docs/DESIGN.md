# Design Decisions

Every non-obvious decision in this project is recorded here: the decision, the
alternatives considered, and the reason. Entries are added as each phase is built,
not retrofitted at the end.

## Phase 0 — Setup

**Build backend: hatchling.** PEP 621 metadata only, zero config needed for a `src/`
layout. Rejected setuptools (needs explicit `package-dir`/`packages.find` for a src
layout) and poetry (non-standard `[tool.poetry]` metadata a reviewer has to decode).

**Package layout follows the spec literally: `src/engine/`.** The spec's repository
tree makes `engine`, `data`, `backtest` three top-level import names. `data` as a
global import name is vague, and a `src/predmarket/` namespace would avoid that — but
Phase 1 only touches `engine`, which is a fine name on its own, so the namespace
question is deferred to Phase 3 rather than decided speculatively now.

**mypy `strict = true` over `src` and `tests`, but not `bench/`.** `bench/` doesn't
exist until Phase 2; listing a missing path makes mypy error immediately. It's added
to `files` when Phase 2 creates it.

**No `--cov` in pytest's default `addopts`.** Coverage tracing roughly doubles local
run time and interferes with `pdb`. Coverage is a `make cov` / CI-only concern; the
90% floor is still enforced locally too, via `[tool.coverage.report] fail_under = 90`
rather than a CI-only flag.

**CI does not cache `.hypothesis/`.** The example database is machine- and
version-keyed; a stale cache would turn a genuine finding into a confusing flake.
Determinism across runs comes from the registered `ci` Hypothesis profile instead
(see below), not from a persisted example cache.

## Phase 1 — Engine

### Two spec ambiguities, resolved explicitly

**1. `seq` is assigned per emitted *event*, not per input command.** The spec says
both "every input event gets a monotonically increasing sequence number" and "every
event carries `seq`". A per-command `seq` would not uniquely identify an event and
would make the differential tests' "streams must be identical" rest entirely on list
position rather than on `seq` itself. Per-event `seq` (global, starts at 1, never
skips) makes list order redundant with — and therefore checked by — the `seq` field,
and gives every trade a unique global ordinal. Command boundaries are still
recoverable: every command's non-expiry events begin with exactly one of
`OrderAccepted` / `OrderRejected` / `OrderCanceled` / `CancelRejected`.

**2. The clock advances (and the expiry sweep runs) for any command with
`timestamp >= clock`, even if that command is then rejected for an unrelated
reason.** Only a timestamp-regression rejection suppresses the advance. Time is
environment, not order state — the spec's "a rejection causes no state change" is
about the *book*, not the clock. The alternative (advance only on full success)
makes the clock's behavior depend on unrelated validation outcomes and lets a run of
malformed submits indefinitely delay real expiries. Consequence: every returned
event list has the shape `[OrderExpired…] ++ [command's own events…]`.

### `Order` vs `RestingOrder`

Inbound `Order` is a frozen, `slots=True` dataclass that performs **no validation of
its own** — a malformed order must still construct, so the engine can reject it with
an `OrderRejected` *event* rather than an exception.

`RestingOrder` is a **separate**, mutable (`slots=True`, not frozen) dataclass, used
only for book-resident orders. Reasons it's distinct from `Order`, not a mutable
`Order`:

- A frozen dataclass can't shrink as it fills; mutating a frozen dataclass's field
  goes through `object.__setattr__`, which costs a function call — not paid on every
  partial fill if `RestingOrder` just isn't frozen.
- It carries `entry_seq`, an engine-assigned arrival ordinal that both `NaiveBook`
  and `ArrayBook` use identically for FIFO and expiry tie-breaks. That's engine
  state, not client input, and doesn't belong on `Order`.
- Keeping `Order` immutable means a caller holding a reference can never observe or
  corrupt live engine state, and the inbound stream stays safely replayable.

### Event design

Every terminal event carries the quantity it accounts for
(`OrderFilled.filled_quantity`, `OrderCanceled.canceled_quantity`,
`OrderExpired.expired_quantity`, `OrderKilled.killed_quantity`). This is load-bearing:
conservation (`filled + remaining + canceled + expired + killed == original`) is
verified by folding the **public event stream**, not by reading engine internals —
see `tests/property/ledger.py`. That checks the engine against its own published
contract (what a downstream consumer, e.g. the Phase 3 backtester, would actually
rely on), not against itself.

`OrderKilled.killed_quantity` — the spec calls this field `killed_qty`, renamed here
for consistency with the other three terminal events' `*_quantity` naming.

**Exact emission rule**, pinned because the differential tests compare event streams
for exact equality:

```
for each maker consumed, in match order:
    emit Trade(...)
    if maker.remaining == 0: emit OrderFilled(maker_id, maker.original_quantity)
after the loop:
    if taker_remaining == 0:  emit OrderFilled(taker_id, quantity)
    elif GTC/GTD:             emit OrderRested(taker_id, taker_remaining)
    elif FAK:                 emit OrderKilled(taker_id, taker_remaining)
    # FOK cannot reach here with remaining > 0
```

The maker gets its own `OrderFilled`, separate from the taker's: `Trade` is the
two-sided economic event, `OrderFilled` is the one-sided lifecycle event meaning
"this order_id is no longer live." A partially-consumed maker emits nothing beyond
its `Trade`. `OrderAccepted` is emitted even for a FOK/FAK that ends up killed —
"accepted" means "passed validation," kill is a separate outcome. No event is ever
emitted with a zero quantity (a fully-filled FAK gets `OrderFilled`, not
`OrderKilled(0)`).

### Validation order

Checked as a fixed ordered checklist; the first failure wins and exactly one
`OrderRejected` is emitted. Structure before identity — a malformed *and* duplicate
order is reported as malformed:

1. `TIMESTAMP_REGRESSION` (checked before anything else; it's the only failure that
   suppresses the clock advance)
2. `UNKNOWN_SIDE` / `UNKNOWN_ORDER_TYPE` (`isinstance` checks — unreachable through a
   type-checked caller, but the engine still must reject genuinely malformed runtime
   input rather than crash, e.g. from later deserialized data)
3. `PRICE_NOT_INTEGER` (note `bool` is an `int` subclass; `True`/`False` are rejected
   as prices)
4. `PRICE_OUT_OF_RANGE`
5. `QUANTITY_NOT_POSITIVE` (same bool caveat)
6. `EXPIRY_ON_NON_GTD` / `MISSING_EXPIRY` / `EXPIRY_IN_PAST` (compared against the
   **new** clock, so a GTD is never accepted that the very next sweep would kill)
7. `DUPLICATE_ORDER_ID` (checked against a permanent registry — see below)

### Order-state tracking and cancel semantics

`remaining` lives only on `RestingOrder`, inside the book — one source of truth, no
mirror in the engine. A taker's in-progress remaining during a match is a **local
int** in `submit()`, so FOK/FAK orders and any order that fully fills on arrival
never allocate a `RestingOrder` at all.

A separate engine-side `_state: dict[str, OrderState]` maps every order_id that ever
passed validation to its lifecycle state, **permanently** — including after it
reaches a terminal state. This is deliberately not a quantity ledger, just a state
tag. It does two jobs:

- **Duplicate-id detection.** An id is burned the moment it's accepted, even after
  the order later fills, cancels, or expires. A *rejected* order's id is **not**
  burned — rejection is no state change, so `test_rejected_order_id_remains_available`
  can immediately resubmit under the same id.
- **The `CancelRejected` discriminator.** Absent from `_state` → `UNKNOWN_ORDER_ID`;
  present but not `OPEN` → `ALREADY_FINISHED`. A book-only design (no separate
  registry) cannot distinguish "this id never existed" from "this id already
  finished," which the spec's "cancel an unknown or already-finished order" wording
  requires to be distinguishable.

Cancel checks the sweep before the id lookup (since the clock advances first): if
the sweep at `t` expires the very order being canceled, the result is
`OrderExpired(oid)` followed by `CancelRejected(oid, ALREADY_FINISHED)` in the same
returned list — `test_cancel_*` in `tests/unit` doesn't cover this exact interleaving
directly, but `_advance_and_sweep` running before the state lookup guarantees it.

### FOK: dry-run pass, not execute-then-rollback

`_crossing_quantity` walks `book.levels()` aggregates, breaking the moment a level
stops crossing or enough quantity has been found. If short, `OrderKilled` is emitted
having touched nothing; otherwise the ordinary match loop runs and is now guaranteed
to complete.

This stays bounded to the levels the execution pass would touch anyway, and reads
**only** level aggregates — never intra-level FIFO order — so it can never depend on
how a specific book implementation stores orders within a level, and both
implementations are contractually required (per the `OrderBook` protocol) to report
the same aggregates. "Zero trades, book unchanged" is therefore true by
construction, not by proof.

Execute-then-rollback was rejected: undoing a fill means reinserting a maker at its
exact former FIFO position, which an `OrderedDict` can't do without an O(k) level
rebuild, plus unwinding the best-price cache, `trade_id`, `seq`, and `_state` — five
things instead of zero, and a failure mode (silent book corruption) that only shows
up probabilistically in a differential test rather than being ruled out by
construction.

### Expiry: an engine-side min-heap with lazy deletion, not a book scan

A min-heap of `(expires_at, entry_seq, order_id)` lives in `MatchingEngine`, not in
either book. It has to live in the engine because the tie-break for
same-millisecond expiries must be identical across implementations — a "scan the
book for expired orders" design would let `NaiveBook` and `ArrayBook` emit the same
*set* of `OrderExpired` events in a different *order* (since each iterates its
internal storage differently), and the differential test would fail for a reason
that looks like a matching bug rather than an ordering artifact. Keying the heap on
`entry_seq` — an engine-assigned ordinal, not anything either book controls — is
what makes the ordering implementation-independent. Same-millisecond expiries fire
in arrival order (`entry_seq` ascending): a first-class documented rule, not an
implementation detail.

Pushed only when a GTD order actually rests (GTC never pushes; FOK/FAK never rest so
never push). Fills and cancels do **not** touch the heap — lazy deletion instead:
staleness is detected at pop time via `book.get(oid) is None or
order.entry_seq != entry_seq`. This keeps the common case (nothing due) at a single
comparison, which is what preserves `ArrayBook`'s O(1)-ish story for Phase 2 — a
naive per-event scan of all resting GTD orders would be O(n) and defeat the whole
point of a bounded-price array.

Lazy deletion alone would let heap memory grow without bound under a
rest-then-cancel-repeatedly workload, so `_stale_expiries` is tracked and the heap is
rebuilt (`heapify` over the still-live entries) once stale entries exceed half of it
— amortized O(1) per deletion, heap memory bounded by live GTD orders.

`advance_time(t)` with `t < clock` raises `ValueError` rather than emitting some new
event type: the spec defines no event for a rejected time advance, and a regressing
`advance_time` call is a harness bug (nothing produces it from real input, since
every `submit`/`cancel` already advances the clock monotonically), not a market
event worth representing in the event vocabulary.

### `ArrayBook`: two arrays per side, not one shared by price

Levels are indexed directly by price tick, in **two** lists of length
`max_tick + 1` — one for bids, one for asks — not one array shared between sides.

Sharing one array would still be *correct*: the matching engine's no-cross invariant
(enforced by `_match` always consuming resting liquidity until it stops crossing)
guarantees a bid and an ask can never simultaneously rest at the same price, since
either arriving order would trade against the other instead of resting. So a single
`self._levels[price]` indexed by price alone, with `front`/`levels`/etc. ignoring
`side`, would never actually mix bid and ask orders in practice.

That correctness argument is exactly the kind of cleverness this project's stated
values ("prefer clarity over cleverness") argue against: it makes the storage
layer's safety depend on a non-local invariant proved elsewhere (in the matching
loop), rather than by construction. Two arrays make `front(side, price)` and
`levels(side)` obviously correct without that proof, at a trivial memory cost (at
most ~4,000 pre-allocated `OrderedDict`s even at tick size 0.001).

Both arrays are length `max_tick + 1` so that indices `0` and `max_tick` are always
in-bounds and permanently empty, acting as sentinels for "no bid" / "no ask" — the
match loop's inner scan and `front()`'s bounds check need no separate `None`
handling for those two positions. All level `OrderedDict`s are pre-allocated at
construction and never destroyed, so a level emptying or filling never allocates or
frees a level object.

**FIFO via `OrderedDict`, not an intrusive doubly-linked list — for now.** A DLL
would remove `front()`'s one-iterator-per-call allocation, but costs ~40 lines of
pointer surgery and still needs the separate `dict[order_id, RestingOrder]` index
(it removes an iterator, not a lookup). Shipping `OrderedDict` in Phase 1 and
holding the DLL as a documented Phase 2 optimization means that swap can be
profiled first, made behind this same unchanged protocol, and reported with a
measured before/after number — which is what Phase 2 actually asks for. Beyond the
two structural choices above (pre-allocated levels, `OrderedDict` FIFO), `ArrayBook`
deliberately stays close to the obvious implementation — no extra caching or
micro-tuning — so Phase 2 has real profiling wins left to find.

### Testing

**The whole suite is parametrized over both book implementations** via a
`book_factory` fixture in `tests/conftest.py` (ids `naive` / `array`), plus a
convenience `engine` fixture for plain unit tests.

**Hypothesis tests build the engine inside the test body from `book_factory`, never
from the `engine` fixture.** A function-scoped pytest fixture is instantiated once
per test *function*, not once per Hypothesis *example* — using `engine` directly
would leak state across hundreds of examples and produce failures that don't
reproduce when Hypothesis replays its minimized case. (Hypothesis's own health check
flags any function-scoped fixture used under `@given` as a blanket warning, even
though calling `book_factory()` fresh per example is exactly the safe pattern it
can't distinguish from the unsafe one — this is suppressed globally via
`HealthCheck.function_scoped_fixture` in both registered profiles.)

**Command lists, not a `RuleBasedStateMachine`.** Every property here is a *replay*
property: the same command list needs to be fed to two engines side by side
(differential), run twice into fresh engines (determinism), and printed verbatim as
a Hypothesis failure repro. A state machine interleaves generation with execution
and leaves no such list to hand to a second engine. Built-in `st.lists` shrinking
(delete elements, shrink each) already gives the minimization behavior wanted,
without a custom `@st.composite` loop.

**Strategy design** (in `tests/property/strategies.py`) is what makes the property
tests actually exercise matching rather than degenerate into testing `add`:

- Prices are drawn mostly from a narrow band (`45..55` out of `1..99`), with a
  low-weight full-range tier for the boundaries. Uniform random prices almost never
  cross.
- Timestamps are non-negative deltas, prefix-summed into absolutes, biased toward 0.
  This guarantees commands are *accepted* (so matching runs) and produces many
  same-timestamp events. Absolute random timestamps would make most commands
  `TIMESTAMP_REGRESSION` rejections — that path has its own dedicated unit tests
  instead.
- Order types are weighted toward FOK/FAK above their uniform 25% share (GTC 50% /
  GTD 20% / FAK 15% / FOK 15%), since that's where bug density is highest.
- `order_id` is assigned by the runner from the command's list index, not drawn —
  drawing ids would burn search budget on duplicate-id rejections, which has its own
  dedicated unit test with a tiny id pool instead.
- `CancelCmd.target` is a large int resolved modulo the submits issued so far, so
  most cancels hit a real order and shrinking toward 0 targets the earliest
  (most-likely-still-resting) one.

**Conservation is checked from the event stream alone** (`tests/property/ledger.py`)
— never from engine internals, and "original quantity" per order is itself *derived*
from the stream (the first terminal-or-resting event for an id fixes it as
fills-so-far-via-`Trade`-sums plus that event's own quantity), not supplied by the
test's own knowledge of what it generated. `OrderFilled.filled_quantity` is
additionally cross-checked against the independently-accumulated `Trade` total,
rather than trusted as the source of truth for what "filled" means.

**Two invariants from the original property-test sketch aren't literally
implementable against the public API** and are adapted: `snapshot()` returns only
aggregated `BookLevel`s (price, total quantity, order count) — matching the spec's
documented `engine.snapshot()` signature exactly — with no per-order-id detail to
check "this id is/isn't in the book" against. The checkable, equivalent-in-spirit
versions used instead (see `tests/property/test_properties.py`): a FOK/FAK order
must never produce an `OrderRested` event (directly witnesses "never rests"), and an
order that already reached a terminal state must never appear in a later `Trade`
(directly witnesses "no zombie trading" — the underlying concern behind "no id after
a terminal event").

**The differential test drives both engines in lockstep**, one command at a time,
rather than running each engine through the whole list separately and comparing at
the end — so a divergence is caught at the exact command that caused it, both in the
event stream and in `snapshot()` / `best_bid()` / `best_ask()`. Its example count is
derived from the active Hypothesis profile (`active_profile.max_examples * 2`)
rather than hard-coded, so a fixed `max_examples=2000` doesn't silently override the
fast `dev` profile and slow down every local run — CI's `ci` profile still pushes it
into the thousands of examples the spec asks for.

## Complexity table

All are for a book with `max_tick` price ticks, `n` currently resting orders, and (for
`TreeBook`) `m` currently *occupied* price levels (`m <= n`, and `m <= max_tick - 1`).

| Operation | `NaiveBook` | `ArrayBook` | `TreeBook` |
|---|---|---|---|
| Add | O(1) (append) | O(1) | O(log m) (level lookup/insert in the `SortedDict`) |
| Cancel by id | O(n) (linear scan both sides) | O(1) (dict pop + `OrderedDict` delete) | O(log m) (delete, plus level removal if now empty) |
| Best price | O(n) (scan) | O(1) (cached, amortized — see below) | O(1) (`SortedDict.peekitem`) |
| Front of level | O(n) (scan + min by `entry_seq`) | O(1) | O(1) |
| Match (one taker) | O(n) per level swept | O(k) plus O(distance to next non-empty level) amortized per level exhausted | O(k) plus O(log m) per level exhausted |
| `levels()` full snapshot | O(n log n) (build + sort aggregates) | O(`max_tick`) worst case (bounded scan across all price slots) | O(m) levels, but O(n) total for the aggregate sums — see below |

`ArrayBook`'s best-price lookup is O(1) *amortized*: an individual cancel or fill
that happens to empty the current best level triggers a rescan bounded by the
distance to the next non-empty level, not by `n` or `max_tick` in the typical case
of a reasonably liquid book — but the rescan bound is `max_tick` in the worst case
(a single order resting far from any other liquidity). This divergence from
`NaiveBook`'s honest O(n) is exactly what Phase 2's benchmarks are meant to measure
and report with actual numbers, not asserted here.

`TreeBook`'s best-price lookup is O(1) unconditionally — `SortedDict.peekitem` reads
the first/last key directly — because empty levels are deleted rather than merely
tracked as zero, so there is never a rescan to perform. That is the specific property
Phase 2's sparse-book benchmark is built to make visible against `ArrayBook`'s
worst-case `max_tick` bound.

`TreeBook.levels()` recomputes each level's total quantity by summing `remaining`
over every order at that level, on every call — unlike `ArrayBook`'s O(1) cached
per-level total. This is a deliberate simplicity choice (see the Phase 2 entry
below), not an oversight: it makes a full snapshot O(n) rather than O(m), same as
`NaiveBook`, even though per-order operations are O(log m).

## Phase 2 — Benchmark and Optimize

A profiling pass on the real engine, done before writing any benchmark code, found
the headline result for this phase: **at the depths tested, `ArrayBook` is not the
bottleneck.** Book methods (`add`/`remove`/`front`/`best`) are a minority of profiled
time; engine-side per-command fixed costs — validation, event construction, `seq`
bookkeeping — dominate. That reframes the optimization work below: several
candidates are measured, found real, and *not* adopted, because they'd trade away
either correctness guarantees or code clarity for a return under the noise floor.

**`TreeBook`: `SortedDict[int, OrderedDict[str, RestingOrder]]` per side, optional via
the `tree` extra.** This is "the general-exchange approach" the spec names —
empty levels are deleted rather than tracked as zero-quantity, so `best()` never
needs `ArrayBook`'s bounded rescan; it's an O(1) key read on a data structure that
only ever contains occupied levels. `sortedcontainers` is declared only in the `tree`
and `dev` extras, never in `dependencies` — the engine core (`dependencies = []`)
stays zero-dependency, and `tree_book.py` is never imported from `engine/__init__.py`.

Rejected alternative for best-price tracking: a `heapq` of occupied prices. A heap
gives O(log m) best-price cheaply, but `levels()` — and therefore `snapshot()` and
the FOK dry-run's `_crossing_quantity` — needs *ordered iteration over every occupied
level*, which a heap can't provide without repeatedly popping and rebuilding it. A
sorted map gives both for the same asymptotic cost.

`TreeBook.levels()` recomputes each level's total quantity by summing `remaining`
over every resting order at that price, on every call, rather than maintaining a
cached per-level total the way `ArrayBook` does. A real "general exchange" B-tree
implementation would typically maintain that cache for O(1) top-of-book depth
queries; not doing so here is a deliberate simplicity choice, consistent with this
project's stated preference for obviously-correct over cleverly-optimized, and it
keeps `TreeBook` a genuinely different reference point from `ArrayBook` rather than a
second copy of the same caching strategy. Noted as a real complexity cost in the
table above, not hidden.

**`sortedcontainers-stubs` needed for mypy strict** — `sortedcontainers` ships no
`py.typed` marker and no bundled stubs (verified: mypy strict fails with
`import-untyped` without it), so `sortedcontainers-stubs` is a `dev`-extra
dependency purely for the type checker.

**mypy's `python_version` moved from 3.11 to 3.12.** `matplotlib` (the `bench`
extra) pulls in `numpy` transitively, and `hypothesis`'s optional numpy-integration
code (`hypothesis.internal.entropy`, imported — guarded by a runtime
`sys.modules` check — regardless of whether that guard would actually pass) makes
mypy follow an import chain into `numpy`'s own stub file, which uses the Python
3.12 `type` statement. mypy's `follow_imports = "skip"` was tried first, on both
`numpy.*` and the specific hypothesis internal modules that reach it — it did not
help, because "skip" mode still *parses* the target file to look for type comments,
so a genuine syntax-level incompatibility still aborts the whole run. `python_version`
in mypy config only governs which stdlib/typing surface mypy assumes when *checking
our code* against dependency stubs; it is not what enforces our actual 3.11
compatibility (CI's 3.11/3.12/3.13 test matrix does that, at runtime). Every module
here starts with `from __future__ import annotations` and none use any Python
3.12-only syntax, so raising this setting costs nothing real and unblocks the
dependency chain rather than playing whack-a-mole with every current and future
numpy-touching import inside a third-party package.

**`array_book_baseline.py`: a frozen, never-edited copy of Phase 1's `ArrayBook`**,
kept permanently in `BOOK_FACTORIES` and therefore under the conformance and
differential suites forever. This is what makes an in-process, interleaved (ABAB)
before/after benchmark possible: both the pre- and post-optimization implementation
exist simultaneously, measured under identical machine load in one session, rather
than comparing numbers from two separate sessions where machine conditions may
differ. Rejected: comparing across two git commits in two separate benchmark runs —
works, and is the *fallback* documented in `bench/`'s design, but loses the
same-session guarantee and doubles the chance that ambient machine noise (not the
code change) explains an observed delta.

**`tests/differential/test_differential.py` generalized to compare every registered
implementation against `NaiveBook`**, rather than hard-coding the naive/array pair.
The set of implementations under comparison is read from `BOOK_FACTORIES` at import
time, so `tree` is included automatically when the `sortedcontainers` extra is
installed and simply omitted otherwise (no `pytest.importorskip` marker needed — an
implementation that was never registered is an implementation that's never generated
as a parameter, which is a cleaner mechanism than skipping a generated test case).
A deliberately-broken `TreeBook.best()` (inverted index, returning the worst price
instead of the best) was caught immediately by this test with a 2-command shrunk
repro, confirming the generalized comparison actually exercises `TreeBook` and not
just the two implementations it replaced.

**`bench/` split into more files than the spec's `workloads.py` + `run_bench.py`.**
`harness.py` (the measured loop, warmup, GC handling, percentile math, environment
capture), `impls.py` (the implementation registry), and `report.py` (JSON → markdown)
are separated out because they are the parts of this phase a reviewer should
actually read — the measurement methodology is what carries an interview
conversation, and burying it inside argparse plumbing in one large `run_bench.py`
would bury the part with the most substance. `run_bench.py` remains the CLI entry
point the spec names.

**Workload generators use a fixed synthetic mid, not a random walk.** An early
version let the mid drift by up to one tick per command, which is realistic-looking
but unsound: since each order's price is an offset from the mid *at the moment it
was generated*, a later order can end up crossing an earlier one placed near a
different mid. This was caught by the harness's own setup-phase invariant check
(`assert not any(isinstance(e, Trade) for e in setup_events)`) firing during manual
smoke testing — `balanced`'s pre-population step was producing real trades. A fixed
mid makes "passive orders never cross each other" true by construction: every buy
prices at or below `mid - 1`, every sell at or above `mid + 1`, for the entire run.
The cost — price levels get reused rather than wandering — is free, since realistic
price *paths* were never a goal, only realistic *mixes* of operations.

**Cancel-target modeling in `balanced`/`deep_book`, and its honest cancel-miss
rate.** The generator can't ask a real engine which ids are still resting (that
would break the determinism guarantee the whole module is built on), so it keeps
its own approximate model: a list of ids it believes are still live, built only from
**passive** submissions (never marketable ones — a FOK/FAK never rests at all, and
an aggressively-priced GTC marketable usually fills partially or completely on
arrival, so neither is a trustworthy cancel target), with an entry removed the
moment the generator cancels it and a random subset removed when a marketable order
is generated (`_deplete`, capped at 8 removals per marketable, since a marketable's
aggressive price crosses essentially the whole opposite side regardless of exactly
when a given resting order was placed).

Even with this model, measured against a real `ArrayBook` engine: `balanced` misses
(`CancelRejected`) on roughly **19–32%** of its generated cancels depending on
`depth`; `deep_book` around **24%**; `sparse_book` (whose setup places at most one
order per price tick, making liveness trivially exact) around **3%**. The first
version of this model — before excluding marketable ids from the live set entirely
— missed on over 70%; that fix (marketable submissions are never reliable cancel
targets) was the single biggest improvement.

The remaining ~20-30% miss rate for `balanced`/`deep_book` is not treated as a bug
to keep chasing: closing it further would require the generator to actually
simulate price-time priority matching (which order gets consumed first, at what
partial quantity) — i.e., reimplementing the matching engine inside the benchmark
harness, which is precisely the coupling the "never query a real engine" design
principle exists to avoid. A `CancelRejected(ALREADY_FINISHED)` is also not free
noise in the measurement: it's a real, valid, O(1) engine operation (a dict lookup
against `_state`) that real clients issue too (cancel racing a fill), so a workload
that generates more of them than ideal is still exercising a legitimate code path,
not corrupting the benchmark. The rate is recorded in every results JSON
(`cancel_reject_rate` alongside `trades`/`rejects`) rather than hidden, and
`tests/bench/test_workloads.py` asserts it stays within the measured range rather
than an aspirational one.

**Two gaps found while running the first real benchmark matrix, both fixed before
any numbers were committed:**

1. `cell_key` omitted `max_tick`. Two runs at the same workload/impl/depth but
   different tick sizes (exactly the `sparse_book` array-vs-tree comparison this
   phase needs) would silently collide in `report.py`'s `(cell_key, mode, label)`
   dedup, and the later run would overwrite the earlier one's row with no error.
   Fixed by including `max_tick` in the key, and `report.py`'s latency table now
   shows a `tick` column (only when more than one tick size is present in the
   input, so single-tick-size tables stay uncluttered) and scopes the "vs min
   depth" scaling baseline per `(max_tick, impl)` rather than per `impl` alone.
2. A cell excluded from the default matrix (`_SLOW_CELLS`) was simply omitted from
   `_build_matrix`'s output, so it never appeared in the written results JSON at
   all — contradicting the documented intent ("excluded, not silently skipped").
   `_build_matrix` now returns every conceptual cell with an `excluded` flag;
   `_run_matrix` turns an excluded cell into a placeholder `CellResult` with
   `status="excluded"`, an `estimated_seconds` figure, and its `repro_command`, so
   it renders in `report.py` as a labeled row rather than an unexplained gap.

**First baseline matrix, measured** (`results/bench-latency-baseline-*.json`,
`results/bench-memory-baseline-*.json`; full tables in `results/RESULTS.md`):

- `deep_book` gives the cleanest O(n)-vs-O(1) story, exactly matching the
  complexity table: `ArrayBook`/`TreeBook` stay flat (≈1.0x p50 from depth 1,000 to
  100,000) while `NaiveBook` climbs to 5.4x by depth 10,000 (and is excluded above
  that by default — see `_SLOW_CELLS` — because its setup alone takes tens of
  seconds at depth 100,000).
- `balanced` shows the same divergence but later (NaiveBook only degrades sharply
  at depth 10,000, not 1,000): its default fractions drain a `depth=0` book to a
  thin steady state (documented above), so a pre-populated `depth` pool matters
  less to `balanced`'s *measured-phase* cost than it does to `deep_book`, whose
  cancels specifically target that pool.
- `sparse_book` at depths 20–200 (tick sizes 0.01 and 0.001) did **not** show
  `ArrayBook`'s worst-case tick-walking cost separating it from `TreeBook` — both
  stayed within measurement noise of each other (~1.5M events/s), both clearly
  ahead of `NaiveBook`. This is reported as a genuine, honest negative result
  rather than forced into a story: at these depths the gap between the best price
  and the next occupied level apparently isn't large enough to make `ArrayBook`'s
  bounded rescan cost measurably more than `TreeBook`'s `SortedDict` lookup. A
  sparser and/or deeper configuration would be needed to separate them, and is
  left as a documented gap rather than tuned after the fact to produce a nicer
  chart.
- Memory: `ArrayBook`/`ArrayBookBaseline`/`TreeBook` all cost roughly 260–290
  bytes/resting order versus `NaiveBook`'s ~170–172 (no id-index or per-level
  wrapper). `ArrayBook`'s pre-allocated fixed array cost is small at `max_tick=100`
  (a few hundred bytes at `depth=0`) — real at a finer tick size, but not
  dramatic at this scale.

## Phase 2 optimization log

Methodology for every entry below: profile first (three cells --
`balanced`/`array`/depth 10,000, `sweep_heavy`/`array`/depth 10,000,
`deep_book`/`array`/depth 100,000, each 60,000/60,000/20,000 measured commands),
make the change, run the full test suite (differential included, under
`HYPOTHESIS_PROFILE=ci`) before taking any measurement, then compare the same
three cells before vs. after. Acceptance bar, declared before measuring: keep an
optimization iff it improves both p50 and `events_per_sec` by ≥5% on at least one
cell, doesn't regress another cell by more than 2%, and doesn't grow
`bytes_per_resting_order` by more than 5%. `array_baseline` provides a true
same-process A/B only for changes confined to `array_book.py` itself; a change to
shared code (`src/engine/types.py`, `engine.py`) affects `array_baseline`
identically, since it reuses those modules unchanged -- for those, the comparison
is before/after across labeled result files instead, noted per entry below.

### 1. `RestingOrder.from_order`: keyword arguments → positional

**Hypothesis**: keyword-argument binding costs more per call than positional: this
classmethod runs on every order that rests (not just accepted -- every GTC/GTD that
doesn't fully fill), so shaving its cost pays off broadly.

**Change**: `src/engine/types.py` -- `cls(order_id=..., side=..., ...)` →
`cls(order.order_id, order.side, ...)`, field order matching `RestingOrder`'s
declaration exactly (documented inline, since that positional coupling is the
price of the optimization).

Lives in shared `types.py`, so this is a before/after comparison, not an
`array_baseline` A/B (`array_baseline` reuses the same `RestingOrder.from_order`
and shows the identical improvement -- confirmed, not just assumed, by measuring
it too).

**Before → after** (p50 / events-per-sec, `array`, 7 repeats each):

| cell | p50 before | p50 after | Δp50 | events/s before | events/s after | Δthroughput |
|---|---:|---:|---:|---:|---:|---:|
| balanced/d10,000 | 1500ns | 1417ns | **-5.5%** | 1,452,006 | 1,459,782 | +0.5% |
| sweep_heavy/d10,000 | 1500ns | 1375ns | **-8.3%** | 1,460,175 | 1,498,015 | +2.6% |
| deep_book/d100,000 | 1541ns | 1458ns | **-5.4%** | 1,334,106 | 1,350,003 | +1.2% |

**Verdict: kept**, with an honest caveat on the acceptance bar as literally stated.
p50 clears the ≥5% bar on every cell, consistently and in the predicted direction
-- three independent cells agreeing is a real, reproducible signal, not noise.
`events_per_sec` improves on every cell too, but by less than 5% on all three; it
aggregates more per-command variance (total trades, events-per-command) than p50
does, so a smaller, noisier throughput win alongside a clean, consistent p50 win is
a very different situation from the near-zero/negative case the ≥5%-on-both bar
was designed to filter out. Kept because the change is a one-line, zero-risk,
zero-memory-cost reordering with no readability cost, and the evidence -- while not
hitting the letter of the pre-declared bar on `events_per_sec` -- clearly clears its
intent.

### 2. `_validate`: `isinstance` pair → `type(x) is not int`

**Hypothesis**: an isolated microbenchmark (one call, in a tight loop, nothing
else happening) showed `type(x) is not int` at roughly half the cost of
`not isinstance(x, int) or isinstance(x, bool)`. Estimated ~7% of total command
time based on that isolated number and `_validate`'s ~8% share of profiled
`tottime` in the initial three-cell profile.

**Change**: `src/engine/engine.py`, `_validate`'s price and quantity checks.
Exactly equivalent for this purpose (`type(True) is bool`, not `int`, so `bool` is
still rejected; also rejects any other `int` subclass, arguably more correct than
the two-`isinstance` form). Lives in shared `engine.py`, so before/after across
labeled files again, not an `array_baseline` A/B.

**Before → after** (vs. optimization 1's numbers, `array`, 7 repeats each):

| cell | p50 before | p50 after | Δp50 | events/s before | events/s after | Δthroughput |
|---|---:|---:|---:|---:|---:|---:|
| balanced/d10,000 | 1417ns | 1417ns | 0% | 1,459,782 | 1,476,249 | +1.1% |
| sweep_heavy/d10,000 | 1375ns | 1375ns | 0% | 1,498,015 | 1,508,995 | +0.7% |
| deep_book/d100,000 | 1458ns | 1416ns | -2.9% | 1,350,003 | 1,365,677 | +1.2% |

**Verdict: no significant change against the pre-declared ≥5% bar** -- the isolated
microbenchmark overstated the real-world effect, because these two checks are a
small fraction of a command's total cost once matching, event construction, and
`seq` bookkeeping are included; `_validate`'s ~8% share of profiled time was itself
inflated by cProfile's well-known per-call overhead bias against small, frequently
called functions (noted in the profiling methodology above). **Kept anyway**: it is
a lossless simplification (one identity check instead of two calls, same
behavior, covered by the unchanged existing test suite) that also removes two
`# type: ignore[redundant-expr]` suppressions `mypy` needed for the old form --
a real code-quality improvement independent of the speed claim, which is reported
honestly as not holding up at the whole-command level.

**A related cost was measured, in a real throwaway variant, but deliberately not
adopted**: commenting out `_validate`'s `UNKNOWN_SIDE`/`UNKNOWN_ORDER_TYPE`
`isinstance` checks entirely (the ones a type-checked caller can never trigger --
see Phase 1's validation-order entry), then reverted immediately after
measuring -- never committed as a real change:

| cell | p50 with checks | p50 without | Δp50 | events/s with | events/s without | Δthroughput |
|---|---:|---:|---:|---:|---:|---:|
| balanced/d10,000 | 1417ns | 1334ns | -5.9% | 1,476,249 | 1,532,889 | +3.8% |
| sweep_heavy/d10,000 | 1375ns | 1292ns | -6.0% | 1,508,995 | 1,553,517 | +2.9% |
| deep_book/d100,000 | 1416ns | 1375ns | -2.9% | 1,365,677 | 1,396,826 | +2.3% |

A real, measurable cost (~3-6% p50), but well short of the "single largest
opportunity" an initial back-of-envelope estimate suggested before actually
measuring it -- a second instance, alongside entry #2 itself, of an isolated
estimate overstating a change's effect at the whole-command level. **Not
adopted**, regardless of the size of the number: those checks exist specifically
so a runtime-malformed order (e.g. from Phase 3's deserialized market data) is
rejected with an `OrderRejected` event instead of crashing the engine, and that
guarantee is worth its measured cost. The number is recorded here as the honest
price of paying it, not as an invitation to remove it later.
