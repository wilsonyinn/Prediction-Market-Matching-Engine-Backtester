"""Shared fixtures and Hypothesis profiles for the whole test suite.

The entire suite runs against both book implementations via the parametrized
``book_factory`` / ``engine`` fixtures, so a new behavior only needs to be written
once. See ``tests/property/conftest.py`` note below for why Hypothesis tests must NOT
use the ``engine`` fixture directly.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, settings

from engine.array_book import ArrayBook
from engine.array_book_baseline import ArrayBookBaseline
from engine.book import OrderBook
from engine.engine import MatchingEngine
from engine.naive_book import NaiveBook

BOOK_FACTORIES: dict[str, Callable[..., OrderBook]] = {
    "naive": lambda max_tick=100: NaiveBook(max_tick=max_tick),
    "array": lambda max_tick=100: ArrayBook(max_tick=max_tick),
    "array_baseline": lambda max_tick=100: ArrayBookBaseline(max_tick=max_tick),
}

try:
    from engine.tree_book import TreeBook
except ImportError:
    pass
else:
    BOOK_FACTORIES["tree"] = lambda max_tick=100: TreeBook(max_tick=max_tick)


@pytest.fixture(params=sorted(BOOK_FACTORIES), ids=sorted(BOOK_FACTORIES))
def book_factory(request: pytest.FixtureRequest) -> Callable[..., OrderBook]:
    """A zero/one-arg factory (``max_tick``) for one of the book implementations.

    Hypothesis-based tests must use this fixture and build their engine inside the
    test body, per example -- see the ``dev``/``ci`` profile note below.
    """
    factory: Callable[..., OrderBook] = BOOK_FACTORIES[request.param]
    return factory


@pytest.fixture
def engine(book_factory: Callable[..., OrderBook]) -> MatchingEngine:
    """A fresh engine over a fresh book. Convenience fixture for plain unit tests.

    Do NOT use this fixture inside a ``@given``-decorated test: pytest creates a
    function-scoped fixture once per test *function*, not once per Hypothesis
    example, so a shared engine here would leak state across hundreds of examples
    and produce failures that don't reproduce when Hypothesis replays the minimal
    case. Property tests take ``book_factory`` and construct
    ``MatchingEngine(book=book_factory())`` inside the test body instead.
    """
    return MatchingEngine(book=book_factory())


# function_scoped_fixture is suppressed globally: every Hypothesis test here uses
# `book_factory` (never the pre-built `engine` fixture) and calls it fresh inside
# the test body for each example, which is exactly the safe pattern the health
# check can't distinguish from the unsafe one -- Hypothesis has no way to know the
# fixture value itself is never reused across examples, only invoked as a factory.
_common_suppressions = [HealthCheck.function_scoped_fixture]
settings.register_profile(
    "dev", max_examples=50, deadline=None, suppress_health_check=_common_suppressions
)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    suppress_health_check=[*_common_suppressions, HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
