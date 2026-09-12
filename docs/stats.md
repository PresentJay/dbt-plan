# `stats`: project readiness counts

`dbt-plan stats` prints static readiness counts for a compiled dbt project: how
many models exist by materialization, how incremental models handle schema
changes, whether compiled SQL still leans on `SELECT *`, and how readable that
compiled SQL is to a column extractor. It reads already-compiled artifacts and a
`manifest.json`; it does not connect to a warehouse or need a dbt adapter.

## Running it

From a checkout of `main` (or any release that ships the command):

```bash
uv sync --extra test
uv run --no-sync dbt-plan stats --project-dir tests/dbt_project
```

At the time of writing, the committed fixture project yields:

```
dbt-plan stats -- 3 model(s) in manifest

Materializations:
  view                    1
  table                   1
  incremental             1

on_schema_change (incremental only):
  sync_all_columns        1  ← dbt-plan monitors this

SELECT * usage: 0/3 models (0%)
Columns readable: 3/3 compiled model(s)

DDL rules: 3/3 model(s)
```

Run it from the repository root so `--project-dir tests/dbt_project` resolves to
the checked-in fixture. The command is implemented in `_do_stats` in
`src/dbt_plan/cli.py` and counts over the same model index that `dbt-plan check`
builds, so package models and disabled models are left out of every figure the
same way.

## What the counts mean

### Materializations

One line per materialization actually present: `view`, `table`, `incremental`,
and so on. A project whose models are a mix of materializations gets one line
each, in order of prevalence. This is a shape summary only — sizes and runtimes
are not measured.

### `on_schema_change` (incremental only)

Only incremental models report an `on_schema_change` policy, so the counter is
broken down for that materialization alone; `ignore` is shown when the policy is
absent. `sync_all_columns` and `fail` are marked `← dbt-plan monitors this`
because those policies make a changed schema visible to the DDL prediction; a
project that leans on them is the project `dbt-plan` was built to review.

### SELECT * usage

The share of compiled models whose final SELECT still asks for `*`
(`extract_columns == ["*"]` before any table resolution). `0/3` means no model
writes a bare star at the top level. A star is readable to a human but opaque to
static analysis until it is resolved through `ref()` or a CTE, which is why
`check` reports those models as `review required` unless they can be expanded.

### Columns readable

The share of compiled models for which dbt-plan can resolve the exact output
column list after table resolution (`SELECT *` that cannot be expanded, an
unresolvable `* except(...)`, or a parse failure all count as unreadable).
Unreadable models are exactly the ones `check` cannot give a confident verdict
for, so a declining ratio is a warning sign — not a tests-to-write hint.

### DDL rules

The share of models matched to a DDL prediction rule for their materialization
and `on_schema_change` combination (`has_ddl_rule`). Models without a matching
rule are always reported as `review required` by `check`, no matter what changed.

## What these counts do — and do not — measure

These are **static readiness counts**:

- They are not test coverage. A model without a data test can still be fully
  readable to dbt-plan.
- They are not proof of safe changes. `stats` never predicts DDL, so a perfect
  score here says nothing about whether the next change is destructive.
- They are not data-quality validation and not a live adapter certification.
  No query runs and no warehouse is contacted.

A manifest-only invocation cannot produce the compiled-SQL figures: when there
is no compiled SQL to read, the `SELECT * usage` and `Columns readable` lines are
omitted entirely. Only the manifest-derived counts (`Materializations`,
`on_schema_change`, `DDL rules`) are shown.

## Related docs

- [README](../README.md) — what dbt-plan is for
- [CONTRIBUTING](../CONTRIBUTING.md) — running dbt-plan and the test suite
- [Selection](selection.md) — narrowing a check to a subset of models
- [Performance](performance.md) — reasoning about larger projects
- [Exit codes](exit-codes.md) — what a nonzero `check` exit means