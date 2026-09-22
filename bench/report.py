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


def _depth(cell: dict[str, Any]) -> int:
    depth = cell.get("workload_params", {}).get("depth", 0)
    return int(depth) if isinstance(depth, (int, float)) else 0


def _max_tick(cell: dict[str, Any]) -> int:
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

    multi_tick = len({_max_tick(c) for c in ok_cells + other_cells}) > 1

    ok_cells.sort(key=lambda c: (_max_tick(c), c["impl"], _depth(c)))
    # sorted by (max_tick, impl, depth) ascending, so the first cell seen for each
    # (max_tick, impl) pair is necessarily its minimum-depth row -- the scaling
    # baseline. Keyed on max_tick too: sparse_book runs at two tick sizes must not
    # share a baseline, or the scaling column compares across an unrelated axis.
    baseline_p50: dict[tuple[int, str], float] = {}
    for c in ok_cells:
        baseline_p50.setdefault((_max_tick(c), c["impl"]), c["latency_ns"]["p50"])

    tick_col = " tick |" if multi_tick else ""
    tick_sep = "---:|" if multi_tick else ""
    lines = [
        f"|{tick_col} depth | impl | events | events/s | trades/s | p50 µs | p95 µs | "
        "p99 µs | max µs | vs min depth |",
        f"|{tick_sep}---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    footnotes: list[str] = []
    for c in ok_cells:
        lat = c["latency_ns"]
        thr = c["throughput"]
        base = baseline_p50.get((_max_tick(c), c["impl"]))
        scale = f"{lat['p50'] / base:.1f}x" if base else "—"
        tick_cell = f" {_max_tick(c)} |" if multi_tick else ""
        lines.append(
            f"|{tick_cell} {_depth(c):,} | {c['impl']} | {c['measured']['commands']:,} | "
            f"{_fmt(thr['events_per_sec'], 0)} | {_fmt(thr['trades_per_sec'], 0)} | "
            f"{lat['p50'] / 1000:.2f} | {lat['p95'] / 1000:.2f} | {lat['p99'] / 1000:.2f} | "
            f"{lat['max'] / 1000:.2f} | {scale} |"
        )
    for i, c in enumerate(other_cells, start=1):
        marker = f"[{i}]"
        tick_cell = f" {_max_tick(c)} |" if multi_tick else ""
        lines.append(
            f"|{tick_cell} {_depth(c):,} | {c['impl']} | — | — | — | — | — | — | — | {marker} |"
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
    mem_cells.sort(key=lambda c: (c["impl"], _depth(c)))

    lines = [
        "| depth | impl | book bytes after setup | bytes/resting order | peak bytes | "
        "current bytes (end) |",
        "|---:|:---|---:|---:|---:|---:|",
    ]
    for c in mem_cells:
        m = c["memory"]
        lines.append(
            f"| {_depth(c):,} | {c['impl']} | {m['book_bytes_after_setup']:,} | "
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
