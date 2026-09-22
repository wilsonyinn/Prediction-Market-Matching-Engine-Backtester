"""``ArrayBook``: a bounded-price array implementation.

Levels are indexed directly by price tick, in two lists of length ``max_tick + 1``
(one per side) so that indices ``0`` and ``max_tick`` are always in-bounds and always
empty -- they act as sentinels for "no bid" / "no ask" and remove the need for a
bounds check in the match loop's inner scan. All per-level state is pre-allocated at
construction and never destroyed, so a level emptying or filling never allocates or
frees anything; only a handful of pointers and counters change.

Bid and ask levels are kept in genuinely separate arrays rather than one array shared
by price. Sharing one would still be *correct* -- the matching engine's no-cross
invariant guarantees a bid and an ask can never simultaneously rest at the same price,
since either arriving order would trade against the other instead of resting -- but
relying on that invariant for the storage layer's safety is exactly the kind of
cleverness this project prefers to avoid; two arrays make the design obviously
correct without a proof.

**FIFO per level is an intrusive doubly-linked list**, not an ``OrderedDict``. This
is the Phase 2 optimization Phase 1 deliberately deferred (see its module-docstring
note, and the "before ArrayBookBaseline" DESIGN.md entry): ``front()`` becomes one
array read (``head[price]``) instead of allocating an iterator over
``OrderedDict.values()`` and consuming one item from it. The links live directly on
``RestingOrder`` (``_dll_prev``/``_dll_next``, book-private scratch documented on
that type) rather than in a wrapper node, so resting an order costs no extra
allocation beyond the ``RestingOrder`` itself -- see ``docs/DESIGN.md``'s optimization
log for the measured before/after and the rejected alternatives (a wrapper node
class; a book-specific ``RestingOrder`` subclass).
"""

from __future__ import annotations

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
        width = max_tick + 1
        self._bid_head: list[RestingOrder | None] = [None] * width
        self._bid_tail: list[RestingOrder | None] = [None] * width
        self._bid_count: list[int] = [0] * width
        self._ask_head: list[RestingOrder | None] = [None] * width
        self._ask_tail: list[RestingOrder | None] = [None] * width
        self._ask_count: list[int] = [0] * width
        self._bid_qty: list[int] = [0] * width
        self._ask_qty: list[int] = [0] * width
        self._index: dict[str, RestingOrder] = {}
        self._best_bid: int = 0
        self._best_ask: int = max_tick

    @property
    def max_tick(self) -> int:
        return self._max_tick

    def add(self, order: RestingOrder) -> None:
        # Per-side arrays selected inline, not via a shared helper: a Python call
        # plus the tuple it returns cost real nanoseconds on every book op
        # (measured -- see docs/DESIGN.md's Phase 2 optimization log).
        if order.side is Side.BUY:
            head, tail, count, qty = self._bid_head, self._bid_tail, self._bid_count, self._bid_qty
        else:
            head, tail, count, qty = self._ask_head, self._ask_tail, self._ask_count, self._ask_qty
        price = order.price
        prev_tail = tail[price]
        order._dll_prev = prev_tail
        order._dll_next = None
        if prev_tail is None:
            head[price] = order
        else:
            prev_tail._dll_next = order
        tail[price] = order
        count[price] += 1
        qty[price] += order.remaining
        self._index[order.order_id] = order
        if order.side is Side.BUY:
            self._best_bid = max(self._best_bid, price)
        else:
            self._best_ask = min(self._best_ask, price)

    def remove(self, order_id: str) -> RestingOrder | None:
        order = self._index.pop(order_id, None)
        if order is None:
            return None
        if order.side is Side.BUY:
            head, tail, count, qty = self._bid_head, self._bid_tail, self._bid_count, self._bid_qty
        else:
            head, tail, count, qty = self._ask_head, self._ask_tail, self._ask_count, self._ask_qty
        price = order.price
        prv, nxt = order._dll_prev, order._dll_next
        if prv is None:
            head[price] = nxt
        else:
            prv._dll_next = nxt
        if nxt is None:
            tail[price] = prv
        else:
            nxt._dll_prev = prv
        # Nulling the links is not optional: a removed order still pointing into
        # the list keeps off-book objects reachable and turns a future double-
        # remove into silent list corruption instead of a clean no-op/KeyError.
        order._dll_prev = None
        order._dll_next = None
        count[price] -= 1
        qty[price] -= order.remaining
        if qty[price] == 0:
            self._advance_best_past(order.side, price)
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
        head = self._bid_head if side is Side.BUY else self._ask_head
        return head[price]

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        qty = self._bid_qty if order.side is Side.BUY else self._ask_qty
        order.remaining -= quantity
        qty[order.price] -= quantity
        if qty[order.price] == 0:
            self._advance_best_past(order.side, order.price)

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        if side is Side.BUY:
            qty, count = self._bid_qty, self._bid_count
            price = self._best_bid
            while price >= 1:
                if qty[price] > 0:
                    yield price, qty[price], count[price]
                price -= 1
        else:
            qty, count = self._ask_qty, self._ask_count
            price = self._best_ask
            while price <= self._max_tick - 1:
                if qty[price] > 0:
                    yield price, qty[price], count[price]
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
