# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0] - 2026-09-01

### Fixed
- **A verdict resting on the manifest fallback is no longer reported as safe.**
  When a model's SQL resolves to `["*"]`, dbt-plan substitutes the columns
  documented in `manifest.json` — on *both* sides of the diff. `schema.yml`
  conventionally documents only the columns you test (jaffle_shop's own
  `stg_orders` lists 2 of its 4), and you edit SQL far more often than docs, so
  both sides receive the same incomplete list, the difference is zero, and the
  verdict was `SAFE` for a model whose SQL may have dropped a column. The
  comparison never happened. Such a model is now "review required". Scoped to
  materializations where columns matter: `table` and `view` are rebuilt by
  `CREATE OR REPLACE` regardless, so escalating those would be noise. A real
  destructive finding still stands on its own and still exits 1.

### Added
- The guidance `dbt-plan agent-setup` writes lists the two new ways to reach an
  exit code of 2, and warns against the matching new silencer: adding columns to
  `schema.yml` purely to clear "columns came from the manifest". Documenting the
  columns is right; documenting *some* of them is what caused the problem.
- **Explicit `CAST` type changes are detected.** The README listed this as
  deliberately out of scope, reasoning that deciding whether a type changed needs
  the warehouse's current type. True in general, and false in the case that
  matters: when both revisions carry an explicit `CAST` on the same column, the
  comparison is compiled SQL against compiled SQL, which is all this tool ever
  does. dbt acts on it — its docs describe `sync_all_columns` as "inclusive of
  data type changes" — and dbt-plan previously said `SAFE` because the column
  *names* matched. Reported as review required, never destructive and never safe,
  since whether `VARCHAR -> INT` loses data or `INT -> BIGINT` is a harmless
  widening is not decidable from the SQL. Columns cast on only one side are not
  reported: the other type is unknown.
- **`SELECT *` is now resolved through the CTEs of the same statement.** The
  canonical dbt staging model — the one dbt's own style guide teaches — ends in
  `select * from renamed`, where `renamed` is a CTE with an explicit column list.
  Every name is in the file, and dbt-plan used to answer `["*"]` to all of them.
  On a jaffle_shop project extended with realistic patterns (window functions,
  pivots, unions, nested CTEs), precise extraction went from **4 of 11 models to
  10 of 11**. No warehouse, no new dependency — the SQL was already in hand.

  This matters beyond coverage. A model that falls back to `["*"]` then falls
  back again to the manifest's documented columns, and a `schema.yml` that lists
  only the tested columns makes both sides of the diff identical — a confident
  "safe" built on a column list that was never complete. jaffle_shop's own
  `stg_orders` documents 2 of its 4 columns.

  Resolution refuses rather than guesses. It falls back to `["*"]` for an
  unqualified star over a join (whose columns come from every joined source), a
  set operation or recursive CTE, a source that is not a CTE, a subquery source,
  a chain that bottoms out in another unknown, a circular reference, and anything
  combined with `EXCEPT`. A merely plausible column list is worse than admitting
  ignorance: it gets compared against another plausible list and yields a silent
  "safe".

## [0.7.0] - 2026-09-01

### Fixed
- **A model dbt-plan never examined no longer reports as safe.** Two paths
  computed a warning and then discarded it. `skipped_models` — a model in the
  compiled diff but absent from the manifest — was dropped twice over: the text
  and markdown formatters returned "no model changes detected" before reaching
  the warning block, and `_exit_code_for` never consulted it. Through 0.6.0, a
  model that dropped a column reported clean and exited 0 whenever the manifest
  did not contain it, which a stale manifest or a wrong `--manifest` path is
  enough to cause. This is a false all-clear, the one verdict this tool exists
  to prevent.
- An unreadable `manifest.json` is no longer exit 0 when nothing changed. The
  path with a diff already exited 2; the two now agree. "I could not read your
  project metadata" is not evidence of safety.

### Added
- **Incomplete-compile detection, for the dbt Fusion engine.** dbt Core aborts a
  compile on the first failure, so `target/compiled/` was effectively
  all-or-nothing. Fusion keeps compiling the rest of the DAG after a node fails,
  which makes a partial target directory an ordinary outcome. A model missing
  from *both* compiled directories produces no diff entry at all, so it was
  silently never examined — and with nothing else changed, dbt-plan printed
  "no model changes detected" and exited 0. It now cross-checks the manifest
  against the compiled directory and reports what did not compile, naming the
  compile rather than implying a deletion.
