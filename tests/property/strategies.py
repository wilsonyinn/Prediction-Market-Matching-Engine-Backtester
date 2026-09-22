"""Hypothesis strategies and the shared command-list runner.

A command list is a first-class value: the same list can be replayed into a fresh
engine (determinism), fed to two engines side by side (the differential test), and
printed verbatim as a Hypothesis failure repro. This is why property/differential
tests generate a plain ``list[Command]`` via ``@given`` rather than driving a
``RuleBasedStateMachine`` -- a state machine interleaves generation with execution
and leaves no such list to hand to a second engine.

Strategy design is what decides whether these tests find anything:

* Prices are drawn mostly from a narrow band around one point, with a low-weight
  full-range tier for the 1 / max_tick-1 boundaries. Uniform random prices over the
  whole range almost never cross, degenerating the suite into testing ``add``.
* Timestamps are non-negative deltas, prefix-summed into absolutes by the runner and
  biased toward 0. This guarantees every command is *accepted* (so matching actually
  runs) and produces many same-timestamp events. Drawing absolute timestamps would
  make most commands TIMESTAMP_REGRESSION rejections; that path has its own unit
  tests instead.
* order_id is assigned by the runner from the command's list index, not drawn --
  drawing ids would burn the search budget on duplicate-id rejections, which also
  has its own dedicated unit test.
* CancelCmd.target is a large int resolved modulo the number of submits issued so
  far, so most cancels hit a real resting-or-finished order and shrinking toward 0
  targets the earliest (most likely still-resting) one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import strategies as st

from engine.types import Order, OrderType, Side

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.engine import MatchingEngine
    from engine.types import EngineEvent

MAX_TICK = 100


@dataclass(frozen=True, slots=True)
class SubmitCmd:
    delta: int
    side: Side
    price: int
    quantity: int
    order_type: OrderType
    expiry_delta: int


@dataclass(frozen=True, slots=True)
class CancelCmd:
    delta: int
    target: int


@dataclass(frozen=True, slots=True)
class AdvanceCmd:
    delta: int


Command = SubmitCmd | CancelCmd | AdvanceCmd

# Non-negative, biased toward 0 -- guarantees every command lands at or after the
# clock, so it is accepted rather than rejected for TIMESTAMP_REGRESSION.
_delta_strategy = st.one_of(st.just(0), st.just(0), st.integers(min_value=1, max_value=5))

# Mostly a narrow band so orders actually cross each other; occasionally the full
# valid range so the 1 / max_tick - 1 boundaries get exercised too.
_price_strategy = st.one_of(
    st.integers(min_value=45, max_value=55),
    st.integers(min_value=45, max_value=55),
    st.integers(min_value=45, max_value=55),
    st.integers(min_value=1, max_value=MAX_TICK - 1),
)

_quantity_strategy = st.one_of(
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=1, max_value=50),
)

# GTC 50%, GTD 20%, FAK 15%, FOK 15% -- FOK/FAK oversampled relative to a uniform
# 25% each, since that is where bug density is highest.
_order_type_strategy = st.sampled_from(
    [OrderType.GTC] * 10 + [OrderType.GTD] * 4 + [OrderType.FAK] * 3 + [OrderType.FOK] * 3
)

_submit_strategy = st.builds(
    SubmitCmd,
    delta=_delta_strategy,
    side=st.sampled_from([Side.BUY, Side.SELL]),
    price=_price_strategy,
    quantity=_quantity_strategy,
    order_type=_order_type_strategy,
    expiry_delta=st.integers(min_value=1, max_value=20),
)

_cancel_strategy = st.builds(
    CancelCmd,
    delta=_delta_strategy,
    target=st.integers(min_value=0, max_value=2**16),
)

_advance_strategy = st.builds(AdvanceCmd, delta=_delta_strategy)

# Weighted 65% submit / 25% cancel / 10% advance via duplication.
_command_strategy = st.one_of(
    *([_submit_strategy] * 13 + [_cancel_strategy] * 5 + [_advance_strategy] * 2)
)

command_lists = st.lists(_command_strategy, min_size=1, max_size=40)


def run_commands(
    engine: MatchingEngine,
    commands: list[Command],
    on_step: Callable[[list[EngineEvent], Order | None], None] | None = None,
) -> list[EngineEvent]:
    """Replay ``commands`` into ``engine``, returning the full concatenated stream.

    If given, ``on_step`` is called after every command with that command's events
    and (for a submit) the ``Order`` that was submitted -- ``None`` for a cancel or
    an advance. This lets a caller check invariants after each command (so a
    failure localizes to the offending command) without duplicating the id
    assignment and clock bookkeeping done here.
    """
    events: list[EngineEvent] = []
    clock = 0
    submitted_ids: list[str] = []
    for i, cmd in enumerate(commands):
        clock += cmd.delta
        if isinstance(cmd, SubmitCmd):
            order_id = f"o{i}"
            expires_at = clock + cmd.expiry_delta if cmd.order_type is OrderType.GTD else None
            order = Order(
                order_id=order_id,
                side=cmd.side,
                price=cmd.price,
                quantity=cmd.quantity,
                order_type=cmd.order_type,
                timestamp=clock,
                expires_at=expires_at,
            )
            step_events: list[EngineEvent] = list(engine.submit(order))
            submitted_ids.append(order_id)
            submitted_order = order
        elif isinstance(cmd, CancelCmd):
            target_id = (
                submitted_ids[cmd.target % len(submitted_ids)]
                if submitted_ids
                else "never-submitted"
            )
            step_events = list(engine.cancel(target_id, timestamp=clock))
            submitted_order = None
        else:
            step_events = list(engine.advance_time(clock))
            submitted_order = None
        events.extend(step_events)
        if on_step is not None:
            on_step(step_events, submitted_order)
    return events
