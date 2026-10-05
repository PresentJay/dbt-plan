# Reporting exact model exclusions

`ignore_models` in `.dbt-plan.yml`, or the comma-separated
`DBT_PLAN_IGNORE_MODELS` environment variable, excludes exact model names from
analysis under the existing ignore policy. The environment variable overrides
the file setting. Matching is literal: `orders_*` does not match `orders_daily`.
Use the compiled file stem used by the report; this does not introduce glob,
package, or model-version family matching.

```yaml
ignore_models: [scratch_orders, imported_orders]
```

Every JSON report includes two top-level arrays (empty when there are no entries):

```json
{
  "ignored_models": ["scratch_orders"],
  "unmatched_ignore_models": ["imported_orders"]
}
```

This is a field excerpt, not a complete report. Both arrays contain unique,
sorted names:

- `ignored_models` lists actual exclusions: changed SQL, added or removed
  models, manifest-only changes, and relevant missing current/baseline SQL
  that the exact policy excluded. An unchanged, fully compiled model does not
  count merely because it appears in the configuration.
- `unmatched_ignore_models` lists requested names absent from both manifests
  and compiled model files. These names are informational; they do not change
  severity or the exit code. A known model outside `--select` is not unmatched.

With `--select`, changed models count only if selected. Missing SQL checks also
cover the selection's dependency/consumer graph, so an ignored uncompiled model
in that graph is an exclusion even if it has no prediction row. Unrelated
models do not count. These are analysis exclusions, not a complete inventory of
the project or an acknowledgement of findings.

Text and GitHub reports label these sections **Excluded by exact ignore policy
(not checked)** and **Unmatched ignore names (informational)**. The sections
appear even when there are zero predictions. Excluded models never acquire SAFE
prediction rows or increase the checked/safe totals. An exclusion says nothing
about the model's safety; downstream references can still appear in other
models' findings.

Existing shared-source and baseline refusals remain active, including stale
shared files and missing/corrupt baseline manifests. Remaining findings keep
their severity. This reporting change does not alter acknowledgement/waiver
rules, warning exit configuration, or exit codes: 0 for a policy pass, 1 for
active destructive findings, 2 for warnings by default, and 3 for execution
errors. An all-excluded report can pass the existing policy without establishing
that the excluded models are safe.
