"""FAK: fills as much as possible immediately; the remainder is killed, never rests."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import Order, OrderFilled, OrderKilled, OrderType, Side, Trade


def test_fak_partial_execution_remainder_killed(engine: MatchingEngine) -> None:
    engine.submit(Order("maker", Side.SELL, 50, 4, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FAK, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].quantity == 4
    killed = [e for e in events if isinstance(e, OrderKilled)]
    assert killed == [
        OrderKilled(seq=killed[0].seq, timestamp=2, order_id="taker", killed_quantity=6)
    ]


def test_fak_against_empty_side_is_fully_killed(engine: MatchingEngine) -> None:
    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FAK, timestamp=1))

    assert not any(isinstance(e, Trade) for e in events)
    killed = [e for e in events if isinstance(e, OrderKilled)]
    assert killed[0].killed_quantity == 10


def test_fak_full_fill_emits_no_killed_event(engine: MatchingEngine) -> None:
    """A FAK that fills completely is OrderFilled, not OrderKilled(0)."""
    engine.submit(Order("maker", Side.SELL, 50, 10, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FAK, timestamp=2))

    assert not any(isinstance(e, OrderKilled) for e in events)
    filled = [e for e in events if isinstance(e, OrderFilled) and e.order_id == "taker"]
    assert filled


def test_fak_never_rests(engine: MatchingEngine) -> None:
    engine.submit(Order("maker", Side.SELL, 50, 4, OrderType.GTC, timestamp=1))

    engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FAK, timestamp=2))

    assert engine.snapshot().bids == ()
    assert engine.best_bid() is None
