"""CLI entry point: ``python -m bench.run_bench [options]``.

Expands a (workload, depth, implementation) matrix, runs each cell through
``bench.harness``, and writes one JSON file per invocation to ``results/``. The
methodology lives in ``bench/harness.py``; this module is just argument parsing and
matrix expansion.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from bench.harness import (
    CellResult,
    _apply,
    capture_environment,
    replay_untimed,
    run_cell,
    run_memory_cell,
)
from bench.impls import IMPLS
from bench.workloads import WORKLOADS, Workload
from engine.engine import MatchingEngine

if TYPE_CHECKING:
    from collections.abc import Mapping

# Default depths swept per workload when --depth isn't given. sparse_book's whole
# point (see its docstring) needs a fine tick size, so its defaults assume
# --max-tick 1000 -- pass that explicitly when running it; running it at the
# --max-tick default (100) is legal but not the interesting case.
_DEFAULT_DEPTHS: dict[str, list[int]] = {
    "balanced": [0, 1_000, 10_000, 100_000],
    "deep_book": [1_000, 10_000, 100_000],
    "sweep_heavy": [1_000, 10_000],
    "sparse_book": [50, 200],
}

# Per-depth measured-event budget (see docs/DESIGN.md's Phase 2 "NaiveBook at
# depth" entry): NaiveBook's setup is quadratic and its per-command cost is O(n),
# so a flat event count everywhere would spend most of a run's wall clock
# re-confirming a result the smaller depths already establish.
_EVENT_BUDGET: dict[int, int] = {0: 100_000, 1_000: 100_000, 10_000: 50_000, 100_000: 20_000}

# Cells excluded from the default matrix (present with --include-slow). Naive at
# 100k events took ~36s of setup alone in development; excluded by default, not
# silently skipped -- see the "excluded" status cells this produces.
_SLOW_CELLS: frozenset[tuple[str, str, int]] = frozenset(
    {("balanced", "naive", 100_000), ("deep_book", "naive", 100_000)}
)

# Rough microseconds-per-command estimates used only for --list's wall-clock
# estimate, taken from development measurements. Not used for anything else.
_ESTIMATED_US_PER_CMD: dict[str, dict[int, float]] = {
    "naive": {0: 5, 1_000: 18, 10_000: 78, 100_000: 766},
    "array": {0: 2, 1_000: 2, 10_000: 2, 100_000: 2.5},
    "array_baseline": {0: 2, 1_000: 2, 10_000: 2, 100_000: 2.5},
    "tree": {0: 3, 1_000: 3, 10_000: 3.5, 100_000: 4},
}


def _events_for_depth(depth: int) -> int:
    if depth in _EVENT_BUDGET:
        return _EVENT_BUDGET[depth]
    closest = min(_EVENT_BUDGET, key=lambda d: abs(d - depth))
    return _EVENT_BUDGET[closest]


def _estimate_seconds(
    workload_name: str, impl_name: str, depth: int, n_events: int, repeats: int
) -> float:
    per_depth = _ESTIMATED_US_PER_CMD.get(impl_name, {})
    closest = min(per_depth, key=lambda d: abs(d - depth)) if per_depth else 0
    us_per_cmd = per_depth.get(closest, 5.0)
    del workload_name  # not used in this rough model
    return (n_events * us_per_cmd * repeats) / 1e6


def _build_workload(
    workload_name: str, *, seed: int, depth: int, max_tick: int, n_events: int
) -> Workload:
    factory = WORKLOADS[workload_name]
    return factory(seed=seed, n_events=n_events, depth=depth, max_tick=max_tick)


def _repro_command(args: argparse.Namespace, workload_name: str, impl_name: str, depth: int) -> str:
    return (
        f"python -m bench.run_bench --workload {workload_name} --impl {impl_name} "
        f"--depth {depth} --seed {args.seed} --max-tick {args.max_tick} "
        f"--repeats {args.repeats} --mode {args.mode}"
    )


def _resolve_names(requested: list[str] | None, registry: Mapping[str, object]) -> list[str]:
    if not requested or "all" in requested:
        return sorted(registry)
    unknown = [name for name in requested if name not in registry]
    if unknown:
        msg = f"unknown name(s) {unknown}; choices are {sorted(registry)} or 'all'"
        raise SystemExit(msg)
    return requested


def _build_matrix(
    args: argparse.Namespace,
) -> list[tuple[str, str, int, int, bool]]:
    """Returns (workload, impl, depth, n_events, excluded) for every matrix cell.

    Slow cells are included with ``excluded=True`` rather than omitted: a cell
    dropped from the *runnable* matrix is not the same as a cell that should
    silently vanish from the results. ``_run_matrix`` turns an excluded cell into a
    placeholder ``CellResult`` with ``status="excluded"`` so it still shows up in
    the report as a labeled row rather than an unexplained gap.
    """
    workload_names = _resolve_names(args.workload, WORKLOADS)
    impl_names = _resolve_names(args.impl, IMPLS)

    matrix: list[tuple[str, str, int, int, bool]] = []
    for workload_name in workload_names:
        depths = args.depth or _DEFAULT_DEPTHS.get(workload_name, [0])
        for depth in depths:
            n_events = args.events if args.events is not None else _events_for_depth(depth)
            for impl_name in impl_names:
                is_slow = (workload_name, impl_name, depth) in _SLOW_CELLS
                excluded = is_slow and not args.include_slow
                matrix.append((workload_name, impl_name, depth, n_events, excluded))
    return matrix


def _excluded_cell(
    args: argparse.Namespace, workload_name: str, impl_name: str, depth: int, n_events: int
) -> CellResult:
    repro = _repro_command(args, workload_name, impl_name, depth)
    cell_key = f"{workload_name}/{impl_name}/d{depth}/n{n_events}/t{args.max_tick}"
    est = _estimate_seconds(workload_name, impl_name, depth, n_events, args.repeats)
    return CellResult(
        cell_key=cell_key,
        status="excluded",
        workload=workload_name,
        workload_params={"depth": depth, "n_events": n_events, "max_tick": args.max_tick},
        impl=impl_name,
        impl_description=IMPLS[impl_name].description,
        repeats=0,
        repro_command=repro,
        setup={"commands": 0},
        measured=None,
        throughput=None,
        latency_ns=None,
        per_repeat=[],
        memory=None,
        estimated_seconds=est,
        error="excluded from the default matrix (slow); pass --include-slow to run it",
    )


def _run_matrix(args: argparse.Namespace) -> list[CellResult]:
    matrix = _build_matrix(args)
    results: list[CellResult] = []
    for workload_name, impl_name, depth, n_events, excluded in matrix:
        if excluded:
            results.append(_excluded_cell(args, workload_name, impl_name, depth, n_events))
            continue
        workload = _build_workload(
            workload_name, seed=args.seed, depth=depth, max_tick=args.max_tick, n_events=n_events
        )
        impl = IMPLS[impl_name]
        repro = _repro_command(args, workload_name, impl_name, depth)
        if not args.quiet:
            print(f"running {workload_name}/{impl_name}/d{depth}/n{n_events}...", file=sys.stderr)
        if args.mode == "memory":
            result = run_memory_cell(workload, impl, repro_command=repro)
        else:
            result = run_cell(
                workload,
                impl,
                repeats=args.repeats,
                warmup=args.warmup,
                max_seconds=args.max_seconds,
                min_samples=args.min_samples,
                gc_mode=args.gc,
                repro_command=repro,
            )
        results.append(result)
    return results


def _print_list(args: argparse.Namespace) -> None:
    matrix = _build_matrix(args)
    runnable = [row for row in matrix if not row[4]]
    excluded = [row for row in matrix if row[4]]

    total_seconds = 0.0
    print(f"{'workload':<12} {'impl':<15} {'depth':>8} {'events':>8} {'est. seconds':>13}")
    for workload_name, impl_name, depth, n_events, _excluded in runnable:
        est = _estimate_seconds(workload_name, impl_name, depth, n_events, args.repeats)
        total_seconds += est
        print(f"{workload_name:<12} {impl_name:<15} {depth:>8} {n_events:>8} {est:>13.1f}")
    if excluded:
        print(f"\n{len(excluded)} cell(s) excluded by default (pass --include-slow):")
        for workload_name, impl_name, depth, _n_events, _excluded in excluded:
            print(f"  {workload_name}/{impl_name}/d{depth}")
    print(
        f"\n{len(runnable)} cells, estimated {total_seconds:.1f}s total "
        "(rough, dev-machine estimate)"
    )


def _write_results(results: list[CellResult], args: argparse.Namespace) -> Path:
    env = capture_environment()
    if env["git_dirty"] and not args.allow_dirty:
        msg = (
            "refusing to write results from a dirty git tree "
            "(pass --allow-dirty for a development run; those results are not "
            "meant to be committed as official numbers)"
        )
        raise SystemExit(msg)

    started_utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    git_short = env["git_commit"][:7] if env["git_commit"] != "unknown" else "nogit"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"bench-{args.mode}-{args.label}-{started_utc}-{git_short}.json"
    final_path = out_dir / filename

    payload = {
        "schema_version": 1,
        "run_id": f"{started_utc}-{git_short}-{args.mode}",
        "label": args.label,
        "mode": args.mode,
        "started_at_utc": started_utc,
        "environment": env,
        "methodology": {
            "latency_unit": "ns_per_engine_call",
            "percentile_method": "nearest_rank",
            "aggregation_across_repeats": "median_of_per_repeat_percentiles",
            "warmup_events": args.warmup,
            "gc_mode": args.gc,
            "setup_method": "deepcopy_per_repeat",
        },
        "results": results,
    }

    tmp_path = out_dir / f".{filename}.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(final_path)
    return final_path


def _run_profile(args: argparse.Namespace) -> None:
    """Profile one cell with cProfile instead of timing it."""
    workload_names = _resolve_names(args.workload, WORKLOADS)
    impl_names = _resolve_names(args.impl, IMPLS)
    if len(workload_names) != 1 or len(impl_names) != 1:
        msg = "--profile requires exactly one --workload and one --impl"
        raise SystemExit(msg)
    depth = (args.depth or _DEFAULT_DEPTHS.get(workload_names[0], [0]))[0]
    n_events = args.events if args.events is not None else _events_for_depth(depth)

    workload = _build_workload(
        workload_names[0], seed=args.seed, depth=depth, max_tick=args.max_tick, n_events=n_events
    )
    impl = IMPLS[impl_names[0]]

    engine = MatchingEngine(book=impl.factory(max_tick=workload.max_tick))
    replay_untimed(engine, workload.setup)

    # Reuses harness._apply -- the exact same dispatch the timed loop uses -- so
    # the profile reflects the real measured code path, not a re-implementation
    # of it.
    profiler = cProfile.Profile()
    profiler.enable()
    for cmd in workload.measured:
        _apply(engine, cmd)
    profiler.disable()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"profile-{impl.name}-{workload.name}-d{depth}-{args.label}"
    profiler.dump_stats(out_dir / f"{stem}.pstats")

    stats = pstats.Stats(profiler)
    stats.sort_stats("tottime")
    with (out_dir / f"{stem}.txt").open("w") as f:
        stats.stream = f  # type: ignore[attr-defined]
        stats.print_stats(20)
    stats.stream = sys.stdout  # type: ignore[attr-defined]
    stats.print_stats(20)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", action="append", choices=[*WORKLOADS, "all"])
    parser.add_argument("--impl", action="append", choices=[*IMPLS, "all"])
    parser.add_argument("--depth", action="append", type=int)
    parser.add_argument("--events", type=int, default=None)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-tick", type=int, default=100)
    parser.add_argument("--mode", choices=["latency", "memory"], default="latency")
    parser.add_argument("--gc", choices=["enabled", "frozen", "disabled"], default="frozen")
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--min-samples", type=int, default=2_000)
    parser.add_argument("--include-slow", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--out", default="results")
    parser.add_argument("--label", default="dev")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to --list, --profile, or a matrix run."""
    args = _parse_args(argv)

    if args.list:
        _print_list(args)
        return 0

    if args.profile:
        _run_profile(args)
        return 0

    results = _run_matrix(args)
    path = _write_results(results, args)
    if not args.quiet:
        print(f"wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
