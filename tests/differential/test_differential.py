"""Differential test: every book implementation must behave identically to NaiveBook.

``NaiveBook`` is the obviously-correct reference; every other registered
implementation (``array``, ``array_baseline``, ``tree`` when installed) is driven
alongside it in lockstep, one command at a time, and checked for identical events
*and* identical ``snapshot()`` / ``best_bid()`` / ``best_ask()`` after each one.
Checking book state too (not just the event stream) catches internal ordering drift
at the exact command where it happened, rather than an arbitrary number of events
later. Lockstep execution (rather than running each engine through the whole command
list separately and comparing at the end, which
``tests.property.strategies.run_commands`` would make easy) is what makes that
localization possible, so this test steps every engine itself instead of reusing
that helper's internal loop.

The set of implementations compared is read from ``tests.conftest.BOOK_FACTORIES``
rather than hard-coded, so it automatically covers ``tree`` when the optional
``sortedcontainers`` extra is installed and simply omits it otherwise -- and so a
future fifth implementation is covered by construction, not by remembering to add it
here.

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
# multiplier over whatever the active profile otherwise provides. Runtime scales
# with the number of non-reference implementations (one extra engine driven per
# command per implementation), not with this multiplier alone -- if CI time becomes
# a problem, reduce this multiplier before dropping an implementation from the
# comparison.
DIFF_EXAMPLES = _active_profile.max_examples * 2

_REFERENCE_NAME = "naive"
_OTHER_NAMES = sorted(name for name in BOOK_FACTORIES if name != _REFERENCE_NAME)


@settings(max_examples=DIFF_EXAMPLES, deadline=None)
@given(commands=command_lists)
def test_every_book_agrees_with_naive(commands) -> None:
    reference = MatchingEngine(book=BOOK_FACTORIES[_REFERENCE_NAME]())
    others = {name: MatchingEngine(book=BOOK_FACTORIES[name]()) for name in _OTHER_NAMES}

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
            reference_events = reference.submit(order)
            other_events = {name: engine.submit(order) for name, engine in others.items()}
        elif isinstance(cmd, CancelCmd):
            target_id = (
                submitted_ids[cmd.target % len(submitted_ids)]
                if submitted_ids
                else "never-submitted"
            )
            reference_events = reference.cancel(target_id, timestamp=clock)
            other_events = {
                name: engine.cancel(target_id, timestamp=clock) for name, engine in others.items()
            }
        else:
            reference_events = reference.advance_time(clock)
            other_events = {name: engine.advance_time(clock) for name, engine in others.items()}

        for name, events in other_events.items():
            assert reference_events == events, (
                f"command #{i} ({cmd}) diverged: {_REFERENCE_NAME}={reference_events} "
                f"{name}={events}"
            )
            engine = others[name]
            assert reference.snapshot() == engine.snapshot(), (
                f"{name} snapshot diverged after command #{i}"
            )
            assert reference.best_bid() == engine.best_bid(), (
                f"{name} best_bid diverged after command #{i}"
            )
            assert reference.best_ask() == engine.best_ask(), (
                f"{name} best_ask diverged after command #{i}"
            )
