# Snapshot SHA256 integrity

New `dbt-plan snapshot` captures include `inventory.json`. Its format version is
independent of `provenance.json`'s `layout_version` (currently 1) and the package
version. Example:

```json
{
  "version": 1,
  "files": [
    {"path": "compiled/models/orders.sql", "sha256": "<64 lowercase hexadecimal characters>"},
    {"path": "manifest.json", "sha256": "<64 lowercase hexadecimal characters>"}
  ]
}
```

Paths are unique, sorted, canonical POSIX paths relative to the snapshot root.
The inventory covers the copied manifest and every regular compiled `.sql` file,
including tests and other non-model SQL (`.SQL` is covered on every platform too).
It excludes itself, provenance, and
non-SQL supporting files. Digests cover exact bytes, including line endings, and
are calculated with Python's standard library in chunks of at most 1 MiB.

`check` validates the complete inventory before reading baseline metadata or
analyzing SQL. A changed digest, missing or unexpected SQL, invalid schema,
duplicate JSON key or path, noncanonical/escaping path, or unsupported version
(including `true` or `1.0`) returns execution error **3**, with no completed report.
Selection and ignore rules cannot bypass this validation. Recreate a damaged
baseline with `dbt-plan snapshot` after compiling the intended baseline revision.

A validated inventory sets `analysis.baseline.integrity` to `"verified"` in JSON
reports. This verifies bytes, not SQL safety: existing parse failures and missing
analysis inputs still require review or produce execution errors.

Snapshots without an inventory keep the existing layout and input validation,
and report `"unrecorded"`, never `"verified"`. Checking does not create an
inventory or rewrite an old snapshot. A malformed or symlinked inventory is an
error, not a legacy snapshot. The status is derived, never trusted from provenance.

Capture copies compiled symlinks into staging without traversing them, then
rejects the staged inventory before publication, just as the reader rejects
symlinks or Windows junctions without reading their targets. Replace links with
regular compiled files before capturing a baseline. A missing or unreadable
manifest also fails capture with exit 3 and preserves the old baseline.

The inventory is written and validated in the staging directory before
publication. A hashing, writing, layout, or inventory validation failure leaves
all previous baseline bytes, including its inventory, intact; retrying capture
is supported. Existing recoverable rename and rollback
guarantees still apply. Readers and writers must be externally serialized; this
is not a concurrent-reader atomic swap or protection against concurrent edits.

SHA256 detects accidental drift. An attacker who replaces both the file and its
digest can bypass it. This is neither a signature nor a trusted provenance system;
deleting an inventory also makes the snapshot indistinguishable from a historical
unrecorded snapshot.
