"""The book-implementation registry benchmarks are run against.

Deliberately separate from ``tests.conftest.BOOK_FACTORIES``: that registry exists to
parametrize the test suite and its keys are pytest ids, not a public/documented
contract, whereas this one appears in every results JSON's ``impl`` field and every
``--impl`` CLI argument -- a schema surface that should not silently change if the
test fixture is refactored. Both happen to list the same implementations today by
construction, not by import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from engine.array_book import ArrayBook
from engine.array_book_baseline import ArrayBookBaseline
from engine.naive_book import NaiveBook

if TYPE_CHECKING:
    from engine.book import OrderBook


class BookFactory(Protocol):
    """A zero-or-one-arg constructor for a book implementation."""

    def __call__(self, *, max_tick: int) -> OrderBook:
        """Build a fresh, empty book with the given tick range."""
        ...


@dataclass(frozen=True, slots=True)
class Impl:
    """One benchmarkable book implementation."""

    name: str
    description: str
    factory: BookFactory


_IMPLS: list[Impl] = [
    Impl("naive", "one flat list per side, O(n) reference implementation", NaiveBook),
    Impl("array", "bounded price array, O(1) add/cancel/best-price", ArrayBook),
    Impl(
        "array_baseline",
        "frozen Phase 1 ArrayBook, kept for before/after comparison",
        ArrayBookBaseline,
    ),
]

try:
    from engine.tree_book import TreeBook
except ImportError:
    pass
else:
    _IMPLS.append(
        Impl("tree", "sorted-map (SortedDict) per side, general-exchange comparison", TreeBook)
    )

IMPLS: dict[str, Impl] = {impl.name: impl for impl in _IMPLS}
