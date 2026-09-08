"""Execute generated shell blocks with GitHub's Bash error handling."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dbt_plan.cli import _CI_WORKFLOW


def step(name):
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - |\Z)", _CI_WORKFLOW, re.M | re.S
    )
    assert match, f"missing step: {name}"
    return match.group(1)


def script(name):
    return textwrap.dedent(step(name).split("        run: |\n", 1)[1])


def execute(name, project, env, prefix=""):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is needed for the generated Ubuntu workflow")
    body = script(name)
    body = body.replace("${{ github.event.pull_request.base.sha }}", '"$BASE_REF"')
    body = body.replace("${{ github.event.pull_request.head.sha }}", '"$HEAD_REF"')
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", prefix + body],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def environment(tmp_path):
    return {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "RUNNER_TEMP": tmp_path.as_posix(),
        "GITHUB_OUTPUT": (tmp_path / "outputs").as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix(),
        "BASE_REF": "base",
        "HEAD_REF": "head",
    }


def outputs(env):
    path = Path(env["GITHUB_OUTPUT"])
    return (
        dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}
    )


def stub(code, report='{"summary": {}, "models": []}', render_code=None):
    render_code = code if render_code is None else render_code
    return f"""git() {{ return 0; }}
dbt() {{ return 0; }}
dbt-plan() {{
  case "$*" in
    *"--format json"*) printf '%s\\n' {shlex.quote(report)}; return {code} ;;
    *) printf '%s\\n' 'review report'; return {render_code} ;;
  esac
}}
"""


def test_example_equals_generated_workflow():
    example = Path(__file__).parents[1] / "examples/ci-workflow/dbt-plan.yml"
    assert example.read_text(encoding="utf-8") == _CI_WORKFLOW


@pytest.mark.parametrize("policy", ["destructive", "warning", "never"])
@pytest.mark.parametrize("code", [0, 1, 2, 3, 42])
def test_report_cannot_decide_gate_policy(tmp_path, environment, code, policy):
    checked = execute("Check current", tmp_path, environment, stub(code))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    captured = outputs(environment)
    assert captured["exit-code"] == str(code)
    environment.update(CODE=captured["exit-code"], FAIL_ON=policy)
    reported = execute("Report", tmp_path, environment, stub(code))
    assert reported.returncode == 0
    assert Path(environment["GITHUB_STEP_SUMMARY"]).read_text()
    gated = execute("Gate", tmp_path, environment)
    expected = (
        3
        if code not in (0, 1, 2)
        else (
            1
            if (policy == "warning" and code != 0) or (policy == "destructive" and code == 1)
            else 0
        )
    )
    assert gated.returncode == expected, gated.stdout + gated.stderr


@pytest.mark.parametrize("code", [0, 1, 2])
@pytest.mark.parametrize("report", ["", "not JSON", "{}"])
def test_old_package_errors_without_reports_always_fail(tmp_path, environment, code, report):
    checked = execute("Check current", tmp_path, environment, stub(code, report))
    assert checked.returncode == 0, checked.stderr
    environment.update(CODE=outputs(environment)["exit-code"], FAIL_ON="never")
    gated = execute("Gate", tmp_path, environment)
    assert gated.returncode == 3


def test_failed_markdown_render_falls_back_without_changing_gate(tmp_path, environment):
    checked = execute("Check current", tmp_path, environment, stub(1))
    assert checked.returncode == 0
    environment.update(CODE=outputs(environment)["exit-code"], FAIL_ON="destructive")
    report = execute("Report", tmp_path, environment, stub(1, render_code=3))
    assert report.returncode == 0
    assert '"models"' in Path(environment["GITHUB_STEP_SUMMARY"]).read_text()
    assert execute("Gate", tmp_path, environment).returncode == 1


def test_report_failure_does_not_skip_gate():
    assert "continue-on-error: true" in step("Report")
    assert "!cancelled()" in step("Gate")
    assert "steps.check.outcome == 'success'" in step("Gate")


def test_unknown_policy_is_rejected(tmp_path, environment):
    environment.update(CODE="0", FAIL_ON="typo")
    assert execute("Gate", tmp_path, environment).returncode == 3


@pytest.mark.parametrize("name", ["Snapshot base", "Check current"])
def test_compile_failure_stops_before_analysis(tmp_path, environment, name):
    prefix = "git() { return 0; }; dbt() { return 7; }; dbt-plan() { touch wrongly-ran; };\n"
    proc = execute(name, tmp_path, environment, prefix)
    assert proc.returncode == 7
    assert not (tmp_path / "wrongly-ran").exists()
    assert outputs(environment) == {}
