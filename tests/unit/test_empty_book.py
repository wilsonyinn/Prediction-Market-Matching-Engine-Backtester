"""Empty-book behavior."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import BookSnapshot, Order, OrderRested, OrderType, Side, Trade


def test_best_bid_ask_none_on_empty_book(engine: MatchingEngine) -> None:
    assert engine.best_bid() is None
    assert engine.best_ask() is None
    assert engine.snapshot() == BookSnapshot(bids=(), asks=())


def test_order_against_empty_opposite_side_rests(engine: MatchingEngine) -> None:
    events = engine.submit(Order("o1", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))

    assert not any(isinstance(e, Trade) for e in events)
    assert any(isinstance(e, OrderRested) for e in events)
    assert engine.best_bid() == 50
