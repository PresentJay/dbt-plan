# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Data tests broken by a dropped column are now reported.** (#85, #86) This closes
  a false all-clear, and the most common one left: unit tests are rare, `not_null`
  is in nearly every dbt project.

  A `not_null` on a dropped column is not a failing assertion, it is a query dbt
  cannot bind:

  ```
  Failure in test not_null_stg_orders_customer_id (models/schema.yml)
    Binder Error: Referenced column "customer_id" not found in FROM clause!
    Candidate bindings: "order_id"
  ```

  0.12.0 reported `SAFE  stg_orders (view, ignore)` and exited 0.

  A generic test names its column in the manifest, so `not_null`, `unique` and
  `accepted_values` are answered without reading a file. `relationships` is the one
  built-in that reads a second model, and the far side is named only in its kwargs;
  that is matched too. A singular test names nothing in the manifest, so its
  compiled SQL is searched — which meant indexing `target/compiled/<project>/tests/`,
  outside the `models/` tree dbt-plan scans. A test whose SQL cannot be found is
  reported as unreadable rather than passed over.

  Tests on a model that inherits the loss through a `SELECT *` are caught as well.
  Neither this nor #24 covered that alone: a downstream `table` is rebuilt safely, so
  the inherited-loss check stays quiet, while the `not_null` on it still cannot bind.


## [0.12.0] - 2026-09-05

