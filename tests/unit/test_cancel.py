"""Cancellation: resting, partially filled, unknown id, and double-cancel."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import (
    CancelRejected,
    CancelRejectReason,
    Order,
    OrderCanceled,
    OrderType,
    Side,
)


def test_cancel_resting_order(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTC, timestamp=1))

    events = engine.cancel("o1", timestamp=2)

    assert events == [
        OrderCanceled(seq=events[0].seq, timestamp=2, order_id="o1", canceled_quantity=10)
    ]
    assert engine.best_bid() is None


def test_cancel_partially_filled_order(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTC, timestamp=1))
    engine.submit(Order("taker", Side.SELL, 50, 4, OrderType.GTC, timestamp=2))

    events = engine.cancel("o1", timestamp=3)

    assert events == [
        OrderCanceled(seq=events[0].seq, timestamp=3, order_id="o1", canceled_quantity=6)
    ]


def test_cancel_unknown_order_id(engine: MatchingEngine) -> None:
    events = engine.cancel("nonexistent", timestamp=1)

    assert events == [
        CancelRejected(
            seq=events[0].seq,
            timestamp=1,
            order_id="nonexistent",
            reason=CancelRejectReason.UNKNOWN_ORDER_ID,
        )
    ]


def test_cancel_twice_is_rejected(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTC, timestamp=1))
    engine.cancel("o1", timestamp=2)

    events = engine.cancel("o1", timestamp=3)

    assert events == [
        CancelRejected(
            seq=events[0].seq,
            timestamp=3,
            order_id="o1",
            reason=CancelRejectReason.ALREADY_FINISHED,
        )
    ]


def test_cancel_a_fully_filled_order_is_rejected(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("taker", Side.SELL, 50, 5, OrderType.GTC, timestamp=2))

    events = engine.cancel("o1", timestamp=3)

    assert events == [
        CancelRejected(
            seq=events[0].seq,
            timestamp=3,
            order_id="o1",
            reason=CancelRejectReason.ALREADY_FINISHED,
        )
    ]