- `uncompiled_models` in the JSON output. Additive; the four existing top-level
  keys are unchanged.
- The guidance `dbt-plan agent-setup` writes now separates the three things an
  exit code of 2 can mean — columns unreadable, manifest stale, compile
  incomplete — because each needs a different response, and names the newest way
  to silence the check: adding an uncompiled model to `ignore_models`, which
  turns the check green while leaving the one unexamined model unexamined.

### Changed
- Verified against the dbt Fusion engine `2.0.0-preview.218`: the compiled
  layout matches dbt Core, the manifest is schema v12 with only additive
  Fusion-specific fields, ephemeral models are written to `compiled/` as before,
  and the `<model>.macro_spans.json` sidecars Fusion writes beside each `.sql`
  are ignored. No code change was needed for compatibility itself.
- Exit codes: a skipped or uncompiled model now yields `warning_exit_code`
  (default 2) where it previously yielded 0. A destructive finding still
  outranks both and exits 1.

## [0.6.0] - 2026-08-31

### Added
- **`dbt-plan agent-setup`** writes dbt-plan guidance into a consuming project's
  `AGENTS.md`, creating the file or appending a marked section. It leads with
  what a coding agent is most likely to get wrong: adding a model to
  `ignore_models`, or downgrading `on_schema_change` from `sync_all_columns` to
  `ignore`, silences a real finding without making the change safe.
- **A GitHub Action.** `PresentJay/dbt-plan@v1` wraps install, compile-base,
  snapshot, compile-head, check and gate. Seven inputs, all defaulted; outputs
  `verdict`, `exit-code` and `report`. Inputs reach the shell through `env:`
  rather than being interpolated into `run:`, since the action runs beside
  pull-request-authored code compiling with credentials attached.
- `docs/llms.txt`, plus canonical and social meta tags on the landing page, so
  an agent asked about dbt-plan reads the scope boundaries instead of guessing.

### Changed
- **`dbt-plan ci-setup` now wires warehouse credentials.** The generated
  workflow had none, so it failed in CI the same way it fails locally. `dbt
  compile` runs Jinja and macros authored in the pull request, so secrets sit
  in a job-level `env:` instead of being interpolated into `run:`,
  `pull_request_target` is forbidden in a header comment, `permissions` drops
  from `pull-requests: write` to `contents: read`, `persist-credentials` is
  off, and a Preflight step fails fast naming the fork case.
- The README leads with the output example rather than a feature table. The
  `What Works` table was a changelog with a version number in its heading, so
  it was guaranteed to go stale — and had, claiming v0.3.5 at v0.5.2.
- The release workflow triggers on `v*.*.*` rather than `v*`, so the action's
  moving `v1` tag cannot fire a PyPI publish.

### Fixed
- **"No warehouse connection needed" is gone from the README, the Korean
  README and the landing page.** dbt-plan never connects, but producing its
  input requires `dbt compile`, which does — and that sentence was the one
  users acted on before hitting an empty-password error. It shipped on the
  PyPI page, which is where most people meet this project.

## [0.5.2] - 2026-08-22

### Fixed
- **`dbt-plan run` could strand your uncommitted work in the stash.** The
  command stashes changes to compile a clean baseline, but `_do_snapshot`
  exits the process rather than returning, so any snapshot failure — the
  common one being "No compiled SQL found" — skipped the restore entirely.
  The work vanished from the tree, sat in the stash, and nothing said so.
  Restore now runs in a `finally`.
- A failed `git stash push` no longer continues. Previously the return code
  was ignored, so the baseline was compiled from the still-dirty tree — making
  it identical to the current state and reporting "no changes", a false safe —
  and the later restore popped a stash entry dbt-plan never created, which
  could be the user's own.
- A failed `git stash pop` is now reported with the recovery command instead
  of being discarded. The restore also verifies the entry it pushed is still
  on top before popping, so it can never consume an unrelated stash.

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