### Added
- **A model that loses a column without its own file changing is now reported.**
  (#24) This closes a false all-clear.

  ```
  stg_orders:  SELECT order_id, customer_id, status  ->  SELECT order_id, status
  fct_orders:  SELECT * FROM {{ ref('stg_orders') }}  ->  byte-identical
  ```

  `fct_orders` loses `customer_id` too, and on `incremental` + `sync_all_columns`
  dbt issues a DROP COLUMN against a table with data in it. Measured on duckdb:

  ```
  alter table "dev"."main"."fct_orders" drop column
  ```

  0.11.2 reported `SAFE  stg_orders (view, ignore)` and exited 0. The diff only
  carries models whose own file changed, and the broken-ref check looks for the
  column by name in SQL that never names it.

  Both sides of an unchanged downstream model are now resolved through `ref()`
  from the project's own compiled SQL and run back through `predict_ddl`, so the
  verdict follows the same materialization rules as any other model. A downstream
  model that rebuilds itself -- `table`, `view`, `incremental` + `ignore` -- is
  still safe and still silent. One whose columns cannot be read on both sides
  gets REVIEW REQUIRED where its configuration allows a drop.

  Measured at 0.20-0.30s on a 200-model project chained entirely by `SELECT *`,
  the worst case for this, against a budget of 5s.

### Changed
- **Long cascade lists are truncated in the text and GitHub reports.** One
  `SELECT *` chain can put every model in the project downstream of one change,
  and a hundred bullets in a pull request comment is a wall nobody reads. Sorted
  worst-first before the cut, so it only ever removes the least severe;
  `--format json` still carries all of them.

- **Exposures downstream of a change that is not safe are now named, with their
  owners.** (#44) An exposure records that something outside the project -- a
  dashboard, a notebook, a reverse-ETL sync -- reads a model. `find_downstream`
  walked into them and `build_node_index` dropped them, so the report never
  mentioned the people who most needed telling:

  ```
  -- EXPOSURE  orders_dashboard (dashboard) -- owner: Data Team <data@example.com>
  ```

  An exposure declares its dependencies at model granularity, with no columns in
  it, so this never claims a dashboard breaks. It is deliberately not a risk and
  deliberately does not escalate the verdict: an exposure existing does not make
  a change more dangerous, and inflating the verdict for it would train people to
  ignore the line. For the same reason it is left off safe verdicts.

- **Unit tests downstream of a dropped column are now reported.** (#43) A dbt unit
  test pins its columns down by hand, in `expect` and in every `given` input, and
  dbt checks each of those against the real relation it stands for:

  ```
  Invalid column name: 'customer_id' in unit test fixture for 'stg_orders'.
  Accepted columns for 'stg_orders' are: ['order_id', 'store_id', 'order_date']
  ```

  So dropping a column fails the build even when the model's own DDL is safe --
  a view is `CREATE OR REPLACE`, and dbt-plan called that SAFE while `dbt build`
  was going to stop. It is now a `UNIT_TEST_FAILURE` cascade risk and a warning.

  Both the `expect` block of the changed model's own tests and the `given` inputs
  of tests further downstream are checked, since a `given` fixture standing in for
  the changed model is validated the same way. Only fixtures that stand for the
  changed model are compared. Only removals are reported: adding a column leaves
  every fixture passing, measured against dbt 1.11.7.

  A fixture that cannot be read from the manifest -- `fixture:` file references,
  `format: sql` -- is reported as `UNIT_TEST_UNREADABLE` rather than passed over.

### Fixed
- **dbt-plan no longer treats compiled unit test SQL as a model.** `dbt build`
  writes unit tests into `target/compiled/` alongside the models, under a
  directory named for the schema file that declared them. dbt-plan diffed them
  as models, so any project with unit tests got a spurious
  `Skipped N model(s) not found in manifest` on every run -- and two models that
  each carried a `test_shape` aborted the run outright on the duplicate-name
  check. Anything under a `*.yml` path segment is now skipped.

## [0.11.2] - 2026-09-05

### Added
- **An `mcp-name:` line in the README, so the MCP registry will accept the entry.**
  The registry will not take a PyPI-backed server on the publisher's word that they
  own the package: it fetches the package page and looks for the server name spelled
  `mcp-name: io.github.PresentJay/dbt-plan`. Without it, `mcp-publisher publish`
  fails with a 400 and no entry is created.

  It checks the README on PyPI, not the one on GitHub, and the PyPI page is only
  rebuilt by a release — which is what this release is for. `tests/test_packaging.py`
  now fails if the line is dropped, because otherwise dropping it costs nothing until
  the next release, by which point the cause is several commits back.

  No source changes.

## [0.11.1] - 2026-09-05

### Changed
- **Documentation only, released so PyPI stops describing the tool by the wrong
  axis.** The PyPI page is built from `README.md` and only updates on a release,
  and it is currently the first search result for this project — so the page most
  people read was still framing dbt-plan as a CI check.

  It is used the way `terraform plan` is used: before the command that changes
  the warehouse, not only in CI afterwards. The README now leads with that loop,
  states the measured cost of running it (`check` 0.11s on 3 models, 0.48s on 200
  with every one changed — the `dbt compile` is the whole cost and you were paying
  it anyway), and surfaces `agent-setup` in Quick Start rather than fourth in a
  command list.

  `docs/use-cases.md` gains a first case with no cheap alternative anywhere else:
  a macro change. One line removed from a macro, no model file touched, and
  `fct_orders` loses a column from a table that has data in it. The pull request
  diff is two lines and says none of that.

  No source changes.

## [0.11.0] - 2026-09-03

### Removed
- **The `include_packages` config key and `DBT_PLAN_INCLUDE_PACKAGES`.** It
  claimed to "also check models from dbt packages" and never did. It widened the
  manifest index to keep package models, while the compiled scan covers the root
  project's directory alone — so those models were indexed and then never
  examined.

  It was worse than a no-op. The uncompiled-model check added in 0.7.0
  cross-references the manifest against the compiled directory, found those
  entries with no compiled SQL, and reported **"the compile is incomplete — fix
  the compile and rerun"** about a compile that was fine. The advice could not be
  followed, because there was nothing to fix.

  Setting it now warns, with the line number, that it is ignored. Removing the
  key outright would have been silent, and this way a stale config says what
  happened rather than quietly changing meaning.

  Making it work would mean scanning several project directories, which runs into
  `diff_compiled_dirs` refusing on a duplicate file stem — dbt requires model
  names to be unique per package, not globally, so a collision is legal. That is a
  real design question and not worth opening for a feature whose only observable
  effect was a wrong warning. The finding that matters — a package model dropping
  a column one of your models reads — is already caught as a broken ref by
  cascade analysis.

  `build_node_index(include_packages=True)` is untouched. That was the half that
  worked, it stays tested, and it is the hook if the scan is ever widened.

## [0.10.1] - 2026-09-03

### Fixed
- **A dbt package that ships models no longer stops dbt-plan from running.**
  dbt compiles every installed package into its own directory under
  `target/compiled/`, and `_find_compiled_dir` treated more than one as
  unresolvable and aborted. Any project depending on elementary,
  dbt_project_evaluator, dbt_artifacts or similar could not run `snapshot` or
  `check` at all.

  The error also suggested `--project-dir`, which does not help: that flag points
  at the dbt project, and the competing directories are inside *that project's*
  `target/`. Passing it produced the identical message, so the only advice given
  was a dead end.

  The root project is now identified from `metadata.project_name` in the
  manifest — the same field `build_node_index` already uses to keep package
  models out of the index. The file side and the manifest side now agree on which
  project is yours.

  Still refuses when it genuinely cannot tell: no manifest, an unreadable one, or
  one naming a project that is not there. The message now points at the manifest
  rather than at a flag that changes nothing. The manifest is read only when there
  is more than one candidate, so the ordinary single-project case does not start
  paying to parse the largest file dbt writes.

## [0.10.0] - 2026-09-02

### Fixed
- **A materialization dbt-plan has no rule for is no longer reported safe.**
  `predict_ddl` handled `table`, `view`, `ephemeral` and `snapshot` by name and
  fell through to the incremental branch for everything else. With no
  `on_schema_change` set, that branch defaulted to `"ignore"` and returned
  `SAFE / NO DDL` — so a `materialized_view`, a Snowflake dynamic table or a
  custom materialization could drop a column and report clean.

  It was reasoning with a setting that does not apply: dbt drives materialized
  views with `on_configuration_change`, not `on_schema_change`, and its docs do
  not say what a column change does to one. `materialized_view` now says exactly
  that instead of a generic "unknown".

  Note the asymmetry this closes. An unrecognized *`on_schema_change`* already
  returned `WARNING`; an unrecognized *materialization* did not.

### Changed
- **An explicitly set `on_schema_change` is still honoured on any
  materialization.** Setting it is an assertion by the author about how their
  materialization behaves, so a custom materialization with `sync_all_columns`
  still earns a `DESTRUCTIVE` verdict rather than being downgraded to a warning.
  Only its *absence* is treated as unknown — silence is not a claim, and reading
  it as `"ignore"` is what produced the false all-clear.

  The verdict carries the column diff either way, since "unknown" with nothing
  attached tells a reviewer nothing about what to look at.

## [0.9.2] - 2026-09-02

### Fixed
- **Files are read and written as UTF-8 rather than as whatever the locale
  happens to be.** `Path.read_text()` with no `encoding=` uses the locale codec,
  which is cp1252 on Windows, so a UTF-8 file containing any non-ASCII byte
  raised `UnicodeDecodeError`. dbt writes `manifest.json` as UTF-8 and compiled
  SQL inherits the model's encoding, so **a project with a column description in
  any language but English could not be read on Windows at all.** Twenty-two call
  sites, covering compiled SQL, `manifest.json`, `.dbt-plan.yml`, `.gitignore`,
  the generated CI workflow and the consumer's `AGENTS.md`.

  `subprocess(text=True)` decodes the same way, so `git` and `dbt compile` output
  is now decoded explicitly too — with `errors="replace"`, since crashing on
  another tool's diagnostic output would be absurd and the values actually
  consumed from it are commit ids and emptiness checks.

  File reads stay strict: a genuinely mis-encoded compiled SQL file still raises,
  and the caller treats that as "could not read", which is the safe direction.

  It hit this project's own guarantees first. `tests/test_invariants.py` reads
  `src/dbt_plan/cli.py` to assert the package imports no database driver, nothing
  that reaches the network, and never passes `shell=True`. That file contains 72
  non-ASCII bytes, so on Windows **the three checks enforcing this tool's security
  premises did not run at all.**

  Found because an outside contributor added a Windows CI matrix while fixing a
  different bug (#38, #32). Nothing in this repository had ever run on Windows.

### Added
- An invariant test asserting that no `read_text` / `write_text` / `open` /
  `subprocess(text=True)` call in `src/` omits its encoding, so this cannot
  return quietly.

## [0.9.1] - 2026-09-01

### Fixed
- **A star carrying `EXCEPT` / `EXCLUDE` / `RENAME` / `REPLACE` is no longer
  resolved as if it were plain.** Regression introduced in 0.8.0 by CTE star
  resolution and widened in 0.9.0 by `ref()` resolution. Each of these modifiers
  changes what the star expands to, so resolving it while ignoring the modifier
  produced the *unmodified* column list — on both sides of the diff, which made
  adding an `EXCEPT(secret)` compare equal and report `SAFE` while dbt drops the
  column. A false all-clear, the one verdict this tool exists to prevent.

  The guard shipped with the feature only checked `except_` on the outer
  expression. For a qualified `a.*` the modifier hangs off the inner `Star` node,
  so it was never seen — and `RENAME` and `REPLACE` were not checked at all, in
  either form. The check is now a whitelist: a star with *any* argument is
  refused, so a modifier this code has not heard of cannot default to being
  ignored.

  Affects 0.8.0 and 0.9.0 only. Before 0.8.0 these all returned `["*"]` and the
  verdict was "review required".

## [0.9.0] - 2026-09-01

### Added
- **`select * from {{ ref(other_model) }}` is resolved through the project's own
  DAG.** After 0.8.0 this was the last shape dbt-plan could not read on the
  measured corpus. The columns are not in the file, but they are not in the
  warehouse either — they are in the other model's compiled SQL, which dbt-plan
  already holds on disk, indexed by the manifest it already parses. Compiled SQL
  names the physical relation rather than the model, so the lookup is keyed on
  the manifest's `relation_name`, with the bare model name as a fallback (dbt
  model names are unique across a project). Resolution is memoized per model and
  refuses on a chain that loops; a referenced model that is itself unreadable
  propagates the refusal instead of yielding a shorter, wrong list.

  On the measured corpus, precise extraction is now **6 of 6** — up from 4 of 11
  before 0.8.0.

  The refusals from 0.8.0 all still apply. A qualified `t.*` over a physical
  table is deliberately not resolved: that needs alias-to-table mapping, which is
  not attempted.
- **A `SELECT *` left behind by `dbt_utils.star()` is explained.** The macro
  introspects the warehouse at compile time, so against a schema where the
  relation does not exist yet — a fresh CI run — it returns nothing and emits a
  bare `*`. dbt-plan reported "review required (SELECT *)" on every such model
  forever with no hint that the cause was upstream of it, which reads as the tool
  being noisy and makes `ignore_models` look like the fix. It now names the macro
  and says to compile against an environment where the relation exists.

### Changed
- `extract_columns` and `extract_cast_types` take an optional `table_columns`
  resolver. Without it, behaviour is exactly as before.

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
