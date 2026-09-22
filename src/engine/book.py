"""The ``OrderBook`` protocol.

A book is storage for resting orders. It knows about placement, FIFO priority, and
per-level aggregates -- it contains **no matching logic**. It never decides whether
two orders cross, never emits events, and never consults a clock. All of that lives
once, in ``MatchingEngine``, so that ``NaiveBook`` and ``ArrayBook`` can differ only in
data structure and still behave identically from the engine's point of view.

``typing.Protocol`` is used rather than an ABC specifically because an ABC invites a
shared concrete helper method to creep into the base class -- exactly the failure mode
this module exists to prevent. It is not ``@runtime_checkable``: a runtime check only
compares method names, which gives false confidence about semantics (e.g. what
``front`` returns for an empty level). Real conformance is exercised by
``tests/unit/test_book_conformance.py`` against both implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engine.types import RestingOrder, Side


class OrderBook(Protocol):
    """Storage protocol satisfied by every book implementation."""

    @property
    def max_tick(self) -> int:
        """Ticks per $1.00. Valid prices are ``1 <= price <= max_tick - 1``."""
        ...

    def add(self, order: RestingOrder) -> None:
        """Append ``order`` to the back of its price level's FIFO queue.

        Precondition: ``order.remaining > 0`` and ``order.order_id`` is not already
        resting in this book.
        """
        ...

    def remove(self, order_id: str) -> RestingOrder | None:
        """Detach and return the resting order, or ``None`` if it is not resting."""
        ...

    def get(self, order_id: str) -> RestingOrder | None:
        """Return the resting order without mutating the book, or ``None``."""
        ...

    def best(self, side: Side) -> int | None:
        """The best price on ``side``: highest bid or lowest ask. ``None`` if empty."""
        ...

    def front(self, side: Side, price: int) -> RestingOrder | None:
        """The FIFO-first order resting at ``(side, price)``.

        Returns ``None`` if that level is empty, including when ``price`` is
        outside ``1 .. max_tick - 1``. Never raises, so the match loop needs no
        bounds check before calling this.
        """
        ...

    def reduce(self, order: RestingOrder, quantity: int) -> None:
        """Decrease ``order.remaining`` by ``quantity`` and update level aggregates.

        Requires ``0 < quantity <= order.remaining``. Does **not** remove the order
        when ``remaining`` reaches zero -- callers that fully consume an order must
        call ``remove`` instead, since they need to observe that terminal
        transition anyway (to emit ``OrderFilled``).
        """
        ...

    def levels(self, side: Side) -> Iterator[tuple[int, int, int]]:
        """Yield ``(price, total_quantity, order_count)`` for non-empty levels.

        Yielded best price first, lazily. The caller must not mutate the book
        while iterating.
        """
        ...

    def __len__(self) -> int:
        """Number of resting orders across both sides."""
        ...
