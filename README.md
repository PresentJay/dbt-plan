# dbt-plan

Static analysis tool that warns about risky DDL changes before `dbt run`.

Like `terraform plan` for dbt, and used the same way: you run it **before** the thing
that changes your warehouse, not only in CI afterwards.

Runs on compiled SQL. It reads files and nothing else, so it works with any warehouse —
Snowflake, BigQuery, Redshift, Postgres, DuckDB — through one code path.

## What It Looks Like

```
$ dbt-plan check

dbt-plan -- 2 model(s) changed

DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  DROP COLUMN  shipping_info
  DROP COLUMN  billing_info
  ADD COLUMN   shipping_city
  Downstream: dim_customers, fct_orders (2 model(s))
  >> BROKEN_REF  fct_orders: references dropped column(s): shipping_info

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE

dbt-plan: 2 checked, 1 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

## What It Does

dbt-plan analyzes compiled SQL diffs to catch dangerous schema changes at PR time:

- **Column changes**: detects ADD/DROP COLUMN from SQL diff
- **Risk assessment**: judges safety based on materialization x on_schema_change rules
- **Cascade analysis**: finds downstream models that reference dropped columns
- **Config changes**: detects materialization or on_schema_change policy changes
- **Type changes**: compares explicit `CAST` types between revisions
- **`SELECT *` resolution**: reads the columns from the CTEs of the same statement, and follows a `ref()` into the referenced model's compiled SQL

It does NOT execute anything, connect to any warehouse, or simulate `dbt run`. It reads files, compares them, and warns you.

## Quick Start

```bash
pip install dbt-plan
dbt-plan run               # compile baseline → compile current → check
```

`dbt-plan run` does the whole thing in one command, and needs whatever credentials your
`dbt compile` normally needs.

### The loop it is built for

Once you have a baseline, the inner loop is a single sub-second command. Edit a model or a
macro, recompile, and see what `dbt run` would do — *before* running it:

```bash
dbt-plan snapshot          # once, on the revision you are changing from
                           # ... edit models, edit macros ...
dbt compile && dbt-plan check
```

Measured on a project of 3 models, median of 3 runs:

| step | time |
|---|---|
| `dbt compile` (Fusion) | 1.8 – 3.8 s |
| **`dbt-plan check`** | **0.11 s** |
| `dbt-plan snapshot` | 0.10 s |

200 models, every one of them changed: **0.48 s**. The compile is the cost, and you were
compiling anyway — dbt-plan itself is fast enough to sit in the edit loop rather than at
the end of it.

### Working with a coding agent

An agent editing models cannot eyeball a diff and hesitate. Give it the check and the
reasons behind it:

```bash
dbt-plan agent-setup       # writes dbt-plan guidance into your AGENTS.md
dbt-plan check --format json
```

The guidance leads with what an agent most often gets wrong: adding a model to
`ignore_models`, or downgrading `on_schema_change` from `sync_all_columns` to `ignore`,
silences a real finding without making the change safe.

Or give it the check as an MCP tool:

```bash
pip install 'dbt-plan[mcp]'
dbt-plan-mcp                # stdio MCP server exposing `plan` and `snapshot`
```

`plan` returns the verdict, the per-model operations, and — separately — a `refusals`
list naming everything dbt-plan declined to judge. That separation is the point: a person
reading "safe" may still glance at the diff, an agent reading it proceeds, so a
non-empty `refusals` must never be collapsed into the verdict.

The server is a separate package from the analysis core. The core is offline and
synchronous by design and `tests/test_invariants.py` fails the build on an `asyncio` or
network import anywhere inside it; an MCP server is both, so keeping them apart is what
keeps that guarantee provable.

### More commands

```bash
dbt-plan init              # Generate .dbt-plan.yml config + update .gitignore
dbt-plan stats             # Analyze project readiness
dbt-plan ci-setup          # Generate GitHub Actions workflow
dbt-plan check --format github   # GitHub markdown output
dbt-plan check --format json     # JSON for CI pipelines
dbt-plan check --select model1   # Check specific model only
```



## Scope

dbt-plan is a **static analysis warning tool**, not a runtime simulator.

| In scope | Out of scope |
|----------|-------------|
| Column ADD/DROP detection from compiled SQL | `dbt run` simulation |
| materialization × on_schema_change risk rules | Warehouse connection |
| Cascade broken ref / build failure analysis | `seed` / `source` change detection |
| Config change detection (materialization, osc) | `pre_hook` / `post_hook` DDL analysis |
| Explicit `CAST` type changes | Type changes on uncast columns |
| `SELECT *` resolved through CTEs and `ref()` | `SELECT *` over a source or a raw table |
| CI exit codes + structured output | `full_refresh` mode judgment |

**Design principle**: false warnings are OK, false safe is never OK.

## When to use it

dbt-plan answers a narrower question than the warehouse-connected tools (Recce,
SQLMesh, data-diff) and costs nothing to run, so it works as the cheap gate in
front of them — and on the Fusion engine, which compiles without a warehouse
connection, that includes fork pull requests where they cannot run at all.
See [use cases](docs/use-cases.md) for the comparison, real timings, and what it
gets wrong.

## Deliberately Not Planned

Ideas that look useful but contradict what this tool is:

| Idea | Why not |
|------|---------|
| INFORMATION_SCHEMA query | Requires a warehouse connection. dbt-plan reads files and nothing else, which is what lets it run wherever its input exists — including a fork's pull request, once the project compiles on Fusion. |
| Type changes on columns with no explicit `CAST` | The type is whatever the warehouse assigned, so seeing a change would mean asking it. Columns that *are* cast explicitly on both sides are compared — see below. |

## DDL Prediction Rules

| Materialization | on_schema_change | Predicted DDL | Safety |
|-----------------|------------------|---------------|--------|
| table | any | `CREATE OR REPLACE TABLE` | SAFE |
| view | any | `CREATE OR REPLACE VIEW` | SAFE |
| ephemeral | any | (no physical object) | SAFE |
| snapshot | any | `REVIEW REQUIRED` | WARNING |
| incremental | ignore | no DDL | SAFE |
| incremental | fail | build failure | WARNING |
| incremental | append_new_columns | `ADD COLUMN` only | SAFE |
| incremental | sync_all_columns | `ADD + DROP COLUMN` | DESTRUCTIVE if columns removed |
| any | (model removed) | `MODEL REMOVED` | DESTRUCTIVE |
| any | (unknown osc) | `UNKNOWN on_schema_change` | WARNING |
| materialized_view / custom | (none set) | `UNKNOWN materialization` | WARNING |
| materialized_view / custom | (osc set) | follows the incremental rules | per osc |

## CI Integration (GitHub Actions)

```yaml
name: dbt-plan
on:
  pull_request:
    paths: ['models/**', 'macros/**', 'dbt_project.yml']

