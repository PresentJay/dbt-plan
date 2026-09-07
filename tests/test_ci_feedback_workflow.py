"""The privileged CI helper must never execute a contributor's checkout."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_feedback_executes_only_default_branch_code():
    workflow = (ROOT / ".github/workflows/ci-feedback.yml").read_text(encoding="utf-8")
    assert workflow.count("uses: actions/checkout@") == 2
    assert workflow.count("repository: ${{ github.repository }}") == 2
    assert workflow.count("ref: ${{ github.event.repository.default_branch }}") == 2
    assert workflow.count("persist-credentials: false") == 2
    assert "pull_request.head" not in workflow
    assert "workflow_run.head" not in workflow
    assert "github.event.comment.body }}" not in workflow
    assert "secrets." not in workflow
    assert "download-artifact" not in workflow
    assert not re.search(r"^\s+-?\s*run:", workflow, re.MULTILINE)
    for action in re.findall(r"uses: (\S+)", workflow):
        assert re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", action)


def test_feedback_serializes_per_pr_and_listens_for_results():
    workflow = (ROOT / ".github/workflows/ci-feedback.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in workflow
    assert "issue_comment:" in workflow
    assert "workflows: [CI]" in workflow
    assert "types: [requested, in_progress, completed]" in workflow
    assert "group: ci-feedback-${{ matrix.pr }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "contents: write" not in workflow
    assert "actions: write" in workflow
    resolve_job, report_job = workflow.split("  report:\n", 1)
    assert "pull-requests: read" in resolve_job
    assert "pull-requests: write" not in resolve_job
    # PR conversation comments use the issue-comment endpoint but need PR write
    # permission for the workflow token; issues: write with PR read returned 403.
    assert "pull-requests: write" in report_job
    assert "issues: write" not in workflow


def test_feedback_behavior_tests_are_part_of_required_lint_job():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    lint_job = workflow.split("  lint:", 1)[1].split("\n  test:", 1)[0]
    assert "node --test .github/scripts/ci-feedback.test.cjs" in lint_job
