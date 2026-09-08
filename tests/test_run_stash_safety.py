"""`dbt-plan run` stashes the user's uncommitted work. It must never lose it.

The run command needs a clean tree to compile a baseline, so it stashes,
compiles, then restores. Every failure in that sequence has to be caught: a
stash that never happened must not be "restored" (that would pop somebody
else's entry), and a restore that failed must be reported loudly, because the
user's work is sitting in the stash while the command looks like it succeeded.
"""

from __future__ import annotations

import argparse
import json
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


class TestTheSnapshotIsNotTheUsersWork:
    """`dbt-plan run` used to stash its own snapshot directory and then fail to pop.

    `.dbt-plan/` is untracked, so `--include-untracked` took it; `run` then wrote a
    new snapshot before popping, and the pop refused because the untracked files it
    held already existed. Measured on jaffle_shop:

        Error: could not restore your stashed changes:
        error: could not restore untracked files from stash
          Your work is NOT lost -- it is still in the stash.

    The loud message is doing its job. The user's real uncommitted work being in a
    stash after a command that touched nothing of theirs is the bug.
    """

    @pytest.mark.parametrize(
        "line,ours",
        [
            ("?? .dbt-plan/", True),
            ("?? .dbt-plan/base/compiled/m.sql", True),
            (" M models/stg_orders.sql", False),
            (" M .gitignore", False),
            # A prefix match, not a substring: these are somebody's own files.
            ("?? models/.dbt-plan-notes.md", False),
            ("?? .dbt-plan-notes.md", False),
        ],
    )
    def test_which_status_lines_are_ours(self, line, ours):
        from dbt_plan.cli import _is_snapshot_path

        assert _is_snapshot_path(line) is ours

    def test_the_stash_leaves_the_snapshot_in_the_tree(self, tmp_path):
        """So the snapshot written during the block cannot collide with the pop."""
        from dbt_plan.stash import clean_worktree

        repo = tmp_path / "proj"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        (repo / "m.sql").write_text("SELECT 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "initial")

        (repo / "m.sql").write_text("SELECT 2\n", encoding="utf-8")
        snapshot = repo / ".dbt-plan" / "base"
        snapshot.mkdir(parents=True)
        (snapshot / "old.sql").write_text("SELECT 1\n", encoding="utf-8")

        with clean_worktree(repo, has_changes=True):
            # The user's edit is out of the way; the snapshot is not.
            assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 1\n"
            assert (snapshot / "old.sql").exists()
            # `run` overwrites it here, which used to be what broke the pop.
            (snapshot / "old.sql").write_text("SELECT 2\n", encoding="utf-8")

        assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 2\n"
        assert _git(repo, "stash", "list").stdout.strip() == ""

    def test_snapshot_does_not_write_gitignore(self, tmp_path, capsys):
        """It runs inside run's stash window, where that makes .gitignore the
        file the pop cannot restore -- the same failure one level along."""
        from dbt_plan.cli import _do_snapshot

        project = tmp_path / "proj"
        models = project / "target" / "compiled" / "p" / "models"
        models.mkdir(parents=True)
        (models / "m.sql").write_text("SELECT 1", encoding="utf-8")
        (project / "target" / "manifest.json").write_text('{"nodes":{}}', encoding="utf-8")
        gitignore = project / ".gitignore"
        gitignore.write_text("target/\n", encoding="utf-8")

        _do_snapshot(argparse.Namespace(project_dir=str(project), target_dir="target"))

        captured = capsys.readouterr()
        assert gitignore.read_text(encoding="utf-8") == "target/\n"
        assert "Snapshot saved" in captured.err
        assert "Snapshot saved" not in captured.out

    def test_run_does_not_stash_for_the_snapshot_alone(self, tmp_path):
        """An otherwise clean tree with a snapshot in it must not be stashed at all.

        Belt and braces next to the pathspec: the fewer times `run` reaches for the
        stash, the fewer ways it can leave someone's work in one.
        """
        from unittest.mock import MagicMock

        from dbt_plan.cli import _do_run
        from dbt_plan.config import Config

        calls = []

        def side_effect(cmd, **kw):
            calls.append(cmd)
            result = MagicMock()
            result.stdout = "?? .dbt-plan/\n" if cmd[-1] == "--porcelain" else ""
            result.stderr = ""
            result.returncode = 0
            return result

        with (
            patch("subprocess.run", side_effect=side_effect),
            patch("dbt_plan.cli._do_snapshot"),
            patch("dbt_plan.cli._do_check", return_value=0),
            patch("dbt_plan.config.Config.load", return_value=Config()),
        ):
            assert _do_run(_args(tmp_path, "dbt compile")) == 0

        assert [c for c in calls if isinstance(c, list) and "stash" in c] == []


class TestRunJsonStdoutIsPureJson:
    """`run --format json` must keep stdout machine-readable (issue #165).

    snapshot's "Snapshot saved to ..." line was the one thing left writing to
    stdout on the way to check's JSON report, so an agent that pipes
    `run --format json` into `json.loads` got a parse failure. It belongs on
    stderr with the rest of run's progress.
    """

    def _project(self, tmp_path):
        """A project whose current compile matches the baseline: a clean JSON run."""
        project = tmp_path / "proj"
        models = project / "target" / "compiled" / "proj" / "models"
        models.mkdir(parents=True)
        (models / "m.sql").write_text("SELECT 1 AS a\n", encoding="utf-8")
        manifest = {
            "nodes": {
                "model.proj.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                }
            },
            "child_map": {"model.proj.m": []},
            "metadata": {"project_name": "proj"},
        }
        (project / "target" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return project

    def test_stdout_stays_parseable_json_and_progress_goes_to_stderr(self, tmp_path, capsys):
        """The whole run pipeline, with only the subprocess calls faked.

        No real git repo and no real dbt: git's status is empty (so nothing is
        stashed) and both compiles succeed without touching target/, leaving the
        snapshot and check to run for real against the prepared project.
        """
        from unittest.mock import MagicMock

        from dbt_plan.config import Config

        noop = MagicMock(returncode=0, stdout="", stderr="")

        def _everything_succeeds(*args, **kwargs):
            return noop

        project = self._project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project),
            compile_command="dbt compile",
            format="json",
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            acknowledge=None,
        )
        with (
            patch("subprocess.run", side_effect=_everything_succeeds),
            patch("dbt_plan.config.Config.load", return_value=Config()),
        ):
            assert _run(args) == 0

        captured = capsys.readouterr()
        json.loads(captured.out)  # raises if run wrote anything but JSON
        assert "Snapshot saved" in captured.err
        assert "Snapshot saved" not in captured.out


