"""Folds an engine's event stream into a per-order conservation ledger.

Deliberately reads only the public event stream, never engine internals -- per
``docs/DESIGN.md``, conservation is checked against the engine's own published
contract, not against itself. "Original quantity" is derived from the stream too
(not from the generator's own knowledge of what it submitted): the first terminal or
resting event for an order fixes it as *fills-so-far + that event's quantity*, using
only ``Trade`` sums for fills-so-far, so this never trusts ``OrderFilled``'s own
``filled_quantity`` field for the derivation -- that field is instead cross-checked
against the independently-computed fill total.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.types import (
    OrderCanceled,
    OrderExpired,
    OrderFilled,
    OrderKilled,
    OrderRested,
    Trade,
)


@dataclass
class OrderLedger:
    """Accumulates conservation state for every order seen in a stream."""

    original: dict[str, int] = field(default_factory=dict)
    filled: dict[str, int] = field(default_factory=dict)
    terminal_quantity: dict[str, int] = field(default_factory=dict)
    is_terminal: dict[str, bool] = field(default_factory=dict)

    def apply(self, event: object) -> None:
        if isinstance(event, Trade):
            self.filled[event.maker_order_id] = (
                self.filled.get(event.maker_order_id, 0) + event.quantity
            )
            self.filled[event.taker_order_id] = (
                self.filled.get(event.taker_order_id, 0) + event.quantity
            )
        elif isinstance(event, OrderRested):
            self.original.setdefault(
                event.order_id, self.filled.get(event.order_id, 0) + event.remaining
            )
        elif isinstance(event, OrderFilled):
            filled_so_far = self.filled.get(event.order_id, 0)
            assert event.filled_quantity == filled_so_far, (
                f"OrderFilled.filled_quantity ({event.filled_quantity}) disagrees "
                f"with the summed Trade quantities so far ({filled_so_far}) for "
                f"{event.order_id}"
            )
            self.original.setdefault(event.order_id, filled_so_far)
            self.is_terminal[event.order_id] = True
        elif isinstance(event, OrderCanceled):
            self.original.setdefault(
                event.order_id, self.filled.get(event.order_id, 0) + event.canceled_quantity
            )
            self.terminal_quantity[event.order_id] = (
                self.terminal_quantity.get(event.order_id, 0) + event.canceled_quantity
            )
            self.is_terminal[event.order_id] = True
        elif isinstance(event, OrderExpired):
            self.original.setdefault(
                event.order_id, self.filled.get(event.order_id, 0) + event.expired_quantity
            )
            self.terminal_quantity[event.order_id] = (
                self.terminal_quantity.get(event.order_id, 0) + event.expired_quantity
            )
            self.is_terminal[event.order_id] = True
        elif isinstance(event, OrderKilled):
            self.original.setdefault(
                event.order_id, self.filled.get(event.order_id, 0) + event.killed_quantity
            )
            self.terminal_quantity[event.order_id] = (
                self.terminal_quantity.get(event.order_id, 0) + event.killed_quantity
            )
            self.is_terminal[event.order_id] = True

    def check(self) -> None:
        """Assert conservation for every order seen so far.

        ``filled + remaining + canceled + expired + killed == original`` holds by
        construction once expanded (remaining is never stored, only derived), so
        the substantive checks are: the accounted-for quantity never exceeds the
        original, and it exactly equals the original once the order is terminal
        (i.e. remaining has reached zero).
        """
        for order_id, original_qty in self.original.items():
            accounted = self.filled.get(order_id, 0) + self.terminal_quantity.get(order_id, 0)
            assert accounted <= original_qty, (
                f"overfill for {order_id}: accounted {accounted} > original {original_qty}"
            )
            if self.is_terminal.get(order_id, False):
                assert accounted == original_qty, (
                    f"conservation violated for {order_id}: accounted {accounted} "
                    f"!= original {original_qty}"
                )
