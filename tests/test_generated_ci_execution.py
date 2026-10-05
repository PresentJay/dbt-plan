"""Execute generated shell blocks with GitHub's Bash error handling."""

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


def stub(code, report=None, render_code=None):
    if report is None:
        report = json.dumps(
            {"summary": {"total": 0, "safe": 0, "warning": 0, "destructive": 0}, "models": []}
        )
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


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize("malformed_graph", [False, True])
def test_graph_without_findings_cannot_bypass_validator(
    tmp_path, environment, wrapper, malformed_graph
):
    from dbt_plan.formatter import CheckResult, format_json

    data = json.loads(format_json(CheckResult()))
    if malformed_graph:
        data["causal_graph"] = {"edges": "malformed"}
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="false")
    prefix = stub(0, json.dumps(data))
    checked = (
        shell(prefix + action_script("Check"), tmp_path, environment)
        if wrapper == "action"
        else execute("Check current", tmp_path, environment, prefix)
    )
    captured = outputs(environment)
    if malformed_graph:
        assert checked.returncode == 3 if wrapper == "action" else captured["exit-code"] == "3"
        assert captured.get("verdict") != "safe"
    else:
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert captured["verdict"] == "safe"


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("canonical", "destructive"),
        ("unknown_reason", "warning"),
        ("nested", "destructive"),
        ("unknown_risk", "warning"),
        ("malformed_nested", None),
        ("malformed_canonical", None),
    ],
)
def test_nested_and_canonical_risks_are_not_safe(tmp_path, environment, wrapper, kind, expected):
    from dbt_plan.formatter import CheckResult, format_json
    from tests.test_finding_adapters import payload

    data = json.loads(format_json(CheckResult()))
    if kind in {"canonical", "unknown_reason", "malformed_canonical"}:
        fact = next(f for f in payload()["findings"] if f["rule_code"] == "ddl.drop_column")
        data["findings"] = [fact]
        if kind == "unknown_reason":
            fact.update(rule_code="ddl.add_column", severity="safe", raw_risk="safe")
            fact["evidence"]["reason_code"] = "future_reason"
        elif kind == "malformed_canonical":
            fact["evidence"] = {}
    else:
        data["summary"].update(total=1, safe=1)
        data["models"] = [
            {
                "model_name": "orders",
                "safety": "safe",
                "downstream_impacts": [
                    {
                        "model_name": "reader",
                        "risk": "future" if kind == "unknown_risk" else "broken_ref",
                        "reason": "risk",
                    }
                ],
            }
        ]
        if kind == "malformed_nested":
            data["models"][0]["downstream_impacts"][0]["risk"] = []
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="false")
    prefix = stub(0, json.dumps(data))
    checked = (
        shell(prefix + action_script("Check"), tmp_path, environment)
        if wrapper == "action"
        else execute("Check current", tmp_path, environment, prefix)
    )
    captured = outputs(environment)
    if expected is None:
        assert checked.returncode == 3 if wrapper == "action" else captured["exit-code"] == "3"
    else:
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert captured["verdict"] == expected
        assert captured["exit-code"] == "0"  # Raw facts never rewrite gate/ack policy.


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


def action_script(name):
    from tests.test_action_yml import ACTION_TEXT

    block = ACTION_TEXT.split(f"    - name: {name}\n", 1)[1].split("    - name:", 1)[0]
    return textwrap.dedent(block.split("      run: |\n", 1)[1])


