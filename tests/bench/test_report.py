"""Tests for the report generator: a pure function over committed JSON.

No engine involved -- these build small synthetic results payloads directly rather
than running a real benchmark, since ``report.py``'s job is folding JSON into
markdown, not measuring anything.
"""

from __future__ import annotations

import json

from bench.report import load_cells, render, update_readme


def _write_payload(path, *, label: str, mode: str, results: list[dict[str, object]]) -> None:
    payload = {
        "schema_version": 1,
        "run_id": f"20260101T000000Z-abc1234-{mode}",
        "label": label,
        "mode": mode,
        "started_at_utc": "20260101T000000Z",
        "environment": {
            "python_version": "3.13.2",
            "os": "Darwin",
            "os_release": "25.6.0",
            "cpu_brand": "Apple M5",
            "timer": {"overhead_ns_median": 42, "resolution_ns": 41.7},
        },
        "methodology": {},
        "results": results,
    }
    path.write_text(json.dumps(payload))


def _ok_latency_cell(*, impl: str, depth: int, p50: int) -> dict[str, object]:
    return {
        "cell_key": f"balanced/{impl}/d{depth}/n1000",
        "status": "ok",
        "workload": "balanced",
        "workload_params": {"depth": depth},
        "impl": impl,
        "impl_description": "x",
        "repeats": 3,
        "repro_command": f"python -m bench.run_bench --impl {impl} --depth {depth}",
        "setup": {"commands": depth},
        "measured": {
            "commands": 1000,
            "events": 2000,
            "trades": 100,
            "rejects": 0,
            "cancel_rejects": 0,
        },
        "throughput": {"events_per_sec": 1_000_000.0, "trades_per_sec": 50_000.0},
        "latency_ns": {"p50": p50, "p95": p50 * 2, "p99": p50 * 3, "max": p50 * 10},
        "per_repeat": [],
        "memory": None,
        "estimated_seconds": None,
        "error": None,
    }


def test_load_cells_merges_multiple_files(tmp_path) -> None:
    _write_payload(
        tmp_path / "bench-latency-a-20260101T000000Z-abc1234.json",
        label="baseline",
        mode="latency",
        results=[_ok_latency_cell(impl="array", depth=0, p50=100)],
    )
    _write_payload(
        tmp_path / "bench-latency-b-20260101T000100Z-abc1234.json",
        label="baseline",
        mode="latency",
        results=[_ok_latency_cell(impl="naive", depth=0, p50=200)],
    )

    cells = load_cells(tmp_path)

    assert len(cells) == 2
    assert {c["impl"] for c in cells} == {"array", "naive"}


def test_load_cells_later_file_supersedes_same_cell_key(tmp_path) -> None:
    _write_payload(
        tmp_path / "bench-latency-x-20260101T000000Z-abc1234.json",
        label="baseline",
        mode="latency",
        results=[_ok_latency_cell(impl="array", depth=0, p50=100)],
    )
    _write_payload(
        tmp_path / "bench-latency-y-20260101T000100Z-abc1234.json",
        label="baseline",
        mode="latency",
        results=[_ok_latency_cell(impl="array", depth=0, p50=999)],
    )

    cells = load_cells(tmp_path)

    assert len(cells) == 1
    assert cells[0]["latency_ns"]["p50"] == 999


def test_render_shows_scaling_relative_to_minimum_depth() -> None:
    cells = [
        {**_ok_latency_cell(impl="naive", depth=0, p50=100), "_mode": "latency", "_label": "b"},
        {**_ok_latency_cell(impl="naive", depth=1000, p50=400), "_mode": "latency", "_label": "b"},
    ]
    for c in cells:
        c["_source_file"] = "f.json"
        c["_started_at"] = "20260101T000000Z"
        c["_environment"] = {
            "cpu_brand": "x",
            "os": "Darwin",
            "os_release": "1",
            "python_version": "3.13",
            "timer": {"overhead_ns_median": 1, "resolution_ns": 1.0},
        }

    markdown = render(cells)

    assert "1.0x" in markdown
    assert "4.0x" in markdown


def test_render_scopes_scaling_baseline_per_label() -> None:
    """A 'baseline' and 'optimized' run of the same cell must not share a
    scaling baseline with each other -- otherwise 'optimized' rows would scale
    against 'baseline' numbers instead of their own minimum-depth row."""
    common_env = {
        "cpu_brand": "x",
        "os": "Darwin",
        "os_release": "1",
        "python_version": "3.13",
        "timer": {"overhead_ns_median": 1, "resolution_ns": 1.0},
    }
    cells = [
        {
            **_ok_latency_cell(impl="array", depth=0, p50=100),
            "_mode": "latency",
            "_label": "baseline",
        },
        {
            **_ok_latency_cell(impl="array", depth=1000, p50=200),
            "_mode": "latency",
            "_label": "baseline",
        },
        {
            **_ok_latency_cell(impl="array", depth=0, p50=50),
            "_mode": "latency",
            "_label": "optimized",
        },
        {
            **_ok_latency_cell(impl="array", depth=1000, p50=75),
            "_mode": "latency",
            "_label": "optimized",
        },
    ]
    for c in cells:
        c["_source_file"] = "f.json"
        c["_started_at"] = "20260101T000000Z"
        c["_environment"] = common_env

    markdown = render(cells)

    assert "label" in markdown
    assert "baseline" in markdown
    assert "optimized" in markdown
    # optimized/d1000 (75) scales against optimized/d0 (50) -> 1.5x, not against
    # baseline/d0 (100).
    assert "1.5x" in markdown
    # baseline/d1000 (200) scales against baseline/d0 (100) -> 2.0x.
    assert "2.0x" in markdown


def test_render_reports_non_ok_status_as_footnoted_row() -> None:
    cell = _ok_latency_cell(impl="naive", depth=100_000, p50=0)
    cell["status"] = "excluded"
    cell["error"] = "excluded by default; pass --include-slow"
    cell["latency_ns"] = None
    cell["throughput"] = None
    cell["_mode"] = "latency"
    cell["_label"] = "b"
    cell["_source_file"] = "f.json"
    cell["_started_at"] = "20260101T000000Z"
    cell["_environment"] = {
        "cpu_brand": "x",
        "os": "Darwin",
        "os_release": "1",
        "python_version": "3.13",
        "timer": {"overhead_ns_median": 1, "resolution_ns": 1.0},
    }

    markdown = render([cell])
    repro_command = cell["repro_command"]
    assert isinstance(repro_command, str)

    assert "excluded" in markdown
    assert "[1]" in markdown
    assert repro_command in markdown


def test_render_empty_input_does_not_crash() -> None:
    markdown = render([])
    assert "No results" in markdown


def test_update_readme_rewrites_only_the_marked_block(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Title\n\nSome intro.\n\n<!-- BENCH:BEGIN -->\nold content\n"
        "<!-- BENCH:END -->\n\nFooter.\n"
    )

    update_readme(readme, "NEW REPORT CONTENT")

    text = readme.read_text()
    assert "old content" not in text
    assert "NEW REPORT CONTENT" in text
    assert "Some intro." in text
    assert "Footer." in text


def test_update_readme_missing_markers_raises(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n\nNo markers here.\n")

    try:
        update_readme(readme, "content")
    except SystemExit:
        pass
    else:
        msg = "expected SystemExit when BENCH markers are missing"
        raise AssertionError(msg)
