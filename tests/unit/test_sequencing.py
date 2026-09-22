"""Same-timestamp sequencing and the never-crossed invariant."""

from __future__ import annotations

import pytest

from engine.engine import MatchingEngine
from engine.types import CancelRejected, CancelRejectReason, Order, OrderType, Side, Trade


def test_same_timestamp_events_process_in_call_order(engine: MatchingEngine) -> None:
    """Multiple events sharing a timestamp are still processed strictly in sequence."""
    e1 = engine.submit(Order("a", Side.BUY, 50, 5, OrderType.GTC, timestamp=5))
    e2 = engine.submit(Order("b", Side.BUY, 51, 5, OrderType.GTC, timestamp=5))
    e3 = engine.submit(Order("c", Side.SELL, 50, 10, OrderType.GTC, timestamp=5))

    all_seqs = [e.seq for e in (*e1, *e2, *e3)]
    assert all_seqs == sorted(all_seqs)
    # b (better price) fills before a, despite a arriving first at the same timestamp
    trades = [e for e in e3 if isinstance(e, Trade)]
    assert trades[0].maker_order_id == "b"


def test_book_never_crossed_after_resting(engine: MatchingEngine) -> None:
    engine.submit(Order("bid", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("ask", Side.SELL, 60, 5, OrderType.GTC, timestamp=2))

    bid, ask = engine.best_bid(), engine.best_ask()
    assert bid is not None
    assert ask is not None
    assert bid < ask


def test_cancel_rejects_timestamp_regression(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=10))

    events = engine.cancel("o1", timestamp=1)

    assert events == [
        CancelRejected(
            seq=events[0].seq,
            timestamp=10,
            order_id="o1",
            reason=CancelRejectReason.TIMESTAMP_REGRESSION,
        )
    ]
    # no state change: the order is still cancelable at a valid timestamp
    events2 = engine.cancel("o1", timestamp=11)
    assert not any(isinstance(e, CancelRejected) for e in events2)


def test_clock_tracks_the_latest_accepted_timestamp(engine: MatchingEngine) -> None:
    assert engine.clock == 0
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=7))
    assert engine.clock == 7


def test_advance_time_rejects_regression(engine: MatchingEngine) -> None:
    engine.advance_time(10)

    with pytest.raises(ValueError, match="cannot regress"):
        engine.advance_time(5)
