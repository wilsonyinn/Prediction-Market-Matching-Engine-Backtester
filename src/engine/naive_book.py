"""``NaiveBook``: the O(n) reference implementation.

One flat list per side. Best price and FIFO order are found by linear scan. Keep this
permanently as the reference implementation -- never delete it, and never give it an
id index or any other optimization. It exists to be *obviously* correct, not cleverly
correct, so that ``ArrayBook`` has something honest to be checked against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.types import RestingOrder, Side

if TYPE_CHECKING:
    from collections.abc import Iterator


class NaiveBook:
    """Reference ``OrderBook`` implementation. O(n) on every operation."""

    def __init__(self, max_tick: int) -> None:
        self._max_tick = max_tick
        self._bids: list[RestingOrder] = []
        self._asks: list[RestingOrder] = []

    @property
    def max_tick(self) -> int:
        return self._max_tick

    def _side_list(self, side: Side) -> list[RestingOrder]:
        return self._bids if side is Side.BUY else self._asks

    def add(self, order: RestingOrder) -> None:
        self._side_list(order.side).append(order)

    def remove(self, order_id: str) -> RestingOrder | None:
        for orders in (self._bids, self._asks):
            for i, order in enumerate(orders):
                if order.order_id == order_id:
                    orders.pop(i)
                    return order
        return None

    def get(self, order_id: str) -> RestingOrder | None:
        for orders in (self._bids, self._asks):
            for order in orders:
                if order.order_id == order_id:
                    return order
        return None

    def best(self, side: Side) -> int | None:
        orders = self._side_list(side)
        if not orders:
            return None
        if side is Side.BUY:
            return max(order.price for order in orders)
        return min(order.price for order in orders)

    def front(self, side: Side, price: int) -> RestingOrder | None:
        candidates = [o for o in self._side_list(side) if o.price == price]
        if not candidates:
            return None
        return min(candidates, key=lambda o: o.entry_seq)

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        order.remaining -= quantity

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        aggregates: dict[int, list[int]] = {}
        for order in self._side_list(side):
            agg = aggregates.setdefault(order.price, [0, 0])
            agg[0] += order.remaining
            agg[1] += 1
        prices = sorted(aggregates, reverse=(side is Side.BUY))
        for price in prices:
            qty, count = aggregates[price]
            yield price, qty, count

    def __len__(self) -> int:
        return len(self._bids) + len(self._asks)
