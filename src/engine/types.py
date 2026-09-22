"""Core types: enums, orders, events, and reject reasons.

Prices are integer ticks and quantities are positive integers in base units; no float
ever appears in this module or anywhere else under ``src/engine``. Converting from the
exchange's decimal sizes happens only at the data-ingestion boundary (Phase 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(Enum):
    """Which side of the book an order rests on or crosses into."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        """The side an order on this side crosses against."""
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(Enum):
    """Time-in-force / fill semantics for an incoming order."""

    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class OrderState(Enum):
    """Lifecycle state of an *accepted* order.

    Rejected orders never get an entry here at all -- a rejection is not a state
    change, and the order's id remains available for reuse. Every other id, once
    accepted, is tracked here permanently: this is what lets ``cancel`` distinguish
    an unknown id from one that already reached a terminal state.
    """

    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    KILLED = "KILLED"


class RejectReason(Enum):
    """Why ``MatchingEngine.submit`` rejected an order.

    Checked in this exact order by the engine; the first failure wins. Structural
    validity (side, type, price, quantity, GTD fields) is checked before identity
    (duplicate order_id), so a malformed-and-duplicate order is reported as
    malformed.
    """

    TIMESTAMP_REGRESSION = "TIMESTAMP_REGRESSION"
    UNKNOWN_SIDE = "UNKNOWN_SIDE"
    UNKNOWN_ORDER_TYPE = "UNKNOWN_ORDER_TYPE"
    PRICE_NOT_INTEGER = "PRICE_NOT_INTEGER"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
    QUANTITY_NOT_POSITIVE = "QUANTITY_NOT_POSITIVE"
    EXPIRY_ON_NON_GTD = "EXPIRY_ON_NON_GTD"
    MISSING_EXPIRY = "MISSING_EXPIRY"
    EXPIRY_IN_PAST = "EXPIRY_IN_PAST"
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"


class CancelRejectReason(Enum):
    """Why ``MatchingEngine.cancel`` rejected a cancel request."""

    TIMESTAMP_REGRESSION = "TIMESTAMP_REGRESSION"
    UNKNOWN_ORDER_ID = "UNKNOWN_ORDER_ID"
    ALREADY_FINISHED = "ALREADY_FINISHED"


@dataclass(frozen=True, slots=True)
class Order:
    """An inbound order submitted to the engine.

    Immutable and performs no validation of its own: a malformed order must still
    construct so that the engine can reject it with an ``OrderRejected`` *event*
    rather than an exception. ``timestamp`` is placed ahead of ``expires_at`` (a
    deviation from the spec's prose field order) only because a defaulted field must
    come after all non-defaulted ones.
    """

    order_id: str
    side: Side
    price: int
    quantity: int
    order_type: OrderType
    timestamp: int
    expires_at: int | None = None
    owner_id: str | None = None


@dataclass(slots=True)
class RestingOrder:
    """A book-resident order.

    Distinct from ``Order`` because a frozen dataclass cannot shrink as it fills,
    because it carries ``entry_seq`` (an engine-assigned arrival ordinal that is
    engine state, not client input, and that both book implementations must use
    identically for FIFO and expiry tie-breaks), and because keeping ``Order``
    immutable means a caller holding a reference can never observe or corrupt live
    engine state.

    Not frozen: a frozen dataclass's ``__setattr__`` goes through
    ``object.__setattr__``, which would cost a function call on every partial fill.
    """

    order_id: str
    side: Side
    price: int
    original_quantity: int
    remaining: int
    order_type: OrderType
    timestamp: int
    expires_at: int | None
    owner_id: str | None
    entry_seq: int

    @classmethod
    def from_order(cls, order: Order, entry_seq: int) -> RestingOrder:
        """Build the book-resident form of a freshly accepted order.

        Positional, not keyword, arguments: measured ~130ns faster per call (see
        docs/DESIGN.md's Phase 2 optimization log) since this runs on every order
        that rests, which is the common case. Field order below must track
        RestingOrder's declaration order exactly -- a reader checking one against
        the other is the price of this optimization, spelled out here.
        """
        return cls(
            order.order_id,
            order.side,
            order.price,
            order.quantity,
            order.quantity,
            order.order_type,
            order.timestamp,
            order.expires_at,
            order.owner_id,
            entry_seq,
        )


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every engine output event.

    ``seq`` is a global, per-event counter -- not per input command. This is the
    only reading that satisfies both of the spec's requirements ("every input event
    gets a monotonically increasing sequence number" and "every event carries
    seq"): a per-command seq would not uniquely identify an event and would make
    event-stream equality rest entirely on list position. Command boundaries remain
    recoverable because every command's non-expiry events begin with exactly one of
    OrderAccepted / OrderRejected / OrderCanceled / CancelRejected.
    """

    seq: int
    timestamp: int


@dataclass(frozen=True, slots=True)
class OrderAccepted(Event):
    """The order passed validation. Emitted even for a FOK/FAK later killed."""

    order_id: str


@dataclass(frozen=True, slots=True)
class OrderRejected(Event):
    """The order failed validation; no state change occurred."""

    order_id: str
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class Trade(Event):
    """A single match between a resting maker and the incoming taker.

    Execution always happens at the maker's price.
    """

    trade_id: int
    maker_order_id: str
    taker_order_id: str
    price: int
    quantity: int
    taker_side: Side


@dataclass(frozen=True, slots=True)
class OrderRested(Event):
    """The order (or its remainder) now sits on the book unmatched."""

    order_id: str
    remaining: int


@dataclass(frozen=True, slots=True)
class OrderFilled(Event):
    """The order is completely filled and no longer live.

    Distinct from ``Trade``: a ``Trade`` is the two-sided economic event, this is
    the one-sided lifecycle event marking an order_id as terminal. A maker that is
    only partially consumed by a trade does not get one of these.
    """

    order_id: str
    filled_quantity: int


@dataclass(frozen=True, slots=True)
class OrderCanceled(Event):
    """A resting order (or its remainder) was canceled."""

    order_id: str
    canceled_quantity: int


@dataclass(frozen=True, slots=True)
class OrderExpired(Event):
    """A GTD order was removed by the expiry sweep.

    ``timestamp`` is the command timestamp that triggered the sweep, not the
    order's ``expires_at`` -- the event happened at the former, not the latter.
    """

    order_id: str
    expired_quantity: int


@dataclass(frozen=True, slots=True)
class OrderKilled(Event):
    """The unfilled remainder of a FOK/FAK order was killed rather than rested.

    Named ``killed_quantity`` (not the spec's ``killed_qty``) for consistency with
    the other terminal events' ``*_quantity`` fields.
    """

    order_id: str
    killed_quantity: int


@dataclass(frozen=True, slots=True)
class CancelRejected(Event):
    """A cancel request could not be applied."""

    order_id: str
    reason: CancelRejectReason


EngineEvent = (
    OrderAccepted
    | OrderRejected
    | Trade
    | OrderRested
    | OrderFilled
    | OrderCanceled
    | OrderExpired
    | OrderKilled
    | CancelRejected
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One aggregated price level."""

    price: int
    total_quantity: int
    order_count: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """A point-in-time view of both sides of the book, best price first."""

    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
