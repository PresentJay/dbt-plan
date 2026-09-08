"""The checked-in protection policy must require CI's actual check contexts.

Like the other workflow tests, this inspects our deliberately simple YAML shape
without adding a YAML runtime/test dependency. Unsupported naming or matrix
shapes fail explicitly so changes cannot silently omit required contexts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).parents[1]


def _required_checks() -> list[str]:
    script = (_ROOT / "scripts" / "apply-repo-settings.sh").read_text(encoding="utf-8")
    match = re.search(r"^REQUIRED_CHECKS='([^']+)'$", script, re.MULTILINE)
    assert match, "expected a literal JSON required-check list"
    checks = json.loads(match.group(1))
    assert isinstance(checks, list) and all(isinstance(check, str) for check in checks)
    assert len(checks) == len(set(checks)), "duplicate required check contexts"
    return checks


def test_protection_requires_all_validation_tiers():
    """Deleting a job and its protection entry cannot silently drop a tier."""
    required = set(_required_checks())
    assert {"lint", "minimum-deps", "integrations", "packaging"} <= required
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f"test ({version})" in required
        assert f"test-windows ({version})" in required


def test_required_contexts_match_workflow_jobs():
    """Catch both unchecked new jobs and nonexistent contexts that block merges."""
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = workflow.split("\njobs:\n", 1)[1]
    blocks = re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)", jobs, re.MULTILINE | re.DOTALL)
    assert blocks, "no CI jobs found"
    contexts = set()
    for job, block in blocks:
        assert not re.search(r"^    name:", block, re.MULTILINE), (
            f"{job}: update this check for a custom job name"
        )
        matrix = re.search(
            r"^      matrix:\n(.*?)(?=^      \S|^    \S|\Z)", block, re.MULTILINE | re.DOTALL
        )
        if matrix is None:
            assert "matrix:" not in block, f"{job}: unsupported matrix shape"
            contexts.add(job)
            continue
        entries = [line.strip() for line in matrix.group(1).splitlines() if line.strip()]
        assert len(entries) == 1 and entries[0].startswith("python-version: ["), (
            f"{job}: update this check for the new matrix dimensions"
        )
        versions = json.loads(entries[0].split(":", 1)[1])
        assert versions and all(isinstance(version, str) for version in versions)
        contexts.update(f"{job} ({version})" for version in versions)
    required = set(_required_checks())
    assert required == contexts, (
        f"CI jobs missing from protection: {contexts - required}; "
        f"required checks with no CI job: {required - contexts}"
    )
