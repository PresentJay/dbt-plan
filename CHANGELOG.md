# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.1] - 2026-08-22

### Changed
- The sdist now ships only the package source, READMEs, CHANGELOG, LICENSE
  and SECURITY.md (320 KB -> 36 KB). Previously hatchling's default swept the
  entire working tree in, including `docs/` and `tests/`. An sdist is
  published permanently and mirrored worldwide, so its contents should be a
  deliberate list. Tests, docs and examples remain in the git repository.

### Added
- Packaging tests that assert the sdist declares an explicit include list and
  contains no `docs/`, `tests/`, `examples/` or `.github/` entries, while
  still carrying everything needed to build (`pyproject.toml`, README,
  LICENSE, source, `py.typed`)
- Python 3.14 added to the packaging classifier test

## [0.5.0] - 2026-08-22

### Added
- `--acknowledge` / `DBT_PLAN_ACKNOWLEDGE` / `acknowledge_models`: an escape
  hatch for an intentional destructive change. The model is still reported in
  full and marked `[ACKNOWLEDGED]`; it just stops driving the exit code.
  Models must be named individually — there is deliberately no blanket
  "acknowledge all", which would let unrelated destructive changes ride along
  behind one reviewer's approval.
- `acknowledged` field per model and in the summary of `--format json`

### Changed
- README: replaced the "Future Improvements" table. INFORMATION_SCHEMA
  querying and column type detection both require a warehouse connection,
  which contradicts the tool's stated scope, so they are now listed under
  "Deliberately Not Planned" with the reasoning.

## [0.4.1] - 2026-08-22

### Fixed
- Raised the `sqlglot` floor to `>=28.0.0`. On sqlglot < 28,
  `SELECT * EXCEPT(col)` was extracted as plain `*`, so a dropped column
  produced no diff and the model was reported SAFE — a false safe. The
  declared floor of `>=26.0.0` allowed that combination to be installed.

### Added
- Python 3.14 support (classifier + CI matrix)
- CI job that runs the suite against the declared minimum dependency
  versions, so the floor cannot silently regress

### Changed
- Corrected stale test/coverage counts in both READMEs (1141 tests, 98%)

## [0.4.0] - 2026-08-21

### Changed
- All examples, fixtures, and tests now use a self-contained bookstore demo
  domain (`stg_orders`, `int_order_enriched`, `fct_orders`, `dim_customers`, ...)
- Repository moved to `PresentJay/dbt-plan`; docs at
  https://presentjay.github.io/dbt-plan

### Note
- Releases 0.2.0–0.3.5 were removed from PyPI. Install 0.4.0 or later.

## [0.3.5] - 2026-04-11

### Added
- Real-world dbt SQL test fixtures (window functions, CTE chains, UNION ALL)
- 216 tests total, 93% coverage

### Fixed
- mypy strict type annotations — 0 errors (deque[str], dict[str, Any])
- Release workflow: version-tag consistency check + test gate before publish

## [0.3.4] - 2026-04-11

### Fixed
- `dbt-plan run` no longer crashes when git is not installed (graceful error with fallback instructions)
- `dbt-plan run` detects non-git directories and shows manual workflow alternative
- `--select` with no matching changed models now warns on stderr

### Changed
- README feature table updated to v0.3.3 with all current features
- 211 tests, 93% coverage

## [0.3.3] - 2026-04-10

### Fixed
- **Duplicate column names no longer produce FALSE SAFE**: columns with duplicates (e.g., from JOINs) now return REVIEW REQUIRED instead of potentially wrong SAFE

### Changed
- Landing page: added "Safe by design" and "200+ tests" feature cards
- Test coverage: 206 tests, 93% overall, 6/8 modules at 100%

## [0.3.2] - 2026-04-10

### Changed
- PyPI classifier: `Alpha` → `Beta` (reflecting production readiness)
- Added License, Python version classifiers for PyPI discoverability
- Documentation URL added to PyPI metadata

### Fixed
- README feature table version v0.2.0 → v0.3.1
- CONTRIBUTING.md stale version reference and Good First Issues
- CHANGELOG entries moved from Unreleased to proper version sections
- CI now enforces coverage threshold (`--cov` flag)

### Added
- `SECURITY.md` with vulnerability reporting policy
- CI concurrency groups (cancel duplicate runs)
- 200 tests total, config.py at 100% coverage

## [0.3.1] - 2026-04-08

### Added
- **`compile_command` config**: customizable compile command via CLI flag, env var, or `.dbt-plan.yml`
  - Supports `uv run dbt compile`, `poetry run dbt compile`, custom scripts
  - Priority: CLI flag > env var > config file > default (`dbt compile`)
