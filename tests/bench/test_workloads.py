"""Tests targeting properties that would silently produce *wrong benchmark numbers*.

Engine correctness is covered exhaustively by ``tests/unit``, ``tests/property``, and
``tests/differential`` -- these tests don't repeat that. They instead check the
workload generators themselves: determinism, the prefix property, that a generated
workload means the same thing on every book implementation, and that the intended
command mix is actually achieved against a real engine rather than assumed.

Tolerances below are **earned, not aspirational**: they come from replaying each
workload through a real ``ArrayBook`` engine during development (see
``docs/DESIGN.md``'s Phase 2 entry on the cancel-miss rate) rather than from a target
number decided in advance.
"""

from __future__ import annotations

from collections import Counter

import pytest
from bench.workloads import Advance, Cancel, Submit, balanced, deep_book, sparse_book, sweep_heavy
from bench.workloads import Workload as WorkloadType

from engine.engine import MatchingEngine
from engine.types import EngineEvent, OrderRejected, Trade
from tests.conftest import BOOK_FACTORIES


def _replay(engine: MatchingEngine, commands: tuple[object, ...]) -> list[EngineEvent]:
    events: list[EngineEvent] = []
    for cmd in commands:
        if isinstance(cmd, Submit):
            events.extend(engine.submit(cmd.order))
        elif isinstance(cmd, Cancel):
            events.extend(engine.cancel(cmd.order_id, cmd.timestamp))
        elif isinstance(cmd, Advance):
            events.extend(engine.advance_time(cmd.timestamp))
        else:
            msg = f"unknown command type: {cmd!r}"
            raise TypeError(msg)
    return events


def _build(name: str, max_tick: int = 100) -> MatchingEngine:
    return MatchingEngine(book=BOOK_FACTORIES[name](max_tick=max_tick))


def test_balanced_is_deterministic() -> None:
    a = balanced(seed=42, n_events=500, depth=100, max_tick=100)
    b = balanced(seed=42, n_events=500, depth=100, max_tick=100)
    assert a.setup == b.setup
    assert a.measured == b.measured


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (balanced, {"depth": 0}),
        (balanced, {"depth": 200}),
        (deep_book, {"depth": 200}),
        (sweep_heavy, {"depth": 200}),
        (sparse_book, {"depth": 50, "max_tick": 1000}),
    ],
)
def test_prefix_property(factory, kwargs) -> None:
    full = factory(seed=7, n_events=400, **kwargs)
    half = factory(seed=7, n_events=200, **kwargs)
    assert full.measured[:200] == half.measured
    # setup depends only on depth/seed, never on n_events
    assert full.setup == half.setup


def test_cross_implementation_identity() -> None:
    """The same workload must mean the same thing on every registered book."""
    workload = balanced(seed=3, n_events=300, depth=100, max_tick=100)

    reference_name = "naive"
    reference = _build(reference_name, workload.max_tick)
    reference_events = _replay(reference, workload.setup) + _replay(reference, workload.measured)

    for name in BOOK_FACTORIES:
        if name == reference_name:
            continue
        engine = _build(name, workload.max_tick)
        events = _replay(engine, workload.setup) + _replay(engine, workload.measured)
        assert events == reference_events, f"{name} diverged from {reference_name}"


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (balanced, {"depth": 150}),
        (deep_book, {"depth": 150}),
        (sweep_heavy, {"depth": 150}),
    ],
)
def test_setup_never_trades_and_reaches_depth(factory, kwargs) -> None:
    workload: WorkloadType = factory(seed=1, n_events=10, **kwargs)
    engine = _build("array", workload.max_tick)

    setup_events = _replay(engine, workload.setup)

    assert not any(isinstance(e, Trade) for e in setup_events)
    assert not any(isinstance(e, OrderRejected) for e in setup_events)
    assert len(engine._book) == kwargs["depth"]


def test_sparse_book_setup_has_at_most_one_order_per_level() -> None:
    workload = sparse_book(seed=1, n_events=10, depth=200, max_tick=1000)
    engine = _build("array", workload.max_tick)
    _replay(engine, workload.setup)

    snapshot = engine.snapshot()
    for level in (*snapshot.bids, *snapshot.asks):
        assert level.order_count == 1


def _measured_command_counts(workload: WorkloadType) -> Counter[str]:
    counts: Counter[str] = Counter()
    for cmd in workload.measured:
        if isinstance(cmd, Submit):
            counts["submit"] += 1
        elif isinstance(cmd, Cancel):
            counts["cancel"] += 1
        else:
            counts["advance"] += 1
    return counts


def test_balanced_achieves_intended_command_mix() -> None:
    workload = balanced(seed=1, n_events=5000, depth=0, max_tick=100)
    counts = _measured_command_counts(workload)
    total = sum(counts.values())

    # The generator draws each command's category from fixed probabilities, so the
    # law of large numbers over thousands of draws pins this tightly; a wide
    # tolerance here is about catching a broken split (e.g. a swapped edge), not
    # sampling noise.
    submit_frac = counts["submit"] / total
    cancel_frac = counts["cancel"] / total
    assert 0.65 < submit_frac < 0.85, submit_frac  # passive (0.55) + marketable (0.20)
    assert 0.15 < cancel_frac < 0.35, cancel_frac


def test_balanced_zero_rejects_and_bounded_cancel_misses() -> None:
    workload = balanced(seed=1, n_events=5000, depth=1000, max_tick=100)
    engine = _build("array", workload.max_tick)
    _replay(engine, workload.setup)
    events = _replay(engine, workload.measured)

    counts = Counter(type(e).__name__ for e in events)
    assert counts.get("OrderRejected", 0) == 0

    n_cancels = sum(1 for c in workload.measured if isinstance(c, Cancel))
    if n_cancels:
        miss_rate = counts.get("CancelRejected", 0) / n_cancels
        # See docs/DESIGN.md: closing this further would require the generator to
        # simulate price-time priority, which the design deliberately avoids.
        assert miss_rate < 0.5, miss_rate


def test_deep_book_zero_rejects_and_bounded_cancel_misses() -> None:
    workload = deep_book(seed=1, n_events=2000, depth=1000, max_tick=100)
    engine = _build("array", workload.max_tick)
    _replay(engine, workload.setup)
    events = _replay(engine, workload.measured)

    counts = Counter(type(e).__name__ for e in events)
    assert counts.get("OrderRejected", 0) == 0

    n_cancels = sum(1 for c in workload.measured if isinstance(c, Cancel))
    if n_cancels:
        miss_rate = counts.get("CancelRejected", 0) / n_cancels
        assert miss_rate < 0.5, miss_rate


def test_sparse_book_zero_rejects_and_low_cancel_misses() -> None:
    """Sparse book's one-order-per-level setup makes liveness trivially exact."""
    workload = sparse_book(seed=1, n_events=2000, depth=50, max_tick=1000)
    engine = _build("array", workload.max_tick)
    _replay(engine, workload.setup)
    events = _replay(engine, workload.measured)

    counts = Counter(type(e).__name__ for e in events)
    assert counts.get("OrderRejected", 0) == 0

    n_cancels = sum(1 for c in workload.measured if isinstance(c, Cancel))
    if n_cancels:
        miss_rate = counts.get("CancelRejected", 0) / n_cancels
        assert miss_rate < 0.15, miss_rate


def test_sweep_heavy_replenishes_book_size() -> None:
    workload = sweep_heavy(seed=1, n_events=2000, depth=500, max_tick=100)
    engine = _build("array", workload.max_tick)
    _replay(engine, workload.setup)
    start_size = len(engine._book)

    events = _replay(engine, workload.measured)

    assert not any(isinstance(e, OrderRejected) for e in events)
    end_size = len(engine._book)
    assert start_size * 0.75 <= end_size <= start_size * 1.25, (start_size, end_size)
