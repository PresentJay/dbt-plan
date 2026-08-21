"""Tests for the ci-setup generated GitHub Actions workflow quality.

Validates that `dbt-plan ci-setup` produces a correct, secure, and complete
GitHub Actions workflow for CI integration.

No PyYAML dependency — uses string inspection on the known _CI_WORKFLOW constant
and structural assertions on the generated file.
"""

from __future__ import annotations

import argparse
import re

import pytest

from dbt_plan.cli import _CI_WORKFLOW, _do_ci_setup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_ci_setup(project_dir):
    """Run _do_ci_setup pointing at project_dir, return the workflow file Path."""
    args = argparse.Namespace(project_dir=str(project_dir))
    _do_ci_setup(args)
    return project_dir / ".github" / "workflows" / "dbt-plan.yml"


# ---------------------------------------------------------------------------
# 1. Generated YAML is valid
# ---------------------------------------------------------------------------


class TestGeneratedYamlValid:
    def test_ci_setup_creates_file(self, tmp_path):
        """ci-setup generates a .yml file that exists and is non-empty."""
        wf_path = _run_ci_setup(tmp_path)
        content = wf_path.read_text()
        assert len(content) > 0

    def test_has_required_top_level_keys(self, tmp_path):
        """Generated workflow has name:, on:, jobs:, and concurrency: at top level."""
        wf_path = _run_ci_setup(tmp_path)
        content = wf_path.read_text()
        # These keys must appear at column 0 (top-level YAML keys)
        assert re.search(r"^name:", content, re.MULTILINE)
        assert re.search(r"^on:", content, re.MULTILINE)
        assert re.search(r"^jobs:", content, re.MULTILINE)
        assert re.search(r"^concurrency:", content, re.MULTILINE)

    def test_name_is_dbt_plan(self, tmp_path):
        """Workflow name is 'dbt-plan'."""
        wf_path = _run_ci_setup(tmp_path)
        content = wf_path.read_text()
        assert re.search(r"^name:\s*dbt-plan\s*$", content, re.MULTILINE)


# ---------------------------------------------------------------------------
# 2. Workflow structure correctness
# ---------------------------------------------------------------------------


class TestWorkflowStructure:
    def test_pull_request_paths_include_models(self):
        """on.pull_request.paths includes models/**."""
        assert "models/**" in _CI_WORKFLOW

    def test_pull_request_paths_include_macros(self):
        """on.pull_request.paths includes macros/**."""
        assert "macros/**" in _CI_WORKFLOW

    def test_concurrency_cancel_in_progress_true(self):
        """concurrency.cancel-in-progress is true."""
        assert re.search(r"cancel-in-progress:\s*true", _CI_WORKFLOW)

    def test_concurrency_group_scoped_to_pr(self):
        """concurrency.group references the PR number."""
        assert "github.event.pull_request.number" in _CI_WORKFLOW

    def test_runs_on_ubuntu_latest(self):
        """Job uses ubuntu-latest runner."""
        assert re.search(r"runs-on:\s*ubuntu-latest", _CI_WORKFLOW)

    def test_pull_requests_write_permission(self):
        """Job has permissions.pull-requests: write."""
        assert re.search(r"pull-requests:\s*write", _CI_WORKFLOW)


# ---------------------------------------------------------------------------
# 3. Steps are complete
# ---------------------------------------------------------------------------


