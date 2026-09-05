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

# Everything in the repository except this project's snapshot directory. `:/` is
# the repository root, so a dbt project in a subdirectory still gets the whole
# tree stashed; the exclusion is relative to the project, which is where the
# snapshot lives.
#
# The snapshot has to stay out. It is untracked, so `--include-untracked` would
# take it -- and then `dbt-plan run` writes a new one before popping, and the pop
# refuses because the untracked files it holds already exist. The user's real
# work is left in the stash by a command that touched nothing of theirs.
_STASH_PATHSPEC = (":/", ":(exclude).dbt-plan")


class StashError(RuntimeError):
    """The tree could not be stashed, so no clean baseline is possible."""


class CheckoutError(RuntimeError):
    """The baseline ref could not be checked out, so it cannot be compiled."""


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
        encoding="utf-8",
        errors="replace",
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
        push = _git(
            project_dir,
            "stash",
            "push",
            "-m",
            STASH_LABEL,
            "--include-untracked",
            "--",
            *_STASH_PATHSPEC,
        )
        if push.returncode != 0:
            raise StashError(push.stderr.strip())
        state.ref = current_stash_ref(project_dir)

    try:
        yield state
    finally:
        if state.ref is not None:
            state.restore_failed = _restore(project_dir, state.ref)


def _current_head(project_dir: Path) -> str | None:
    """What to come back to: the branch name if on one, otherwise the commit.

    `--short` matters. `git checkout refs/heads/feature` detaches at that commit
    instead of switching to the branch, so coming back with the full ref name
    leaves the user on a detached HEAD -- which is the thing this is for avoiding.
    """
    branch = _git(project_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode == 0 and branch.stdout.strip():
        return branch.stdout.strip()
    commit = _git(project_dir, "rev-parse", "HEAD")
    return commit.stdout.strip() if commit.returncode == 0 else None


def merge_base(project_dir: Path, ref: str) -> str:
    """Where this branch left `ref`, which is the baseline "before you push" means.

    Comparing against the tip of `ref` instead would fold in everything other
    people merged while the branch was open, and report their columns as yours.
    """
    exists = _git(project_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if exists.returncode != 0:
        raise CheckoutError(
            f"'{ref}' is not a commit in this repository.\n"
            f"  If it is a remote branch, fetch it first: git fetch origin {ref}"
        )
    base = _git(project_dir, "merge-base", "HEAD", ref)
    if base.returncode != 0 or not base.stdout.strip():
        raise CheckoutError(
            f"HEAD and '{ref}' have no common ancestor, so there is no branch point "
            f"to compare against."
        )
    return base.stdout.strip()


@contextmanager
def borrowed_head(project_dir: Path, ref: str | None) -> Iterator[str | None]:
    """Detach at `ref` for the duration of the block, then put HEAD back.

    Same reasoning as `clean_worktree`, one level up: moving someone's HEAD is
    the second most valuable thing this tool touches, and the way back has to be
    structural rather than a line at the end of a function that can be skipped.

    `ref` of None does nothing at all, which is the default `dbt-plan run` path --
    no checkout, no risk, and the caller needs no branch for it.

    Nest this *inside* `clean_worktree`. The order matters: HEAD is restored
    before the stash is popped, so the work lands on the tree it came from.
    """
    if ref is None:
        yield None
        return

    original = _current_head(project_dir)
    if original is None:
        raise CheckoutError("could not read HEAD, so there is nothing to come back to")

    checkout = _git(project_dir, "checkout", "--detach", "--quiet", ref)
    if checkout.returncode != 0:
        raise CheckoutError(checkout.stderr.strip())

    try:
        yield ref
    finally:
        back = _git(project_dir, "checkout", "--quiet", original)
        if back.returncode != 0:
            print(
                "Error: could not return HEAD to where it was:\n"
                f"{back.stderr.strip()}\n"
                f"  You are on a detached HEAD at {ref[:12]}.\n"
                f"  Recover with: git checkout {original}",
                file=sys.stderr,
            )
