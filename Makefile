PY ?= .venv/bin/python

.PHONY: install lint format typecheck test cov all

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