jobs:
  plan:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    env:
      # Whatever your profiles.yml reads. `dbt compile` connects; dbt-plan does not.
      SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
      SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
      SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the base revision has to be in the clone
          persist-credentials: false
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install uv && uv sync

      - uses: PresentJay/dbt-plan@v1
```

Keep the `pull_request` trigger. Never switch it to `pull_request_target` — `dbt compile`
runs Jinja and macros written in the pull request, so that would hand your warehouse
credentials to code from any fork.

| Input | Default | |
|---|---|---|
| `compile-command` | `dbt compile` | Runs twice, once per revision. |
| `base-ref` | the PR base | The revision to compare against. |
| `project-dir` | `.` | dbt project directory. |
| `dialect` | `snowflake` | sqlglot dialect for parsing compiled SQL. |
| `version` | latest | Pin a dbt-plan release. |
| `fail-on` | `destructive` | Or `warning`, or `never`. |
| `summary` | `true` | Write the report to the job step summary. |

Outputs `verdict` (`safe` / `destructive` / `warning`), `exit-code`, and `report`
(path to the JSON report), so a later step can comment on the PR or open a ticket.

For a workflow you own outright rather than a wrapped action, `dbt-plan ci-setup`
generates one with the credential wiring and least-privilege notes inline. Details in
[docs/ci-integration.md](docs/ci-integration.md).

## How It Works

```mermaid
flowchart TD
    A[dbt-plan snapshot] --> B[Save compiled SQL + manifest.json]

    C[dbt-plan check] --> D[diff_compiled_dirs]
    D --> E[base compiled SQL]
    D --> F[current compiled SQL]
    E --> G[extract_columns]
    F --> H[extract_columns]
    G --> I[base columns]
    H --> J[current columns]
    I --> K[column diff]
    J --> K
    K --> L[predict_ddl + manifest config]
    L --> M{Safety?}
    M -->|SAFE| N[exit 0]
    M -->|WARNING| O[exit 2]
    M -->|DESTRUCTIVE| P[exit 1 — block merge]
    L --> Q[find_downstream]
    Q --> R[format_text / format_github]
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, TDD workflow, and coding rules.

### Architecture

```
src/dbt_plan/
├── columns.py      # SQLGlot column extraction (multi-dialect)
├── config.py       # .dbt-plan.yml + env var configuration
├── predictor.py    # DDL risk assessment rules + cascade analysis
├── manifest.py     # manifest.json parsing + downstream BFS
├── diff.py         # compiled SQL directory comparison
├── formatter.py    # text / GitHub markdown / JSON output
└── cli.py          # CLI: snapshot, check, init, stats, run, ci-setup
```

### How to Contribute

**Where to start:** the [open issues](https://github.com/PresentJay/dbt-plan/issues),
particularly those labelled [good first issue](https://github.com/PresentJay/dbt-plan/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
Each one says what it is, how it was found, and what has to be decided before code.

**Design decisions:** See [docs/design-notes.md](docs/design-notes.md).

## Supported

- dbt-core 1.7+, and the dbt Fusion engine (verified against `2.0.0-preview.218`)
- Any warehouse: Snowflake, BigQuery, Redshift, Postgres, DuckDB, etc. (`--dialect`)
- Python 3.10+
- CTE, UNION ALL, QUALIFY, window functions, VARIANT access

## License

Apache-2.0
