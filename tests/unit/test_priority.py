"""Price-time priority: better price first, then FIFO within a price level."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import Order, OrderType, Side, Trade


def test_better_price_fills_first(engine: MatchingEngine) -> None:
    """A higher resting bid fills before a lower one, regardless of arrival order."""
    engine.submit(Order("low", Side.BUY, 40, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("high", Side.BUY, 45, 5, OrderType.GTC, timestamp=2))

    events = engine.submit(Order("s1", Side.SELL, 40, 5, OrderType.GTC, timestamp=3))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].maker_order_id == "high"


def test_fifo_within_price_level(engine: MatchingEngine) -> None:
    """At the same price, the earlier-arriving order fills first."""
    engine.submit(Order("first", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("second", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))

    events = engine.submit(Order("s1", Side.SELL, 50, 5, OrderType.GTC, timestamp=3))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].maker_order_id == "first"


def test_fifo_survives_a_cancel_in_the_middle(engine: MatchingEngine) -> None:
    """Canceling a middle order doesn't disturb the FIFO order of the rest."""
    engine.submit(Order("a", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("b", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))
    engine.submit(Order("c", Side.BUY, 50, 5, OrderType.GTC, timestamp=3))
    engine.cancel("b", timestamp=4)

    events = engine.submit(Order("s1", Side.SELL, 50, 5, OrderType.GTC, timestamp=5))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].maker_order_id == "a"

    events2 = engine.submit(Order("s2", Side.SELL, 50, 5, OrderType.GTC, timestamp=6))
    trades2 = [e for e in events2 if isinstance(e, Trade)]
    assert trades2[0].maker_order_id == "c"
