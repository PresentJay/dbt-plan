# Contributing to dbt-plan

## Development Setup

```bash
git clone https://github.com/PresentJay/dbt-plan
cd dbt-plan
uv sync --extra test             # or: pip install -e ".[dev]"
make test                        # all tests should pass
dbt-plan --version               # should show installed version
```

## Adding Features

1. Write failing test first (TDD)
2. Implement the minimal code to pass
3. Run `make test` to verify
4. Run `make lint` to check style
5. Commit with conventional commits (`feat:`, `fix:`, `docs:`, `test:`)

## Architecture

```text
src/dbt_plan/
├── columns.py      # SQLGlot column extraction (multi-dialect)
├── config.py       # .dbt-plan.yml + env var configuration
├── predictor.py    # DDL prediction rules (materialization x on_schema_change)
├── manifest.py     # manifest.json parsing, node index, downstream BFS
├── diff.py         # compiled SQL directory comparison with caching
├── formatter.py    # text (color) / GitHub markdown / JSON output
└── cli.py          # CLI: snapshot, check, init, stats, run, ci-setup
```

Data flow: `diff_compiled_dirs` -> `extract_columns` -> `predict_ddl` -> `find_downstream_batch` -> `format_text/github/json`

## Key Rules

- No runtime dependencies beyond sqlglot
- Parse failure must never return "safe" (return None, caller handles as WARNING)
- SELECT * must return `["*"]` with manifest column fallback
- Every feature needs tests before implementation
- Multi-dialect support via `--dialect` (default: snowflake)

## Good First Issues

- Add compiled SQL fixtures in `tests/fixtures/` for edge cases (UNION, subqueries, MERGE)
- Add `pre-commit` hooks configuration
- Add type checking with mypy
- Add `SECURITY.md` with vulnerability reporting instructions

## Testing

```bash
make test                        # all tests (verbose)
make test-quick                  # quick run
make test-cov                    # with coverage report
make lint                        # ruff check
make format                      # auto-format
pytest -k "test_sync"            # by name pattern
```

Test fixtures in `tests/fixtures/` contain real-world compiled SQL patterns from production dbt projects.

## Design Decisions

See [docs/architecture-decisions.md](docs/architecture-decisions.md) for the rationale behind key decisions:
- Why SQLGlot (ADR-2)
- Why INFORMATION_SCHEMA is needed (ADR-3)
- Why PR merge is the only interception point (ADR-4)
- DDL prediction rules from dbt-snowflake source (ADR-5)
