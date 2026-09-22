"""GTD expiry: fires exactly at expires_at, not matchable after, past-expiry rejected."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import (
    Order,
    OrderExpired,
    OrderRejected,
    OrderType,
    RejectReason,
    Side,
    Trade,
)


def test_gtd_expires_at_expires_at(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTD, timestamp=1, expires_at=10))

    events = engine.advance_time(10)

    assert events == [
        OrderExpired(seq=events[0].seq, timestamp=10, order_id="o1", expired_quantity=10)
    ]
    assert engine.best_bid() is None


def test_gtd_not_matchable_at_its_own_expiry(engine: MatchingEngine) -> None:
    """An order expiring at t is off the book before any event at t can match it."""
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTD, timestamp=1, expires_at=10))

    events = engine.submit(Order("taker", Side.SELL, 50, 10, OrderType.GTC, timestamp=10))

    assert not any(isinstance(e, Trade) for e in events)
    assert any(isinstance(e, OrderExpired) and e.order_id == "o1" for e in events)


def test_gtd_still_matchable_just_before_expiry(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTD, timestamp=1, expires_at=10))

    events = engine.submit(Order("taker", Side.SELL, 50, 10, OrderType.GTC, timestamp=9))

    assert any(isinstance(e, Trade) for e in events)


def test_gtd_with_past_expiry_is_rejected(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTD, timestamp=5, expires_at=5))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=5, order_id="o1", reason=RejectReason.EXPIRY_IN_PAST
        )
    ]


def test_gtd_missing_expiry_is_rejected(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 50, 10, OrderType.GTD, timestamp=1))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.MISSING_EXPIRY
        )
    ]


def test_expiry_on_non_gtd_is_rejected(engine: MatchingEngine) -> None:
    events = engine.submit(
        Order("o1", Side.BUY, 50, 10, OrderType.GTC, timestamp=1, expires_at=100)
    )

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.EXPIRY_ON_NON_GTD
        )
    ]


def test_multiple_gtd_expiring_same_millisecond_expire_in_arrival_order(
    engine: MatchingEngine,
) -> None:
    engine.submit(Order("first", Side.BUY, 50, 5, OrderType.GTD, timestamp=1, expires_at=10))
    engine.submit(Order("second", Side.BUY, 49, 5, OrderType.GTD, timestamp=2, expires_at=10))

    events = engine.advance_time(10)

    expired_ids = [e.order_id for e in events if isinstance(e, OrderExpired)]
    assert expired_ids == ["first", "second"]


def test_gtd_canceled_before_expiry_leaves_no_stale_expired_event(
    engine: MatchingEngine,
) -> None:
    """A GTD canceled before its expiry must not later emit OrderExpired.

    Cancel uses lazy deletion on the expiry heap (see docs/DESIGN.md): the heap
    entry is left in place and only discovered stale when it's popped. A second
    resting GTD keeps the heap above the compaction threshold (which would
    otherwise eagerly rebuild the heap and remove the stale entry proactively,
    never exercising the lazy-pop path this test targets).
    """
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTD, timestamp=1, expires_at=10))
    engine.submit(Order("o2", Side.BUY, 49, 5, OrderType.GTD, timestamp=1, expires_at=10))
    engine.cancel("o1", timestamp=2)

    events = engine.advance_time(10)

    assert not any(isinstance(e, OrderExpired) and e.order_id == "o1" for e in events)
    assert any(isinstance(e, OrderExpired) and e.order_id == "o2" for e in events)


def test_a_submit_at_a_later_timestamp_triggers_expiry_first(engine: MatchingEngine) -> None:
    """The expiry sweep runs before the triggering submit's own events."""
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTD, timestamp=1, expires_at=5))

    events = engine.submit(Order("o2", Side.BUY, 40, 5, OrderType.GTC, timestamp=5))

    assert isinstance(events[0], OrderExpired)
    assert events[0].order_id == "o1"
