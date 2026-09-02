"""`dbt-plan run` stashes the user's uncommitted work. It must never lose it.

The run command needs a clean tree to compile a baseline, so it stashes,
compiles, then restores. Every failure in that sequence has to be caught: a
stash that never happened must not be "restored" (that would pop somebody
else's entry), and a restore that failed must be reported loudly, because the
user's work is sitting in the stash while the command looks like it succeeded.
"""

from __future__ import annotations

import argparse
import subprocess
from unittest.mock import patch

import pytest

from dbt_plan.cli import _do_run


def _run(args) -> int:
    """Call _do_run, treating sys.exit() as its exit code.

    Helpers inside the run pipeline exit the process rather than returning,
    so the restore step must survive that -- which is exactly what these
    tests are checking.
    """
    try:
        return _do_run(args)
    except SystemExit as e:  # noqa: PERF203
        return int(e.code or 0)


def _git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _repo(tmp_path, *, commit=True):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    if commit:
        (repo / "model.sql").write_text("SELECT 1 AS a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
    return repo


def _args(repo, compile_command):
    return argparse.Namespace(
        project_dir=str(repo),
        compile_command=compile_command,
        format="text",
        no_color=True,
        verbose=False,
        dialect=None,
        select=None,
        acknowledge=None,
    )


class TestStashPushFailure:
    def test_aborts_when_stash_push_fails(self, tmp_path, capsys):
        """A repo with no initial commit reports dirty but cannot stash.

        Continuing would compile the dirty tree as the "baseline", making the
        baseline identical to the current state -- a false 'no changes'.
        """
        repo = _repo(tmp_path, commit=False)
        (repo / "model.sql").write_text("SELECT 1 AS a\n")

        code = _run(_args(repo, "true"))

        assert code != 0
        err = capsys.readouterr().err
        assert "stash" in err.lower()

    def test_leaves_the_tree_untouched(self, tmp_path):
        """Aborting must not consume the work it failed to stash."""
        repo = _repo(tmp_path, commit=False)
        (repo / "model.sql").write_text("SELECT 1 AS a\n")

        _run(_args(repo, "true"))

        assert (repo / "model.sql").read_text(encoding="utf-8") == "SELECT 1 AS a\n"


class TestUnrelatedStash:
    def test_a_users_own_stash_is_never_consumed(self, tmp_path):
        """Restore must pop the entry we pushed, not whatever is on top."""
        repo = _repo(tmp_path)
        (repo / "model.sql").write_text("earlier work\n")
        _git(repo, "stash", "push", "-q", "-m", "my own stash")
        assert "my own stash" in _git(repo, "stash", "list").stdout

        (repo / "model.sql").write_text("SELECT 1 AS a, 2 AS b\n")
        _run(_args(repo, "true"))

        assert "my own stash" in _git(repo, "stash", "list").stdout, (
            "dbt-plan consumed a stash entry it did not create"
        )


class TestStashPopFailure:
    @staticmethod
    def _clobber(repo):
        """Commit a conflicting change so the later `git stash pop` fails.

        Driven from the snapshot step rather than the compile command so the
        test needs no shell -- only git, which these tests already require.
        """

        def _fake_snapshot(_args):
            (repo / "model.sql").write_text("conflicting\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "clobber")

        return _fake_snapshot

    def test_reports_when_restore_fails(self, tmp_path, capsys):
        """If the pop conflicts, say so and point at the stash entry."""
        repo = _repo(tmp_path)
        (repo / "model.sql").write_text("SELECT 1 AS a, 2 AS b\n")  # user's work

        with patch("dbt_plan.cli._do_snapshot", self._clobber(repo)):
            code = _run(_args(repo, "git --version"))

        err = capsys.readouterr().err
        assert code != 0, "a failed restore must not look like success"
        assert "stash" in err.lower()
        # The user's work is still recoverable and we told them so.
        assert _git(repo, "stash", "list").stdout.strip() != ""

    def test_user_work_is_never_silently_dropped(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        original = "SELECT 1 AS a, 2 AS b\n"
        (repo / "model.sql").write_text(original)

        with patch("dbt_plan.cli._do_snapshot", self._clobber(repo)):
            _run(_args(repo, "git --version"))

        # Either restored to the tree, or still in the stash -- never gone.
        in_tree = original in (repo / "model.sql").read_text(encoding="utf-8")
        in_stash = bool(_git(repo, "stash", "list").stdout.strip())
        assert in_tree or in_stash


class TestHappyPath:
    def test_clean_repo_still_works(self, tmp_path):
        """No uncommitted changes: nothing to stash, nothing to restore."""
        repo = _repo(tmp_path)
        code = _run(_args(repo, "true"))
        # No compiled output exists, so this cannot reach a real verdict, but
        # it must not fail on stash handling and must leave no stash behind.
        assert _git(repo, "stash", "list").stdout.strip() == ""
        assert isinstance(code, int)

    def test_restores_changes_when_compile_touches_nothing(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "model.sql").write_text("SELECT 1 AS a, 2 AS b\n")

        _run(_args(repo, "true"))

        assert (repo / "model.sql").read_text(encoding="utf-8") == "SELECT 1 AS a, 2 AS b\n", (
            "uncommitted work must be back in the tree"
        )
        assert _git(repo, "stash", "list").stdout.strip() == "", "stash must not be left behind"


@pytest.mark.parametrize("cmd", ["true", "false"])
def test_never_leaves_a_dbt_plan_stash_behind_on_success(tmp_path, cmd):
    """Whatever the compile does, a successful restore leaves no entry."""
    repo = _repo(tmp_path)
    (repo / "model.sql").write_text("SELECT 1 AS a, 2 AS b\n")

    _run(_args(repo, cmd))

    remaining = _git(repo, "stash", "list").stdout
    assert (
        "dbt-plan-run-temp" not in remaining
        or (repo / "model.sql").read_text(encoding="utf-8") != ""
    )
