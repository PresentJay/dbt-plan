# Contributing to dbt-plan

Already opened a PR? See [Checking CI on your pull request](#checking-ci-on-your-pull-request)
for automatic status updates and comment commands.

## The one rule

A false warning is fine. A false safe is not.

If columns cannot be extracted, `extract_columns` returns `None` and the caller
reports a warning. Nothing in this codebase may return `SAFE` on a path where a
column could have been dropped without us noticing. Every other rule here is
negotiable; this one is the reason the tool exists.

## Development setup

**You never need warehouse credentials to work on dbt-plan.** That is unusual for a
tool in the dbt ecosystem, and it is a direct consequence of what the tool is: it
reads compiled SQL and `manifest.json` from disk and never connects to anything.
Fixtures are checked in, the end-to-end tests compile against duckdb in memory, and
`tests/test_invariants.py` fails the build if a warehouse driver or a network
library is ever imported. Clone, `uv sync`, run the tests.

```bash
git clone https://github.com/PresentJay/dbt-plan
cd dbt-plan
uv sync --extra test --extra dbt   # or: pip install -e ".[dev,dbt]"
make test                          # the full suite
```

The `dbt` extra enables `tests/test_dbt_e2e.py`, which compiles real projects
with DuckDB. Its `run` cases use a real Git repository and real `dbt compile`
to check repeated execution after `init`, destructive findings, and recovery
from baseline/current compile failures, including staged and untracked work.
Without the extra, this integration tier skips. Run `pytest -rs` to see why
anything skipped; the required `integrations` CI job treats a skip as a failure.

If those tests skip complaining that `dbt_plan is not importable`, your virtualenv
lost the editable install:

```bash
uv pip install --reinstall-package dbt-plan -e .
```

## Making a change

1. Write the failing test first
2. Write the smallest code that passes it
3. `make test` and `make lint`
4. Commit with a message that says what breaks without the change

Most valuable contribution, by a wide margin: a compiled SQL pattern that
dbt-plan reads wrong. That is usually a fixture plus a line of parsing logic.

### Checking CI on your pull request

A bot maintains one **CI status** comment on PRs to `PresentJay/dbt-plan`.
This is repository contributor tooling, not part of `dbt-plan ci-setup` or the
GitHub Action installed in your own dbt project. It shows the current commit,
whether CI is waiting for approval or running, and the result with links to failed
jobs and steps. You do not need to ask a maintainer to interpret an approval wait
as a test failure.

| Comment | Who can use it | What happens |
|---|---|---|
| `/ci` | PR author or a maintainer with write access | Refreshes the status comment. |
| `/ci retry` | PR author or a maintainer with write access | Retries failed jobs from an already executed CI run for the current commit. |
| `/ci approve <full commit SHA>` | Maintainer with write access | Approves the current CI run if GitHub is holding it for execution approval. Copy the command from the bot comment after reviewing the changes. |

For a code or test failure, fix the problem and push a commit; CI starts for the new
commit automatically, subject to GitHub's external-contributor approval policy.
Use retry for transient infrastructure failures. Retries have a five-minute
cooldown and stop after three total run attempts. Running, successful, cancelled,
or approval-waiting runs are not restarted by `/ci retry`; a maintainer can inspect
the run in Actions when manual intervention is needed. A retry never approves an
unreviewed external commit. Execution approval does not approve or merge the PR.

Post commands as new comments, not edits or code blocks. Status refreshes happen
when the PR changes or CI starts/finishes; this is event-driven, not a live job log.
Commands queued together are reconciled using the latest authorized command.

The feedback workflow only runs code from the repository's default branch and
uses GitHub's API to control the existing `CI` workflow. It never executes fork
code with its write token. Its tests use Node's built-in runner, without npm
dependencies: `node --test .github/scripts/ci-feedback.test.cjs`.

### Adding a SQL fixture

Drop a `.sql` file in `tests/fixtures/` with the expected column list in a header
comment, then assert on it from `tests/test_columns.py`:

```sql
-- Pattern: lateral flatten over a VARIANT array
-- Expected: ["order_id", "item_sku", "item_qty"]
SELECT ...
```

Use the invented bookstore domain the other fixtures use — orders, customers,
books, publishers. Never paste schema from a real warehouse; this package is
published to PyPI, and a published artifact cannot be taken back.

## Using an AI assistant

Allowed, with two conditions.

**Run it and understand it.** If you cannot explain why the change is correct
without the assistant, it is not ready. Review time is the scarce resource here,
and a patch that has to be verified from scratch costs more than it saves.

**Anything non-trivial should target an open issue first.** Typos, a fixture, a
doc fix — open the pull request. A behaviour change, a new flag, a refactor —
comment on an issue, or open one, before writing code. Issues labelled
[good first issue](https://github.com/PresentJay/dbt-plan/labels/good%20first%20issue)
are already scoped and count as agreed.

Unsolicited large pull requests are closed without review. Not out of hostility
to the tooling — the same rule applies to hand-written ones. It is that this
project's rules are unusual (never return SAFE when unsure; no warehouse
connection; sqlglot is the only dependency), and generated code tends to be
plausible in a way that quietly violates exactly those.

Some of that is enforced mechanically in `tests/test_invariants.py` — a
warehouse driver import, a network import, or `shell=True` fails CI rather than
waiting for review. Those checks exist to save you a round trip, not to catch
you out.

## Architecture

```text
src/dbt_plan/
├── columns.py      # SQLGlot column extraction (multi-dialect)
├── config.py       # .dbt-plan.yml + env var configuration
├── predictor.py    # DDL prediction rules + cascade analysis
├── manifest.py     # manifest.json parsing, node index, downstream BFS
├── diff.py         # compiled SQL directory comparison with caching
├── formatter.py    # text (color) / GitHub markdown / JSON output
├── stash.py        # git stash lifetime for `dbt-plan run`
└── cli.py          # CLI: snapshot, check, init, stats, run, ci-setup
```

Data flow: `diff_compiled_dirs` → `extract_columns` → `predict_ddl` →
`find_downstream_batch` → `format_text/github/json`

See [docs/design-notes.md](docs/design-notes.md) for why the pieces are shaped
this way — particularly why there is no warehouse connection, and where the DDL
prediction table comes from.

## Things worth knowing before you change them

- **sqlglot is the only runtime dependency, and its version is a correctness
  boundary.** Below 28.0.0, `SELECT * EXCEPT(col)` parses as plain `*`, which
  hides a dropped column. The `minimum-deps` CI job pins the declared floor for
  this reason; do not widen it without re-running that job.
- **`SELECT *` returns `["*"]`**, and the manifest column definitions are the
  fallback. It never silently expands to a guess.
- **Helpers in `cli.py` exit the process rather than returning.** If you call one
  while holding recoverable state, wrap it — `dbt-plan run` lost users'
  uncommitted work this way once, which is what `stash.py` now prevents.
- **`enabled: false` models are excluded from the index**, so they do not show up
  as removed.

## Testing

```bash
make test          # everything, verbose
make test-quick    # faster, quieter
make test-cov      # in-process coverage report (threshold 85%)
make lint          # ruff check
make format        # ruff format
pytest -k sync     # by name pattern
pytest -rs         # show skip reasons
```

The coverage floor measures Python executed in the pytest process. It does not
measure the CLI subprocesses in the real-dbt E2E tier. Those tests are enforced
separately by the required `integrations` job; a coverage percentage is not a
measure of end-to-end workflow or adapter coverage.

Generated-workflow tests execute the actual shell blocks under Bash. Installation
tests use real uv with offline fixture wheels to verify environment selection and
lock preservation; they need uv on a POSIX host. The required integrations job
installs uv and rejects skips in these suites. Real-dbt E2E additionally compiles
both Git revisions and checks summary/gate behavior for safe, warning, and
destructive changes. Regenerate `examples/ci-workflow/dbt-plan.yml` from
`_CI_WORKFLOW` whenever the template changes; an equality test prevents drift.

Real-dbt selection tests cover upstream/downstream operators, unions, known
unchanged models, invalid terms, explicit model versions and `defined_in` aliases.
See [selection semantics](docs/selection.md) before extending the grammar; silently
narrowing an unsupported selection can hide destructive findings.

### Compiled fixtures and Action integration

`tests/test_compiled_fixture.py` compares a fresh compile with the committed
`tests/dbt_project/target/compiled/` SQL and meaningful `manifest.json` fields:
model identity/configuration, raw and compiled code, columns, lineage, and test
fixtures. Runtime timestamps, invocation IDs, absolute compiled paths, and
adapter macro internals are excluded. Negative controls verify that SQL, schema,
configuration, and dependency drift are detected.

Use the same fixture toolchain as the required integrations job:

```bash
uv pip install dbt-core==1.11.7 dbt-duckdb==1.10.1
uv run --no-sync pytest tests/test_compiled_fixture.py -q
# To regenerate after an intentional source change:
(cd tests/dbt_project && uv run --no-sync dbt compile --profiles-dir . --no-partial-parse)
```

Review and commit only the generated SQL and manifest changes needed by the
fixture. A toolchain upgrade needs an explicit pin change and a fixture review.
The version assertion deliberately fails when another installed dbt version is
used; a missing optional dependency skips locally and fails in required CI.

`tests/test_action_e2e.py` executes the actual composite shell blocks with real
Git, DuckDB compilation and the CLI. The required integrations job additionally
invokes `uses: ./` against five real projects: safe, warning, destructive, failed
baseline compile, and failed current compile. It installs the current checkout's
built wheel from a local wheel directory, rather than testing an older PyPI
release. Assertions check the revisions compiled, reports, outputs and job
policy; `continue-on-error` permits inspecting expected failures, and the following
verification step fails CI on any mismatch. Compilation errors must fail even
with `fail-on: never`. PostgreSQL parser inference and explicit overrides are
also exercised by the shell/CLI tests, without connecting to PostgreSQL.

For performance changes, run the [CLI benchmark](docs/performance.md). The unit
smoke test validates its workloads and result schema; elapsed-time numbers are
measurements, not cross-machine CI thresholds.

## Where to start

Issues labelled [good first issue](https://github.com/PresentJay/dbt-plan/labels/good%20first%20issue)
are kept scoped to one file with a clear finish line. If none are open, a fixture
for a SQL pattern from your own project is always welcome — that is how parsing
gaps get found.

## Repository settings

Branch protection, the Actions allowlist, and SHA-pinning enforcement live in
the GitHub API, not in this tree — cloning does not bring them along and
recreating the repository loses them. `scripts/apply-repo-settings.sh` puts them
back, and `--check` reports the current state without changing anything.

Branch protection applies to everyone including maintainers, so every change
reaches `main` through a pull request with green CI. Tag pushes are exempt, so
releases are unaffected, and no review is required, so a PR can be merged as
soon as the checks pass.

Required checks include lint, minimum dependencies, every Linux and Windows
Python matrix job, dbt/MCP integrations, and built-package validation. The
Linux matrix alone does not exercise the optional dependencies or installed
artifacts, and Windows failures can expose filesystem and encoding regressions.

If you add or rename a job in `.github/workflows/ci.yml`, update the required
check list in that script too. `tests/test_repo_settings.py` compares the
expanded workflow job names with the list, so an omitted check or a nonexistent
name fails CI. After changing the list, verify the live branch protection too;
merging the script does not apply repository settings automatically.

## If it was useful

A star is the only signal this project gets that is not automated. Clones run at
roughly twenty times the page views, and downloads spike on every release from
scanners and mirrors — neither number says a person decided anything. Stars are
also the gate on several tool directories, which use them as a proxy for whether
anyone actually uses a thing.

So if dbt-plan caught something for you, or you fixed something in it, a star is
genuinely the most useful thing you can leave behind. No obligation, and please
do not star it if you have not used it — an inflated count would cost more than
it is worth, because it is the one number here still worth reading.

## Code of Conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
