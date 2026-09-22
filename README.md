# Prediction-Market Matching Engine & Backtester

A limit order book matching engine modeled on a prediction-market exchange (Polymarket's
CLOB), built in Python: proven correct with a rigorous test suite, benchmarked and
optimized, then used to backtest a simple market-making strategy against recorded
Polymarket data.

> Status: Phase 0/1 (engine + tests) in progress. This README will be filled in per the
> project spec (`SPEC.md`) as each phase completes — pitch, architecture diagram, how to
> run each part, results tables, and limitations.

## Development

```bash
make install   # create .venv and install dev dependencies
make all       # lint + typecheck + test
make cov       # test with coverage report
```

See [`SPEC.md`](SPEC.md) for the full project spec and [`docs/DESIGN.md`](docs/DESIGN.md)
for recorded design decisions.
