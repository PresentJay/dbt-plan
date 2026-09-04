# Design notes

Why dbt-plan is shaped the way it is. Read this before proposing a change that
crosses one of these lines — most rejected ideas are rejected for a reason
recorded here rather than on their merits.

## The tool never connects to a warehouse

dbt-plan reads compiled SQL, `manifest.json`, and model config. That is the whole
input. It does not query the warehouse, run dbt, or simulate a run.

This is a deliberate constraint, not a missing feature:

- It runs on a pull request from a fork, where no credentials exist and none
  should.
- It cannot fail because a warehouse is down, slow, or rate-limiting, so it can
  sit in the required-checks list without becoming a flaky gate.
- There is no query cost, and no way for a static analysis pass to accidentally
  touch production.
- Contributors can reproduce any bug from a `.sql` file, which is why the bug
  report form asks for one.

The cost is real: without the warehouse's current schema, some questions cannot
be answered. `SELECT *` cannot always be expanded, and a column's type change
cannot be seen at all. The tool reports a warning in those cases rather than
guessing. That trade is the point — see the next section.

## A false warning is acceptable; a false safe is not

The tool exists to be believed when it stays quiet. Someone who sees `SAFE` and
merges a column drop has been actively harmed by the tool; someone who sees a
warning on a harmless change is mildly annoyed and moves on.

Everything follows from that asymmetry:

- `extract_columns` returns `None` on a parse failure. It never returns a partial
  or best-guess column list.
- Ambiguous columns — duplicates from a join, unaliased expressions — produce
  `REVIEW REQUIRED` rather than a verdict.
- A removed model is `DESTRUCTIVE` regardless of materialization, because the
  tool cannot know whether the object was dropped by hand afterwards.
- An unrecognised `on_schema_change` value is a warning, not a default to the
  safest interpretation.

Users who find a specific warning noisy can silence it with `ignore_models`, or
accept a specific destructive change with `--acknowledge`. Both are opt-in and
name the model explicitly. There is deliberately no blanket "acknowledge
everything": it would let changes that arrived after the review ride along behind
it, which is exactly the failure this asymmetry is built to prevent.

## Column extraction uses SQLGlot

Compiled dbt SQL is real warehouse SQL — window functions, `QUALIFY`, VARIANT
path access, `SELECT * EXCEPT(...)`, CTE chains. A regex approach fails on the
patterns that matter most, and failing silently on those would produce exactly
the false safes described above.

SQLGlot parses all of them, supports every warehouse dbt targets through one
`--dialect` flag, and has no dependencies of its own — which keeps dbt-plan
installable into a CI job without dragging in a tree.

Its version is a correctness boundary, not a packaging detail. Before 28.0.0,
`SELECT * EXCEPT(revenue)` extracted as plain `*`; a model that dropped a column
that way produced identical column lists on both sides and was reported safe. The
declared floor is `>=28.0.0` and the `minimum-deps` CI job runs the suite pinned
to it so the floor cannot quietly rot.

## Risk comes from materialization × on_schema_change

dbt decides what DDL to emit from the model's materialization and, for
incremental models, its `on_schema_change` setting. The prediction table in the
README mirrors that logic:

- `table` and `view` rebuild the object wholesale, so a column change is safe.
- `ephemeral` has no physical object at all.
- `incremental` is where the risk lives. `ignore` emits no DDL; `append_new_columns`
  only adds; `sync_all_columns` adds *and drops*, which is the one combination
  that can destroy data on a column removal.
- `snapshot` is left as `REVIEW REQUIRED`. Snapshot schema evolution has enough
  edge cases that a confident verdict would be dishonest.

`full_refresh` is out of scope: it is a runtime flag decided by whoever invokes
dbt, not something visible in the files.

## Cascade analysis is textual, on purpose

When a column disappears, downstream models that reference it break. dbt-plan
finds them by walking `child_map` from the manifest and looking for the dropped
name in the downstream compiled SQL, with word-boundary matching.

This over-reports: a column named `id` will match a lot. It does not under-report,
which is the property that matters. Building a real column-level lineage graph
would be more precise and would also mean resolving every expression across every
dialect — much more machinery, and its failure mode is missing a break rather
than flagging a false one. Wrong direction for this tool.

## Snapshot-and-compare, rather than reading git

dbt-plan compares two directories of compiled SQL. `dbt-plan snapshot` saves the
baseline; `dbt-plan check` diffs against it.

Working from compiled output rather than git history means Jinja, macros, package
models, and generated SQL are all already resolved — the tool compares what dbt
will actually run, not what the templates look like. `dbt-plan run` wraps the
whole sequence for local use, stashing uncommitted work to compile the baseline
and restoring it afterwards.

That stash is the most dangerous thing the tool touches, so its lifetime lives in
`stash.py` behind a context manager: there is no path through the block that
skips the restore, including the `sys.exit()` calls that several helpers in
`cli.py` make.

## Out of scope

| Not planned | Why |
|-------------|-----|
| `dbt run` simulation | Runtime behaviour; the tool is static analysis |
| INFORMATION_SCHEMA queries | Needs a warehouse connection |
| Type changes on columns with no explicit `CAST` | The type is whatever the warehouse assigned, so seeing a change would mean asking it. Columns cast explicitly on *both* sides are compared since 0.8.0 — that comparison is compiled SQL against compiled SQL, which needs no connection |
| `seed` / `source` change detection | Neither produces compiled SQL to diff |
| `pre_hook` / `post_hook` DDL analysis | Arbitrary SQL in arbitrary places; high complexity, low signal |
| `full_refresh` judgment | A runtime flag, decided outside the files |
