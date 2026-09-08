"""Run real Action shell blocks locally; CI also invokes the composite via uses:."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.test_dbt_e2e import _missing_requirement

pytestmark = pytest.mark.skipif(
    _missing_requirement() is not None or shutil.which("bash") is None or sys.platform == "win32",
    reason=_missing_requirement() or "the real Action integration runs on POSIX with Bash",
)
ROOT = Path(__file__).parents[1]


def execute(name, project, env):
    action = (ROOT / "action.yml").read_text()
    block = re.search(rf"^    - name: {re.escape(name)}\n(.*?)(?=^    - |\Z)", action, re.M | re.S)
    assert block
    script = textwrap.dedent(block.group(1).split("      run: |\n", 1)[1])
    # Exercise this checkout; CI instead installs its built wheel and uses the Action.
    prefix = f'dbt-plan() {{ {shlex.join([sys.executable, "-m", "dbt_plan.cli"])} "$@"; }}\n'
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", prefix + script],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize("finding", ["safe", "warning", "destructive", "base-error", "head-error"])
def test_real_action_revisions_reports_and_policy(tmp_path, finding):
    from tests.action_project import prepare, verify

    case = prepare(tmp_path / "project with spaces", finding)
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(runner / "outputs"),
        "GITHUB_STEP_SUMMARY": str(runner / "summary"),
        "BASE_REF": case["base"],
        "TARGET_DIR": "compiled target",
        "COMPILE": "git rev-parse HEAD >> compile.log; dbt compile --profiles-dir . --target-path 'compiled target'",
        "DIALECT": "",
        "SUMMARY": "true",
        "FAIL_ON": "never" if "error" in finding else "destructive",
    }
    for name in ("Snapshot base", "Compile current", "Check"):
        result = execute(name, Path(case["project"]), env)
        if result.returncode:
            assert "error" in finding, result.stdout + result.stderr
            verify(case, "failure", {}, runner / "dbt-plan-report.json")
            return
    assert "error" not in finding
    outputs = dict(line.split("=", 1) for line in (runner / "outputs").read_text().splitlines())
    assert (runner / "summary").read_text().strip()
    env.update(CODE=outputs["exit-code"], VERDICT=outputs["verdict"])
    for policy in ("never", "warning", "destructive"):
        env["FAIL_ON"] = policy
        gate = execute("Gate", Path(case["project"]), env)
        blocked = (
            policy == "warning"
            and finding != "safe"
            or policy == "destructive"
            and finding == "destructive"
        )
        assert gate.returncode == int(blocked), gate.stdout + gate.stderr
    verify(
        case,
        "failure" if finding == "destructive" else "success",
        outputs,
        Path(outputs["report"]),
    )


@pytest.mark.parametrize(
    "dialect,expected",
    [("", "destructive"), ("postgres", "destructive"), ("snowflake", "warning")],
)
def test_action_manifest_dialect_and_explicit_override(tmp_path, dialect, expected):
    """Postgres JSON operators must keep their parser and cascade warning."""
    target = tmp_path / "target"
    sql_dir = target / "compiled/p/models"
    sql_dir.mkdir(parents=True)
    nodes = {}
    for name, materialized in [("orders", "view"), ("consumer", "table")]:
        nodes[f"model.p.{name}"] = {
            "name": name,
            "resource_type": "model",
            "package_name": "p",
            "path": f"models/{name}.sql",
            "config": {"materialized": materialized},
            "unrendered_config": {},
        }
    manifest = {
        "metadata": {"project_name": "p", "adapter_type": "postgres"},
        "nodes": nodes,
        "child_map": {"model.p.orders": ["model.p.consumer"]},
    }
    (target / "manifest.json").write_text(json.dumps(manifest))
    source = "select id, customer_id from raw.orders where meta @> '{\"k\":1}'::jsonb"
    (sql_dir / "orders.sql").write_text(source)
    (sql_dir / "consumer.sql").write_text("select customer_id from orders")
    snap = subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", "snapshot"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert snap.returncode == 0, snap.stderr
    (sql_dir / "orders.sql").write_text(source.replace(", customer_id", ""))
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "TARGET_DIR": "target",
        "DIALECT": dialect,
        "SUMMARY": "true",
    }
    result = execute("Check", tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "dbt-plan-report.json").read_text())
    if dialect == "snowflake":
        assert report["parse_failures"] == ["orders"]
    else:
        assert report["parse_failures"] == []
        assert report["models"][0]["safety"] == expected
    assert f"verdict={expected}" in (tmp_path / "outputs").read_text()
