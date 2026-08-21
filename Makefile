# Local equivalent of the CI workflow. `make` alone runs everything CI runs.
.PHONY: all ci lint format typecheck test build clean

all: ci

ci: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest -q

build:
	uv build

clean:
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache
