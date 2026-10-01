# Adapter validation live databases

dbt-plan's prediction rules are adapter dialect guesses (sqlglot) on top of a
synthetic execution trace. The audit rules and DDL predictions sit on top of two
assumptions that only a live engine can check:

1. the column evolution **dbt itself** performs on an existing relation (the
   `on_schema_change` matrix), and
2. whether dbt materially refuses to run what dbt-plan predicted — a destructive
   `DROP COLUMN` dbt says it will do is still a claim if dbt then refuses to.

This directory's tests (`tests/test_pg_e2e.py`, fixture library
`tests/pg_harness.py`) run the audit rules against a **real PostgreSQL server**
and assert the actual dbt run result, columns executed, and row counts that dbt
left behind.

## What the harness covers

| Test class | What it verifies against a real server |
|---|---|
| `TestAudit24RulesAgainstRealPostgres` | The 8 `on_schema_change` x `add`/`remove` matrix of issue #24: exit codes, final columns, and retained rows after a real `dbt run` |
| — ephemeral CTE | `{{ ref() }}` inlining of an ephemeral model used as a CTE |
| — materialization change | `view` -> `table` predicted and executed |
| — macro-only edit | a macro change that moves compiled SQL, with no model file touched |
| `TestAQuotedAliasAndCaseRoundTrip` | an `alias: Fct_Orders` with mixed case survives a column drop |
| `TestContractEnforcementOnPostgres` | enforced contracts: `check` exit 2 when a declared column is dropped or an undeclared column appears, and real dbt refuses to build to a mismatch |

Tests run **only when the toolchain is installed**; otherwise they are skipped
(the default `.venv` and the `test-windows` CI job do not carry dbt). Failure
modes that could someday produce a false "safe" are assertions, not reports. A
prediction divergence on a live server is **recorded in this document**, never
"fixed" by tuning the rule to the engine (see [`design-notes.md`](design-notes.md)).

## Toolchain (pinned)

| Component | Version | Why / where |
|---|---|---|
| PostgreSQL | **16.2** (embedded, pgserver) | `pgserver==0.1.4` bundles PostgreSQL 16.2 on Windows and Linux. This is the *embedded* validation target. |
| PostgreSQL | 17+ (external) | Recommended for the continuous/server case; drive it through env vars below. dbt-core's current support matrix always targets the latest stable server. |
| dbt-core | 1.11.x | The release dbt-plan CI builds against. |
| dbt-postgres | 1.11.x | Matches dbt-core. |
| pgserver | 0.1.4 | Embedded PostgreSQL for tests, no admin required. |
| psycopg2-binary | 2.9.x | The harness's client to read back `target_schema` tables. |

## Local run

```bash
uv venv .venv-pg --python 3.12
.venv-pg\Scripts\python.exe -m pip install -e . dbt-core==1.11.7 dbt-postgres==1.11.0 pgserver==0.1.4 psycopg2-binary==2.9.13
.venv-pg\Scripts\python.exe -m pytest tests/test_pg_e2e.py -q
```

> Windows shells: use `Scripts\python.exe` inside the venv; on POSIX shells the
> path is `.venv-pg/bin/python`.

Installing the editable package into the venv is intentional: `pyproject.toml`
deliberately does not list dbt adapters as dependencies (dbt-plan must stay a
single lightweight dependency of a dbt project; see `packaging.md`). The pinned
lines above are the lockfile-equivalent for running these tests.

## The two ways to supply PostgreSQL

The harness prefers an **external** server when every `DBT_PG_*` variable is set,
otherwise it spawns **embedded pgserver** on a random port. Every test runs
against its own disposable schema, never the shared database, and cleans it up.

| Env var | Meaning |
|---|---|
| `DBT_PG_HOST` | host or `127.0.0.1` |
| `DBT_PG_PORT` | port |
| `DBT_PG_USER` | login user |
| `DBT_PG_PASSWORD` | password (empty for pgserver trust auth) |
| `DBT_PG_DBNAME` | database to connect to |

