"""Tests for chart generation: reproducibility and correct file output.

No visual/pixel assertions -- those are unreliable across matplotlib/fontconfig
versions. What's checked is what would silently produce a *wrong* or *unstable*
artifact: that the expected files get written, and that rendering the same input
data twice produces byte-identical SVGs (the reproducibility settings actually
work), which is what a CI diff-check against committed charts would depend on.
"""

from __future__ import annotations

import json

from bench.plot import render_charts


def _write_payload(path, *, label: str, workload: str, results: list[dict[str, object]]) -> None:
    payload = {
        "schema_version": 1,
        "run_id": "x",
        "label": label,
        "mode": "latency",
        "started_at_utc": "20260101T000000Z",
        "environment": {
            "python_version": "3.13",
            "os": "Darwin",
            "os_release": "1",
            "cpu_brand": "x",
            "timer": {"overhead_ns_median": 1, "resolution_ns": 1.0},
        },
        "methodology": {},
        "results": results,
    }
    path.write_text(json.dumps(payload))
    del workload


def _cell(*, impl: str, depth: int, p50: int, p99: int, max_tick: int = 100) -> dict[str, object]:
    return {
        "cell_key": f"deep_book/{impl}/d{depth}/t{max_tick}",
        "status": "ok",
        "workload": "deep_book",
        "workload_params": {"depth": depth, "max_tick": max_tick},
        "impl": impl,
        "impl_description": "x",
        "repeats": 3,
        "repro_command": "x",
        "setup": {"commands": depth},
        "measured": {
            "commands": 100,
            "events": 100,
            "trades": 10,
            "rejects": 0,
            "cancel_rejects": 0,
        },
        "throughput": {"events_per_sec": 1.0, "trades_per_sec": 1.0},
        "latency_ns": {"p50": p50, "p95": p50 * 2, "p99": p99, "max": p99 * 3},
        "per_repeat": [],
        "memory": None,
        "estimated_seconds": None,
        "error": None,
    }


def test_render_charts_writes_expected_files(tmp_path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    _write_payload(
        results_dir / "bench-latency-baseline-x.json",
        label="baseline",
        workload="deep_book",
        results=[
            _cell(impl="array", depth=1000, p50=100, p99=200),
            _cell(impl="array", depth=10000, p50=110, p99=250),
            _cell(impl="naive", depth=1000, p50=500, p99=1000),
            _cell(impl="naive", depth=10000, p50=5000, p99=20000),
        ],
    )

    out_dir = tmp_path / "charts"
    written = render_charts(results_dir, out_dir, "baseline")

    assert len(written) == 1
    assert written[0].name == "deep_book-latency-vs-depth-baseline.svg"
    assert written[0].exists()
    assert written[0].read_bytes().startswith(b"<?xml")


def test_render_charts_is_reproducible(tmp_path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    _write_payload(
        results_dir / "bench-latency-baseline-x.json",
        label="baseline",
        workload="deep_book",
        results=[
            _cell(impl="array", depth=1000, p50=100, p99=200),
            _cell(impl="array", depth=10000, p50=110, p99=250),
        ],
    )

    written_a = render_charts(results_dir, tmp_path / "charts_a", "baseline")
    written_b = render_charts(results_dir, tmp_path / "charts_b", "baseline")

    assert written_a[0].read_bytes() == written_b[0].read_bytes()


def test_render_charts_no_cells_for_label_writes_nothing(tmp_path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    _write_payload(
        results_dir / "bench-latency-baseline-x.json",
        label="baseline",
        workload="deep_book",
        results=[_cell(impl="array", depth=1000, p50=100, p99=200)],
    )

    written = render_charts(results_dir, tmp_path / "charts", "nonexistent-label")

    assert written == []


def test_sparse_book_chart_written_when_present(tmp_path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    sparse_cells = [
        {
            **_cell(impl="array", depth=20, p50=100, p99=200, max_tick=1000),
            "workload": "sparse_book",
        },
        {**_cell(impl="tree", depth=20, p50=90, p99=180, max_tick=1000), "workload": "sparse_book"},
    ]
    _write_payload(
        results_dir / "bench-latency-baseline-x.json",
        label="baseline",
        workload="sparse_book",
        results=sparse_cells,
    )

    written = render_charts(results_dir, tmp_path / "charts", "baseline")

    names = {p.name for p in written}
    assert "sparse-book-array-vs-tree-baseline.svg" in names
