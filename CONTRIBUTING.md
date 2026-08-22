# Contributing to dbt-plan

## The one rule

A false warning is fine. A false safe is not.

If columns cannot be extracted, `extract_columns` returns `None` and the caller
reports a warning. Nothing in this codebase may return `SAFE` on a path where a
column could have been dropped without us noticing. Every other rule here is
negotiable; this one is the reason the tool exists.

## Development setup

```bash
git clone https://github.com/PresentJay/dbt-plan
cd dbt-plan
uv sync --extra test --extra dbt   # or: pip install -e ".[dev,dbt]"
make test                          # expect 1193 passed
```

The `dbt` extra is what makes the four end-to-end tests in `tests/test_dbt_e2e.py`
actually run; they compile a real dbt project with duckdb. Without it you get
`1189 passed, 4 skipped`, which is fine for most changes but means you are not
exercising the `dbt-plan run` pipeline. Run `pytest -rs` to see why anything
skipped — the reasons name the specific missing piece.

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
make test-cov      # coverage report (threshold 85%, currently 98%)
make lint          # ruff check
make format        # ruff format
pytest -k sync     # by name pattern
pytest -rs         # show skip reasons
```

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

If you add or rename a job in `.github/workflows/ci.yml`, update the required
check list in that script too. A required check that names a job which does not
exist blocks every merge, permanently.

## Code of Conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
