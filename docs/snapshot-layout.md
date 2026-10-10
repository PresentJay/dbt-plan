# Snapshot layout compatibility

New snapshots record integer `layout_version: 1` in the existing
`.dbt-plan/base/provenance.json`, alongside `dbt_plan_version`, `created_at`,
and `revision`. No additional metadata file is created.

Version 1 means `compiled/` contains the project's compiled root, preserving
model-path prefixes, for example `compiled/models/staging/orders.sql` or
`compiled/transformations/orders.sql`. Model paths come from the **baseline**
manifest, so changing model paths in the current project does not hide baseline
SQL. Compiled tests and other non-model resources can coexist in that root.
Manifest-only snapshots (Python models or dbt snapshots) have an empty
`compiled/` directory and remain supported.

The layout number is independent of the producer's package version and any
content integrity inventory version. A recognized layout does not verify file
contents, compilation freshness, or the safety of a change.

## Reading older snapshots

When the provenance file or its `layout_version` field is absent, `check` keeps
the existing directory-based compatibility handling:

- Project root: `compiled/models/staging/orders.sql`.
- Older model root: `compiled/staging/orders.sql`.
- The historical direct baseline form, such as `base/orders.sql`, also works.

These baselines are reported as `legacy/unversioned (not verified)` on stderr,
and JSON reports include `analysis.baseline.layout_status: "legacy"` and
`analysis.baseline.layout_version: null`.
Versioned baselines use `layout_status: "versioned"` and retain `layout_version`
in that metadata. `check` never rewrites or upgrades a baseline.

## Incompatible or damaged metadata

Unknown versions, non-integer values (including booleans, strings, and floats),
unreadable provenance, or a directory tree incompatible with the declared layout
stop `check` before analysis with exit code **3** and no analysis report.
For example, a version 1 baseline with flattened model SQL or a missing
`compiled/` directory cannot fall back to legacy detection.

Compile the intended baseline revision, then run `dbt-plan snapshot` again.
Do not remove or change `layout_version` to bypass a compatibility error.
Missing or corrupt manifests retain the existing conservative check behavior.
If a versioned baseline's manifest is missing or unreadable, its model paths are
unknown: the reader scans nested SQL without guessing the default `models`
prefix, and `check` reports `baseline_problem: "missing"` or `"corrupt"` with
the existing warning policy (exit 2 by default). A readable manifest that
contradicts the directory layout still causes exit 3. Absolute paths, Windows
drive/backslash paths, traversal, and control characters in manifest paths are
rejected consistently across platforms.

Snapshot metadata is written inside the staged publication transaction, together
with SQL and the manifest. A metadata write failure preserves the previous
baseline; layout versioning does not weaken snapshot rollback or recovery.

New snapshots run the same layout reader against the staged content before
publication. A missing required model directory or misplaced SQL returns exit
**3** without creating a first baseline or replacing any existing baseline bytes,
including its inventory. Missing or unreadable new manifests are also rejected.
The diagnostic names the input problem and asks you to run `dbt compile` for the
intended revision before retrying `snapshot`; retrying the same incomplete
artifacts cannot fix it. This differs from `check`'s recovery advice for an
already damaged historical baseline.

This is a reader-compatibility check, not a blanket full-compile requirement:
a partial compile whose declared model directories exist remains supported.
Missing model files remain subject to `check`'s existing input and warning policy.

## Integrity integration interface

`read_snapshot_layout(base, current_model_dirs, baseline_model_dirs)` returns a
`SnapshotLayout(root, model_dirs, provenance)` named tuple. Paths are `Path`
objects, model directories are tuples of top-level directory names (`None` for
an unfiltered legacy tree or unavailable baseline manifest), and provenance is
the report's baseline dictionary.
Invalid metadata/layout raises `ValueError` with a re-snapshot instruction.
The CLI calls this before diffing and translates failures to exit 3.

The reader preserves other provenance fields and overwrites `layout_status`
with its actual finding. It supplies `layout_version: null` only in the report
for legacy input; it never persists that value. An explicitly stored null is
invalid. Integrity validation can add `analysis.baseline.integrity` with values
`"verified"` or `"unrecorded"` independently; layout validation does not set it.
