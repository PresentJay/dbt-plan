# Selecting changed models

`check --select` and `run --select` filter the changed models reported by the
analysis. Supply a model name with one optional `+` at either end:

| Selection | Includes |
|---|---|
| `orders` | That model |
| `orders+` | That model and its downstream models |
| `+orders` | That model and its upstream models |
| `+orders+` | That model, its upstream models, and its downstream models |
| `orders,customers` | The **union** of both selections |

Use letters, digits, underscores or hyphens in model names. Whitespace around
comma-separated terms is ignored. Commas mean union in dbt-plan; dbt's
[selector intersection](https://docs.getdbt.com/reference/node-selection/set-operators)
syntax is not implemented. Graph expansion follows model nodes in
the root project, using the current graph and baseline information for removed
models. Tests and exposures are reported as impacts, not as selected models.

Only changed models from the selection appear in `models`. Selecting an existing
but unchanged model is valid and can produce an empty report with exit 0.
A selected change can still report its downstream impacts outside the selected
set. Project-wide refusals, such as stale or missing artifacts, remain visible;
selection is not a way to bypass an incomplete compile.

## Invalid selections

The next minor release rejects the **entire** selection with exit 3 and an error
on stderr when any term is unsupported or names no model in either manifest.
It writes no completed check report in this case, including with `--format json`.
This also applies when no models changed and when `warning_exit_code: 0` is set.

Examples that fail:

```bash
dbt-plan check --select 'orders+2'        # depth-limited traversal
dbt-plan check --select '2+orders'
dbt-plan check --select '@orders'
dbt-plan check --select 'tag:nightly'
dbt-plan check --select 'path:models'
dbt-plan check --select 'orders*'         # glob
dbt-plan check --select 'orders,typo'     # no partial successful check
dbt-plan check --select 'orders,'        # empty term
dbt-plan check --select ''
```

Correct the term or omit `--select` to analyze all changes. Earlier versions could
warn and return 0 for these inputs; update CI callers using the
[exit-code migration guide](exit-codes.md).

## Versioned models

Select a version explicitly using its compiled SQL file stem, such as
`fct_orders_v1` or `fct_orders_v2`. An unversioned family name (`fct_orders`) is
not expanded to all versions. Use `fct_orders_v1,fct_orders_v2` to select both.

If version 2 has `defined_in: orders_current`, both `orders_current` and
`fct_orders_v2` select the same model. The report uses `orders_current`, the
compiled file stem. Graph operators work with either spelling, and a graph
traversal reaching that version also resolves to `orders_current`. Aliases are
not separate models or extra compiled files.

Real DuckDB integration tests compile both ordinary and versioned projects,
including `defined_in`, and check these selections through the CLI. They also
verify that `run` preserves Git/worktree state when a selection fails.