def shell(body, project, env):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is needed for CI shell controls")
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", body],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize("policy", ["destructive", "warning", "never"])
@pytest.mark.parametrize("lower", ["never", "destructive", "invalid", ""])
@pytest.mark.parametrize("risk,warning_code", [("destructive", 2), ("warning", 2), ("warning", 0)])
def test_ci_owns_policy(tmp_path, environment, wrapper, policy, lower, risk, warning_code):
    from tests.test_fail_on import project_with_risk

    project = project_with_risk(tmp_path, risk)
    (project / ".dbt-plan.yml").write_text(
        f"fail_on: {lower}\nwarning_exit_code: {warning_code}\n"
    )
    environment.update(DBT_PLAN_FAIL_ON=lower, TARGET_DIR="target", DIALECT="", SUMMARY="true")
    if wrapper == "action":
        checked = shell(action_script("Check"), project, environment)
    else:
        checked = execute("Check current", project, environment, "git() { :; }; dbt() { :; };\n")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    captured = outputs(environment)
    expected_code = 1 if risk == "destructive" else warning_code
    assert captured["exit-code"] == str(expected_code)
    assert captured["verdict"] == risk
    environment.update(CODE=captured["exit-code"], VERDICT=captured["verdict"], FAIL_ON=policy)
    gated = (
        shell(action_script("Gate"), project, environment)
        if wrapper == "action"
        else execute("Gate", project, environment)
    )
    expected = int(
        policy != "never" and (expected_code == 1 or (policy == "warning" and expected_code != 0))
    )
    assert gated.returncode == expected, gated.stdout + gated.stderr


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize("risk", ["destructive", "parse"])
def test_ci_raw_verdict_survives_policy(tmp_path, environment, wrapper, risk):
    from tests.test_fail_on import project_with_risk

    project = project_with_risk(tmp_path, risk)
    (project / ".dbt-plan.yml").write_text("acknowledge_models: [orders]\nwarning_exit_code: 0\n")
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="false")
    checked = (
        shell(action_script("Check"), project, environment)
        if wrapper == "action"
        else execute("Check current", project, environment, "git() { :; }; dbt() { :; };\n")
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert outputs(environment)["exit-code"] == "0"
    assert outputs(environment)["verdict"] == ("warning" if risk == "parse" else risk)


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize(
    "report",
    [
        {"summary": {}, "models": []},
        {"summary": {"total": 0, "safe": 0, "warning": 0, "destructive": 0}, "models": [{}]},
        {"summary": {"total": 0, "safe": 0, "warning": 0, "destructive": False}, "models": []},
        {
            "summary": {"total": 0, "safe": 0, "warning": 0, "destructive": 0},
            "models": [],
            "parse_failures": "lost",
        },
    ],
)
def test_ci_malformed_report_is_error(tmp_path, environment, wrapper, report):
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="false")
    prefix = stub(0, json.dumps(report))
    if wrapper == "action":
        result = shell(prefix + action_script("Check"), tmp_path, environment)
        assert result.returncode == 3
        assert outputs(environment) == {}
    else:
        result = execute("Check current", tmp_path, environment, prefix)
        assert result.returncode == 0
        assert outputs(environment)["exit-code"] == "3"


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_old_cli_without_fail_on_option(tmp_path, environment, wrapper, code):
    # Frozen pre-#35 CLI contract: no fail-on option, no interpretation of its
    # environment variable, legacy summary/model fields and process codes.
    safety = {0: "safe", 1: "destructive", 2: "warning"}[code]
    report = json.dumps(
        {
            "summary": {
                "total": 1,
                "safe": int(code == 0),
                "destructive": int(code == 1),
                "warning": int(code == 2),
            },
            "models": [{"model_name": "orders", "safety": safety}],
        }
    )
    prefix = stub(code, report).replace(
        'case "$*" in', 'case "$*" in\n    *"--fail-on"*) echo unsupported >&2; return 3 ;;'
    )
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="true", DBT_PLAN_FAIL_ON="never")
    result = (
        shell(prefix + action_script("Check"), tmp_path, environment)
        if wrapper == "action"
        else execute("Check current", tmp_path, environment, prefix)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs(environment)["exit-code"] == str(code)
    assert outputs(environment)["verdict"] == safety


@pytest.mark.parametrize("wrapper", ["action", "generated"])
@pytest.mark.parametrize("failure", ["reserved", "unreadable", "missing_manifest"])
def test_ci_never_cannot_hide_execution_errors(tmp_path, environment, wrapper, failure):
    from tests.test_fail_on import project_with_risk

    project = project_with_risk(tmp_path, "destructive")
    config = project / ".dbt-plan.yml"
    if failure == "reserved":
        config.write_text("fail_on: invalid\nwarning_exit_code: 3\n")
    elif failure == "unreadable":
        config.mkdir()
    else:
        (project / "target/manifest.json").unlink()
    environment.update(TARGET_DIR="target", DIALECT="", SUMMARY="false", DBT_PLAN_FAIL_ON="never")
    result = (
        shell(action_script("Check"), project, environment)
        if wrapper == "action"
        else execute("Check current", project, environment, "git() { :; }; dbt() { :; };\n")
    )
    if wrapper == "generated" and failure == "missing_manifest":
        assert result.returncode == 0
        assert outputs(environment)["exit-code"] == "3"
        assert "verdict" not in outputs(environment)
        environment.update(CODE="3", FAIL_ON="never")
        assert execute("Gate", project, environment).returncode == 3
    else:
        assert result.returncode == 3
        assert outputs(environment) == {}
