"""Property-based invariants, checked after every command in a random sequence.

Every test here builds a fresh ``MatchingEngine`` from ``book_factory`` *inside* the
test body -- see the warning on ``conftest.py``'s ``engine`` fixture for why a
Hypothesis test must never take that fixture directly.

Two invariants from the original design sketch ("no FOK/FAK id in snapshot()", "no id
in snapshot() after a terminal event") aren't implemented as literally stated: the
engine's public ``snapshot()`` returns only aggregated ``BookLevel``s (price, total
quantity, order count), matching the spec's documented API exactly, with no
per-order-id detail to check against. The checkable, equivalent-in-spirit invariants
used here instead are: a FOK/FAK order must never produce an ``OrderRested`` event
(directly witnesses "never rests"), and an order that has already reached a terminal
state must never appear in a later ``Trade`` (directly witnesses "no zombie
trading" -- the concern behind "no id after a terminal event").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given

from engine.engine import MatchingEngine
from engine.types import OrderKilled, OrderRested, OrderType, Trade
from tests.property.ledger import OrderLedger
from tests.property.strategies import command_lists, run_commands

if TYPE_CHECKING:
    from engine.types import EngineEvent, Order


@given(commands=command_lists)
def test_conservation_no_overfill(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())
    ledger = OrderLedger()

    def check(step_events: list[EngineEvent], _submitted: Order | None) -> None:
        for event in step_events:
            ledger.apply(event)
        ledger.check()

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_book_never_crossed(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())

    def check(_step_events: list[EngineEvent], _submitted: Order | None) -> None:
        bid, ask = engine.best_bid(), engine.best_ask()
        if bid is not None and ask is not None:
            assert bid < ask, f"book crossed: best_bid={bid} best_ask={ask}"

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_trade_prices_respect_both_limits(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())
    order_price: dict[str, int] = {}
    order_side = {}

    def check(step_events: list[EngineEvent], submitted: Order | None) -> None:
        if submitted is not None:
            order_price[submitted.order_id] = submitted.price
            order_side[submitted.order_id] = submitted.side
        for event in step_events:
            if not isinstance(event, Trade):
                continue
            assert event.price == order_price[event.maker_order_id], "not maker's price"
            taker_limit = order_price[event.taker_order_id]
            if event.taker_side.name == "BUY":
                assert event.price <= taker_limit
            else:
                assert event.price >= taker_limit

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_no_nonpositive_quantities(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())

    def check(step_events: list[EngineEvent], _submitted: Order | None) -> None:
        for event in step_events:
            if isinstance(event, Trade):
                assert event.quantity > 0
            elif isinstance(event, OrderKilled):
                assert event.killed_quantity > 0

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_fok_all_or_nothing(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())
    fok_ids: set[str] = set()
    filled_so_far: dict[str, int] = {}

    def check(step_events: list[EngineEvent], submitted: Order | None) -> None:
        if submitted is not None and submitted.order_type is OrderType.FOK:
            fok_ids.add(submitted.order_id)
        for event in step_events:
            if isinstance(event, Trade):
                filled_so_far[event.maker_order_id] = (
                    filled_so_far.get(event.maker_order_id, 0) + event.quantity
                )
                filled_so_far[event.taker_order_id] = (
                    filled_so_far.get(event.taker_order_id, 0) + event.quantity
                )
            elif isinstance(event, OrderKilled) and event.order_id in fok_ids:
                assert filled_so_far.get(event.order_id, 0) == 0, (
                    "a killed FOK order traded before being killed"
                )

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_fok_and_fak_never_rest(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())
    never_resting_types = {OrderType.FOK, OrderType.FAK}

    def check(step_events: list[EngineEvent], submitted: Order | None) -> None:
        for event in step_events:
            if isinstance(event, OrderRested) and submitted is not None:
                assert submitted.order_type not in never_resting_types

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_no_trading_after_terminal(book_factory, commands) -> None:
    engine = MatchingEngine(book=book_factory())
    ledger = OrderLedger()

    def check(step_events: list[EngineEvent], _submitted: Order | None) -> None:
        for event in step_events:
            if isinstance(event, Trade):
                assert not ledger.is_terminal.get(event.maker_order_id, False), (
                    f"{event.maker_order_id} traded after reaching a terminal state"
                )
                assert not ledger.is_terminal.get(event.taker_order_id, False), (
                    f"{event.taker_order_id} traded after reaching a terminal state"
                )
            ledger.apply(event)

    run_commands(engine, commands, on_step=check)


@given(commands=command_lists)
def test_determinism_replay_is_identical(book_factory, commands) -> None:
    engine_a = MatchingEngine(book=book_factory())
    engine_b = MatchingEngine(book=book_factory())

    events_a = run_commands(engine_a, commands)
    events_b = run_commands(engine_b, commands)

    assert events_a == events_b