- **Landing page updates**: All Commands table, Configuration section, CI integration steps

## [0.3.0] - 2026-04-08

### Added
- **Cascade impact analysis**: detect downstream broken column references and build failures
  - `BROKEN_REF`: downstream SQL references a dropped column (word-boundary matching)
  - `BUILD_FAILURE`: downstream incremental with `on_schema_change=fail`
  - Table/view downstream models checked for broken column refs (SQL will fail even though DDL is safe)
  - Removed models trigger cascade analysis (all base columns treated as removed)
  - `incremental+ignore` correctly skips cascade (no physical schema change)
  - Safety escalation: cascade risks affect exit code (broken_ref → DESTRUCTIVE)
  - Cascade risk count in summary line and JSON output
  - Shown in all output formats (text, github, json)
- **Config change detection**: detect materialization and `on_schema_change` policy changes
  - `MATERIALIZATION CHANGED: table -> incremental` shown as WARNING
  - `on_schema_change CHANGED: ignore -> sync_all_columns` shown as WARNING
  - Helps catch dangerous policy transitions (e.g., accumulated schema drift)
- **`dbt-plan run`**: one-command check (compile baseline → compile current → check)
  - Stashes uncommitted changes, compiles baseline, restores, compiles current, runs check
  - Requires dbt to be installed (convenience wrapper, not a core dependency)
- **`dbt-plan ci-setup`**: generates GitHub Actions workflow for dbt-plan CI
  - Creates `.github/workflows/dbt-plan.yml` with snapshot → check → gate pipeline
- **Landing page**: `docs/index.html` for GitHub Pages

### Fixed
- **Exit code for WARNING predictions**: `BUILD FAILURE`, `STALE COLUMNS`, and other WARNING-level predictions now correctly return `warning_exit_code` (default 2) instead of 0
- **Disabled models excluded**: models with `enabled: false` are no longer indexed, preventing false `MODEL REMOVED` warnings
- Exit code bounds validation: `warning_exit_code` now requires 0-255 range
- Graceful handling of unreadable downstream SQL files during cascade analysis
- `--format text` correctly overrides config `format: github`
- `_do_init` exit code consistency (1 → 2)
- Unknown `on_schema_change` shows operation name in output

## [0.2.0] - 2026-04-06

### Added
- **Config system**: `.dbt-plan.yml` + 7 environment variables (`DBT_PLAN_*`)
- **New commands**: `dbt-plan init` (generates config + updates .gitignore), `dbt-plan stats` (project analysis)
- **New flags**: `--format json`, `--select` / `-s`, `--verbose` / `-v`, `--no-color`, `--dialect`
- **Manifest column fallback**: resolves SELECT * models using dbt column definitions
- **Package model filtering**: auto-excludes dbt package models
- **Multi-dialect support**: snowflake, bigquery, postgres, mysql, duckdb, trino
- **CI workflow template**: `examples/ci-workflow/dbt-plan.yml` with PR comment posting
- **CI summary line**: grepable `dbt-plan: N checked, X safe, Y warning, Z destructive`
- **ANSI color output**: red/yellow/green safety labels (auto-disabled when piped)
- **PyPI publishing**: `pip install dbt-plan`

### Fixed
- Qualified star (`SELECT t.*`) correctly returns `["*"]`
- Removed ephemeral models return SAFE (no physical object)
- Snapshot materialization handled explicitly (WARNING)
- Column reordering detected for `sync_all_columns`
- Stale column warnings for `append_new_columns`
- Unaliased expression ambiguity detection
- Flat compiled dir layout support
- Multi-project dir safety (ValueError on ambiguity)
- Actionable error messages
- Exit code 0 for no-args help

### Performance
- O(1) node index, streaming manifest, SQL caching, lazy imports
- Memoized batch downstream BFS, file-size fast path

## [0.1.0] - 2026-04-03

### Added
- `dbt-plan check` command: diff compiled SQL and predict DDL impact
- `dbt-plan snapshot` command: save baseline compiled state + manifest
- SQLGlot-based column extraction (Snowflake dialect)
- DDL prediction rules for all materialization x on_schema_change combinations
- Downstream impact discovery via manifest child_map (BFS with cycle protection)
- Text and GitHub markdown output formats
- Exit codes: 0 (safe), 1 (destructive), 2 (parse error/warning)
- Parse failure safety: never returns SAFE when columns cannot be extracted
- SELECT * detection: returns WARNING instead of false predictions
- Removed model detection: DESTRUCTIVE regardless of materialization
- Base manifest fallback: finds removed models in snapshot manifest
