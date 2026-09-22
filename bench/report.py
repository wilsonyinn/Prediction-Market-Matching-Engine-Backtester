"""Fold ``results/*.json`` into a markdown report.

A pure function of the committed JSON files: reads, groups, and formats, never runs
a benchmark or embeds ``now()`` -- only the source runs' own ``started_at_utc``
timestamps appear in the output. That is what lets CI regenerate the report from
committed data and diff it against what's checked in (see ``.github/workflows``) as
an integrity check: the published table must be exactly what the generator produces
from the published JSON, never hand-edited.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_README_BEGIN = "<!-- BENCH:BEGIN -->"
_README_END = "<!-- BENCH:END -->"


def load_cells(results_dir: Path) -> list[dict[str, Any]]:
    """Load every cell from every ``bench-*.json`` in ``results_dir``.

    Files are processed in filename order (which sorts by the embedded UTC
    timestamp), and a later file's cell overrides an earlier one sharing the same
    ``(cell_key, mode, label)`` -- so re-running a labeled benchmark and committing
    the new JSON naturally supersedes the old numbers without deleting the file.
    """
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in sorted(results_dir.glob("bench-*.json")):
        payload = json.loads(path.read_text())
        mode = payload["mode"]
        label = payload["label"]
        for cell in payload["results"]:
            key = (cell["cell_key"], mode, label)
            by_key[key] = {
                **cell,
                "_mode": mode,
                "_label": label,
                "_source_file": path.name,
                "_started_at": payload["started_at_utc"],
                "_environment": payload["environment"],
                "_repeats_config": payload.get("methodology", {}),
            }
    return list(by_key.values())


def depth_of(cell: dict[str, Any]) -> int:
    """The workload depth a cell was measured at, or 0 if unset."""
    depth = cell.get("workload_params", {}).get("depth", 0)
    return int(depth) if isinstance(depth, (int, float)) else 0


def max_tick_of(cell: dict[str, Any]) -> int:
    """The tick range (``max_tick``) a cell was measured at, or 100 if unset."""
    max_tick = cell.get("workload_params", {}).get("max_tick", 100)
    return int(max_tick) if isinstance(max_tick, (int, float)) else 100


def _fmt(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _latency_table(cells: list[dict[str, Any]]) -> str:
    ok_cells = [c for c in cells if c["_mode"] == "latency" and c["status"] == "ok"]
    other_cells = [c for c in cells if c["_mode"] == "latency" and c["status"] != "ok"]
    if not ok_cells and not other_cells:
        return ""

    multi_tick = len({max_tick_of(c) for c in ok_cells + other_cells}) > 1
    multi_label = len({c["_label"] for c in ok_cells + other_cells}) > 1

    ok_cells.sort(key=lambda c: (c["_label"], max_tick_of(c), c["impl"], depth_of(c)))
    # sorted by (label, max_tick, impl, depth) ascending, so the first cell seen
    # for each (label, max_tick, impl) triple is necessarily its minimum-depth
    # row -- the scaling baseline. Keyed on label and max_tick too: a "baseline"
    # and an "optimized" run of the same cell, or sparse_book's two tick sizes,
    # must never share a scaling baseline with each other.
    baseline_p50: dict[tuple[str, int, str], float] = {}
    for c in ok_cells:
        baseline_p50.setdefault((c["_label"], max_tick_of(c), c["impl"]), c["latency_ns"]["p50"])

    label_col = " label |" if multi_label else ""
    label_sep = ":---|" if multi_label else ""
    tick_col = " tick |" if multi_tick else ""
    tick_sep = "---:|" if multi_tick else ""
    lines = [
        f"|{label_col}{tick_col} depth | impl | events | events/s | trades/s | p50 µs | "
        "p95 µs | p99 µs | max µs | vs min depth |",
        f"|{label_sep}{tick_sep}---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    footnotes: list[str] = []
    for c in ok_cells:
        lat = c["latency_ns"]
        thr = c["throughput"]
        base = baseline_p50.get((c["_label"], max_tick_of(c), c["impl"]))
        scale = f"{lat['p50'] / base:.1f}x" if base else "—"
        label_cell = f" {c['_label']} |" if multi_label else ""
        tick_cell = f" {max_tick_of(c)} |" if multi_tick else ""
        lines.append(
            f"|{label_cell}{tick_cell} {depth_of(c):,} | {c['impl']} | "
            f"{c['measured']['commands']:,} | {_fmt(thr['events_per_sec'], 0)} | "
            f"{_fmt(thr['trades_per_sec'], 0)} | {lat['p50'] / 1000:.2f} | "
            f"{lat['p95'] / 1000:.2f} | {lat['p99'] / 1000:.2f} | {lat['max'] / 1000:.2f} | "
            f"{scale} |"
        )
    for i, c in enumerate(other_cells, start=1):
        marker = f"[{i}]"
        label_cell = f" {c['_label']} |" if multi_label else ""
        tick_cell = f" {max_tick_of(c)} |" if multi_tick else ""
        lines.append(
            f"|{label_cell}{tick_cell} {depth_of(c):,} | {c['impl']} | — | — | — | — | — | — "
            f"| — | {marker} |"
        )
        footnotes.append(
            f"{marker} status=`{c['status']}`: {c.get('error') or 'not measured'}. "
            f"Reproduce: `{c['repro_command']}`"
        )
    if footnotes:
        lines.append("")
        lines.extend(footnotes)
    return "\n".join(lines)


def _memory_table(cells: list[dict[str, Any]]) -> str:
    mem_cells = [c for c in cells if c["_mode"] == "memory" and c["status"] == "ok"]
    if not mem_cells:
        return ""
    multi_label = len({c["_label"] for c in mem_cells}) > 1
    mem_cells.sort(key=lambda c: (c["_label"], c["impl"], depth_of(c)))

    label_col = " label |" if multi_label else ""
    label_sep = ":---|" if multi_label else ""
    lines = [
        f"|{label_col} depth | impl | book bytes after setup | bytes/resting order | "
        "peak bytes | current bytes (end) |",
        f"|{label_sep}---:|:---|---:|---:|---:|---:|",
    ]
    for c in mem_cells:
        m = c["memory"]
        label_cell = f" {c['_label']} |" if multi_label else ""
        lines.append(
            f"|{label_cell} {depth_of(c):,} | {c['impl']} | {m['book_bytes_after_setup']:,} | "
            f"{m['bytes_per_resting_order']:.1f} | {m['peak_bytes']:,} | "
            f"{m['current_bytes_end']:,} |"
        )
    return "\n".join(lines)


def render(cells: list[dict[str, Any]]) -> str:
    """Render every cell into a full markdown report."""
    if not cells:
        return "# Benchmark Results\n\nNo results found under `results/`.\n"

    latest = max(c["_started_at"] for c in cells)
    labels = sorted({c["_label"] for c in cells})
    env = next(iter(cells))["_environment"]

    parts = [
        "# Benchmark Results",
        "",
        f"_Generated from {len({c['_source_file'] for c in cells})} result file(s); "
        f"latest run: {latest}. Labels present: {', '.join(labels)}._",
        "",
        "**Methodology**: per-command (not per-event) latency via `perf_counter_ns`, "
        "nearest-rank percentiles, aggregated as the median of each repeat's own "
        "percentiles (not pooled samples) -- see `docs/DESIGN.md` for why. GC is "
        "frozen (not disabled) before each repeat. Timer overhead "
        f"({env['timer']['overhead_ns_median']}ns median, "
        f"{env['timer']['resolution_ns']:.1f}ns resolution) is recorded, not "
        "subtracted. Machine: "
        f"{env['cpu_brand']}, {env['os']} {env['os_release']}, "
        f"Python {env['python_version']}. This is an unisolated consumer machine, "
        "not a dedicated benchmarking rig -- treat `max` as dominated by OS "
        "scheduling noise and `p99` as the highest generally meaningful percentile.",
        "",
    ]

    workloads = sorted({c["workload"] for c in cells})
    for workload_name in workloads:
        workload_cells = [c for c in cells if c["workload"] == workload_name]
        parts.append(f"## {workload_name}")
        parts.append("")
        latency = _latency_table(workload_cells)
        if latency:
            parts.append("### Latency")
            parts.append("")
            parts.append(latency)
            parts.append("")
        memory = _memory_table(workload_cells)
        if memory:
            parts.append("### Memory")
            parts.append("")
            parts.append(memory)
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def update_readme(readme_path: Path, report_markdown: str) -> None:
    """Rewrite the block between the BENCH markers in ``readme_path`` in place."""
    text = readme_path.read_text()
    pattern = re.compile(re.escape(_README_BEGIN) + r".*?" + re.escape(_README_END), re.DOTALL)
    replacement = f"{_README_BEGIN}\n\n{report_markdown}\n{_README_END}"
    if not pattern.search(text):
        msg = f"{_README_BEGIN} / {_README_END} markers not found in {readme_path}"
        raise SystemExit(msg)
    readme_path.write_text(pattern.sub(replacement, text))


def main(argv: list[str] | None = None) -> int:
    """Parse args, render the report, write it, and optionally update the README."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--out", default="results/RESULTS.md")
    parser.add_argument("--update-readme", default=None)
    args = parser.parse_args(argv)

    cells = load_cells(Path(args.results))
    report_markdown = render(cells)
    Path(args.out).write_text(report_markdown)

    if args.update_readme:
        update_readme(Path(args.update_readme), report_markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
