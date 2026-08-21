"""Subprocess safety audit for _do_run command.

Tests shlex.split edge cases, version-check logic, shell injection safety,
and documents structural concerns about git stash and compile failure handling.
"""

from __future__ import annotations

import argparse
import shlex
from unittest.mock import MagicMock, patch

from dbt_plan.cli import _do_run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_args(project_dir: str, **overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for _do_run."""
    defaults = dict(
        project_dir=project_dir,
        format="text",
        no_color=True,
        verbose=False,
        dialect=None,
        select=None,
        compile_command=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _mock_config(compile_command: str = "dbt compile"):
    """Create a mock Config object."""
    cfg = MagicMock()
    cfg.compile_command = compile_command
    return cfg


# ===========================================================================
# 1. shlex.split safety
# ===========================================================================


class TestShlexSplitSafety:
    """Verify shlex.split handles various compile_command inputs correctly."""

    def test_simple_command(self):
        """Standard: 'dbt compile' -> ['dbt', 'compile']."""
        assert shlex.split("dbt compile") == ["dbt", "compile"]

    def test_wrapper_command(self):
        """Wrapper: 'uv run dbt compile' -> ['uv', 'run', 'dbt', 'compile']."""
        assert shlex.split("uv run dbt compile") == ["uv", "run", "dbt", "compile"]

    def test_quoted_args_single(self):
        """Quoted args with single quotes are preserved correctly."""
        result = shlex.split("dbt compile --target 'my target'")
        assert result == ["dbt", "compile", "--target", "my target"]

    def test_quoted_args_double(self):
        """Quoted args with double quotes are preserved correctly."""
        result = shlex.split('dbt compile --target "my target"')
        assert result == ["dbt", "compile", "--target", "my target"]

    def test_empty_string_returns_empty_list(self):
        """Empty string -> [] which will cause IndexError on [0] access."""
        result = shlex.split("")
        assert result == []

    def test_whitespace_only_returns_empty_list(self):
        """Whitespace-only string -> [] which will cause IndexError on [0] access."""
        result = shlex.split("   ")
        assert result == []

    def test_pipe_is_literal_arg(self):
        """Pipe character is NOT interpreted as shell pipe — it becomes a literal arg."""
        result = shlex.split("dbt compile | tee log.txt")
        assert result == ["dbt", "compile", "|", "tee", "log.txt"]

    def test_redirect_is_literal_arg(self):
        """Redirect operator is NOT interpreted — becomes literal arg."""
        result = shlex.split("dbt compile > /dev/null")
        assert result == ["dbt", "compile", ">", "/dev/null"]

    def test_semicolon_is_literal_arg(self):
        """Semicolon is NOT interpreted as command separator."""
        result = shlex.split("dbt compile ; rm -rf /")
        assert result == ["dbt", "compile", ";", "rm", "-rf", "/"]

    def test_backtick_injection(self):
        """Backtick command substitution is NOT interpreted."""
        result = shlex.split("dbt compile `whoami`")
        # shlex treats backticks as regular characters in default mode
        assert "`whoami`" in result

    def test_dollar_substitution(self):
        """$() command substitution is NOT interpreted by shlex — but gets split.

        shlex.split treats $( and ) as regular chars, splitting on whitespace.
        'dbt compile $(cat /etc/passwd)' -> ['dbt', 'compile', '$(cat', '/etc/passwd)']
        This is still SAFE because subprocess list form does not interpret them.
        """
        result = shlex.split("dbt compile $(whoami)")
        assert "$(whoami)" in result
        # Multi-word subshell gets split by shlex:
        result2 = shlex.split("dbt compile $(cat /etc/passwd)")
        assert result2 == ["dbt", "compile", "$(cat", "/etc/passwd)"]


class TestEmptyCompileCommandHandled:
    """_do_run now guards against empty compile_command (previously would IndexError).

    The guard was added as defense-in-depth. In practice, Config._load_file
    and _load_env both have `if value:` / `if cc:` guards that prevent empty
    strings from overwriting the default 'dbt compile'. But if someone bypasses
    config and passes compile_command="" directly, the explicit guard catches it.
    """

    @patch("dbt_plan.config.Config.load")
    def test_empty_compile_command_returns_2(self, mock_config_load):
        """Empty compile_command returns exit code 2 with helpful message."""
        mock_config_load.return_value = _mock_config("")
        args = _make_run_args("/tmp/fake", compile_command="")

        exit_code = _do_run(args)
        assert exit_code == 2

    @patch("dbt_plan.config.Config.load")
    def test_whitespace_compile_command_returns_2(self, mock_config_load):
        """Whitespace-only compile_command returns exit code 2 with helpful message."""
        mock_config_load.return_value = _mock_config("   ")
        args = _make_run_args("/tmp/fake", compile_command="   ")

        exit_code = _do_run(args)
        assert exit_code == 2

    @patch("dbt_plan.config.Config.load")
    def test_empty_compile_command_error_message(self, mock_config_load, capsys):
        """Error message for empty compile_command is informative."""
        mock_config_load.return_value = _mock_config("")
        args = _make_run_args("/tmp/fake", compile_command="")

        _do_run(args)

        captured = capsys.readouterr()
        assert "compile command is empty" in captured.err
        assert "compile_command" in captured.err


# ===========================================================================
# 2. Version check bypass — non-zero exit != "not found"
# ===========================================================================


class TestVersionCheckLogic:
    """The code treats ANY non-zero --version exit code as 'command not found'.

    This is overly broad: a command might exist but not support --version,
    or --version might fail for reasons other than missing binary.
    """

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_version_check_nonzero_treated_as_not_found(self, mock_config_load, mock_run):
        """A valid command returning exit code 1 for --version is rejected."""
        mock_config_load.return_value = _mock_config("mycli compile")
        args = _make_run_args("/tmp/fake", compile_command="mycli compile")

        # --version call returns non-zero
        version_result = MagicMock()
        version_result.returncode = 1
        mock_run.return_value = version_result

        exit_code = _do_run(args)
        assert exit_code == 2

        # Verify it tried the --version check
        mock_run.assert_called_once_with(["mycli", "--version"], capture_output=True)

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_version_check_file_not_found(self, mock_config_load, mock_run):
        """FileNotFoundError is caught and treated as command not found."""
        mock_config_load.return_value = _mock_config("nonexistent compile")
        args = _make_run_args("/tmp/fake", compile_command="nonexistent compile")

        mock_run.side_effect = FileNotFoundError("No such file")

        exit_code = _do_run(args)
        assert exit_code == 2

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_version_check_success_proceeds(self, mock_config_load, mock_run, tmp_path):
        """When --version returns 0, the code proceeds to git status check."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_count = 0

        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                # --version check succeeds
                result.returncode = 0
                return result
            elif call_count == 2:
                # git status fails (not a git repo)
                result.returncode = 128
                result.stdout = ""
                return result
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        # Should fail at git status step, not at version check
        assert exit_code == 2
        assert call_count == 2


# ===========================================================================
# 3. git stash safety — structural analysis
# ===========================================================================


class TestGitStashSafety:
    """Document git stash concerns via structural code analysis.

    These tests verify the code structure handles stash push/pop correctly
    by tracing the subprocess calls in various scenarios.
    """

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_stash_pop_called_if_baseline_compile_fails(
        self, mock_config_load, mock_run, tmp_path
    ):
        """If baseline compile fails, stash must be popped to restore changes."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_sequence = []

        def side_effect(cmd, **kw):
            call_sequence.append(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = "compile error"
            result.returncode = 0

            if cmd == ["dbt", "--version"]:
                result.returncode = 0
            elif cmd == ["git", "status", "--porcelain"]:
                result.stdout = "M models/test.sql\n"  # has changes
                result.returncode = 0
            elif (
                isinstance(cmd, list)
                and len(cmd) > 2
                and cmd[0:2] == ["git", "stash"]
                and "push" in cmd
            ):
                result.returncode = 0
            elif cmd == ["dbt", "compile"]:
                # Baseline compile fails
                result.returncode = 1
                result.stderr = "dbt compile failed"
            elif cmd == ["git", "stash", "pop"]:
                result.returncode = 0
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 2

        # Verify stash pop was called after compile failure
        stash_push_seen = False
        stash_pop_seen = False
        for cmd in call_sequence:
            if isinstance(cmd, list) and "push" in cmd and "stash" in cmd:
                stash_push_seen = True
            if cmd == ["git", "stash", "pop"]:
                stash_pop_seen = True

        assert stash_push_seen, "stash push should have been called"
        assert stash_pop_seen, "stash pop should have been called after baseline compile failure"

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_no_stash_when_no_changes(self, mock_config_load, mock_run, tmp_path):
        """When git status shows no changes, stash push/pop should be skipped."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_sequence = []

        def side_effect(cmd, **kw):
            call_sequence.append(cmd if isinstance(cmd, list) else list(cmd))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["dbt", "--version"]:
                pass
            elif cmd == ["git", "status", "--porcelain"]:
                result.stdout = ""  # no changes
            elif cmd == ["dbt", "compile"]:
                # Compile fails so we can exit early without needing full project
                result.returncode = 1
                result.stderr = "no project"
            return result

        mock_run.side_effect = side_effect

        _do_run(args)

        # Verify no stash commands were issued
        stash_cmds = [
            cmd
            for cmd in call_sequence
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[0:2] == ["git", "stash"]
        ]
        assert stash_cmds == [], f"No stash commands expected, got: {stash_cmds}"

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_stash_push_failure_is_silent(self, mock_config_load, mock_run, tmp_path):
        """git stash push uses capture_output=True — failures are not reported.

        This is a documented concern: if stash push fails (e.g., nothing to stash
        because changes were committed between status check and stash), the error
        is silently swallowed and the code continues as if stash succeeded.
        """
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        def side_effect(cmd, **kw):
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["dbt", "--version"]:
                pass
            elif cmd == ["git", "status", "--porcelain"]:
                result.stdout = "M models/test.sql\n"
            elif isinstance(cmd, list) and "push" in cmd and "stash" in cmd:
                # stash push fails
                result.returncode = 1
                result.stderr = "error: cannot stash"
            elif cmd == ["dbt", "compile"]:
                result.returncode = 1
                result.stderr = "compile error"
            return result

        mock_run.side_effect = side_effect

        # The function does NOT check stash push return code, so it continues
        exit_code = _do_run(args)
        # It proceeds to compile, which fails, then tries stash pop
        assert exit_code == 2


# ===========================================================================
# 4. Compile failure handling — stderr propagation
# ===========================================================================


class TestCompileFailureHandling:
    """Verify that compile failures report stderr and handle stash correctly."""

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_baseline_compile_stderr_propagated(
        self, mock_config_load, mock_run, tmp_path, capsys
    ):
        """When baseline compile fails, stderr should appear in error message."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        def side_effect(cmd, **kw):
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["dbt", "--version"]:
                pass
            elif cmd == ["git", "status", "--porcelain"]:
                result.stdout = ""  # no changes, skip stash
            elif cmd == ["dbt", "compile"]:
                result.returncode = 1
                result.stderr = "Database connection timeout after 30s"
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 2

        captured = capsys.readouterr()
        assert "Database connection timeout after 30s" in captured.err

    @patch("dbt_plan.cli._do_snapshot")
    @patch("dbt_plan.cli._do_check")
    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_current_compile_stderr_propagated(
        self, mock_config_load, mock_run, mock_check, mock_snapshot, tmp_path, capsys
    ):
        """When current (post-stash-pop) compile fails, stderr should appear."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        compile_call = 0

        def side_effect(cmd, **kw):
            nonlocal compile_call
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["dbt", "--version"]:
                pass
            elif cmd == ["git", "status", "--porcelain"]:
                result.stdout = ""  # no changes, skip stash
            elif cmd == ["dbt", "compile"]:
                compile_call += 1
                if compile_call == 1:
                    # baseline compile succeeds
                    result.returncode = 0
                else:
                    # current compile fails
                    result.returncode = 1
                    result.stderr = "Syntax error in model_x.sql line 42"
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 2

        captured = capsys.readouterr()
        assert "Syntax error in model_x.sql line 42" in captured.err


# ===========================================================================
# 5. Race condition documentation (not testable in unit tests)
# ===========================================================================


class TestRaceConditionDocumentation:
    """Document the theoretical race condition between git status and git stash.

    The code flow is:
      1. git status --porcelain  -> check if there are changes
      2. (time passes)
      3. git stash push          -> stash those changes

    Between steps 1 and 3, a file could be:
      - Modified by an editor autosave
      - Created by a background process (e.g., dbt logs)
      - Deleted by another tool

    This means the stash might capture more or fewer changes than expected.

    This is NOT testable in unit tests because it requires real filesystem
    timing. It is documented here as a known limitation.

    Mitigation: The `--include-untracked` flag on stash push helps capture
    new files, but does not eliminate the fundamental TOCTOU gap.
    """

    def test_race_condition_documented(self):
        """Placeholder test documenting the TOCTOU gap between status and stash."""
        # This test exists only to document the concern in test output
        # The race condition is between git status (line ~689) and git stash push (line ~714)
        pass


# ===========================================================================
# 6. Shell injection safety — pipes and redirects in compile_command
# ===========================================================================


class TestShellInjectionSafety:
    """Verify that shell metacharacters in compile_command are NOT interpreted.

    Because subprocess.run uses list form (no shell=True), pipes, redirects,
    semicolons, and command substitution are passed as literal arguments to
    the command. This is the SAFE behavior — but may confuse users who expect
    shell features to work.
    """

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_pipe_passed_as_literal_args(self, mock_config_load, mock_run, tmp_path):
        """'dbt compile | tee log' -> dbt receives '|', 'tee', 'log' as args."""
        mock_config_load.return_value = _mock_config("dbt compile | tee log.txt")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile | tee log.txt")

        # Version check: the first token is 'dbt'
        version_result = MagicMock()
        version_result.returncode = 0
        git_status = MagicMock()
        git_status.returncode = 0
        git_status.stdout = ""
        compile_result = MagicMock()
        compile_result.returncode = 1
        compile_result.stderr = "unknown arg: |"

        mock_run.side_effect = [version_result, git_status, compile_result]

        _do_run(args)

        # Verify the compile call passed pipe as literal arg (list form, no shell)
        compile_call = mock_run.call_args_list[2]
        assert compile_call[0][0] == ["dbt", "compile", "|", "tee", "log.txt"]

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_redirect_passed_as_literal_args(self, mock_config_load, mock_run, tmp_path):
        """'dbt compile > /dev/null' -> dbt receives '>' and '/dev/null' as args."""
        mock_config_load.return_value = _mock_config("dbt compile > /dev/null")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile > /dev/null")

        version_result = MagicMock()
        version_result.returncode = 0
        git_status = MagicMock()
        git_status.returncode = 0
        git_status.stdout = ""
        compile_result = MagicMock()
        compile_result.returncode = 1
        compile_result.stderr = "unknown arg: >"

        mock_run.side_effect = [version_result, git_status, compile_result]

        _do_run(args)

        compile_call = mock_run.call_args_list[2]
        assert compile_call[0][0] == ["dbt", "compile", ">", "/dev/null"]

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_semicolon_injection_safe(self, mock_config_load, mock_run, tmp_path):
        """'dbt compile ; rm -rf /' -> dbt receives ';', 'rm', '-rf', '/' as literal args."""
        mock_config_load.return_value = _mock_config("dbt compile ; rm -rf /")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile ; rm -rf /")

        version_result = MagicMock()
        version_result.returncode = 0
        git_status = MagicMock()
        git_status.returncode = 0
        git_status.stdout = ""
        compile_result = MagicMock()
        compile_result.returncode = 1
        compile_result.stderr = "unknown arg"

        mock_run.side_effect = [version_result, git_status, compile_result]

        _do_run(args)

        compile_call = mock_run.call_args_list[2]
        # The dangerous command is NOT executed — it's just a literal arg to dbt
        assert compile_call[0][0] == ["dbt", "compile", ";", "rm", "-rf", "/"]

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_subshell_injection_safe(self, mock_config_load, mock_run, tmp_path):
        """'dbt compile $(cat /etc/passwd)' -> split into literal args, not executed.

        shlex.split splits on whitespace inside $(), producing:
          ['dbt', 'compile', '$(cat', '/etc/passwd)']
        This is still SAFE because subprocess list form does not interpret $().
        The command 'dbt' receives '$(cat' and '/etc/passwd)' as literal args.
        """
        mock_config_load.return_value = _mock_config("dbt compile $(cat /etc/passwd)")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile $(cat /etc/passwd)")

        version_result = MagicMock()
        version_result.returncode = 0
        git_status = MagicMock()
        git_status.returncode = 0
        git_status.stdout = ""
        compile_result = MagicMock()
        compile_result.returncode = 1
        compile_result.stderr = "error"

        mock_run.side_effect = [version_result, git_status, compile_result]

        _do_run(args)

        compile_call = mock_run.call_args_list[2]
        # shlex splits $(cat /etc/passwd) into two tokens, but neither is executed
        assert compile_call[0][0] == ["dbt", "compile", "$(cat", "/etc/passwd)"]


# ===========================================================================
# 7. git not found handling
# ===========================================================================


class TestGitNotFound:
    """Verify behavior when git is not installed."""

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_git_not_found_returns_2(self, mock_config_load, mock_run, tmp_path):
        """If git binary is missing, return exit code 2 with helpful message."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_count = 0

        def side_effect(cmd, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # --version check succeeds
                result = MagicMock()
                result.returncode = 0
                return result
            # git status -> FileNotFoundError
            raise FileNotFoundError("git not found")

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 2

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_not_a_git_repo_returns_2(self, mock_config_load, mock_run, tmp_path):
        """If not in a git repo, git status returns non-zero -> exit code 2."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        def side_effect(cmd, **kw):
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            if cmd == ["dbt", "--version"]:
                result.returncode = 0
            elif cmd == ["git", "status", "--porcelain"]:
                result.returncode = 128  # not a git repo
            else:
                result.returncode = 0
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 2


# ===========================================================================
# 8. Full happy path — structural verification
# ===========================================================================


class TestFullRunStructure:
    """Verify the full _do_run call sequence with mocked subprocess."""

    @patch("dbt_plan.cli._do_check")
    @patch("dbt_plan.cli._do_snapshot")
    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_happy_path_with_changes(
        self, mock_config_load, mock_run, mock_snapshot, mock_check, tmp_path
    ):
        """Full run with uncommitted changes: version -> status -> stash -> compile -> snapshot -> pop -> compile -> check."""
        mock_config_load.return_value = _mock_config("dbt compile")
        mock_check.return_value = 0
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_sequence = []

        def side_effect(cmd, **kw):
            call_sequence.append(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["git", "status", "--porcelain"]:
                result.stdout = "M models/test.sql\n"
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 0

        # Verify the expected call sequence
        assert call_sequence[0] == ["dbt", "--version"]
        assert call_sequence[1] == ["git", "status", "--porcelain"]

        # stash push
        assert call_sequence[2][0:2] == ["git", "stash"]
        assert "push" in call_sequence[2]

        # baseline compile
        assert call_sequence[3] == ["dbt", "compile"]

        # stash pop (after snapshot)
        assert call_sequence[4] == ["git", "stash", "pop"]

        # current compile
        assert call_sequence[5] == ["dbt", "compile"]

        # snapshot and check were called
        mock_snapshot.assert_called_once()
        mock_check.assert_called_once()

    @patch("dbt_plan.cli._do_check")
    @patch("dbt_plan.cli._do_snapshot")
    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_happy_path_without_changes(
        self, mock_config_load, mock_run, mock_snapshot, mock_check, tmp_path
    ):
        """Run without uncommitted changes: no stash push/pop."""
        mock_config_load.return_value = _mock_config("dbt compile")
        mock_check.return_value = 1  # destructive
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_sequence = []

        def side_effect(cmd, **kw):
            call_sequence.append(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0
            return result

        mock_run.side_effect = side_effect

        exit_code = _do_run(args)
        assert exit_code == 1

        # No stash commands
        stash_cmds = [c for c in call_sequence if isinstance(c, list) and "stash" in c]
        assert stash_cmds == []

        # Only: version, status, compile (baseline), compile (current)
        assert len(call_sequence) == 4
        assert call_sequence[0] == ["dbt", "--version"]
        assert call_sequence[1] == ["git", "status", "--porcelain"]
        assert call_sequence[2] == ["dbt", "compile"]
        assert call_sequence[3] == ["dbt", "compile"]


# ===========================================================================
# 9. Stash return code not checked — structural bug documentation
# ===========================================================================


class TestStashReturnCodeNotChecked:
    """The code does not check the return code of git stash push.

    This means:
    - If stash push fails (nothing to stash, permission error), the code
      proceeds with baseline compile on a dirty working tree.
    - The baseline compile result may include uncommitted changes.
    - The subsequent stash pop may fail or be a no-op.

    This is a defense-in-depth concern. In practice, the git status check
    filters most cases, but a TOCTOU race could cause this.
    """

    @patch("subprocess.run")
    @patch("dbt_plan.config.Config.load")
    def test_stash_push_return_code_ignored(self, mock_config_load, mock_run, tmp_path):
        """Verify that stash push failure does not halt execution."""
        mock_config_load.return_value = _mock_config("dbt compile")
        args = _make_run_args(str(tmp_path), compile_command="dbt compile")

        call_sequence = []

        def side_effect(cmd, **kw):
            call_sequence.append(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0

            if cmd == ["git", "status", "--porcelain"]:
                result.stdout = "M dirty.sql\n"
            elif isinstance(cmd, list) and "push" in cmd and "stash" in cmd:
                result.returncode = 1  # stash push FAILS
                result.stderr = "No local changes to save"
            elif cmd == ["dbt", "compile"]:
                result.returncode = 1
                result.stderr = "fail"
            return result

        mock_run.side_effect = side_effect

        # Code continues despite stash push failure
        exit_code = _do_run(args)
        assert exit_code == 2

        # The code still attempted compile after failed stash
        compile_cmds = [c for c in call_sequence if c == ["dbt", "compile"]]
        assert len(compile_cmds) >= 1, "compile was attempted despite stash failure"
