"""Conformance suite run against both book implementations directly.

Bypasses ``MatchingEngine`` entirely: these pin down the exact semantics the
``OrderBook`` protocol documents, so a book bug is caught here where it lives rather
than through a confusing differential-test failure downstream.
"""

from __future__ import annotations

from collections.abc import Callable

from engine.book import OrderBook
from engine.types import OrderType, RestingOrder, Side


def _resting(order_id: str, side: Side, price: int, quantity: int, entry_seq: int) -> RestingOrder:
    return RestingOrder(
        order_id=order_id,
        side=side,
        price=price,
        original_quantity=quantity,
        remaining=quantity,
        order_type=OrderType.GTC,
        timestamp=0,
        expires_at=None,
        owner_id=None,
        entry_seq=entry_seq,
    )


def test_front_on_empty_level_is_none(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    assert book.front(Side.BUY, 50) is None


def test_front_on_out_of_range_price_is_none(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory(max_tick=100)
    assert book.front(Side.BUY, -1) is None
    assert book.front(Side.BUY, 1000) is None


def test_best_is_none_on_empty_side(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    assert book.best(Side.BUY) is None
    assert book.best(Side.SELL) is None


def test_levels_yield_best_first_with_correct_aggregates(
    book_factory: Callable[..., OrderBook],
) -> None:
    book = book_factory()
    book.add(_resting("a", Side.BUY, 40, 5, 1))
    book.add(_resting("b", Side.BUY, 45, 3, 2))
    book.add(_resting("c", Side.BUY, 45, 7, 3))

    levels = list(book.levels(Side.BUY))

    assert levels == [(45, 10, 2), (40, 5, 1)]


def test_levels_best_first_for_asks_ascending(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    book.add(_resting("a", Side.SELL, 60, 5, 1))
    book.add(_resting("b", Side.SELL, 55, 3, 2))

    levels = list(book.levels(Side.SELL))

    assert levels == [(55, 3, 1), (60, 5, 1)]


def test_reduce_to_zero_leaves_order_resting(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    order = _resting("a", Side.BUY, 50, 5, 1)
    book.add(order)

    book.reduce(order, 5)

    assert book.get("a") is not None
    assert book.get("a").remaining == 0  # type: ignore[union-attr]


def test_remove_unknown_id_returns_none(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    assert book.remove("nope") is None


def test_front_returns_fifo_first_order(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    book.add(_resting("first", Side.BUY, 50, 5, 1))
    book.add(_resting("second", Side.BUY, 50, 5, 2))

    front = book.front(Side.BUY, 50)

    assert front is not None
    assert front.order_id == "first"


def test_best_price_survives_cancel_at_inner_level(book_factory: Callable[..., OrderBook]) -> None:
    """Canceling a non-best level must not disturb the cached best price."""
    book = book_factory()
    book.add(_resting("best", Side.BUY, 50, 5, 1))
    book.add(_resting("inner", Side.BUY, 45, 5, 2))

    book.remove("inner")

    assert book.best(Side.BUY) == 50


def test_best_price_advances_past_emptied_best_level(
    book_factory: Callable[..., OrderBook],
) -> None:
    book = book_factory()
    book.add(_resting("best", Side.BUY, 50, 5, 1))
    book.add(_resting("next", Side.BUY, 45, 5, 2))

    book.remove("best")

    assert book.best(Side.BUY) == 45


def test_len_counts_both_sides(book_factory: Callable[..., OrderBook]) -> None:
    book = book_factory()
    book.add(_resting("a", Side.BUY, 50, 5, 1))
    book.add(_resting("b", Side.SELL, 60, 5, 2))

    assert len(book) == 2
