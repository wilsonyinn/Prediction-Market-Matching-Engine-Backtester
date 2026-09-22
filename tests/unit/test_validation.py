"""One test per RejectReason.

A handful of these (UNKNOWN_SIDE, UNKNOWN_ORDER_TYPE, PRICE_NOT_INTEGER for a bool,
QUANTITY_NOT_POSITIVE for a bool) construct a deliberately type-invalid ``Order`` --
unreachable through a type-checked caller, but real input can still be malformed
(hand-built dicts, deserialized data in a later phase), and the engine must reject it
with an event rather than crash. The ``type: ignore[arg-type]`` comments mark exactly
those intentional violations.

Every rejection here must cause **no state change**: none of these order_ids may be
reused-and-rejected differently, and the book must stay untouched.
"""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import Order, OrderRejected, OrderType, RejectReason, Side


def test_reject_timestamp_regression(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=10))

    events = engine.submit(Order("o2", Side.BUY, 50, 5, OrderType.GTC, timestamp=5))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=10, order_id="o2", reason=RejectReason.TIMESTAMP_REGRESSION
        )
    ]


def test_reject_unknown_side(engine: MatchingEngine) -> None:
    bad_order = Order("o1", "SIDEWAYS", 50, 5, OrderType.GTC, timestamp=1)  # type: ignore[arg-type]

    events = engine.submit(bad_order)

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.UNKNOWN_SIDE
        )
    ]


def test_reject_unknown_order_type(engine: MatchingEngine) -> None:
    bad_order = Order("o1", Side.BUY, 50, 5, "SOMETHING", timestamp=1)  # type: ignore[arg-type]

    events = engine.submit(bad_order)

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.UNKNOWN_ORDER_TYPE
        )
    ]


def test_reject_price_not_integer(engine: MatchingEngine) -> None:
    bad_order = Order("o1", Side.BUY, 50.5, 5, OrderType.GTC, timestamp=1)  # type: ignore[arg-type]

    events = engine.submit(bad_order)

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.PRICE_NOT_INTEGER
        )
    ]


def test_reject_price_bool_is_not_integer(engine: MatchingEngine) -> None:
    """bool is an int subclass; True/False must not be accepted as a price."""
    # bool is a subtype of int, so this is type-valid but semantically wrong input.
    bad_order = Order("o1", Side.BUY, True, 5, OrderType.GTC, timestamp=1)

    events = engine.submit(bad_order)

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.PRICE_NOT_INTEGER
        )
    ]


def test_reject_price_out_of_range_low(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 0, 5, OrderType.GTC, timestamp=1))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.PRICE_OUT_OF_RANGE
        )
    ]


def test_reject_price_out_of_range_high(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 100, 5, OrderType.GTC, timestamp=1))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.PRICE_OUT_OF_RANGE
        )
    ]


def test_reject_quantity_not_positive_zero(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 50, 0, OrderType.GTC, timestamp=1))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.QUANTITY_NOT_POSITIVE
        )
    ]


def test_reject_quantity_not_positive_negative(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 50, -3, OrderType.GTC, timestamp=1))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=1, order_id="o1", reason=RejectReason.QUANTITY_NOT_POSITIVE
        )
    ]


def test_reject_duplicate_order_id(engine: MatchingEngine) -> None:
    engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("o1", Side.SELL, 60, 5, OrderType.GTC, timestamp=2))

    assert events == [
        OrderRejected(
            seq=events[0].seq, timestamp=2, order_id="o1", reason=RejectReason.DUPLICATE_ORDER_ID
        )
    ]
    # the original order is untouched
    assert engine.best_bid() == 50


def test_rejected_order_id_remains_available(engine: MatchingEngine) -> None:
    """A rejection causes no state change: the id is not burned."""
    engine.submit(Order("o1", Side.BUY, 0, 5, OrderType.GTC, timestamp=1))  # rejected: bad price

    events = engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))

    assert not any(isinstance(e, OrderRejected) for e in events)
