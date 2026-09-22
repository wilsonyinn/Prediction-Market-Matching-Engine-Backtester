"""Partial fills, multi-level sweeps, and maker-price execution."""

from __future__ import annotations

from engine.engine import MatchingEngine
from engine.types import BookLevel, Order, OrderFilled, OrderRested, OrderType, Side, Trade


def test_partial_fill_taker_larger_than_resting(engine: MatchingEngine) -> None:
    """A taker larger than the resting order fills the maker and rests the rest."""
    engine.submit(Order("maker", Side.BUY, 50, 4, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.SELL, 50, 10, OrderType.GTC, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert trades == [
        Trade(
            seq=trades[0].seq,
            timestamp=2,
            trade_id=trades[0].trade_id,
            maker_order_id="maker",
            taker_order_id="taker",
            price=50,
            quantity=4,
            taker_side=Side.SELL,
        )
    ]
    rested = [e for e in events if isinstance(e, OrderRested)]
    assert rested == [OrderRested(seq=rested[0].seq, timestamp=2, order_id="taker", remaining=6)]
    assert engine.snapshot().bids == ()


def test_partial_fill_taker_smaller_than_resting(engine: MatchingEngine) -> None:
    """A taker smaller than the resting order leaves the maker resting, reduced."""
    engine.submit(Order("maker", Side.BUY, 50, 10, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.SELL, 50, 4, OrderType.GTC, timestamp=2))

    filled = [e for e in events if isinstance(e, OrderFilled)]
    assert filled == [
        OrderFilled(seq=filled[0].seq, timestamp=2, order_id="taker", filled_quantity=4)
    ]
    snap = engine.snapshot()
    assert snap.bids[0].total_quantity == 6
    assert snap.bids[0].order_count == 1


def test_sweep_across_multiple_price_levels(engine: MatchingEngine) -> None:
    """One incoming order can sweep several resting orders across several levels."""
    engine.submit(Order("a", Side.SELL, 40, 5, OrderType.GTC, timestamp=1))
    engine.submit(Order("b", Side.SELL, 41, 5, OrderType.GTC, timestamp=2))
    engine.submit(Order("c", Side.SELL, 42, 5, OrderType.GTC, timestamp=3))

    events = engine.submit(Order("taker", Side.BUY, 45, 12, OrderType.GTC, timestamp=4))

    trades = [e for e in events if isinstance(e, Trade)]
    assert [(t.maker_order_id, t.price, t.quantity) for t in trades] == [
        ("a", 40, 5),
        ("b", 41, 5),
        ("c", 42, 2),
    ]
    filled = [e for e in events if isinstance(e, OrderFilled)]
    assert {e.order_id for e in filled} == {"a", "b", "taker"}
    snap = engine.snapshot()
    assert snap.asks == (BookLevel(42, 3, 1),)


def test_execution_price_is_the_makers_price(engine: MatchingEngine) -> None:
    """Even when the taker's limit is more generous, the maker's price wins."""
    engine.submit(Order("maker", Side.SELL, 40, 5, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.BUY, 55, 5, OrderType.GTC, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert trades[0].price == 40


def test_boundary_buy_equals_best_ask_trades(engine: MatchingEngine) -> None:
    """A buy at exactly the best ask price crosses and trades."""
    engine.submit(Order("maker", Side.SELL, 50, 5, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].price == 50


def test_boundary_sell_equals_best_bid_trades(engine: MatchingEngine) -> None:
    """A sell at exactly the best bid price crosses and trades."""
    engine.submit(Order("maker", Side.BUY, 50, 5, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("taker", Side.SELL, 50, 5, OrderType.GTC, timestamp=2))

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].price == 50


def test_non_crossing_order_rests_without_trading(engine: MatchingEngine) -> None:
    """An order that doesn't cross the opposite side rests untouched."""
    engine.submit(Order("resting_sell", Side.SELL, 55, 5, OrderType.GTC, timestamp=1))

    events = engine.submit(Order("buy", Side.BUY, 50, 5, OrderType.GTC, timestamp=2))

    assert not any(isinstance(e, Trade) for e in events)
    rested = [e for e in events if isinstance(e, OrderRested)]
    assert rested[0].order_id == "buy"
    assert engine.best_bid() == 50
    assert engine.best_ask() == 55
