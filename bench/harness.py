"""The measured loop, percentile math, GC handling, and environment capture.

This module is the part of the benchmark that a reviewer should actually read: the
methodology choices here (not the CLI plumbing in ``run_bench.py``) are what make a
published number defensible. Every choice below is explained where it's made, not
just asserted; see ``docs/DESIGN.md``'s Phase 2 section for the measured evidence
behind the ones that needed it (GC mode, timer overhead, aggregation method).
"""

from __future__ import annotations

import copy
import gc
import math
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

from bench.workloads import Advance, Cancel, Submit
from engine.engine import MatchingEngine
from engine.types import CancelRejected, OrderRejected, Trade

if TYPE_CHECKING:
    from bench.impls import Impl
    from bench.workloads import Command, Workload
    from engine.types import EngineEvent

GcMode = Literal["enabled", "frozen", "disabled"]
CellStatus = Literal["ok", "truncated", "skipped_too_slow", "excluded", "error"]

# nearest_rank, not statistics.quantiles: interpolating between order statistics
# would report a latency value that was never actually observed. See DESIGN.md.
_PERCENTILE_METHOD = "nearest_rank"
_AGGREGATION = "median_of_per_repeat_percentiles"


def percentile_nearest_rank(sorted_values: list[int], q: float) -> int:
    """The ``q``-th percentile of already-sorted ``sorted_values`` (0 <= q <= 1).

    Nearest-rank: returns an actual observed value, never an interpolated one.
    """
    n = len(sorted_values)
    if n == 0:
        msg = "percentile of an empty sequence is undefined"
        raise ValueError(msg)
    idx = min(n - 1, max(0, math.ceil(q * n) - 1))
    return sorted_values[idx]