The embedded path pins the server version to what `pgserver` ships (16.2); the
external path is the one to point at PostgreSQL 17 to validate against the newer
server. Behavior differences between the two servers are exactly what this
harness exists to surface: record them here.

## Measured behavior (PostgreSQL 16.2, dbt-core 1.11.7)

Observed on Windows (x86_64) and Linux/CI for the matrix:

- `on_schema_change` column **add** and **remove** parity with the DuckDB
  matrix. `check` exit codes: **2** for `ignore`/`fail` (any change) and for
  `append_new_columns` + remove; **1** for `sync_all_columns` + remove (a real
  `DROP COLUMN` is surfaced in the report and stdout); **0** otherwise. The
  real `dbt run` refuses only for `fail` and for `ignore` + remove; in every
  other cell it completes, and the harness asserts the final columns and row
  counts dbt left behind (2 rows on a successful incremental append, 1 after a
  destructive remove).
- `fail` refuses to build and `ignore` keeps the old relation when a column is
  **removed**; `append_new_columns` and `sync_all_columns` apply the migration
  and keep the data. `sync_all_columns` emits a real `DROP COLUMN` and the
  column does not come back. `append_new_columns` leaves the pre-existing row's
  new column empty (`NULL`), never backfilled.
- Enforced contracts (`contract.enforced: true`) require `data_type` for every
  contracted column on PostgreSQL (`table` materialization), and forbid the
  incremental `on_schema_change: sync_all_columns` combination that dbt 1.11
  rejects at validation time. Both are dbt-level constraints the predictor does
  not (and must not) invent.

## Bugs found by this harness (fixed in this PR)

The harness immediately uncovered two Windows-only bugs in dbt-plan's target
reader, both invisible to CI (the real-dbt e2e job runs on `ubuntu`; the
`test-windows` job runs pytest without dbt, so the e2e suite skips there):

1. **Windows backslash paths.** dbt writes `original_file_path` with the host's
   `\` separator. dbt-plan split manifests with `PurePosixPath(...).parts`, so
   `'models\\fct_orders.sql'` was read as a single directory named
   `models\fct_orders.sql`, `_manifest_layout` reported no model directories,
   and snapshot/check died with "No compiled SQL found ... Run 'dbt compile'
   first" even though the compile was present. Fixed by splitting on both
   separators (`dbt_path_parts`).
2. **CRLF line endings.** dbt embeds `raw_code`/`macro_sql` in the manifest with
   the file's own `\r\n`, while Python's `read_text()` universal-newline
   translation returns `\n`. The provenance comparisons (content and macro
   containment) never matched, so every CRLF source file (i.e. every file on
   Windows) was flagged "stale". Fixed by storing authored text through
   universal-newline normalization.

Both are regression-tested in `tests/test_model_paths.py` and
`tests/test_stale_target.py`.

## Known divergence / notes

- **Snapshot checksums on Windows.** `source_snapshots` digests are taken from
  dbt's own `sha256` checksum over the *bytes* dbt read. Whether that digest is
  line-ending normalized is adapter-dependent and is **not exercised** by this
  suite (snapshots are outside the matrix). The closer verification is a
  live-snapshot run; do that before relying on `snapshot-status` parity on
  Windows.
- **Postgres `materialized_view`** stays conservative-refuse (no DDL estimation
  yet). It is listed per the "unsupported features are a warning, not a guess"
  contract.
- **`ignore` + remove keeps data.** Postgres keeps the existing rows when the
  relation is left untouched; the harness asserts the DuckDB-matched row counts.
- **pgserver base version.** Pin `pgserver==0.1.4`. It bundles PostgreSQL 16.2;
  never assume the bundle version upstream — `make_project` records it via the
  harness's `_POSTGRES_VERSION`.

## Future server matrix (out of scope of the pinned harness)

External servers added without changing code: set `DBT_PG_*` and run. A future
CI job can run the suite twice for `16.2` (embedded) and `17` (external). Adapter
licenses matter for a third database; this harness chose PostgreSQL because
dbt-postgres is MIT-licensed and free to run in CI (see the parent issue on
adapter coverage).