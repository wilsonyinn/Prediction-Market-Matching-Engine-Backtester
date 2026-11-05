"""Seeded, deterministic workload generators.

Every generator here is engine-independent: it produces a :class:`Workload` -- two
tuples of fully-resolved :class:`Command` objects -- using only its own
``random.Random`` instances, never by querying a real engine. That is a deliberate
constraint, not a limitation:

* **Determinism forever.** The same ``(name, seed, params)`` gives byte-identical
  commands on every run, on every machine, indefinitely -- because nothing about the
  generation depends on engine *behavior*, only on the PRNG stream. A generator that
  instead ran a reference engine first to learn which ids are live would still be
  reproducible today, but a future engine change would silently change the workload
  it produces, breaking comparability between a result committed last month and one
  committed today.
* **The prefix property.** Because each command consumes a fixed, position-independent
  number of RNG draws, ``balanced(seed=1, n_events=1000).measured[:500] ==
  balanced(seed=1, n_events=500).measured``. This is what lets a time-boxed or
  event-budgeted run (see ``bench/harness.py``) be treated as an exact prefix of the
  full run rather than a different workload -- the two are directly comparable.

Commands are **eagerly materialized as tuples**, not lazily generated during replay:
a single ``random.Random`` draw costs on the order of 100-200ns, which is not
negligible against a matching-engine command's own cost, so generating commands
inside the measured loop would charge harness overhead to the engine and pollute any
profile taken of it.

The **setup/measured split is a data property of** :class:`Workload`, **not a
callback**. ``bench/harness.py`` has exactly one timed loop and it only ever
receives ``workload.measured`` -- so the harness is physically incapable of timing
the pre-population of a deep book. A ``setup(engine)`` callback could be timed by a
future edit; a tuple cannot be.

**The synthetic mid is fixed, not a random walk.** An earlier version of this module
let the mid drift by up to one tick per command. That is realistic-looking but
unsound: since each passive order's price is an offset from the mid *at the moment
it is generated*, a mid that has since drifted can make a later order cross an
earlier one placed near the old mid -- observed in practice as ``Trade`` events
during what was supposed to be a non-crossing setup phase. A fixed mid makes
"passive orders never cross" true by construction: every buy prices at or below
``mid - 1`` and every sell at or above ``mid + 1``, for the entire run, so no two
passive orders can ever satisfy the crossing condition against each other. The
tradeoff -- price levels get reused rather than wandering over the run -- costs
nothing the benchmarks in this project need; realistic price *paths* were never the
point, only realistic *mixes* of operations.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from engine.types import Order, OrderType, Side

# A price/quantity far larger than any realistic run's event count or timestamp
# range, used as a GTD `expires_at` offset so an order generated deep inside the
# measured phase never actually expires during the run -- the heap-push code path
# is exercised on submission without a nondeterministic expiry contaminating some
# other sample's timing.
_FAR_FUTURE_OFFSET = 10**9

# Price offsets from the synthetic mid, weighted dense-near-the-touch: real order
# books have more resting size close to the market than far from it, and a uniform
# spread across the whole valid range would make every price level roughly equally
# likely to be the best, which trivializes ArrayBook's bounded rescan and flatters
# it relative to a realistic book shape.
_PASSIVE_OFFSETS = (1, 1, 1, 2, 2, 3, 4, 5, 7, 10)

# How far a marketable order's price steps past the mid. Larger than the widest
# passive offset above, so a marketable order's price alone crosses essentially all
# resting liquidity a passive order could have placed -- how much of it actually
# trades is then governed by the marketable order's quantity, not its price.
_MARKETABLE_OFFSET = 15

# Cap on how many "believed live" ids a single marketable submission removes from
# the generator's own liveness model (see `_deplete`). Bounds the worst-case cost
# of generating a long run regardless of how large `quantity` gets.
_MAX_DEPLETE_PER_MARKETABLE = 8


@dataclass(frozen=True, slots=True)
class Submit:
    """Submit ``order``."""

    order: Order


@dataclass(frozen=True, slots=True)
class Cancel:
    """Cancel ``order_id`` at ``timestamp``."""

    order_id: str
    timestamp: int


@dataclass(frozen=True, slots=True)
class Advance:
    """Advance the engine clock to ``timestamp``."""

    timestamp: int


Command = Submit | Cancel | Advance


@dataclass(frozen=True, slots=True)
class Workload:
    """A deterministic, replayable benchmark scenario.

    ``setup`` is replayed once to bring the book to its starting state and is never
    timed. ``measured`` is what the harness actually times. ``params`` is copied
    verbatim into the results JSON so a published cell is self-describing.
    """

    name: str
    params: dict[str, int | float | str]
    max_tick: int
    setup: tuple[Command, ...]
    measured: tuple[Command, ...]


class WorkloadFactory(Protocol):
    """The common call shape every entry in :data:`WORKLOADS` satisfies."""

    def __call__(
        self, *, seed: int, n_events: int, depth: int = 0, max_tick: int = 100
    ) -> Workload:
        """Build a :class:`Workload` for this scenario. See each implementation."""
        ...


def _clamp(price: int, max_tick: int) -> int:
    return max(1, min(max_tick - 1, price))


def _deplete(rng: random.Random, live_ids: list[str], quantity: int) -> None:
    """Approximate a marketable order consuming resting liquidity.

    The generator has no real book to consult, so this is deliberately a rough
    model, not a simulation of price-time priority: remove up to
    ``min(quantity // 8, _MAX_DEPLETE_PER_MARKETABLE)`` ids, chosen at random from
    ``live_ids`` rather than from either end. A marketable's aggressive price (see
    ``_MARKETABLE_OFFSET``) crosses essentially the entire opposite side regardless
    of *when* a given resting order was placed -- offsets are drawn independently
    per order, so insertion order carries no information about which orders sit
    closest to the touch -- which is why this does not prefer the front or back of
    the list. Its only job is to keep the generator's belief about which ids are
    still resting from drifting too far from reality, so that cancel targets mostly
    hit orders the real engine still considers open -- see the module docstring on
    determinism for why this must stay a model, not a query against a real engine.
    """
    # Removal preserves insertion order (an O(n) `del`, not a swap-with-last pop):
    # `balanced`'s cancel-target selection is recency-biased over this same list,
    # so scrambling its order here would quietly break that bias.
    n = min(max(1, quantity // 8), _MAX_DEPLETE_PER_MARKETABLE, len(live_ids))
    for _ in range(n):
        idx = rng.randrange(len(live_ids))
        del live_ids[idx]


def _passive_submit(
    rng: random.Random,
    order_id: str,
    timestamp: int,
    *,
    mid: int,
    max_tick: int,
    gtd_frac: float,
) -> Submit:
    side = rng.choice((Side.BUY, Side.SELL))
    offset = rng.choice(_PASSIVE_OFFSETS)
    price = _clamp(mid - offset if side is Side.BUY else mid + offset, max_tick)
    quantity = rng.randint(1, 20)
    if gtd_frac and rng.random() < gtd_frac:
        order_type = OrderType.GTD
        expires_at: int | None = timestamp + _FAR_FUTURE_OFFSET
    else:
        order_type = OrderType.GTC
        expires_at = None
    order = Order(order_id, side, price, quantity, order_type, timestamp, expires_at)
    return Submit(order)


def _marketable_submit(
    rng: random.Random,
    order_id: str,
    timestamp: int,
    *,
    mid: int,
    max_tick: int,
    fok_frac: float,
    fak_frac: float,
) -> tuple[Submit, int]:
    """Returns the command plus its quantity, so callers can estimate depletion."""
    side = rng.choice((Side.BUY, Side.SELL))
    price = _clamp(
        mid + _MARKETABLE_OFFSET if side is Side.BUY else mid - _MARKETABLE_OFFSET, max_tick
    )
    quantity = rng.randint(10, 60)
    roll = rng.random()
    order_type = (
        OrderType.FOK
        if roll < fok_frac
        else OrderType.FAK
        if roll < fok_frac + fak_frac
        else OrderType.GTC
    )
    order = Order(order_id, side, price, quantity, order_type, timestamp, None)
    return Submit(order), quantity


def _sparse_passive_submit(
    rng: random.Random, order_id: str, timestamp: int, price: int, max_tick: int
) -> Submit:
    # Side is determined by price relative to the fixed midpoint, not drawn
    # independently -- exactly the same fix as the module-level "fixed mid" note
    # above, applied per-order since sparse_book has no single shared mid. A
    # randomly-assigned side could place a BUY above a SELL (or vice versa) and
    # cross during what is supposed to be a non-crossing setup phase.
    side = Side.BUY if price < max_tick // 2 else Side.SELL
    quantity = rng.randint(1, 20)
    order = Order(order_id, side, price, quantity, OrderType.GTC, timestamp, None)
    return Submit(order)


def balanced(
    *,
    seed: int,
    n_events: int,
    depth: int = 0,
    max_tick: int = 100,
    passive_frac: float = 0.55,
    cancel_frac: float = 0.25,
    marketable_frac: float = 0.20,
    fok_frac: float = 0.05,
    fak_frac: float = 0.05,
    gtd_frac: float = 0.10,
) -> Workload:
    """A steady-state mix of operation types, not a precisely-conserved book size.

    "Balanced" describes the ratio of command kinds drawn, which the fixed
    per-draw probabilities guarantee by the law of large numbers over a run of any
    real length. It does **not** mean the book's resting-order count stays flat:
    each passive add or successful cancel changes it by exactly one order, but a
    single marketable command can consume *several* resting orders in one trade
    (its aggressive price crosses the whole opposite side; how many makers it
    actually eats depends on their sizes). Measured against a real engine at the
    defaults, that asymmetry drains a `depth=0` book to a thin single-digit
    steady state well before 5,000 events, not a size proportional to
    `passive_frac * n_events`. ``tests/bench/test_workloads.py`` checks the
    achieved *command-type* mix and the cancel-miss rate against this reality,
    not an idealized "flat book size" that doesn't hold.

    ``fok_frac``/``fak_frac`` are fractions of the *marketable* bucket (not of the
    whole distribution): a marketable order is FOK with probability ``fok_frac``,
    FAK with probability ``fak_frac``, and an ordinary marketable GTC otherwise
    (partial-fill-then-rest). Nonzero by default so the FOK dry-run and FAK kill
    paths are exercised at all -- a workload that never touched them would silently
    miss profiling one of the engine's real code paths.

    Cancel targets are drawn from the generator's own **live-id model**: a list of
    ids believed still resting, built from measured-phase **passive** submissions
    only (never ``depth`` setup orders, and never marketable submissions -- a FOK or
    FAK order never rests at all by protocol, and an aggressively-priced GTC
    marketable usually fills partially or completely on arrival, so neither is a
    reliable cancel target). An entry is removed the moment the generator itself
    cancels it, and approximately depleted when a marketable order is generated
    (see ``_deplete``), since a marketable can still consume resting *passive*
    liquidity. Selection within the model is recency-biased -- real cancels are
    disproportionately of orders just placed. The model is inherently approximate
    (the generator never queries a real engine, by design -- see the module
    docstring), so some cancels still miss; ``tests/bench/test_workloads.py`` measures
    the actual miss rate against a real engine and asserts it stays low rather than
    assuming a number.
    """
    setup_rng = random.Random(seed)
    measured_rng = random.Random(seed + 1)
    mid = max_tick // 2

    setup: list[Command] = [
        _passive_submit(setup_rng, f"setup{i}", 0, mid=mid, max_tick=max_tick, gtd_frac=0.0)
        for i in range(depth)
    ]

    total = passive_frac + cancel_frac + marketable_frac
    passive_edge = passive_frac / total
    cancel_edge = passive_edge + cancel_frac / total

    measured: list[Command] = []
    live_ids: list[str] = []
    for i in range(n_events):
        timestamp = i + 1
        roll = measured_rng.random()
        if roll < passive_edge:
            order_id = f"m{i}"
            measured.append(
                _passive_submit(
                    measured_rng, order_id, timestamp, mid=mid, max_tick=max_tick, gtd_frac=gtd_frac
                )
            )
            live_ids.append(order_id)
        elif roll < cancel_edge and live_ids:
            window = min(len(live_ids), 40)
            idx = len(live_ids) - 1 - measured_rng.randrange(window)
            target = live_ids.pop(idx)
            measured.append(Cancel(order_id=target, timestamp=timestamp))
        else:
            order_id = f"m{i}"
            submit, quantity = _marketable_submit(
                measured_rng,
                order_id,
                timestamp,
                mid=mid,
                max_tick=max_tick,
                fok_frac=fok_frac,
                fak_frac=fak_frac,
            )
            measured.append(submit)
            # Not added to live_ids: a FOK/FAK never rests, and an aggressively
            # priced GTC marketable is not a reliable cancel target either. It can,
            # however, still consume resting passive liquidity, hence the deplete.
            _deplete(measured_rng, live_ids, quantity)

    return Workload(
        name="balanced",
        params={
            "seed": seed,
            "n_events": n_events,
            "depth": depth,
            "max_tick": max_tick,
            "passive_frac": passive_frac,
            "cancel_frac": cancel_frac,
            "marketable_frac": marketable_frac,
            "fok_frac": fok_frac,
            "fak_frac": fak_frac,
        },
        max_tick=max_tick,
        setup=tuple(setup),
        measured=tuple(measured),
    )


def deep_book(
    *,
    seed: int,
    n_events: int,
    depth: int = 0,
    max_tick: int = 100,
    cancel_frac: float = 0.40,
    marketable_frac: float = 0.20,
) -> Workload:
    """Pre-populate ``depth`` resting orders, then measure operations against them.

    Unlike :func:`balanced`, cancel targets are drawn from the **setup** pool: the
    whole point of this workload is reaching into the deep pre-populated book (an
    O(n) linear scan for ``NaiveBook``, O(1) for ``ArrayBook``), not measuring
    steady-state churn of freshly-submitted orders. As in :func:`balanced`, a
    canceled id is removed from the live-id model and a marketable submission
    approximately depletes it (see ``_deplete``).
    """
    setup_rng = random.Random(seed)
    measured_rng = random.Random(seed + 1)
    mid = max_tick // 2

    setup: list[Command] = []
    setup_ids: list[str] = []
    for i in range(depth):
        order_id = f"setup{i}"
        setup.append(
            _passive_submit(setup_rng, order_id, 0, mid=mid, max_tick=max_tick, gtd_frac=0.0)
        )
        setup_ids.append(order_id)

    passive_frac = max(0.0, 1.0 - cancel_frac - marketable_frac)
    passive_edge = passive_frac
    cancel_edge = passive_edge + cancel_frac

    measured: list[Command] = []
    live_setup_ids = list(setup_ids)
    for i in range(n_events):
        timestamp = i + 1
        roll = measured_rng.random()
        if roll < passive_edge:
            measured.append(
                _passive_submit(
                    measured_rng, f"m{i}", timestamp, mid=mid, max_tick=max_tick, gtd_frac=0.0
                )
            )
        elif roll < cancel_edge and live_setup_ids:
            idx = measured_rng.randrange(len(live_setup_ids))
            target = live_setup_ids.pop(idx)
            measured.append(Cancel(order_id=target, timestamp=timestamp))
        else:
            submit, quantity = _marketable_submit(
                measured_rng,
                f"m{i}",
                timestamp,
                mid=mid,
                max_tick=max_tick,
                fok_frac=0.0,
                fak_frac=0.0,
            )
            measured.append(submit)
            _deplete(measured_rng, live_setup_ids, quantity)

    return Workload(
        name="deep_book",
        params={
            "seed": seed,
            "n_events": n_events,
            "depth": depth,
            "max_tick": max_tick,
            "cancel_frac": cancel_frac,
            "marketable_frac": marketable_frac,
        },
        max_tick=max_tick,
        setup=tuple(setup),
        measured=tuple(measured),
    )


def sweep_heavy(
    *,
    seed: int,
    n_events: int,
    depth: int = 0,
    max_tick: int = 100,
    levels_per_sweep: int = 8,
    refills_per_sweep: int = 12,
) -> Workload:
    """Alternate a large marketable sweep with replenishing passive adds.

    Must replenish, or the book drains within a few sweeps and the workload
    degenerates into "orders against an empty side" -- which exercises none of the
    multi-level matching code this workload exists to stress.
    ``tests/bench/test_workloads.py`` asserts end-of-run book size stays within
    ±25% of where it started, as a check that replenishment is actually keeping up.
    """
    setup_rng = random.Random(seed)
    measured_rng = random.Random(seed + 1)
    mid = max_tick // 2

    setup: list[Command] = [
        _passive_submit(setup_rng, f"setup{i}", 0, mid=mid, max_tick=max_tick, gtd_frac=0.0)
        for i in range(depth)
    ]

    measured: list[Command] = []
    sweep_side = Side.BUY
    i = 0
    while i < n_events:
        timestamp = i + 1
        price = _clamp(
            mid + _MARKETABLE_OFFSET * levels_per_sweep
            if sweep_side is Side.BUY
            else mid - _MARKETABLE_OFFSET * levels_per_sweep,
            max_tick,
        )
        quantity = measured_rng.randint(10, 20) * levels_per_sweep
        order = Order(f"m{i}", sweep_side, price, quantity, OrderType.GTC, timestamp, None)
        measured.append(Submit(order))
        i += 1
        sweep_side = Side.SELL if sweep_side is Side.BUY else Side.BUY

        for _ in range(refills_per_sweep):
            if i >= n_events:
                break
            timestamp = i + 1
            measured.append(
                _passive_submit(
                    measured_rng, f"m{i}", timestamp, mid=mid, max_tick=max_tick, gtd_frac=0.0
                )
            )
            i += 1

    return Workload(
        name="sweep_heavy",
        params={
            "seed": seed,
            "n_events": n_events,
            "depth": depth,
            "max_tick": max_tick,
            "levels_per_sweep": levels_per_sweep,
            "refills_per_sweep": refills_per_sweep,
        },
        max_tick=max_tick,
        setup=tuple(setup),
        measured=tuple(measured),
    )


def sparse_book(
    *,
    seed: int,
    n_events: int,
    depth: int = 0,
    max_tick: int = 100,
    cancel_frac: float = 0.50,
    passive_frac: float = 0.40,
) -> Workload:
    """Orders spread thinly across the whole valid price range.

    The discriminating workload for array-vs-tree: setup places **at most one
    resting order per price tick**, scattered across ``1 .. max_tick - 1`` rather
    than clustered near a mid. ``ArrayBook``'s best-price rescan and ``levels()``
    walk tick by tick and are worst-case O(max_tick) regardless of how sparse the
    book actually is; ``TreeBook`` only ever iterates *occupied* levels. Run at a
    fine tick size (large ``max_tick``) with a small ``depth`` to make the gaps
    between occupied levels large.

    Because each level holds at most one order, cancelling any resting order fully
    empties its level -- so a plain mix of cancels and passive adds already forces
    the best-price-advance/rescan behavior this workload exists to expose, without
    needing to specifically target the best price.
    """
    setup_rng = random.Random(seed)
    measured_rng = random.Random(seed + 1)

    available_prices = list(range(1, max_tick))
    setup_rng.shuffle(available_prices)
    setup_prices = available_prices[: min(depth, len(available_prices))]

    setup: list[Command] = []
    setup_ids: list[str] = []
    for i, price in enumerate(setup_prices):
        order_id = f"setup{i}"
        setup.append(_sparse_passive_submit(setup_rng, order_id, 0, price, max_tick))
        setup_ids.append(order_id)

    remaining_prices = available_prices[len(setup_prices) :]
    cancel_edge = cancel_frac
    passive_edge = cancel_edge + passive_frac

    measured: list[Command] = []
    live_ids = list(setup_ids)
    price_cursor = 0
    for i in range(n_events):
        timestamp = i + 1
        roll = measured_rng.random()
        if roll < cancel_edge and live_ids:
            idx = measured_rng.randrange(len(live_ids))
            target = live_ids.pop(idx)
            measured.append(Cancel(order_id=target, timestamp=timestamp))
        elif roll < passive_edge and price_cursor < len(remaining_prices):
            order_id = f"m{i}"
            price = remaining_prices[price_cursor]
            price_cursor += 1
            measured.append(
                _sparse_passive_submit(measured_rng, order_id, timestamp, price, max_tick)
            )
            live_ids.append(order_id)
        else:
            side = measured_rng.choice((Side.BUY, Side.SELL))
            price = max_tick - 1 if side is Side.BUY else 1
            quantity = measured_rng.randint(1, 5)
            order = Order(f"m{i}", side, price, quantity, OrderType.GTC, timestamp, None)
            measured.append(Submit(order))

    return Workload(
        name="sparse_book",
        params={
            "seed": seed,
            "n_events": n_events,
            "depth": depth,
            "max_tick": max_tick,
            "cancel_frac": cancel_frac,
            "passive_frac": passive_frac,
        },
        max_tick=max_tick,
        setup=tuple(setup),
        measured=tuple(measured),
    )


WORKLOADS: dict[str, WorkloadFactory] = {
    "balanced": balanced,
    "deep_book": deep_book,
    "sweep_heavy": sweep_heavy,
    "sparse_book": sparse_book,
}
