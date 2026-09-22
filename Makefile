PY ?= .venv/bin/python
LABEL ?= dev

.PHONY: install lint format typecheck test cov all bench bench-full bench-memory bench-report profile

install:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

cov:
	$(PY) -m pytest --cov=src/engine --cov-report=term-missing

all: lint typecheck test

# Default matrix: balanced/deep_book/sweep_heavy across implementations and depths.
# Naive above depth 10,000 is excluded (see docs/DESIGN.md) -- use bench-full for it.
bench:
	$(PY) -m bench.run_bench --workload balanced --workload deep_book --workload sweep_heavy \
		--label $(LABEL)

bench-full:
	$(PY) -m bench.run_bench --workload balanced --workload deep_book --workload sweep_heavy \
		--include-slow --label $(LABEL)

bench-memory:
	$(PY) -m bench.run_bench --mode memory --workload balanced --workload deep_book \
		--label $(LABEL)

bench-report:
	$(PY) -m bench.report --results results --out results/RESULTS.md

# One cell's measured loop under cProfile: `make profile WORKLOAD=balanced IMPL=array DEPTH=10000`
WORKLOAD ?= balanced
IMPL ?= array
DEPTH ?= 10000
profile:
	$(PY) -m bench.run_bench --profile --workload $(WORKLOAD) --impl $(IMPL) --depth $(DEPTH) \
		--label $(LABEL) --out results/profiles
