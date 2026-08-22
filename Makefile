# Local equivalent of the CI workflow. `make` alone runs everything CI runs.
#
# ruff and mypy are not pinned here: they are dev dependencies, so uv.lock is
# the pin and `uv run` uses it. That makes the version identical to CI, which
# syncs from the same lock. Bumping either is therefore a lockfile change and
# shows up in review, which is the point -- `ruff format --check` is a blocking
# gate, and a newer ruff that reflows one expression differently would turn a
# branch red for a reason nobody could reproduce locally.
.PHONY: help all ci check lint format format-check typecheck test build clean

.DEFAULT_GOAL := help

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-13s %s\n", $$1, $$2}'

all: ci

ci: check ## Alias for `check`

check: lint typecheck test ## Everything CI runs

lint: ## Ruff, plus the formatter and import order as a check
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply ruff's fixes and the formatter in place
	uv run ruff check --fix .
	uv run ruff format .

format-check: ## Fail if anything is unformatted or the imports are unsorted
	uv run ruff format --check .
	uv run ruff check --select I .

# `--all-extras` is load-bearing, not tidiness. Every optional extra is listed
# under `ignore_missing_imports` in pyproject.toml, so an extra that is not
# installed resolves to `Any` and its call sites stop being checked. `rich`
# ships py.typed; without it installed, two genuine subclass checks degrade to
# `Any` and mypy reports errors CI never sees. CI runs `uv sync --all-extras`,
# so this is what makes the local run mean the same thing.
typecheck: ## mypy, strict, over mbprint/ with every extra installed
	uv run --all-extras mypy

test: ## pytest
	uv run pytest -q

build: ## Build the sdist and wheel
	uv build

clean: ## Remove build and tool caches
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache
