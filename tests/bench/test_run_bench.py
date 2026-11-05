"""Tests for the CLI's matrix expansion and I/O -- not for actual timings.

The measured-loop methodology is tested in ``test_harness.py``; these tests target
argument parsing, matrix expansion (defaults, --depth/--events overrides,
--include-slow), and that a run actually writes a well-formed, self-consistent JSON
file.
"""

from __future__ import annotations

import json

from bench.run_bench import _build_matrix, _parse_args, _resolve_names, main
from bench.workloads import WORKLOADS


def test_resolve_names_all_expands_to_full_registry() -> None:
    assert _resolve_names(["all"], WORKLOADS) == sorted(WORKLOADS)


def test_resolve_names_passes_through_explicit_choices() -> None:
    assert _resolve_names(["balanced", "deep_book"], WORKLOADS) == ["balanced", "deep_book"]


def test_resolve_names_rejects_unknown() -> None:
    try:
        _resolve_names(["not_a_workload"], WORKLOADS)
    except SystemExit:
        pass
    else:
        msg = "expected SystemExit for an unknown workload name"
        raise AssertionError(msg)


def test_matrix_excludes_slow_cells_by_default() -> None:
    """A slow cell stays in the matrix, flagged excluded -- not silently dropped."""
    args = _parse_args(["--workload", "balanced", "--impl", "naive", "--depth", "100000"])
    matrix = _build_matrix(args)
    assert len(matrix) == 1
    assert matrix[0] == ("balanced", "naive", 100_000, 20_000, True)


def test_matrix_includes_slow_cells_with_flag() -> None:
    args = _parse_args(
        ["--workload", "balanced", "--impl", "naive", "--depth", "100000", "--include-slow"]
    )
    matrix = _build_matrix(args)
    assert len(matrix) == 1
    assert matrix[0][:3] == ("balanced", "naive", 100_000)


def test_matrix_events_override_applies_to_every_cell() -> None:
    args = _parse_args(
        [
            "--workload",
            "balanced",
            "--impl",
            "array",
            "--depth",
            "0",
            "--depth",
            "1000",
            "--events",
            "777",
        ]
    )
    matrix = _build_matrix(args)
    assert all(n_events == 777 for *_rest, n_events, _excluded in matrix)


def test_main_writes_a_well_formed_results_file(tmp_path) -> None:
    out_dir = tmp_path / "results"
    exit_code = main(
        [
            "--workload",
            "balanced",
            "--impl",
            "array",
            "--depth",
            "0",
            "--events",
            "300",
            "--repeats",
            "1",
            "--warmup",
            "0",
            "--min-samples",
            "10",
            "--label",
            "citest",
            "--out",
            str(out_dir),
            "--allow-dirty",
            "--quiet",
        ]
    )
    assert exit_code == 0

    files = list(out_dir.glob("bench-latency-citest-*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text())
    assert payload["schema_version"] == 1
    assert payload["label"] == "citest"
    assert payload["mode"] == "latency"
    assert "environment" in payload
    assert "methodology" in payload
    assert len(payload["results"]) == 1

    cell = payload["results"][0]
    assert cell["status"] == "ok"
    assert cell["impl"] == "array"
    assert cell["workload"] == "balanced"
    assert cell["measured"]["rejects"] == 0


def test_main_writes_excluded_cell_as_a_labeled_row(tmp_path) -> None:
    """A slow cell must appear in the JSON with status='excluded', not vanish."""
    out_dir = tmp_path / "results"
    exit_code = main(
        [
            "--workload",
            "balanced",
            "--impl",
            "naive",
            "--depth",
            "100000",
            "--label",
            "citest",
            "--out",
            str(out_dir),
            "--allow-dirty",
            "--quiet",
        ]
    )
    assert exit_code == 0

    files = list(out_dir.glob("bench-latency-citest-*.json"))
    payload = json.loads(files[0].read_text())
    assert len(payload["results"]) == 1
    cell = payload["results"][0]
    assert cell["status"] == "excluded"
    assert cell["latency_ns"] is None
    assert "include-slow" in cell["error"]
    assert cell["repro_command"]


def test_main_memory_mode_writes_memory_block(tmp_path) -> None:
    out_dir = tmp_path / "results"
    exit_code = main(
        [
            "--workload",
            "deep_book",
            "--impl",
            "array",
            "--depth",
            "200",
            "--events",
            "50",
            "--mode",
            "memory",
            "--label",
            "citest",
            "--out",
            str(out_dir),
            "--allow-dirty",
            "--quiet",
        ]
    )
    assert exit_code == 0

    files = list(out_dir.glob("bench-memory-citest-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    cell = payload["results"][0]
    assert cell["status"] == "ok"
    assert cell["latency_ns"] is None
    assert cell["memory"]["book_bytes_after_setup"] > 0
