# dbt-plan

Static analysis tool that warns about risky DDL changes before `dbt run`.

Like `terraform plan` for dbt. Runs on compiled SQL — you need a `dbt compile` (which connects), but from there dbt-plan works on files alone. Works with any warehouse (Snowflake, BigQuery, Redshift, Postgres, etc.).

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

It does NOT execute anything, connect to any warehouse, or simulate `dbt run`. It reads files, compares them, and warns you.

## Quick Start

```bash
pip install dbt-plan

# In your dbt project directory:
dbt-plan run               # One command: compile baseline → compile current → check
```

That's it. `dbt-plan run` handles `dbt compile`, snapshotting, and checking automatically — so it needs whatever credentials your `dbt compile` normally needs. If you can't compile locally, run it in CI (see below) and dbt-plan reads the artifacts there.

### More commands

```bash
dbt-plan init              # Generate .dbt-plan.yml config + update .gitignore
dbt-plan stats             # Analyze project readiness
dbt-plan ci-setup          # Generate GitHub Actions workflow
dbt-plan agent-setup       # Write AGENTS.md so coding agents know to run the check
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
| CI exit codes + structured output | `full_refresh` mode judgment |

**Design principle**: false warnings are OK, false safe is never OK.

## When to use it

dbt-plan answers a narrower question than the warehouse-connected tools (Recce,
SQLMesh, data-diff) and costs nothing to run, so it works as the cheap gate in
front of them — including on fork pull requests, where they cannot run at all.
See [use cases](docs/use-cases.md) for the comparison, real timings, and what it
gets wrong.

## Deliberately Not Planned

Two ideas that look useful but contradict what this tool is:

| Idea | Why not |
|------|---------|
| INFORMATION_SCHEMA query | Requires a warehouse connection. dbt-plan reads files and nothing else — that is what makes it safe to run anywhere, including on a fork's PR. |
| Column type detection (`ALTER TYPE`) | Compiled SQL only reveals a type where an explicit CAST exists, and deciding whether a type *changed* needs the warehouse's current type — the same connection problem. |

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

## CI Integration (GitHub Actions)

```yaml
name: dbt-plan
on:
  pull_request:
    paths: ['models/**', 'macros/**']

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }

      - run: pip install uv && uv sync
      - run: pip install dbt-plan

      # Compile and snapshot base branch
      - run: |
          git checkout ${{ github.event.pull_request.base.sha }}
          dbt compile
          dbt-plan snapshot

      # Compile current and check
      - run: |
          git checkout ${{ github.event.pull_request.head.sha }}
          dbt compile
          dbt-plan check --format github >> $GITHUB_STEP_SUMMARY

      # Block destructive changes (exit 1)
      - run: dbt-plan check
```

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

**Good first issues:**
- Add compiled SQL fixtures in `tests/fixtures/` for edge cases (UNION, subqueries, etc.)
- Improve error messages for common mistakes

**Medium issues:**
- `ddl-reviewed` label override — escape hatch for intentional destructive changes
- INFORMATION_SCHEMA integration — query warehouse for SELECT * resolution

**Design decisions:** See [docs/design-notes.md](docs/design-notes.md).

## Supported

- dbt-core 1.7+
- Any warehouse: Snowflake, BigQuery, Redshift, Postgres, DuckDB, etc. (`--dialect`)
- Python 3.10+
- CTE, UNION ALL, QUALIFY, window functions, VARIANT access

## License

Apache-2.0
