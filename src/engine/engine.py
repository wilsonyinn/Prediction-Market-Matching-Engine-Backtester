"""``MatchingEngine``: validation, sequencing, clock, matching.

All matching logic lives here and nowhere else -- book implementations differ only in
data structure (see ``book.py``). The engine is single-threaded and strictly
sequential: every input event gets a monotonically increasing sequence number, and
time comes only from input timestamps, never the wall clock, which is what makes
replay deterministic.

Two decisions here resolve genuine ambiguities in the spec; both are recorded in
``docs/DESIGN.md`` with the rejected alternative:

* ``seq`` is assigned per emitted *event*, not per input command -- the only reading
  under which both "every input event gets a monotonically increasing sequence
  number" and "every event carries seq" hold simultaneously.
* Any command whose timestamp is ``>= clock`` advances the clock and runs the expiry
  sweep *before* validation, even if the command is subsequently rejected for an
  unrelated reason. Only a timestamp-regression rejection suppresses the advance.
  Time is environment, not order state, so it does not depend on whether the order
  that carried it turned out to be well-formed.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING

from engine.types import (
    BookLevel,
    BookSnapshot,
    CancelRejected,
    CancelRejectReason,
    Order,
    OrderAccepted,
    OrderCanceled,
    OrderExpired,
    OrderFilled,
    OrderKilled,
    OrderRejected,
    OrderRested,
    OrderState,
    OrderType,
    RejectReason,
    RestingOrder,
    Side,
    Trade,
)

if TYPE_CHECKING:
    from engine.book import OrderBook
    from engine.types import EngineEvent


class MatchingEngine:
    """Price-time-priority matching engine over an arbitrary ``OrderBook``."""

    def __init__(self, book: OrderBook) -> None:
        """Construct an engine over ``book``, reading ``max_tick`` from it.

        Taking only the book (per the spec's constructor example) structurally
        eliminates any possibility of the engine and book disagreeing about the
        valid price range.
        """
        self._book = book
        self._clock: int = 0
        self._next_seq: int = 1
        self._next_trade_id: int = 1
        self._next_entry_seq: int = 1
        # Every order_id that ever passed validation, forever -- this is both the
        # duplicate-id check and the CancelRejected discriminator ("never existed"
        # vs. "already gone"). Rejected orders never enter it.
        self._state: dict[str, OrderState] = {}
        # Min-heap of (expires_at, entry_seq, order_id) for resting GTD orders.
        # Lazily cleaned: a fill or cancel does not touch the heap, so an entry may
        # be stale by the time it is popped. entry_seq disambiguates in case an id
        # were ever reused (it cannot be, since ids are burned permanently, but the
        # check turns any future bug here into an assertion rather than a silently
        # wrong expiry).
        self._expiry: list[tuple[int, int, str]] = []
        self._stale_expiries: int = 0

    @property
    def clock(self) -> int:
        """The engine's current event-time clock, in integer milliseconds."""
        return self._clock

    def submit(self, order: Order) -> list[EngineEvent]:
        """Validate, match, and (if unfilled) rest an incoming order."""
        out: list[EngineEvent] = []
        if order.timestamp < self._clock:
            out.append(
                OrderRejected(
                    self._seq(), self._clock, order.order_id, RejectReason.TIMESTAMP_REGRESSION
                )
            )
            return out

        self._advance_and_sweep(order.timestamp, out)

        reason = self._validate(order)
        if reason is not None:
            out.append(OrderRejected(self._seq(), order.timestamp, order.order_id, reason))
            return out

        self._state[order.order_id] = OrderState.OPEN
        out.append(OrderAccepted(self._seq(), order.timestamp, order.order_id))

        if order.order_type is OrderType.FOK:
            self._submit_fok(order, out)
            return out

        remaining = self._match(order, order.quantity, out)

        if remaining == 0:
            self._state[order.order_id] = OrderState.FILLED
            out.append(OrderFilled(self._seq(), order.timestamp, order.order_id, order.quantity))
        elif order.order_type in (OrderType.GTC, OrderType.GTD):
            self._rest(order, remaining, out)
        else:  # FAK
            self._state[order.order_id] = OrderState.KILLED
            out.append(OrderKilled(self._seq(), order.timestamp, order.order_id, remaining))

        return out

    def cancel(self, order_id: str, timestamp: int) -> list[EngineEvent]:
        """Cancel a resting order, or reject the request with a reason."""
        out: list[EngineEvent] = []
        if timestamp < self._clock:
            out.append(
                CancelRejected(
                    self._seq(), self._clock, order_id, CancelRejectReason.TIMESTAMP_REGRESSION
                )
            )
            return out

        self._advance_and_sweep(timestamp, out)

        state = self._state.get(order_id)
        if state is None:
            out.append(
                CancelRejected(
                    self._seq(), timestamp, order_id, CancelRejectReason.UNKNOWN_ORDER_ID
                )
            )
            return out
        if state is not OrderState.OPEN:
            out.append(
                CancelRejected(
                    self._seq(), timestamp, order_id, CancelRejectReason.ALREADY_FINISHED
                )
            )
            return out

        order = self._book.remove(order_id)
        assert order is not None, "OPEN state implies the order is resting"
        self._state[order_id] = OrderState.CANCELED
        if order.order_type is OrderType.GTD:
            self._stale_expiries += 1
            self._maybe_compact_expiry()
        out.append(OrderCanceled(self._seq(), timestamp, order_id, order.remaining))
        return out

    def advance_time(self, timestamp: int) -> list[EngineEvent]:
        """Advance the clock to ``timestamp``, expiring any GTD orders now due."""
        if timestamp < self._clock:
            msg = f"advance_time cannot regress the clock: at {self._clock}, got {timestamp}"
            raise ValueError(msg)
        out: list[EngineEvent] = []
        self._advance_and_sweep(timestamp, out)
        return out

    def best_bid(self) -> int | None:
        """The current best (highest) bid price, or ``None`` if there is none."""
        return self._book.best(Side.BUY)

    def best_ask(self) -> int | None:
        """The current best (lowest) ask price, or ``None`` if there is none."""
        return self._book.best(Side.SELL)

    def snapshot(self) -> BookSnapshot:
        """A point-in-time view of both sides of the book, best price first."""
        bids = tuple(BookLevel(p, q, c) for p, q, c in self._book.levels(Side.BUY))
        asks = tuple(BookLevel(p, q, c) for p, q, c in self._book.levels(Side.SELL))
        return BookSnapshot(bids=bids, asks=asks)

    # -- internals ---------------------------------------------------------

    def _seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _advance_and_sweep(self, t: int, out: list[EngineEvent]) -> None:
        self._clock = t
        heap = self._expiry
        while heap and heap[0][0] <= t:
            _expires_at, entry_seq, order_id = heapq.heappop(heap)
            order = self._book.get(order_id)
            if order is None or order.entry_seq != entry_seq:
                self._stale_expiries -= 1
                continue
            self._book.remove(order_id)
            self._state[order_id] = OrderState.EXPIRED
            out.append(OrderExpired(self._seq(), t, order_id, order.remaining))

    def _maybe_compact_expiry(self) -> None:
        """Rebuild the expiry heap once stale entries dominate it.

        Fills and cancels leave a GTD order's heap entry in place (lazy deletion),
        so a workload that rests and then cancels many GTD orders would otherwise
        grow heap memory without bound. Compacting keeps it O(live GTD orders).
        """
        if self._stale_expiries <= len(self._expiry) // 2:
            return
        fresh = [
            (expires_at, entry_seq, order_id)
            for expires_at, entry_seq, order_id in self._expiry
            if (order := self._book.get(order_id)) is not None and order.entry_seq == entry_seq
        ]
        heapq.heapify(fresh)
        self._expiry = fresh
        self._stale_expiries = 0

    def _validate(self, order: Order) -> RejectReason | None:
        # order's fields are statically typed, so a type-checked caller can never
        # trigger the isinstance/type checks below; they exist because the engine
        # must still reject a runtime-malformed order (untyped input, e.g. from
        # deserialized data in a later phase) with an event rather than a crash.
        # mypy considers them unreachable/redundant given the static types, hence
        # the targeted ignores.
        if not isinstance(order.side, Side):
            return RejectReason.UNKNOWN_SIDE  # type: ignore[unreachable]
        if not isinstance(order.order_type, OrderType):
            return RejectReason.UNKNOWN_ORDER_TYPE  # type: ignore[unreachable]
        if not isinstance(order.price, int) or isinstance(  # type: ignore[redundant-expr]
            order.price, bool
        ):
            return RejectReason.PRICE_NOT_INTEGER
        max_price = self._book.max_tick - 1
        if not (1 <= order.price <= max_price):
            return RejectReason.PRICE_OUT_OF_RANGE
        if (
            not isinstance(order.quantity, int)  # type: ignore[redundant-expr]
            or isinstance(order.quantity, bool)
            or order.quantity <= 0
        ):
            return RejectReason.QUANTITY_NOT_POSITIVE
        if order.order_type is not OrderType.GTD and order.expires_at is not None:
            return RejectReason.EXPIRY_ON_NON_GTD
        if order.order_type is OrderType.GTD:
            if order.expires_at is None:
                return RejectReason.MISSING_EXPIRY
            if order.expires_at <= self._clock:
                return RejectReason.EXPIRY_IN_PAST
        if order.order_id in self._state:
            return RejectReason.DUPLICATE_ORDER_ID
        return None

    @staticmethod
    def _crosses(taker_side: Side, limit: int, best_price: int) -> bool:
        if taker_side is Side.BUY:
            return limit >= best_price
        return limit <= best_price

    def _crossing_quantity(self, taker_side: Side, limit: int, needed: int) -> int:
        """Quantity available to a taker at ``limit``, capped at ``needed``.

        Used only for the FOK dry run. Reads solely from ``levels()`` aggregates,
        so it never depends on a book's intra-level storage, and it breaks the
        moment a level stops crossing or enough quantity has been found -- so it
        never visits a level the execution pass wouldn't.
        """
        available = 0
        for price, level_qty, _count in self._book.levels(taker_side.opposite):
            if not self._crosses(taker_side, limit, price):
                break
            available += level_qty
            if available >= needed:
                return needed
        return available

    def _submit_fok(self, order: Order, out: list[EngineEvent]) -> None:
        available = self._crossing_quantity(order.side, order.price, order.quantity)
        if available < order.quantity:
            self._state[order.order_id] = OrderState.KILLED
            out.append(OrderKilled(self._seq(), order.timestamp, order.order_id, order.quantity))
            return
        remaining = self._match(order, order.quantity, out)
        assert remaining == 0, "dry run guaranteed enough crossing liquidity"
        self._state[order.order_id] = OrderState.FILLED
        out.append(OrderFilled(self._seq(), order.timestamp, order.order_id, order.quantity))

    def _rest(self, order: Order, remaining: int, out: list[EngineEvent]) -> None:
        entry_seq = self._next_entry_seq
        self._next_entry_seq += 1
        resting = RestingOrder.from_order(order, entry_seq)
        resting.remaining = remaining
        self._book.add(resting)
        if order.order_type is OrderType.GTD:
            assert order.expires_at is not None  # enforced by _validate
            heapq.heappush(self._expiry, (order.expires_at, entry_seq, order.order_id))
        out.append(OrderRested(self._seq(), order.timestamp, order.order_id, remaining))

    def _match(self, order: Order, needed: int, out: list[EngineEvent]) -> int:
        """Sweep resting liquidity on the opposite side, emitting Trade/OrderFilled.

        Returns the taker quantity still unfilled (0 if fully matched).
        """
        remaining = needed
        opp_side = order.side.opposite
        while remaining > 0:
            best_price = self._book.best(opp_side)
            if best_price is None or not self._crosses(order.side, order.price, best_price):
                break
            maker = self._book.front(opp_side, best_price)
            assert maker is not None, "a non-empty best level has a front order"

            trade_qty = min(remaining, maker.remaining)
            maker_order_id = maker.order_id
            maker_original_qty = maker.original_quantity
            maker_order_type = maker.order_type
            fully_filled = trade_qty == maker.remaining

            if fully_filled:
                self._book.remove(maker_order_id)
            else:
                self._book.reduce(maker, trade_qty)
            remaining -= trade_qty

            trade_id = self._next_trade_id
            self._next_trade_id += 1
            out.append(
                Trade(
                    self._seq(),
                    order.timestamp,
                    trade_id,
                    maker_order_id,
                    order.order_id,
                    best_price,
                    trade_qty,
                    order.side,
                )
            )

            if fully_filled:
                self._state[maker_order_id] = OrderState.FILLED
                if maker_order_type is OrderType.GTD:
                    self._stale_expiries += 1
                    self._maybe_compact_expiry()
                out.append(
                    OrderFilled(self._seq(), order.timestamp, maker_order_id, maker_original_qty)
                )

        return remaining
