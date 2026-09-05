"""`dbt-plan run --against main`.

Without it the baseline is HEAD of the current branch, so a change you already
committed is not in the comparison at all:

    $ git commit -am "drop status"
    $ dbt-plan run
    dbt-plan -- no model changes detected

"Before you push" -- which is what the command is for, and what the instructions
`dbt-plan agent-setup` writes into a repo tell an agent to do -- is exactly when
the interesting changes are committed already.

The default is unchanged, because changing it would mean checking out another
commit on every run, and moving someone's HEAD is not something to do uninvited.
What is new is the flag, and a line saying which baseline you got.
"""

from __future__ import annotations

import subprocess

import pytest

from dbt_plan.stash import CheckoutError, borrowed_head, clean_worktree, merge_base


def _git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def repo(tmp_path):
    """main with one commit, then a `feature` branch with a second."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "m.sql").write_text("SELECT 1 AS a\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "checkout", "-qb", "feature")
    (r / "m.sql").write_text("SELECT 2 AS a\n", encoding="utf-8")
    _git(r, "commit", "-qam", "second")
    return r


def _head(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


class TestMergeBase:
    def test_it_resolves_where_the_branch_left(self, repo):
        expected = _git(repo, "rev-parse", "main").stdout.strip()
        assert merge_base(repo, "main") == expected

    def test_it_is_the_branch_point_not_the_tip(self, repo):
        """Otherwise everything merged into main since would be reported as yours."""
        _git(repo, "checkout", "-q", "main")
        (repo / "other.sql").write_text("SELECT 3 AS c\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "someone else")
        moved_tip = _git(repo, "rev-parse", "main").stdout.strip()
        _git(repo, "checkout", "-q", "feature")

        assert merge_base(repo, "main") != moved_tip

    def test_a_ref_that_does_not_exist_says_how_to_get_it(self, repo):
        with pytest.raises(CheckoutError, match="not a commit in this repository"):
            merge_base(repo, "no_such_branch")

    def test_an_unrelated_history_is_named_as_such(self, repo, tmp_path):
        _git(repo, "checkout", "-q", "--orphan", "unrelated")
        _git(repo, "commit", "-qm", "orphan", "--allow-empty")
        with pytest.raises(CheckoutError, match="no common ancestor"):
            merge_base(repo, "main")


class TestBorrowedHead:
    def test_none_does_nothing_at_all(self, repo):
        """The default `dbt-plan run` path: no checkout, so no way to leave HEAD wrong."""
        with borrowed_head(repo, None) as ref:
            assert ref is None
            assert _head(repo) == "feature"
        assert _head(repo) == "feature"

    def test_it_comes_back_to_the_branch_not_a_detached_head(self, repo):
        """`git checkout refs/heads/feature` detaches. Coming back has to switch."""
        base = merge_base(repo, "main")
        with borrowed_head(repo, base):
            assert _head(repo) == "HEAD"  # detached, on purpose, inside the block
            assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 1 AS a\n"
        assert _head(repo) == "feature"
        assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 2 AS a\n"

    def test_it_comes_back_when_the_block_raises(self, repo):
        base = merge_base(repo, "main")
        with pytest.raises(RuntimeError), borrowed_head(repo, base):
            raise RuntimeError("compile failed")
        assert _head(repo) == "feature"

    def test_it_comes_back_from_a_detached_head_too(self, repo):
        _git(repo, "checkout", "-q", "--detach")
        started_at = _git(repo, "rev-parse", "HEAD").stdout.strip()
        with borrowed_head(repo, merge_base(repo, "main")):
            pass
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == started_at

    def test_a_ref_that_cannot_be_checked_out_leaves_head_alone(self, repo):
        with (
            pytest.raises(CheckoutError),
            borrowed_head(repo, "0000000000000000000000000000000000000000"),
        ):
            pytest.fail("should not have entered the block")
        assert _head(repo) == "feature"


class TestTheTwoRestoresTogether:
    """HEAD has to come back before the stash is popped, or the work lands wrong."""

    def test_uncommitted_work_survives_a_borrowed_head(self, repo):
        (repo / "m.sql").write_text("SELECT 2 AS a, 3 AS b\n", encoding="utf-8")
        base = merge_base(repo, "main")

        with clean_worktree(repo, has_changes=True), borrowed_head(repo, base):
            # The baseline is what main had, with the uncommitted work out of the way.
            assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 1 AS a\n"

        assert _head(repo) == "feature"
        assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 2 AS a, 3 AS b\n"
        assert _git(repo, "stash", "list").stdout.strip() == ""

    def test_they_both_restore_when_the_block_raises(self, repo):
        (repo / "m.sql").write_text("SELECT 2 AS a, 3 AS b\n", encoding="utf-8")
        base = merge_base(repo, "main")

        with (
            pytest.raises(RuntimeError),
            clean_worktree(repo, has_changes=True),
            borrowed_head(repo, base),
        ):
            raise RuntimeError("compile failed")

        assert _head(repo) == "feature"
        assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 2 AS a, 3 AS b\n"
        assert _git(repo, "stash", "list").stdout.strip() == ""


class TestThroughTheCommand:
    """The reported failure, end to end. dbt is stubbed; the git work is real."""

    def _args(self, repo, **kw):
        import argparse

        return argparse.Namespace(
            project_dir=str(repo),
            format="text",
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            against=None,
            compile_command="git --version",  # a command that exists and succeeds
            **kw,
        )

    def _run(self, args):
        from dbt_plan.cli import _do_run

        try:
            return _do_run(args)
        except SystemExit as e:
            return int(e.code or 0)

    def test_the_default_says_which_baseline_you_got(self, repo, capsys):
        """The trap was silent. Now the command names it and names the way out."""
        self._run(self._args(repo))
        err = capsys.readouterr().err
        assert "Baseline: your last commit" in err
        assert "already committed on this branch is not compared" in err
        assert "--against main" in err

    def test_against_names_the_branch_point_instead(self, repo, capsys):
        args = self._args(repo)
        args.against = "main"
        self._run(args)
        err = capsys.readouterr().err
        assert "Baseline: where this branch left main" in err
        assert "your last commit" not in err

    def test_a_bad_ref_stops_before_touching_anything(self, repo, capsys):
        args = self._args(repo)
        args.against = "no_such_branch"
        (repo / "m.sql").write_text("SELECT 2 AS a, 3 AS b\n", encoding="utf-8")

        assert self._run(args) == 2
        assert "not a commit in this repository" in capsys.readouterr().err
        assert _head(repo) == "feature"
        assert (repo / "m.sql").read_text(encoding="utf-8") == "SELECT 2 AS a, 3 AS b\n"
        assert _git(repo, "stash", "list").stdout.strip() == ""
