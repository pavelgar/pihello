# Thin shortcuts for uv. Each recipe is a plain `uv ...` command you can also
# run directly (e.g. on Windows without make).

.PHONY: install fmt lint test test-integration test-matrix docs screenshots build clean

install:          ## create .venv with all dev dependencies
	uv sync

fmt:              ## format and auto-fix
	uv run ruff format
	uv run ruff check --fix

lint:             ## what the CI lint job runs
	uv run ruff format --check
	uv run ruff check
	uv run mypy
	uv run python scripts/gen_docs.py --check
	uv run python scripts/render_screenshots.py --check

test:             ## unit tests with coverage (no network, no containers)
	uv run pytest --cov

test-integration: ## end-to-end tests against real Pi-hole containers (podman or docker)
	uv run pytest -m integration

test-matrix:      ## the integration tests against every recorded Pi-hole version, with a summary
	uv run python scripts/test_matrix.py

docs:             ## regenerate the generated docs: variables, filters, galleries, SVGs
	uv run python scripts/gen_docs.py
	uv run python scripts/render_screenshots.py

screenshots:      ## regenerate the SVG screenshots in docs/
	uv run python scripts/render_screenshots.py

build:            ## build sdist + wheel into dist/
	uv build

clean:            ## remove build output and tool caches
	rm -rf build dist htmlcov .coverage .coverage.* coverage.xml .pytest_cache .ruff_cache .mypy_cache .hypothesis
