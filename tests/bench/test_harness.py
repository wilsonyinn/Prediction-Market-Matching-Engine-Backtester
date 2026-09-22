"""Tests for the measured-loop plumbing itself -- not for actual timings.

Per the harness's own design, timings, environment-block contents, and
tracemalloc byte counts are machine-dependent and are never asserted on. What's
tested here is everything whose breakage would produce a *wrong* number silently:
the percentile function's exactness, deepcopy's fidelity for the aliasing
``ArrayBook`` relies on, and that ``run_cell`` produces a self-consistent result.
"""

from __future__ import annotations

import copy
from typing import cast

from bench.harness import (
    _deepcopy_engine,
    percentile_nearest_rank,
    replay_untimed,
    run_cell,
    run_memory_cell,
)
from bench.impls import IMPLS
from bench.workloads import balanced

from engine.array_book import ArrayBook
from engine.engine import MatchingEngine
from engine.types import Order, OrderType, Side


def test_percentile_nearest_rank_hand_computed() -> None:
    assert percentile_nearest_rank([10], 1.0) == 10
    assert percentile_nearest_rank([10], 0.5) == 10
    assert percentile_nearest_rank([1, 2], 1.0) == 2
    assert percentile_nearest_rank([1, 2], 0.5) == 1
    assert percentile_nearest_rank(list(range(1, 101)), 0.5) == 50
    assert percentile_nearest_rank(list(range(1, 101)), 0.99) == 99
    assert percentile_nearest_rank(list(range(1, 101)), 1.0) == 100


def test_percentile_returns_an_observed_value_not_interpolated() -> None:
    # An even-length input where the "true" median would fall between values --
    # nearest-rank must still return one of the actual observations.
    values = [10, 20]
    assert percentile_nearest_rank(values, 0.5) in values


def test_arraybook_deepcopy_preserves_index_aliasing() -> None:
    """The engine's O(1) `get` relies on `_index[oid] is` the level's stored order.

    ``run_cell`` deepcopies a pre-populated engine once per repeat rather than
    re-replaying setup; if deepcopy ever broke this aliasing, book operations would
    still run without crashing but would silently read/write two different
    objects, corrupting the measurement without raising anything. Also checks the
    intrusive doubly-linked-list FIFO's own aliasing: `_bid_head[price]` must be
    the exact same object as `_index[oid]`, and a second order's `_dll_prev` link
    must point at the deepcopy's own first order, not the original's.
    """
    engine = MatchingEngine(book=ArrayBook(max_tick=100))
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTC, timestamp=1))
    engine.submit(Order("o2", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))

    book = cast(ArrayBook, engine._book)
    assert book._index["o1"] is book._bid_head[50]
    assert book._index["o2"] is book._bid_tail[50]
    assert book._index["o1"]._dll_next is book._index["o2"]

    copied = copy.deepcopy(engine)
    copied_book = cast(ArrayBook, copied._book)
    assert copied_book._index["o1"] is copied_book._bid_head[50]
    assert copied_book._index["o1"] is not book._index["o1"]
    assert copied_book._index["o1"]._dll_next is copied_book._index["o2"]
    assert copied_book._index["o1"]._dll_next is not book._index["o2"]

    # And the copy is independent: mutating one must not affect the other.
    copied.cancel("o1", timestamp=3)
    assert copied.best_bid() == 50
    assert cast(ArrayBook, copied._book)._bid_head[50] is copied_book._index["o2"]
    assert engine.best_bid() == 50
    assert book._bid_head[50] is book._index["o1"]


def test_replay_untimed_counts_events() -> None:
    engine = MatchingEngine(book=ArrayBook(max_tick=100))
    workload = balanced(seed=1, n_events=50, depth=20, max_tick=100)

    outcome = replay_untimed(engine, workload.setup)

    assert outcome.trades == 0
    assert outcome.rejects == 0
    # each setup submit rests without crossing: OrderAccepted + OrderRested
    assert outcome.events == 2 * len(workload.setup)


def test_run_cell_produces_a_self_consistent_result() -> None:
    workload = balanced(seed=1, n_events=500, depth=100, max_tick=100)
    result = run_cell(
        workload,
        IMPLS["array"],
        repeats=2,
        warmup=50,
        max_seconds=10,
        min_samples=50,
        gc_mode="frozen",
        repro_command="pytest",
    )

    assert result["status"] == "ok"
    assert result["measured"] is not None
    assert result["measured"]["rejects"] == 0
    assert result["measured"]["commands"] == 500
    assert result["latency_ns"] is not None
    assert result["latency_ns"]["p50"] <= result["latency_ns"]["p95"] <= result["latency_ns"]["p99"]
    assert result["latency_ns"]["p99"] <= result["latency_ns"]["max"]
    assert len(result["per_repeat"]) == 2
    assert result["throughput"] is not None
    assert result["throughput"]["events_per_sec"] > 0


def test_run_memory_cell_captures_setup_allocations() -> None:
    """Regression test: tracemalloc must start before setup runs, not after.

    An earlier version started tracemalloc after replaying setup, which measures
    zero setup memory no matter how deep the book is -- tracemalloc only counts
    allocations made while it's enabled.
    """
    workload = balanced(seed=1, n_events=20, depth=200, max_tick=100)
    result = run_memory_cell(workload, IMPLS["array"], repro_command="pytest")

    assert result["status"] == "ok"
    assert result["memory"] is not None
    assert result["memory"]["book_bytes_after_setup"] > 0
    assert result["memory"]["bytes_per_resting_order"] > 0
    assert result["memory"]["peak_bytes"] >= result["memory"]["book_bytes_after_setup"]


def test_deepcopy_engine_handles_a_densely_populated_level() -> None:
    """Regression test for a RecursionError found while benchmarking the DLL FIFO.

    ArrayBook's intrusive doubly-linked-list FIFO makes copy.deepcopy recurse one
    Python stack frame per _dll_next link when it walks a price level's resting
    orders. A balanced setup at depth 5,000 concentrates well over Python's
    default recursion limit (1,000) of orders at a single price tick, since
    passive offsets cluster near the touch by design -- this raised
    RecursionError before _deepcopy_engine existed.
    """
    workload = balanced(seed=1, n_events=10, depth=5_000, max_tick=100)
    engine = MatchingEngine(book=IMPLS["array"].factory(max_tick=workload.max_tick))
    replay_untimed(engine, workload.setup)

    copied = _deepcopy_engine(engine)

    assert copied.best_bid() == engine.best_bid()
    assert copied.snapshot() == engine.snapshot()


def test_run_cell_skips_too_slow_below_sample_floor() -> None:
    """A min_samples floor above what a tiny, time-boxed run can produce."""
    workload = balanced(seed=1, n_events=200, depth=20, max_tick=100)
    result = run_cell(
        workload,
        IMPLS["array"],
        repeats=1,
        warmup=0,
        max_seconds=10,
        min_samples=10_000,  # unreachable for a 200-command workload
        gc_mode="enabled",
        repro_command="pytest",
    )

    assert result["status"] == "skipped_too_slow"
    assert result["latency_ns"] is None
    assert result["error"] is not None
