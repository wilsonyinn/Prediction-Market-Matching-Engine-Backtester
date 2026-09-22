"""Differential test: NaiveBook and ArrayBook must behave identically.

This is the strongest single correctness signal for ``ArrayBook`` -- ``NaiveBook`` is
the obviously-correct reference. It drives both engines through the same commands in
lockstep, one command at a time, and asserts identical events *and* identical
``snapshot()`` / ``best_bid()`` / ``best_ask()`` after each one. Checking book state
too (not just the event stream) catches internal ordering drift at the exact command
where it happened, rather than an arbitrary number of events later. Lockstep
execution (rather than running each engine through the whole command list separately
and comparing at the end, which ``tests.property.strategies.run_commands`` would make
easy) is what makes that localization possible, so this test steps the two engines
itself instead of reusing that helper's internal loop.

The example count is derived from the active Hypothesis profile rather than
hard-coded: a fixed ``max_examples=2000`` would silently override the fast ``dev``
profile and make every local run slow. Scaling relative to whichever profile is
loaded keeps local runs quick while still letting CI's ``ci`` profile push this test
into the thousands of examples the spec asks for.
"""

from __future__ import annotations

import os

from hypothesis import given, settings

from engine.engine import MatchingEngine
from engine.types import Order, OrderType
from tests.conftest import BOOK_FACTORIES
from tests.property.strategies import CancelCmd, SubmitCmd, command_lists

_active_profile = settings.get_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
# Differential testing is the highest-value correctness check here, so it gets a
# multiplier over whatever the active profile otherwise provides.
DIFF_EXAMPLES = _active_profile.max_examples * 2


@settings(max_examples=DIFF_EXAMPLES, deadline=None)
@given(commands=command_lists)
def test_naive_and_array_book_agree(commands) -> None:
    naive = MatchingEngine(book=BOOK_FACTORIES["naive"]())
    array = MatchingEngine(book=BOOK_FACTORIES["array"]())

    clock = 0
    submitted_ids: list[str] = []
    for i, cmd in enumerate(commands):
        clock += cmd.delta

        if isinstance(cmd, SubmitCmd):
            expires_at = clock + cmd.expiry_delta if cmd.order_type is OrderType.GTD else None
            order = Order(
                order_id=f"o{i}",
                side=cmd.side,
                price=cmd.price,
                quantity=cmd.quantity,
                order_type=cmd.order_type,
                timestamp=clock,
                expires_at=expires_at,
            )
            submitted_ids.append(order.order_id)
            events_naive = naive.submit(order)
            events_array = array.submit(order)
        elif isinstance(cmd, CancelCmd):
            target_id = (
                submitted_ids[cmd.target % len(submitted_ids)]
                if submitted_ids
                else "never-submitted"
            )
            events_naive = naive.cancel(target_id, timestamp=clock)
            events_array = array.cancel(target_id, timestamp=clock)
        else:
            events_naive = naive.advance_time(clock)
            events_array = array.advance_time(clock)

        assert events_naive == events_array, (
            f"command #{i} ({cmd}) diverged: naive={events_naive} array={events_array}"
        )
        assert naive.snapshot() == array.snapshot(), f"snapshot diverged after command #{i}"
        assert naive.best_bid() == array.best_bid(), f"best_bid diverged after command #{i}"
        assert naive.best_ask() == array.best_ask(), f"best_ask diverged after command #{i}"
