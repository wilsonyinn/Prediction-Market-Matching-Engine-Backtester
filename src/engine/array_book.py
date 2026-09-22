"""``ArrayBook``: a bounded-price array implementation.

Levels are indexed directly by price tick, in two lists of length ``max_tick + 1``
(one per side) so that indices ``0`` and ``max_tick`` are always in-bounds and always
empty -- they act as sentinels for "no bid" / "no ask" and remove the need for a
bounds check in the match loop's inner scan. All level ``OrderedDict``s are
pre-allocated at construction and never destroyed, so a level emptying or filling
never allocates or frees a level object; only the O(1)-amortized dict operations on
it change.

Bid and ask levels are kept in genuinely separate arrays rather than one array shared
by price. Sharing one would still be *correct* -- the matching engine's no-cross
invariant guarantees a bid and an ask can never simultaneously rest at the same price,
since either arriving order would trade against the other instead of resting -- but
relying on that invariant for the storage layer's safety is exactly the kind of
cleverness this project prefers to avoid; two arrays make the design obviously
correct without a proof.

This file intentionally stays close to the obvious implementation. The two
performance choices here -- pre-allocated levels and O(1) FIFO via ``OrderedDict`` --
are structural, not tuning; the intrusive-doubly-linked-list optimization that would
remove ``front``'s iterator allocation is deliberately deferred to Phase 2, where it
can be profiled, swapped in behind this unchanged protocol, and reported with a
measured before/after number.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from engine.types import Side

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.types import RestingOrder


class ArrayBook:
    """Bounded-price-array ``OrderBook`` implementation.

    Add, cancel, and best-price lookup are O(1). A level emptying or filling
    triggers a best-price rescan bounded by the distance to the next non-empty
    level, not by book size.
    """

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
        # Per-side arrays selected inline, not via a shared _arrays_for helper: a
        # Python call plus the 2-tuple it returns cost real nanoseconds on every
        # book op (measured -- see docs/DESIGN.md's Phase 2 optimization log). The
        # four-line if/else duplicated per method is the readability price of that.
        if order.side is Side.BUY:
            levels, qty = self._bid_levels, self._bid_qty
        else:
            levels, qty = self._ask_levels, self._ask_qty
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
        if order.side is Side.BUY:
            levels, qty = self._bid_levels, self._bid_qty
        else:
            levels, qty = self._ask_levels, self._ask_qty
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
        levels = self._bid_levels if side is Side.BUY else self._ask_levels
        return next(iter(levels[price].values()), None)

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        qty = self._bid_qty if order.side is Side.BUY else self._ask_qty
        order.remaining -= quantity
        qty[order.price] -= quantity
        if qty[order.price] == 0:
            self._advance_best_past(order.side, order.price)

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        if side is Side.BUY:
            levels, qty = self._bid_levels, self._bid_qty
        else:
            levels, qty = self._ask_levels, self._ask_qty
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
