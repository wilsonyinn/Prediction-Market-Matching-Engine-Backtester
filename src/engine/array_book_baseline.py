"""``ArrayBookBaseline``: a frozen snapshot of Phase 1's ``ArrayBook``.

This is a verbatim copy of ``array_book.py`` as it stood at commit ``9641993``
(the Phase 0/1 merge, before any Phase 2 optimization), renamed to
``ArrayBookBaseline``. It exists solely so that "before" and "after" can be
benchmarked in the same process, interleaved (ABAB), under identical machine
conditions -- rather than comparing numbers from two separate sessions on
potentially different machine load. It stays registered in
``tests.conftest.BOOK_FACTORIES`` so it remains under the conformance and
differential suites permanently: a "baseline" that silently drifted out of
correctness would make every before/after comparison meaningless.

**This file must never be edited.** If Phase 1's `ArrayBook` had a bug that needs
fixing, fix it in ``array_book.py``; this file's entire value is being a fixed
point. If it is ever no longer needed as a benchmark comparator, delete it --
don't repurpose it.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from engine.types import Side

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.types import RestingOrder


class ArrayBookBaseline:
    """Frozen Phase 1 ``ArrayBook``. See module docstring -- do not edit."""

    def __init__(self, max_tick: int) -> None:
        self._max_tick = max_tick
        self._bid_levels: list[OrderedDict[str, RestingOrder]] = [
            OrderedDict() for _ in range(max_tick + 1)
        ]
        self._ask_levels: list[OrderedDict[str, RestingOrder]] = [
            OrderedDict() for _ in range(max_tick + 1)
        ]
        self._bid_qty: list[int] = [0] * (max_tick + 1)
        self._ask_qty: list[int] = [0] * (max_tick + 1)
        self._index: dict[str, RestingOrder] = {}
        self._best_bid: int = 0
        self._best_ask: int = max_tick

    @property
    def max_tick(self) -> int:
        return self._max_tick

    def add(self, order: RestingOrder) -> None:
        levels, qty = self._arrays_for(order.side)
        levels[order.price][order.order_id] = order
        qty[order.price] += order.remaining
        self._index[order.order_id] = order
        if order.side is Side.BUY:
            self._best_bid = max(self._best_bid, order.price)
        else:
            self._best_ask = min(self._best_ask, order.price)

    def remove(self, order_id: str) -> RestingOrder | None:
        order = self._index.pop(order_id, None)
        if order is None:
            return None
        levels, qty = self._arrays_for(order.side)
        del levels[order.price][order_id]
        qty[order.price] -= order.remaining
        if qty[order.price] == 0:
            self._advance_best_past(order.side, order.price)
        return order

    def get(self, order_id: str) -> RestingOrder | None:
        return self._index.get(order_id)

    def best(self, side: Side) -> int | None:
        if side is Side.BUY:
            return self._best_bid if self._best_bid != 0 else None
        return self._best_ask if self._best_ask != self._max_tick else None

    def front(self, side: Side, price: int) -> RestingOrder | None:
        if price < 0 or price > self._max_tick:
            return None
        levels, _qty = self._arrays_for(side)
        return next(iter(levels[price].values()), None)

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        _levels, qty = self._arrays_for(order.side)
        order.remaining -= quantity
        qty[order.price] -= quantity
        if qty[order.price] == 0:
            self._advance_best_past(order.side, order.price)

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        levels, qty = self._arrays_for(side)
        if side is Side.BUY:
            price = self._best_bid
            while price >= 1:
                if qty[price] > 0:
                    yield price, qty[price], len(levels[price])
                price -= 1
        else:
            price = self._best_ask
            while price <= self._max_tick - 1:
                if qty[price] > 0:
                    yield price, qty[price], len(levels[price])
                price += 1

    def __len__(self) -> int:
        return len(self._index)

    def _arrays_for(self, side: Side) -> tuple[list[OrderedDict[str, RestingOrder]], list[int]]:
        if side is Side.BUY:
            return self._bid_levels, self._bid_qty
        return self._ask_levels, self._ask_qty

    def _advance_best_past(self, side: Side, emptied_price: int) -> None:
        """Rescan for the new best price after ``emptied_price`` hit zero.

        A no-op unless the emptied level *was* the cached best -- an inner-level
        cancel or reduce-to-zero must not touch the cache at all.
        """
        if side is Side.BUY:
            if emptied_price != self._best_bid:
                return
            price = emptied_price - 1
            while price >= 1 and self._bid_qty[price] == 0:
                price -= 1
            self._best_bid = price if price >= 1 else 0
        else:
            if emptied_price != self._best_ask:
                return
            price = emptied_price + 1
            while price <= self._max_tick - 1 and self._ask_qty[price] == 0:
                price += 1
            self._best_ask = price if price <= self._max_tick - 1 else self._max_tick
