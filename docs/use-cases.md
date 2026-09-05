# When to use dbt-plan

Every output on this page was produced by running the tool, not written by hand.

## What it is not

dbt-plan does not tell you your numbers changed. It cannot: it never queries
anything. If you need to know that yesterday's revenue moved, you want a data
diff, and the tools below do that properly.

| Tool | Answers | Needs a warehouse |
|------|---------|-------------------|
| [Recce](https://reccehq.com) | did the values change — row, profile, top-k diffs | yes |
| [SQLMesh plan](https://sqlmesh.com) | column-level impact, blue-green deploys | yes |
| dbt Fusion / VS Code compare | how your edit affects data in your account | yes |
| data-diff, dbt-audit-helper | row-level comparison between two relations | yes |
| **dbt-plan** | **will this DDL drop a column, and what breaks downstream** | **no** |

The question dbt-plan answers is narrower. In exchange it costs nothing, takes
under a second, and runs in places the others cannot run at all. Use it as the
cheap gate in front of the expensive one: dbt-plan on every pull request, a data
diff when it flags something worth looking at.

## What it catches

```
dbt-plan -- 4 model(s) changed

DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  ADD COLUMN  billing_method
  ADD COLUMN  shipping_city
  DROP COLUMN  billing_info
  DROP COLUMN  shipping_info
  Downstream: dim_customers, fct_daily_sales (2 model(s))
  >> BROKEN_REF  fct_daily_sales: references dropped column(s): shipping_info

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE

SAFE  fct_daily_sales (incremental, append_new_columns)
  ADD COLUMN  total_sales

dbt-plan: 4 checked, 3 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

Exit code 1. The interesting line is the last one under `int_order_enriched`:
`fct_daily_sales` still references a column that is about to disappear. Nothing
about that is visible in the diff of either model on its own.

Reproduce it with `bash examples/sample-project/run-example.sh`.

---

## Four situations where nothing else fits

### 1. A pull request from a fork, on Fusion

A contributor forks your dbt project and opens a pull request. Warehouse
credentials are not available to that workflow, and should not be — that is the
whole point of the restriction.

Every warehouse-connected tool is unavailable here. Whether dbt-plan is depends
on which engine compiles the project, and the difference is not cosmetic.

**On dbt Core this does not work.** dbt-plan itself never connects, but producing
its input does: `dbt compile` connects, and a fork pull request has no secrets to
connect with. `dbt-plan ci-setup` generates a workflow that says so plainly rather
than failing later with a driver error.

**On the Fusion engine it does.** Fusion compiles without a warehouse connection.
Verified against a Snowflake profile pointing at an account that does not exist:

```
dbt-fusion 2.0.0-preview.218
Finished 'compile' successfully for target 'dev' [3.1s]
Processed: 3 models | 3 total | 3 success
```

Three models compiled, `manifest.json` written, nothing dialled out. So this runs
with no credentials at all:

```yaml
on: pull_request          # not pull_request_target — no secrets, deliberately

jobs:
  plan:
    runs-on: ubuntu-latest
    permissions:
      contents: read      # no secrets needed, so none are granted
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - run: pip install dbt-plan

      - run: |
          git checkout ${{ github.event.pull_request.base.sha }}
          dbt compile && dbt-plan snapshot
          git checkout ${{ github.event.pull_request.head.sha }}
          dbt compile
          dbt-plan check --format github >> $GITHUB_STEP_SUMMARY

      - run: dbt-plan check   # exit 1 blocks the merge
```

#### The boundary: introspective macros

A macro that queries the warehouse — `run_query`, `get_column_values`,
`adapter.get_columns_in_relation` — needs a connection even under Fusion, and the
models using it fail to compile:

```
[error] [DbConnectionFailed (dbt1300)]: [Snowflake] 261004 (08004): failed to auth
  --> models/marts/introspective.sql:2:22
Summary: 4 total | 3 success | 1 error
```

Only those models fail; the rest still compile. dbt-plan does not quietly analyse
what is left — it names what is missing:

```
DESTRUCTIVE  fct_orders (incremental, sync_all_columns)
  DROP COLUMN  store_id

WARNING: The compile is incomplete -- 1 model(s) in the manifest have no compiled
         SQL: introspective
```

So on a fork pull request you get a real verdict on everything that compiled, and
an explicit statement about everything that did not. That is the same boundary
`ci-setup` already names when it explains least privilege: `dbt compile` reads no
tables unless your macros introspect.

### 2. A required check on every pull request

Required checks have to be fast and they have to be reliable, or people start
asking for merge overrides.

Measured on a generated 200-model project, 20 of them dropping a column, chained
lineage, three consecutive runs:

```
run1: real 0.41
run2: real 0.27
run3: real 0.25
```

No warehouse means nothing to be slow, nothing to be down, nothing to rate-limit,
and no query bill for running it on all 40 pull requests you opened this week.

### 3. Before you push

```bash
dbt-plan run     # compile baseline, compile current, check — one command
```

It stashes uncommitted work to build the baseline and restores it afterwards. If
anything fails in between, the restore still runs and tells you where your
changes are.

### 4. Reviewing a change you cannot run

Reviewers frequently lack access to the warehouse the author used. The GitHub
output is designed to make the risk legible to someone in that position:

```markdown
🔴 **DESTRUCTIVE** `int_order_enriched` (incremental, sync_all_columns)
- `DROP COLUMN` shipping_info
- Downstream: dim_customers, fct_daily_sales (2 model(s))
- 🔴 **BROKEN_REF** `fct_daily_sales`: references dropped column(s): shipping_info
```

---

## What it will get wrong

Worth knowing before you put it in front of your team.

**It over-reports downstream breakage.** Cascade detection is textual — it looks
for the dropped column name in downstream compiled SQL. A column named `id` will
match a great deal. The benchmark above reports 1890 cascade risks from 20
dropped columns, because that project is a 200-deep chain. Real projects are
wider and shallower, but the direction of the error is deliberate: it would
rather point at something harmless than miss a break.

**It reports a warning when it cannot be sure.** `SELECT *` without manifest
column definitions, a duplicate column name from a join, an unrecognised
`on_schema_change` — all produce `REVIEW REQUIRED` rather than a verdict. That is
the tool working as intended; see [design notes](design-notes.md).

**It says nothing about data.** A change can be perfectly safe by DDL and still
be catastrophically wrong. dbt-plan will pass it.

For a genuine false positive you have accepted, `--acknowledge` keeps it in the
report while letting the build through; `ignore_models` hides it entirely.
Details in the [configuration reference](configuration.md).
