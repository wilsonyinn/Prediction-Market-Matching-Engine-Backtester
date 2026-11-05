"""Render charts from ``results/*.json`` into ``results/charts/*.svg``.

Kept entirely separate from ``bench.harness``/``bench.run_bench``: a benchmark run
never imports ``matplotlib`` (an optional ``bench``-extra dependency), so measuring
never pays its import cost or risks it being absent. This module only ever reads
already-written JSON, via ``bench.report.load_cells``.

Two charts, matching the spec's "optionally chart latency vs. book depth":

1. p50 and p99 latency vs. book depth, one line per implementation, log-log axes --
   the O(n)-vs-O(1) divergence as a picture rather than a table.
2. `ArrayBook` vs. `TreeBook` on the `sparse_book` workload, at two tick sizes --
   the discriminating comparison for the general-exchange question.

Reproducibility settings (fixed ``svg.hashsalt``, no timestamp in output metadata)
mean re-running this on unchanged input data produces byte-identical SVGs, so a
regenerate-and-diff check (like the one CI runs on ``results/RESULTS.md``) would
show no changes -- charts don't silently churn in git across unrelated commits.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "predmarket-engine-bench-charts"

import matplotlib.pyplot as plt  # noqa: E402

from bench.report import depth_of, load_cells, max_tick_of  # noqa: E402

_IMPL_ORDER = ["naive", "array_baseline", "array", "tree"]
_IMPL_COLORS = {
    "naive": "#888888",
    "array_baseline": "#8888cc",
    "array": "#1f77b4",
    "tree": "#d62728",
}

_SAVEFIG_KWARGS: dict[str, Any] = {"format": "svg", "metadata": {"Date": None}}


def _latency_vs_depth_chart(cells: list[dict[str, Any]], workload_name: str, label: str) -> Any:
    ok_cells = [
        c
        for c in cells
        if c["_mode"] == "latency"
        and c["status"] == "ok"
        and c["workload"] == workload_name
        and c["_label"] == label
    ]
    fig, (ax_p50, ax_p99) = plt.subplots(1, 2, figsize=(10, 4.5))
    for impl in _IMPL_ORDER:
        rows = sorted((c for c in ok_cells if c["impl"] == impl), key=depth_of)
        if not rows:
            continue
        depths = [max(1, depth_of(c)) for c in rows]  # depth=0 has no log-scale position
        p50s = [c["latency_ns"]["p50"] for c in rows]
        p99s = [c["latency_ns"]["p99"] for c in rows]
        color = _IMPL_COLORS.get(impl, "#333333")
        ax_p50.plot(depths, p50s, marker="o", label=impl, color=color)
        ax_p99.plot(depths, p99s, marker="o", label=impl, color=color)

    for ax, title in ((ax_p50, "p50"), (ax_p99, "p99")):
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("resting orders (depth)")
        ax.set_ylabel("latency (ns)")
        ax.set_title(f"{workload_name}: {title} latency vs. depth ({label})")
        ax.grid(True, which="both", linestyle=":", linewidth=0.5)
    ax_p50.legend(fontsize="small")
    fig.tight_layout()
    return fig


def _sparse_book_chart(cells: list[dict[str, Any]], label: str) -> Any:
    ok_cells = [
        c
        for c in cells
        if c["_mode"] == "latency"
        and c["status"] == "ok"
        and c["workload"] == "sparse_book"
        and c["_label"] == label
        and c["impl"] in ("array", "tree")
    ]
    tick_sizes = sorted({max_tick_of(c) for c in ok_cells})
    fig, axes = plt.subplots(1, max(1, len(tick_sizes)), figsize=(5 * max(1, len(tick_sizes)), 4.5))
    if len(tick_sizes) <= 1:
        axes = [axes]

    for ax, tick in zip(axes, tick_sizes, strict=True):
        for impl in ("array", "tree"):
            rows = sorted(
                (c for c in ok_cells if c["impl"] == impl and max_tick_of(c) == tick), key=depth_of
            )
            if not rows:
                continue
            depths = [depth_of(c) for c in rows]
            p50s = [c["latency_ns"]["p50"] for c in rows]
            ax.plot(depths, p50s, marker="o", label=impl, color=_IMPL_COLORS.get(impl))
        ax.set_xlabel("resting orders (depth)")
        ax.set_ylabel("p50 latency (ns)")
        ax.set_title(f"sparse_book, tick size 1/{tick} ({label})")
        ax.grid(True, linestyle=":", linewidth=0.5)
        ax.legend(fontsize="small")
    fig.tight_layout()
    return fig


def render_charts(results_dir: Path, out_dir: Path, label: str) -> list[Path]:
    """Render both charts for ``label`` and write them to ``out_dir``. Returns paths."""
    cells = load_cells(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for workload_name in ("deep_book", "balanced"):
        if not any(
            c["workload"] == workload_name and c["_label"] == label and c["_mode"] == "latency"
            for c in cells
        ):
            continue
        fig = _latency_vs_depth_chart(cells, workload_name, label)
        path = out_dir / f"{workload_name}-latency-vs-depth-{label}.svg"
        fig.savefig(path, **_SAVEFIG_KWARGS)
        plt.close(fig)
        written.append(path)

    if any(c["workload"] == "sparse_book" and c["_label"] == label for c in cells):
        fig = _sparse_book_chart(cells, label)
        path = out_dir / f"sparse-book-array-vs-tree-{label}.svg"
        fig.savefig(path, **_SAVEFIG_KWARGS)
        plt.close(fig)
        written.append(path)

    return written


def main(argv: list[str] | None = None) -> int:
    """Parse args and render charts for the requested label."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--out", default="results/charts")
    parser.add_argument("--label", default="optimized")
    args = parser.parse_args(argv)

    written = render_charts(Path(args.results), Path(args.out), args.label)
    for path in written:
        print(f"wrote {path}")
    if not written:
        print(f"no cells found for label={args.label!r}; nothing to render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
