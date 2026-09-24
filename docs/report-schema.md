# Check report JSON Schema

[`schemas/check-report-v1.schema.json`](../schemas/check-report-v1.schema.json)
defines the existing `dbt-plan check --format json` report using JSON Schema
Draft 2020-12. The filename versions this contract; reports do not gain a
`schema_version` field. This is not the `stats` format or SARIF.

The schema lives in the source repository, not the runtime Python package.
Consumers can vendor the file from a pinned repository revision. All `$ref`
references point to bundled `$defs`; validation needs no network access.

## Fields and compatibility

The required root fields are `summary`, `models`, `parse_failures`,
`stale_sources`, `skipped_models`, and `uncompiled_models`. The last four are
arrays of strings, including when empty. `models` is an array of model objects.
Arrays are never `null`.

`summary` requires nonnegative integer counts `total`, `safe`, `warning`, and
`destructive`. Optional `acknowledged` and `cascade_risks` are also nonnegative
integers. Booleans and numeric strings are not counts. JSON Schema treats an
integral JSON number such as `1.0` as an integer.

Each model requires `model_name`, `materialization`, `on_schema_change`,
`safety`, `operations`, `columns_added`, `columns_removed`, and `acknowledged`.
`on_schema_change` and each operation's required `column` may be `null`.
`acknowledged` is a boolean. Optional `downstream`, `downstream_impacts`, and
`downstream_exposures` are arrays; impact and exposure items have their own
required fields in the schema. Exposure `owner` is a display string, not an
object; absent owner/type/URL values are empty strings.

`baseline_problem`, when present, is a string. `analysis` and its provenance
fields are optional, so older reports without them remain valid. Known metadata
is checked when present. Baseline `revision` and `created_at`, `adapter_type`,
and `selection` accept `null` for unknown or unspecified values. `selection` is
the original CLI selection string, not an array. Revision/timestamp strings
are descriptive provenance, not proof that inputs are current.

Every object permits unknown fields, including nested objects. Consumers must
tolerate additive metadata such as waiver information without requiring it in
legacy reports. New optional fields do not require a new report format.

## Validation is not a safety decision

Known model safety strings are `safe`, `warning`, and `destructive`. Safety and
downstream risk strings deliberately use an open vocabulary. **Treat an unknown
safety or risk string as requiring review**, even if schema validation succeeds.
An unknown field must not silently override a known warning or destructive finding.

Schema validation checks structure and types, not policy or completeness. A
valid report can contain refusals, parse failures, stale inputs, baseline
problems, or destructive changes. Acknowledgement does not erase the raw
destructive count or reclassify the model's safety. Use the CLI exit code and
the [exit-code contract](exit-codes.md) when enforcing CI policy. Execution
errors can exit 3 without emitting a report; absence of JSON is not a clean run.

Arithmetic is checked separately in `tests/test_report_schema.py`: total equals
the number of models; safety counts match the models; acknowledgement counts
match flags; cascade counts match impact entries. JSON Schema does not express
these cross-field sums. A negative control deliberately changes a total and
demonstrates that schema validation passes while the semantic check fails.

## Run the contract tests

```sh
uv sync --extra test
uv run --no-sync pytest tests/test_report_schema.py -q
```

`jsonschema` is a test/dev extra only; the analysis core still depends only on
`sqlglot`. The tests validate the schema against its bundled meta-schema and
use a reference registry without remote retrieval. They run the actual snapshot
and check CLI against local compiled artifacts for unchanged, safe, warning,
destructive, acknowledged, refusal, baseline-problem, stale-input, cascade and
provenance cases. No dbt installation or warehouse connection is needed.

Negative controls remove required fields, substitute null arrays, use invalid
counts and model field types, and verify rejection after validating the original
report. Separate positive controls cover nullable fields, legacy metadata, and
unknown additions at every object level.
