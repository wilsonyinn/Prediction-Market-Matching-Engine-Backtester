"""``TreeBook``: a sorted-map implementation, for general-exchange comparison.

Optional -- requires the ``sortedcontainers`` extra (``pip install -e ".[tree]"`` or
``".[dev]"``). Not imported from ``engine/__init__.py``, so the core package stays
importable with zero dependencies; a caller that wants ``TreeBook`` imports this
module directly and gets an ``ImportError`` with a clear cause if the extra isn't
installed.

Unlike ``ArrayBook``, price levels are **not pre-allocated**: a level is created on
first use and deleted the moment it empties. This is the load-bearing difference for
Phase 2's benchmarking: ``best()`` and the post-empty rescan are ``SortedDict`` key
lookups (O(log n) in the number of *occupied* levels), never a tick-by-tick scan --
so a sparse book at a fine tick size (many valid prices, few occupied) is exactly the
workload that should separate this implementation from ``ArrayBook``, whose bounded
rescan is worst-case O(max_tick) regardless of how sparse the book actually is.

Rejected alternative for tracking best price: a ``heapq`` of occupied prices. A heap
gives O(log n) best-price cheaply, but ``levels()`` -- and therefore ``snapshot()``
and the FOK dry run -- needs ordered iteration over *all* occupied levels, which a
heap does not provide without popping and rebuilding it. A sorted map gives both for
the same cost.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from sortedcontainers import SortedDict

from engine.types import Side

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.types import RestingOrder


class TreeBook:
    """Sorted-map ``OrderBook`` implementation using ``sortedcontainers.SortedDict``."""

    def __init__(self, max_tick: int) -> None:
        self._max_tick = max_tick
        self._bids: SortedDict[int, OrderedDict[str, RestingOrder]] = SortedDict()
        self._asks: SortedDict[int, OrderedDict[str, RestingOrder]] = SortedDict()
        self._index: dict[str, RestingOrder] = {}

    @property
    def max_tick(self) -> int:
        return self._max_tick

    def add(self, order: RestingOrder) -> None:
        levels = self._levels_for(order.side)
        level = levels.get(order.price)
        if level is None:
            level = OrderedDict()
            levels[order.price] = level
        level[order.order_id] = order
        self._index[order.order_id] = order

    def remove(self, order_id: str) -> RestingOrder | None:
        order = self._index.pop(order_id, None)
        if order is None:
            return None
        levels = self._levels_for(order.side)
        level = levels[order.price]
        del level[order_id]
        if not level:
            del levels[order.price]
        return order

    def get(self, order_id: str) -> RestingOrder | None:
        return self._index.get(order_id)

    def best(self, side: Side) -> int | None:
        levels = self._levels_for(side)
        if not levels:
            return None
        return levels.peekitem(-1 if side is Side.BUY else 0)[0]

    def front(self, side: Side, price: int) -> RestingOrder | None:
        if price < 0 or price > self._max_tick:
            return None
        level = self._levels_for(side).get(price)
        if level is None:
            return None
        return next(iter(level.values()), None)

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        order.remaining -= quantity

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        levels = self._levels_for(side)
        prices = reversed(levels) if side is Side.BUY else iter(levels)
        for price in prices:
            level = levels[price]
            total_qty = sum(o.remaining for o in level.values())
            yield price, total_qty, len(level)

    def __len__(self) -> int:
        return len(self._index)

    def _levels_for(self, side: Side) -> SortedDict[int, OrderedDict[str, RestingOrder]]:
        return self._bids if side is Side.BUY else self._asks