class TestStepsComplete:
    def test_checkout_v4_with_fetch_depth_0(self):
        """Checkout step uses actions/checkout@v4 with fetch-depth: 0."""
        assert "actions/checkout@v4" in _CI_WORKFLOW
        assert "fetch-depth: 0" in _CI_WORKFLOW

    def test_setup_python_v5(self):
        """Setup step uses actions/setup-python@v5."""
        assert "actions/setup-python@v5" in _CI_WORKFLOW

    def test_install_step_has_uv_sync(self):
        """Install step includes 'uv sync'."""
        # Find the Install step block and verify uv sync is present
        install_match = re.search(
            r"- name: Install\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert install_match, "Install step not found"
        install_block = install_match.group(1)
        assert "uv sync" in install_block

    def test_install_step_has_pip_install_dbt_plan(self):
        """Install step includes 'pip install dbt-plan'."""
        install_match = re.search(
            r"- name: Install\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert install_match, "Install step not found"
        install_block = install_match.group(1)
        assert "pip install dbt-plan" in install_block

    def test_snapshot_step_checks_out_base_sha(self):
        """Snapshot step checks out the base SHA."""
        snapshot_match = re.search(
            r"- name: Snapshot base\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert snapshot_match, "Snapshot base step not found"
        snapshot_block = snapshot_match.group(1)
        assert "github.event.pull_request.base.sha" in snapshot_block

    def test_snapshot_step_runs_dbt_compile(self):
        """Snapshot step runs dbt compile."""
        snapshot_match = re.search(
            r"- name: Snapshot base\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert snapshot_match
        assert "dbt compile" in snapshot_match.group(1)

    def test_snapshot_step_runs_dbt_plan_snapshot(self):
        """Snapshot step runs dbt-plan snapshot."""
        snapshot_match = re.search(
            r"- name: Snapshot base\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert snapshot_match
        assert "dbt-plan snapshot" in snapshot_match.group(1)

    def test_check_step_checks_out_head_sha(self):
        """Check step checks out the head SHA."""
        check_match = re.search(
            r"- name: Check current\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert check_match, "Check current step not found"
        check_block = check_match.group(1)
        assert "github.event.pull_request.head.sha" in check_block

    def test_check_step_runs_dbt_compile(self):
        """Check step runs dbt compile."""
        check_match = re.search(
            r"- name: Check current\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert check_match
        assert "dbt compile" in check_match.group(1)

    def test_check_step_uses_format_github(self):
        """Check step runs dbt-plan check --format github."""
        check_match = re.search(
            r"- name: Check current\s*\n\s*run: \|(.+?)(?=\n\s*- )", _CI_WORKFLOW, re.DOTALL
        )
        assert check_match
        assert "dbt-plan check --format github" in check_match.group(1)

    def test_gate_step_runs_dbt_plan_check(self):
        """Gate step runs dbt-plan check (for exit code)."""
        gate_match = re.search(r"- name: Gate\s*\n\s*run:\s*(.+?)(?:\n|$)", _CI_WORKFLOW)
        assert gate_match, "Gate step not found"
        assert "dbt-plan check" in gate_match.group(1)

    def test_steps_are_in_correct_order(self):
        """Steps appear in logical order: checkout, setup-python, install, snapshot, check, gate."""
        positions = {
            "checkout": _CI_WORKFLOW.index("actions/checkout@v4"),
            "setup-python": _CI_WORKFLOW.index("actions/setup-python@v5"),
            "install": _CI_WORKFLOW.index("name: Install"),
            "snapshot": _CI_WORKFLOW.index("name: Snapshot base"),
            "check": _CI_WORKFLOW.index("name: Check current"),
            "gate": _CI_WORKFLOW.index("name: Gate"),
        }
        ordered = sorted(positions.keys(), key=lambda k: positions[k])
        assert ordered == ["checkout", "setup-python", "install", "snapshot", "check", "gate"]


# ---------------------------------------------------------------------------
# 4. Security considerations
# ---------------------------------------------------------------------------


class TestSecurityConsiderations:
    def test_uses_safe_base_sha_reference(self):
        """Workflow uses github.event.pull_request.base.sha (safe, not user-controlled)."""
        assert "github.event.pull_request.base.sha" in _CI_WORKFLOW

    def test_uses_safe_head_sha_reference(self):
        """Workflow uses github.event.pull_request.head.sha (safe for checkout)."""
        assert "github.event.pull_request.head.sha" in _CI_WORKFLOW

    def test_no_pr_title_injection(self):
        """Workflow does not use github.event.pull_request.title (injection vector)."""
        assert "github.event.pull_request.title" not in _CI_WORKFLOW

    def test_no_pr_body_injection(self):
        """Workflow does not use github.event.pull_request.body (injection vector)."""
        assert "github.event.pull_request.body" not in _CI_WORKFLOW

    def test_no_head_ref_injection(self):
        """Workflow does not use github.head_ref (user-controlled branch name)."""
        assert "github.head_ref" not in _CI_WORKFLOW

    def test_no_comment_body_injection(self):
        """Workflow does not use github.event.comment.body (injection vector)."""
        assert "github.event.comment.body" not in _CI_WORKFLOW

    def test_all_dangerous_patterns_absent(self):
        """Comprehensive check: no user-controlled GitHub expressions in workflow."""
        dangerous_patterns = [
            "github.event.pull_request.title",
            "github.event.pull_request.body",
            "github.event.comment.body",
            "github.event.review.body",
            "github.event.issue.title",
            "github.event.issue.body",
            "github.head_ref",
        ]
        for pattern in dangerous_patterns:
            assert pattern not in _CI_WORKFLOW, f"Dangerous pattern found: {pattern}"


# ---------------------------------------------------------------------------
# 5. File placement
# ---------------------------------------------------------------------------


class TestFilePlacement:
    def test_creates_workflow_at_correct_path(self, tmp_path):
        """File is created at .github/workflows/dbt-plan.yml."""
        wf_path = _run_ci_setup(tmp_path)
        assert wf_path.exists()
        assert wf_path.name == "dbt-plan.yml"
        assert wf_path.parent.name == "workflows"
        assert wf_path.parent.parent.name == ".github"

    def test_creates_parent_directories(self, tmp_path):
        """Parent directories .github/workflows/ are created if missing."""
        assert not (tmp_path / ".github").exists()
        _run_ci_setup(tmp_path)
        assert (tmp_path / ".github" / "workflows").is_dir()

    def test_exits_2_if_file_already_exists(self, tmp_path):
        """Second call exits with code 2 when file already exists."""
        _run_ci_setup(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            _run_ci_setup(tmp_path)
        assert exc_info.value.code == 2

    def test_already_exists_message_includes_path(self, tmp_path, capsys):
        """Error message includes the workflow file path."""
        _run_ci_setup(tmp_path)
        with pytest.raises(SystemExit):
            _run_ci_setup(tmp_path)
        captured = capsys.readouterr()
        assert "dbt-plan.yml" in captured.err
        assert "already exists" in captured.err.lower()


# ---------------------------------------------------------------------------
# 6. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_first_call_creates_file(self, tmp_path):
        """First call creates the workflow file successfully."""
        wf_path = tmp_path / ".github" / "workflows" / "dbt-plan.yml"
        assert not wf_path.exists()
        _run_ci_setup(tmp_path)
        assert wf_path.exists()

    def test_second_call_exits_2(self, tmp_path):
        """Second call exits with code 2."""
        _run_ci_setup(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            _run_ci_setup(tmp_path)
        assert exc_info.value.code == 2

    def test_file_content_unchanged_after_failed_second_call(self, tmp_path):
        """File content is not modified when second call fails."""
        _run_ci_setup(tmp_path)
        wf_path = tmp_path / ".github" / "workflows" / "dbt-plan.yml"
        original_content = wf_path.read_text()
        with pytest.raises(SystemExit):
            _run_ci_setup(tmp_path)
        assert wf_path.read_text() == original_content


# ---------------------------------------------------------------------------
# 7. Generated workflow matches _CI_WORKFLOW constant
# ---------------------------------------------------------------------------


class TestContentMatchesConstant:
    def test_file_content_is_exactly_ci_workflow(self, tmp_path):
        """The generated file content is exactly the _CI_WORKFLOW constant."""
        wf_path = _run_ci_setup(tmp_path)
        assert wf_path.read_text() == _CI_WORKFLOW

    def test_ci_workflow_constant_is_nonempty(self):
        """The _CI_WORKFLOW constant is a non-empty string."""
        assert isinstance(_CI_WORKFLOW, str)
        assert len(_CI_WORKFLOW) > 100  # sanity: a real workflow is at least 100 chars

    def test_ci_workflow_starts_with_name(self):
        """The constant starts with 'name:' (valid YAML workflow)."""
        assert _CI_WORKFLOW.strip().startswith("name:")

    def test_ci_workflow_ends_with_newline(self):
        """The constant ends with a newline (POSIX convention)."""
        assert _CI_WORKFLOW.endswith("\n")
