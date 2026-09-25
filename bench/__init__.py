"""Benchmark harness for the matching engine's book implementations.

Deterministic, seeded workload generators (``workloads.py``) drive the public
``MatchingEngine`` API; ``run_bench.py`` measures latency/throughput/memory and
writes ``results/*.json``; ``report.py`` folds those into a markdown table.
"""