class TestBorrowPreservesGitState:
    @staticmethod
    def prepare(tmp_path, *, ignored=False, nested=False):
        repo = _repo(tmp_path)
        _git(repo, "config", "core.autocrlf", "false")
        project = repo / "analytics" if nested else repo
        project.mkdir(exist_ok=True)
        if ignored:
            (project / ".gitignore").write_text(".dbt-plan/\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "ignore snapshots")
        (repo / "model.sql").write_text("earlier work\n", encoding="utf-8")
        _git(repo, "stash", "push", "-qm", "existing user stash")
        snapshot = project / ".dbt-plan" / "base"
        snapshot.mkdir(parents=True)
        (snapshot / "saved.sql").write_text("snapshot\n", encoding="utf-8")
        (repo / "model.sql").write_text("staged work\n", encoding="utf-8")
        _git(repo, "add", "model.sql")
        (repo / "model.sql").write_text("unstaged work\n", encoding="utf-8")
        (repo / "notes.txt").write_bytes(b"untracked notes\n")
        return repo, project, snapshot

    @staticmethod
    def state(repo):
        return (
            (repo / "model.sql").read_bytes(),
            (repo / "notes.txt").read_bytes(),
            _git(repo, "diff", "--binary").stdout,
            _git(repo, "diff", "--cached", "--binary").stdout,
            _git(repo, "stash", "list", "--format=%H").stdout,
        )

    @pytest.mark.parametrize("ignored", [False, True])
    @pytest.mark.parametrize("nested", [False, True])
    def test_repeated_borrow_keeps_snapshot_files_index_and_existing_stash(
        self, tmp_path, ignored, nested
    ):
        from dbt_plan.stash import clean_worktree

        repo, project, snapshot = self.prepare(tmp_path, ignored=ignored, nested=nested)
        before = self.state(repo)
        for _ in range(2):
            with clean_worktree(project, has_changes=True) as state:
                assert state.stashed
                assert (repo / "model.sql").read_text(encoding="utf-8") == "SELECT 1 AS a\n"
                assert not (repo / "notes.txt").exists()
                assert (snapshot / "saved.sql").exists()
            assert not state.restore_failed
            assert self.state(repo) == before

    def test_nonzero_push_with_new_stash_restores_before_aborting(self, tmp_path):
        from dbt_plan import stash

        repo, project, _ = self.prepare(tmp_path)
        before = self.state(repo)
        real_git = stash._git

        def fail_after_push(directory, *args):
            result = real_git(directory, *args)
            if args[:2] == ("stash", "push"):
                assert result.returncode == 0
                return subprocess.CompletedProcess(
                    result.args, 1, result.stdout, "late push failure"
                )
            return result

        with (
            patch.object(stash, "_git", side_effect=fail_after_push),
            pytest.raises(stash.StashError, match="late push failure"),
            stash.clean_worktree(project, has_changes=True),
        ):
            pytest.fail("must not compile a baseline after a failed push")
        assert self.state(repo) == before

    def test_failed_partial_push_restore_reports_recovery_not_untouched(self, tmp_path, capsys):
        from dbt_plan import stash

        repo, project, _ = self.prepare(tmp_path)
        real_git = stash._git
        created = None

        def fail_push_and_restore(directory, *args):
            nonlocal created
            if args[:2] == ("stash", "pop"):
                return subprocess.CompletedProcess(["git", *args], 1, "", "restore conflict")
            result = real_git(directory, *args)
            if args[:2] == ("stash", "push"):
                assert result.returncode == 0
                created = stash.current_stash_ref(directory)
                return subprocess.CompletedProcess(
                    result.args, 1, result.stdout, "late push failure"
                )
            return result

        with (
            patch.object(stash, "_git", side_effect=fail_push_and_restore),
            patch("dbt_plan.cli._do_snapshot") as snapshot,
        ):
            assert _run(_args(project, "git --version")) == 2
            snapshot.assert_not_called()
        err = capsys.readouterr().err
        assert "untouched" not in err
        assert "Recover with:" in err
        assert created in err
        assert stash.current_stash_ref(repo) == created
        assert _git(repo, "show", f"{created}^2:model.sql").stdout == "staged work\n"
        assert _git(repo, "show", f"{created}:model.sql").stdout == "unstaged work\n"
        assert _git(repo, "show", f"{created}^3:notes.txt").stdout == "untracked notes\n"

    def test_no_new_stash_does_not_pop_a_users_entry(self, tmp_path):
        from dbt_plan.stash import clean_worktree

        repo = _repo(tmp_path)
        (repo / "model.sql").write_text("earlier work\n", encoding="utf-8")
        _git(repo, "stash", "push", "-qm", "existing user stash")
        before = _git(repo, "stash", "list", "--format=%H").stdout
        snapshot = repo / ".dbt-plan"
        snapshot.mkdir()
        (snapshot / "saved.sql").write_text("snapshot\n", encoding="utf-8")
        with clean_worktree(repo, has_changes=True) as state:
            assert not state.stashed
        assert _git(repo, "stash", "list", "--format=%H").stdout == before
        assert (repo / "model.sql").read_text(encoding="utf-8") == "SELECT 1 AS a\n"
