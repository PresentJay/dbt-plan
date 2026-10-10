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

Model SQL present: 3/3 SQL model(s)
Model columns readable: 3/3 compiled model(s)

SELECT * usage: 0/3 SQL files (0%)
Columns readable: 3/3 compiled SQL file(s)

DDL rules: 3/3 model(s)
```

Run it from the repository root so `--project-dir tests/dbt_project` resolves to
the checked-in fixture. The command is implemented in `_do_stats` in
`src/dbt_plan/cli.py` and counts over the same model index that `dbt-plan check`
builds. Manifest and model-SQL counts exclude package and disabled models.
Each node ID is counted once, including when a `defined_in` filename gives it
several lookup aliases. Different model versions remain different node IDs.
Model counts and model relation resolution use an ID index before filename
aliases are registered, so alias collisions and manifest key order cannot hide
another enabled model.
Filename lookup also takes precedence over generated version aliases in
`check`, preserving the model's own materialization and schema-change policy.
The legacy SQL-file measurements can include orphan or excluded files left in
the compiled model directories; they are not model readiness counts.

## JSON output

Use `--format json` when another tool needs to consume the counts:

```bash
uv run --no-sync dbt-plan stats --project-dir tests/dbt_project --format json | jq .summary
```

The JSON document follows the same top-level `summary` convention as
`check --format json`:

- `summary.total` and `summary.materializations` count unique indexed model
  nodes, including artifact-relative nodes whose authored role is unknown.
- `summary.on_schema_change` counts explicit policies on incremental models.
- `summary.select_star` contains `used`, `compiled`, `percentage`, and
  `resolved_through_ref_or_cte`.
- `summary.columns_readable` contains `readable`, `compiled`, and `unreadable`.
- These two existing fields retain their file-scan semantics: they measure SQL
  files under manifest-derived model directories, not unique manifest models.
  `percentage` is the share of those files using `SELECT *`, not model readiness.
- `summary.model_sql` is an additive field measuring identified SQL models:
  `total`, `compiled` (files present), `readable`, `unreadable`, and `missing`.
  It also records `manifest_only` and `unclassified` node counts outside that
  SQL-model population. `total = compiled + missing`,
  `compiled = readable + unreadable`, and
  `summary.total = model_sql.total + manifest_only + unclassified`.
- `summary.cascade_risk` and `summary.ddl_rules` expose the remaining readiness
  counts.
- `details` contains manifest-column fallback counts and materialization/policy
  combinations without a DDL rule.
- `details.missing_model_sql` and `details.unclassified_model_nodes` are additive
  lists of unique node IDs explaining those model counts.

When no compiled SQL directory is available, the manifest-derived fields remain
available and `summary.select_star` and `summary.columns_readable` are `null`.
This distinguishes “not measured” file statistics from zero files present for
identified models. `model_sql` still reports those models and their missing SQL.
Text remains the default output. The existing JSON fields are retained; clients
that need model counts should read the additive `model_sql` field.

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

The share of scanned SQL files whose final SELECT still asks for `*`
(`extract_columns == ["*"]` before any table resolution). `0/3` means none of
those three files writes a bare star at the top level. A star is readable to a
human but opaque to static analysis until it is resolved through `ref()` or a CTE, which is why
`check` reports those models as `review required` unless they can be expanded.

### Columns readable

The legacy `Columns readable` line measures SQL files for which dbt-plan can
resolve the output column list after table resolution (`SELECT *` that cannot be expanded, an
unresolvable `* except(...)`, or a parse failure all count as unreadable).
An orphan file may contribute to this figure. Use `Model SQL present` and
`Model columns readable` for the manifest model population instead.

### Model SQL

`Model SQL present` matches each identified SQL model to its manifest
`original_file_path` inside the compiled project root. A different file with
the same stem cannot replace a missing declared model. Older manifests lacking
that path use an unambiguous model-name/filename alias match. Versioned and
`defined_in` models and custom or multiple model paths are supported.
Equivalent relative path spellings such as `./models/` and `models//` are
normalized after checking that the declared path stays inside the project.

`Model columns readable` uses only those matched model files to resolve model
relations, so an orphan cannot supply a missing upstream model's columns.
Relations are matched by node ID; excluded nodes sharing a filename cannot
borrow project-model SQL. Ambiguous bare names or aliases remain unreadable.
Qualified relations must match their full schema/catalog identity. A source,
seed or unknown relation cannot borrow a same-named project's model columns.
Quoted identifiers retain the SQL dialect's case rules. Physical aliases are
used for SQL lookups; a logical model name is not an overridden table alias.
The same identifier rules apply to CTE names, table aliases and qualified stars.
If an older manifest lacks the relation metadata needed for a qualified
reference, its columns remain unreadable rather than guessed from the name.
Python models and model nodes materialized as snapshots are manifest-only.
Ephemeral SQL models are included because their compiled SQL can be read;
missing SQL is an input observation, not a runtime build-error verdict.

A node declared inside the configured artifact directory (`--target-dir`,
usually `target/`) is **unclassified**: the available manifest may describe a
`dbt show` query or an authored model sharing that directory. Unsupported
languages or non-SQL source paths are also unclassified. These nodes are
listed and excluded from the SQL-model denominator rather than assumed to be
regular models. A model named `inline_orders` is not excluded by its name.
If artifacts are in `build/`, an ordinary model path named `target/` is supported.
No `dbt_project.yml` parsing or live producer certification is performed here.

For a manifest with `models/orders.sql` and `target/inline.sql`, but compiled
files only at `target/inline.sql` and `target/orphan.sql`, the report shows
`Model SQL present: 0/1`, missing node `model.<project>.orders`, and one
unclassified node. The legacy file line can still show `2/2 compiled SQL file(s)`;
it does not establish model analysis readiness.

File presence does not prove that SQL was compiled successfully at the current
source SHA. These counts do not verify invocation identity, source freshness,
warehouse state, or the safety of any change. Use `check` for its input validation
and findings, and preserve the artifact producer's revision evidence separately.

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

A manifest-only invocation cannot produce the legacy compiled-file figures:
when there is no compiled SQL to read, `SELECT * usage` and `Columns readable`
are omitted. Model-SQL presence and missing node IDs remain visible; Python
models are explicitly outside the SQL counts.

## Related docs

- [README](../README.md) — what dbt-plan is for
- [CONTRIBUTING](../CONTRIBUTING.md) — running dbt-plan and the test suite
- [Selection](selection.md) — narrowing a check to a subset of models
- [Performance](performance.md) — reasoning about larger projects
- [Exit codes](exit-codes.md) — what a nonzero `check` exit means
