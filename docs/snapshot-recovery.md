# Snapshot publication and recovery

`dbt-plan snapshot` builds compiled SQL, `manifest.json` (when present), and
`provenance.json` in a unique `.dbt-plan/.snapshot-stage-*` directory. It uses the
same filesystem as `.dbt-plan/base`. Manifest-only projects still get an empty
`compiled/` directory. A missing manifest still produces the existing warning;
this durability mechanism does not make an incomplete input valid for `check`.

Only after staging finishes does snapshot rename the previous `base` to
`.dbt-plan/.snapshot-backup-*/base`, then rename the staging directory to `base`.
After publication it deletes that invocation's backup. Successful replacement
removes obsolete files and retains the complete new snapshot.

Copy or metadata-write failures leave the previous baseline untouched. Rename
failures restore the previous baseline. This includes `KeyboardInterrupt` during
staging/publication: cleanup runs and the CLI exits 3 without a successful-snapshot
message. Other exceptions also unwind recovery; `SystemExit` is re-raised after
recovery rather than being swallowed. Once publication has committed, a backup
cleanup failure reports exit 3 and the backup location, while the new baseline
remains usable. Cleanup may have removed some obsolete backup files at that point.

## When rollback fails

If the restore rename also fails (for example, permissions or an open file on
Windows), snapshot preserves the recoverable old baseline and prints its exact
absolute recovery path. It exits 3 and does not print `Snapshot saved`.

1. Stop other snapshot, check, and run processes for this project.
2. Inspect the path from the error, including its SQL, manifest, and provenance.
   Resolve the permission or file-handle problem before attempting recovery.
   If the reported path is already `.dbt-plan/base`, the restore may have completed
   just before an interrupt was delivered: verify it in place instead of moving it.
3. If `base` exists, move it to a separate unused location for inspection. Rename
   the reported backup `base` back to the project's `.dbt-plan/base`. This step
   applies only when the reported recovery path is a separate backup.
4. Verify the restored files before removing empty backup containers or abandoned
   staging directories. You can then retry the snapshot command.

A retry never automatically deletes backups retained by earlier attempts. Do not
delete a backup simply because its name starts with `.snapshot-backup-`. A
previous baseline that was a relative file symlink must be restored at its original
location for its link target to resolve correctly; moving the link to a backup
does not move or copy its target.

## Boundaries

This is a recoverable sequence of renames, **not a universally atomic swap of
nonempty directories**. Between the two publication renames, `base` is absent.
Concurrent readers can see this gap; concurrent writers can race. Serialize all
access to a project's baseline externally. No global Git lock is added, and
this mechanism does not change Git stash/checkout behavior.

Abrupt OS termination, SIGKILL, power loss, or repeated interruption of recovery
can leave staging or backup directories behind. There is no startup auto-recovery,
filesystem journal, or fsync guarantee. After such termination, inspect `base`
and retained backups before running another check or snapshot. A staging directory
may be incomplete and is not evidence of a valid baseline.

Destination containment is checked even when `base` is missing or a symlink is
dangling. A destination resolving outside the project or to the project itself
is refused. Directory/dangling symlinks at `base` are refused; an internal file
symlink can be replaced without modifying its target. Compiled-tree symlinks
remain links (`copytree(..., symlinks=True)`), including dangling links; manifest
copy behavior remains unchanged. These checks assume paths are not concurrently
replaced by another process and do not claim protection against such races.
