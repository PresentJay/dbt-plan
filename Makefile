.PHONY: test test-quick test-cov lint format format-check dev clean

test:
	uv run --extra test pytest -v

test-quick:
	uv run --extra test pytest -q

test-cov:
	uv run --extra test pytest --cov=dbt_plan --cov-report=term-missing

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

format-check:
	uv run ruff format --check src/ tests/

dev:
	uv sync --extra test --extra dbt

clean:
	rm -rf .venv .pytest_cache src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