def _median(values: list[int]) -> int:
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    # Even count: the two middle values average to a non-integer in general, but
    # latencies are integer nanoseconds and callers want an integer back; round to
    # the nearest ns rather than truncate.
    return round((s[n // 2 - 1] + s[n // 2]) / 2)


def _median_float(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


class TimerInfo(TypedDict):
    """Timer characterization: back-to-back call overhead and clock resolution."""

    name: str
    resolution_ns: float
    overhead_ns_median: int
    overhead_ns_p99: int


def measure_timer_overhead(samples: int = 10_000) -> TimerInfo:
    """Characterize the timer itself: back-to-back call overhead and resolution.

    Not subtracted from measured latencies -- see DESIGN.md -- only recorded, so a
    reader can judge how much of a very small p50 is timer noise versus real work.
    """
    perf = time.perf_counter_ns
    deltas = [0] * samples
    prev = perf()
    for i in range(samples):
        now = perf()
        deltas[i] = now - prev
        prev = now
    deltas.sort()
    return TimerInfo(
        name="perf_counter_ns",
        resolution_ns=time.get_clock_info("perf_counter").resolution * 1e9,
        overhead_ns_median=_median(deltas),
        overhead_ns_p99=percentile_nearest_rank(deltas, 0.99),
    )


class Environment(TypedDict):
    """Everything a results JSON records about the machine and code it ran on."""

    python_version: str
    python_implementation: str
    os: str
    os_release: str
    platform: str
    machine: str
    cpu_brand: str
    cpu_count_logical: int
    git_commit: str
    git_dirty: bool
    timer: TimerInfo


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _cpu_brand() -> str:
    # platform.system(), not sys.platform: mypy specially narrows sys.platform
    # checks using its own configured/inferred target platform (this dev machine's,
    # absent an explicit `platform =` setting) and marks the non-matching branch
    # unreachable -- which would be wrong here, since CI's ubuntu-latest job
    # genuinely takes that branch at runtime. platform.system() isn't special-cased,
    # so both branches stay live for mypy as well as at runtime.
    if platform.system() == "Darwin":
        try:
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return platform.processor() or "unknown"
    return platform.processor() or "unknown"


def capture_environment() -> Environment:
    """Snapshot Python version, OS, CPU, git state, and timer overhead."""
    commit = _git("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git("status", "--porcelain"))

    return Environment(
        python_version=platform.python_version(),
        python_implementation=platform.python_implementation(),
        os=platform.system(),
        os_release=platform.release(),
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_brand=_cpu_brand(),
        cpu_count_logical=os.cpu_count() or 0,
        git_commit=commit,
        git_dirty=dirty,
        timer=measure_timer_overhead(),
    )


@dataclass
class _ReplayOutcome:
    events: int
    trades: int
    rejects: int
    cancel_rejects: int


def _apply(engine: MatchingEngine, cmd: Command) -> list[EngineEvent]:
    if isinstance(cmd, Submit):
        return engine.submit(cmd.order)
    if isinstance(cmd, Cancel):
        return engine.cancel(cmd.order_id, cmd.timestamp)
    if isinstance(cmd, Advance):
        return engine.advance_time(cmd.timestamp)
    # mypy proves Command = Submit | Cancel | Advance is exhaustively covered above;
    # kept as a defensive runtime check in case that union ever grows uncaught.
    msg = f"unknown command type: {cmd!r}"  # type: ignore[unreachable]
    raise TypeError(msg)


def replay_untimed(engine: MatchingEngine, commands: tuple[Command, ...]) -> _ReplayOutcome:
    """Replay ``commands`` with no timing at all. Used for the setup phase."""
    events = trades = rejects = cancel_rejects = 0
    for cmd in commands:
        for event in _apply(engine, cmd):
            events += 1
            if isinstance(event, Trade):
                trades += 1
            elif isinstance(event, OrderRejected):
                rejects += 1
            elif isinstance(event, CancelRejected):
                cancel_rejects += 1
    return _ReplayOutcome(events, trades, rejects, cancel_rejects)


class RepeatResult(TypedDict):
    """One repeat's summary: percentiles, engine time, and GC activity."""

    repeat: int
    commands: int
    engine_seconds: float
    p50: int
    p95: int
    p99: int
    max: int
    gc_collections: list[int]


def _run_one_repeat(
    engine: MatchingEngine,
    commands: tuple[Command, ...],
    repeat_index: int,
    *,
    max_seconds: float,
    gc_mode: GcMode,
) -> tuple[RepeatResult, _ReplayOutcome, bool]:
    """Time ``commands`` against ``engine``.

    Returns (repeat stats, event counts, whether every command completed within
    ``max_seconds``).
    """
    if gc_mode == "frozen":
        gc.collect()
        gc.freeze()
    elif gc_mode == "disabled":
        gc.collect()
        gc.disable()
    else:
        gc.collect()

    before = gc.get_stats()
    perf = time.perf_counter_ns
    n = len(commands)
    lat = [0] * n
    events = trades = rejects = cancel_rejects = 0
    completed = n
    wall_start = perf()
    time_budget_ns = int(max_seconds * 1e9)

    try:
        for i, cmd in enumerate(commands):
            if i % 1024 == 0 and (perf() - wall_start) > time_budget_ns:
                completed = i
                break
            if isinstance(cmd, Submit):
                order = cmd.order
                t0 = perf()
                ev = engine.submit(order)
                t1 = perf()
            elif isinstance(cmd, Cancel):
                oid, ts = cmd.order_id, cmd.timestamp
                t0 = perf()
                ev = engine.cancel(oid, ts)
                t1 = perf()
            else:
                ts = cmd.timestamp
                t0 = perf()
                ev = engine.advance_time(ts)
                t1 = perf()
            lat[i] = t1 - t0
            for event in ev:
                events += 1
                if isinstance(event, Trade):
                    trades += 1
                elif isinstance(event, OrderRejected):
                    rejects += 1
                elif isinstance(event, CancelRejected):
                    cancel_rejects += 1
    finally:
        if gc_mode == "frozen":
            gc.unfreeze()
        elif gc_mode == "disabled":
            gc.enable()

    after = gc.get_stats()
    engine_seconds = sum(lat[:completed]) / 1e9
    used = sorted(lat[:completed]) if completed else [0]

    result = RepeatResult(
        repeat=repeat_index,
        commands=completed,
        engine_seconds=engine_seconds,
        p50=percentile_nearest_rank(used, 0.50),
        p95=percentile_nearest_rank(used, 0.95),
        p99=percentile_nearest_rank(used, 0.99),
        max=used[-1],
        gc_collections=[
            after[g]["collections"] - before[g]["collections"] for g in range(len(before))
        ],
    )
    outcome = _ReplayOutcome(events, trades, rejects, cancel_rejects)
    return result, outcome, completed == n


class CellResult(TypedDict):
    """One (workload, implementation) cell as it appears in the results JSON."""

    cell_key: str
    status: CellStatus
    workload: str
    workload_params: dict[str, int | float | str]
    impl: str
    impl_description: str
    repeats: int
    repro_command: str
    setup: dict[str, int]
    measured: dict[str, int | float] | None
    throughput: dict[str, float] | None
    latency_ns: dict[str, int] | None
    per_repeat: list[RepeatResult]
    memory: None
    estimated_seconds: float | None
    error: str | None


def run_cell(  # noqa: PLR0913
    workload: Workload,
    impl: Impl,
    *,
    repeats: int,
    warmup: int,
    max_seconds: float,
    min_samples: int,
    gc_mode: GcMode,
    repro_command: str,
) -> CellResult:
    """Run one (workload, implementation) cell: setup once, measure ``repeats`` times.

    Setup runs once, then each repeat starts from a ``copy.deepcopy`` of the
    already-set-up engine rather than re-replaying setup ``repeats`` times -- at
    depth 100k on ``NaiveBook``, setup alone takes ~36s (its O(n) ``best()`` makes
    population quadratic), so re-replaying it 5 times would cost three extra
    minutes for zero additional signal. ``deepcopy`` was verified to preserve
    ``ArrayBook``'s id-index aliasing (see ``tests/bench/test_harness.py``).

    Warmup runs on a **separate** throwaway engine built the same way, over the
    first ``warmup`` measured commands, discarded entirely -- CPython's adaptive
    specializing interpreter specializes the hot bytecode after a few thousand
    calls, and that specialization is per-code-object so it carries over to the
    real run, but running it against the real engine would consume part of the
    measured command list and leave the book in a different state.
    """
    cell_key = f"{workload.name}/{impl.name}/d{_infer_depth(workload)}/n{len(workload.measured)}"

    base_engine = MatchingEngine(book=impl.factory(max_tick=workload.max_tick))
    setup_outcome = replay_untimed(base_engine, workload.setup)
    if setup_outcome.trades or setup_outcome.rejects:
        return CellResult(
            cell_key=cell_key,
            status="error",
            workload=workload.name,
            workload_params=workload.params,
            impl=impl.name,
            impl_description=impl.description,
            repeats=0,
            repro_command=repro_command,
            setup={"commands": len(workload.setup), "trades": setup_outcome.trades},
            measured=None,
            throughput=None,
            latency_ns=None,
            per_repeat=[],
            memory=None,
            estimated_seconds=None,
            error="setup produced trades or rejects; workload is not clean",
        )

    if warmup:
        warmup_engine = copy.deepcopy(base_engine)
        warmup_commands = workload.measured[:warmup]
        _run_one_repeat(
            warmup_engine, warmup_commands, -1, max_seconds=max_seconds, gc_mode=gc_mode
        )

    per_repeat: list[RepeatResult] = []
    last_outcome = _ReplayOutcome(0, 0, 0, 0)
    fully_completed = True
    for r in range(repeats):
        repeat_engine = copy.deepcopy(base_engine)
        result, outcome, completed_fully = _run_one_repeat(
            repeat_engine, workload.measured, r, max_seconds=max_seconds, gc_mode=gc_mode
        )
        per_repeat.append(result)
        last_outcome = outcome
        fully_completed = fully_completed and completed_fully

    min_completed = min(r["commands"] for r in per_repeat)
    if min_completed < min_samples:
        return CellResult(
            cell_key=cell_key,
            status="skipped_too_slow",
            workload=workload.name,
            workload_params=workload.params,
            impl=impl.name,
            impl_description=impl.description,
            repeats=repeats,
            repro_command=repro_command,
            setup={"commands": len(workload.setup)},
            measured={"commands": len(workload.measured), "commands_completed_min": min_completed},
            throughput=None,
            latency_ns=None,
            per_repeat=per_repeat,
            memory=None,
            estimated_seconds=None,
            error=f"only {min_completed} samples completed within max_seconds; "
            f"below min_samples={min_samples}",
        )

    p50 = _median([r["p50"] for r in per_repeat])
    p95 = _median([r["p95"] for r in per_repeat])
    p99 = _median([r["p99"] for r in per_repeat])
    med_max = _median([r["max"] for r in per_repeat])
    worst_max = max(r["max"] for r in per_repeat)
    engine_seconds_median = _median_float([r["engine_seconds"] for r in per_repeat])
    commands_median = _median([r["commands"] for r in per_repeat])

    events_per_sec = last_outcome.events / engine_seconds_median if engine_seconds_median else 0.0
    commands_per_sec = commands_median / engine_seconds_median if engine_seconds_median else 0.0
    trades_per_sec = last_outcome.trades / engine_seconds_median if engine_seconds_median else 0.0

    return CellResult(
        cell_key=cell_key,
        status="ok" if fully_completed else "truncated",
        workload=workload.name,
        workload_params=workload.params,
        impl=impl.name,
        impl_description=impl.description,
        repeats=repeats,
        repro_command=repro_command,
        setup={"commands": len(workload.setup)},
        measured={
            "commands": len(workload.measured),
            "commands_completed_median": commands_median,
            "events": last_outcome.events,
            "trades": last_outcome.trades,
            "rejects": last_outcome.rejects,
            "cancel_rejects": last_outcome.cancel_rejects,
            "engine_seconds_median": engine_seconds_median,
        },
        throughput={
            "events_per_sec": events_per_sec,
            "commands_per_sec": commands_per_sec,
            "trades_per_sec": trades_per_sec,
        },
        latency_ns={
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "max": med_max,
            "max_worst_repeat": worst_max,
        },
        per_repeat=per_repeat,
        memory=None,
        estimated_seconds=None,
        error=None,
    )


def _infer_depth(workload: Workload) -> int:
    depth = workload.params.get("depth", 0)
    return int(depth) if isinstance(depth, (int, float)) else 0
