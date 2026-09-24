"""Stage complete snapshots and retain the old baseline until publication succeeds.

This is a recoverable rename sequence, not a concurrent-reader atomic swap.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _cleanup(path: Path) -> None:
    """Do not hide a publication/recovery error with a cleanup error."""
    try:
        shutil.rmtree(path)
    except BaseException as exc:
        print(f"Warning: snapshot cleanup failed; retained {path}: {exc}", file=sys.stderr)


@contextmanager
def staged_snapshot(project: Path, base: Path) -> Iterator[Path]:
    """Yield an empty sibling directory and publish it after the caller fills it.

    All writers/readers must be externally serialized. The backup container is
    unique and private, so a failed rollback never overwrites another recovery copy.
    """
    resolved_project = project.resolve()
    resolved_base = base.resolve()
    resolved_parent = base.parent.resolve()
    if (
        resolved_base == resolved_project
        or not resolved_base.is_relative_to(resolved_project)
        or resolved_parent == resolved_project
        or not resolved_parent.is_relative_to(resolved_project)
    ):
        raise ValueError("snapshot base directory escapes project directory")
    # rmtree historically refused directory symlinks; keep that refusal. File
    # symlinks can be replaced without touching the file they point to.
    if base.is_symlink() and not base.is_file():
        raise ValueError("snapshot base directory must not be a directory or dangling symlink")

    base.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-stage-", dir=base.parent)).absolute()
    backup: Path | None = None
    previous: Path | None = None
    publishing = False
    try:
        yield stage
        if _present(base):
            backup = Path(tempfile.mkdtemp(prefix=".snapshot-backup-", dir=base.parent)).absolute()
            previous = backup / "base"
            base.rename(previous)
        publishing = True
        stage.rename(base)
    except BaseException as exc:
        try:
            # Inspect disk state, not just flags set after rename: an interrupt
            # can arrive after the OS completes the rename but before it returns.
            if publishing and not _present(stage) and _present(base):
                base.rename(stage)
            if previous is not None and _present(previous):
                previous.rename(base)
        except BaseException as rollback:
            recovery = previous if previous is not None and _present(previous) else base
            raise OSError(
                f"Snapshot publication failed ({type(exc).__name__}: {exc}); "
                f"rollback failed ({type(rollback).__name__}: {rollback}). "
                f"Retained snapshot recovery path: {recovery.absolute()}"
            ) from exc
        if isinstance(exc, KeyboardInterrupt):
            raise OSError("Snapshot interrupted; previous baseline preserved") from exc
        raise
    else:
        # Publication has committed. Cleanup cannot be rolled back once any old
        # files have been removed; report failures with the new baseline intact.
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except (Exception, KeyboardInterrupt) as exc:
                raise OSError(
                    f"Snapshot published, but backup cleanup failed at {backup}: {exc}"
                ) from exc
    finally:
        if _present(stage):
            _cleanup(stage)
        # Never delete the sole recoverable baseline when rollback failed.
        if backup is not None and backup.exists() and previous is not None:
            if not _present(previous):
                _cleanup(backup)
