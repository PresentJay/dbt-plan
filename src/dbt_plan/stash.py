"""Borrow a clean working tree from git, and always give it back.

`dbt-plan run` needs a clean tree to compile a baseline, so it stashes the
user's uncommitted work. That work is the most valuable thing the tool ever
touches, and every step between stashing and restoring can fail -- including
by exiting the process, which several helpers in this package do.

Exposing this as a context manager makes the restore structural: there is no
code path through the `with` block that skips it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

STASH_LABEL = "dbt-plan-run-temp"


class StashError(RuntimeError):
    """The tree could not be stashed, so no clean baseline is possible."""


@dataclass
class StashState:
    """Outcome of the borrow. `restore_failed` means the work is still stashed."""

    ref: str | None = None
    restore_failed: bool = False

    @property
    def stashed(self) -> bool:
        return self.ref is not None


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )


def current_stash_ref(project_dir: Path) -> str | None:
    """Commit id of the stash entry on top, so we can restore that exact one."""
    rev = _git(project_dir, "rev-parse", "stash@{0}")
    return rev.stdout.strip() if rev.returncode == 0 else None


def _restore(project_dir: Path, ref: str) -> bool:
    """Pop the entry we pushed. Returns True if the user's work is still stashed.

    Popping by position would restore whatever happens to be on top, which may
    be a stash the user made themselves. Verify identity first, and if the pop
    fails, say so loudly with the recovery command -- silence here reads as
    "your changes are gone".
    """
    if current_stash_ref(project_dir) != ref:
        print(
            "Error: the stash entry dbt-plan created is no longer on top, so it "
            "was not restored automatically.\n"
            f"  Your changes are saved as commit {ref[:12]}.\n"
            f"  Recover with: git stash apply {ref}",
            file=sys.stderr,
        )
        return True

    pop = _git(project_dir, "stash", "pop")
    if pop.returncode != 0:
        print(
            "Error: could not restore your stashed changes:\n"
            f"{pop.stderr.strip()}\n"
            "  Your work is NOT lost -- it is still in the stash.\n"
            f"  Recover with: git stash pop   (entry: {STASH_LABEL})",
            file=sys.stderr,
        )
        return True
    return False


@contextmanager
def clean_worktree(project_dir: Path, *, has_changes: bool) -> Iterator[StashState]:
    """Stash uncommitted work for the duration of the block, then restore it.

    Raises StashError if the stash fails. Callers must not continue in that
    case: the tree is still dirty, so a "baseline" compiled from it would match
    the current state and report no changes -- and restoring would pop an entry
    dbt-plan never created, possibly the user's own.
    """
    state = StashState()
    if has_changes:
        push = _git(project_dir, "stash", "push", "-m", STASH_LABEL, "--include-untracked")
        if push.returncode != 0:
            raise StashError(push.stderr.strip())
        state.ref = current_stash_ref(project_dir)

    try:
        yield state
    finally:
        if state.ref is not None:
            state.restore_failed = _restore(project_dir, state.ref)
