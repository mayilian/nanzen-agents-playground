.PHONY: install style test run pipeline benchmark smoke

install:
	uv sync

style:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test:
	uv run pytest -v

# Run the renewal-risk pipeline for one account.
# Examples:
#   make pipeline ARGS="MERID-001"
#   make pipeline ARGS="MERID-001 --skip-llm"
pipeline:
	uv run python -m challenge $(ARGS)

# Convenience alias for the legacy name.
run: pipeline

# End-to-end run + append a row to BENCHMARK.md.
benchmark:
	uv run python -m scripts.run_benchmark MERID-001

# Live smoke test (hits Anthropic). Cheap but uses your API key.
smoke:
	uv run python -m challenge MERID-001
