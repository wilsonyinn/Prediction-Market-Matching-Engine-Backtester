"""FOK: fills completely and immediately, or is killed with zero trace."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import Order, OrderFilled, OrderKilled, OrderType, Side, Trade


def test_fok_success_single_level(engine: MatchingEngine) -> None:
    engine.submit(Order("maker", Side.SELL, 50, 10, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FOK, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    filled = [e for e in events if isinstance(e, OrderFilled)]
    assert {e.order_id for e in filled} == {"maker", "taker"}
    assert not any(isinstance(e, OrderKilled) for e in events)


def test_fok_success_needs_multiple_levels(engine: MatchingEngine) -> None:
    engine.submit(Order("a", Side.SELL, 40, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("b", Side.SELL, 41, 5, OrderType.GTC, timestamp=2))

    events = engine.submit(Order("taker", Side.BUY, 41, 10, OrderType.FOK, timestamp=3))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 2
    assert engine.snapshot().asks == ()


def test_fok_failure_zero_trades_book_unchanged(engine: MatchingEngine) -> None:
    engine.submit(Order("maker", Side.SELL, 50, 5, OrderType.GTC, timestamp=1))

    snap_before = engine.snapshot()
    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FOK, timestamp=2))

    assert not any(isinstance(e, Trade) for e in events)
    killed = [e for e in events if isinstance(e, OrderKilled)]
    assert killed == [
        OrderKilled(seq=killed[0].seq, timestamp=2, order_id="taker", killed_quantity=10)
    ]
    assert engine.snapshot() == snap_before


def test_fok_against_empty_side_is_killed(engine: MatchingEngine) -> None:
    events = engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FOK, timestamp=1))

    killed = [e for e in events if isinstance(e, OrderKilled)]
    assert killed[0].killed_quantity == 10


def test_fok_never_rests(engine: MatchingEngine) -> None:
    engine.submit(Order("taker", Side.BUY, 50, 10, OrderType.FOK, timestamp=1))

    assert engine.snapshot().bids == ()
    assert engine.best_bid() is None
