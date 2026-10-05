"""Explicit exit policy never changes findings or execution failures."""

import argparse
import json
import os
import subprocess
from unittest.mock import Mock

import pytest

from dbt_plan import cli
from dbt_plan.config import Config, ConfigError
from tests.test_cli import _make_project
from tests.test_error_contract import invoke


@pytest.fixture(autouse=True)
def clean_policy_env(monkeypatch):
    for key in ("DBT_PLAN_FAIL_ON", "DBT_PLAN_WARNING_EXIT_CODE", "DBT_PLAN_ACKNOWLEDGE"):
        monkeypatch.delenv(key, raising=False)


def project_with_risk(tmp_path, risk):
    manifest = {
        "nodes": {
            "model.p.orders": {
                "name": "orders",
                "resource_type": "model",
                "config": {"materialized": "incremental"},
                "unrendered_config": {
                    "on_schema_change": "sync_all_columns" if risk == "destructive" else "fail"
                },
            }
        },
        "child_map": {},
    }
    project = _make_project(
        tmp_path,
        models_sql={"orders": "SELECT 1 AS id"},
        base_sql={"orders": "SELECT 1 AS id, 2 AS amount"},
        manifest=manifest,
        base_manifest=manifest,
    )
    if risk == "safe":
        (project / ".dbt-plan/base/compiled/orders.sql").write_text("SELECT 1 AS id")
    elif risk == "parse":
        (project / "target/compiled/my_project/models/orders.sql").write_bytes(b"SELECT \xff")
    return project


@pytest.mark.parametrize("policy", [None, "warning", "destructive", "never"])
@pytest.mark.parametrize("warning_code", [0, 1, 2, 7, 255])
@pytest.mark.parametrize("risk", ["safe", "warning", "destructive", "parse"])
def test_completed_policy_matrix(tmp_path, policy, warning_code, risk):
    project = project_with_risk(tmp_path, risk)
    (project / ".dbt-plan.yml").write_text(f"warning_exit_code: {warning_code}\n")
    args = ["check", "--format", "json"]
    if policy is not None:
        args += ["--fail-on", policy]
    proc = invoke(project, *args)
    expected = 0
    if risk == "destructive" and policy != "never":
        expected = 1
    elif risk in ("warning", "parse") and policy not in ("never", "destructive"):
        expected = (warning_code or 2) if policy == "warning" else warning_code
    assert proc.returncode == expected, proc.stderr + proc.stdout
    report = json.loads(proc.stdout)
    if risk == "destructive":
        assert report["models"][0]["safety"] == "destructive"
        assert report["summary"]["destructive"] == 1
    elif risk == "warning":
        assert report["models"][0]["safety"] == "warning"
    elif risk == "parse":
        assert report["parse_failures"]


@pytest.mark.parametrize(
    "file_value,env_value,cli_value,expected",
    [
        (None, None, None, None),
        ("never", None, None, "never"),
        ("never", "destructive", None, "destructive"),
        ("never", "destructive", "warning", "warning"),
        ("invalid", "warning", None, "warning"),
        ("invalid", "invalid", "warning", "warning"),
        ("never", "", "warning", "warning"),
    ],
)
def test_resolution(tmp_path, monkeypatch, file_value, env_value, cli_value, expected):
    if file_value is not None:
        (tmp_path / ".dbt-plan.yml").write_text(f"fail_on: '{file_value}' # policy\n")
    if env_value is not None:
        monkeypatch.setenv("DBT_PLAN_FAIL_ON", env_value)
    assert Config.load(tmp_path, fail_on=cli_value).fail_on == expected


@pytest.mark.parametrize("command", ["check", "run"])
@pytest.mark.parametrize(
    "source,value",
    [
        (source, value)
        for source in ("file", "env", "cli")
        for value in ("invalid", "WARNING", "", "[never]")
    ],
)
def test_invalid_policy_precedes_side_effects(tmp_path, monkeypatch, command, source, value):
    if source == "file":
        (tmp_path / ".dbt-plan.yml").write_text(f"fail_on: {value}\n")
    elif source == "env":
        monkeypatch.setenv("DBT_PLAN_FAIL_ON", value)
    args = argparse.Namespace(
        project_dir=str(tmp_path), fail_on=value if source == "cli" else None
    )
    process = Mock(side_effect=AssertionError("must not start a subprocess"))
    monkeypatch.setattr("subprocess.run", process)
    with pytest.raises(ConfigError, match="fail_on"):
        (cli._do_check if command == "check" else cli._do_run)(args)
    process.assert_not_called()
    assert not (tmp_path / ".dbt-plan").exists()


@pytest.mark.parametrize("command", ["check", "run"])
def test_empty_env_cannot_fall_back_to_never(tmp_path, monkeypatch, command):
    (tmp_path / ".dbt-plan.yml").write_text("fail_on: never\n")
    monkeypatch.setenv("DBT_PLAN_FAIL_ON", "")
    process = Mock(side_effect=AssertionError("must not start a subprocess"))
    monkeypatch.setattr("subprocess.run", process)
    with pytest.raises(ConfigError, match="fail_on"):
        (cli._do_check if command == "check" else cli._do_run)(
            argparse.Namespace(project_dir=str(tmp_path))
        )
    process.assert_not_called()
    assert not (tmp_path / ".dbt-plan").exists()


@pytest.mark.parametrize("policy", ["warning", "destructive", "never"])
@pytest.mark.parametrize("command", ["check", "run"])
def test_errors_remain_three(tmp_path, policy, command):
    proc = invoke(tmp_path, command, "--fail-on", policy)
    assert proc.returncode == 3
    assert not proc.stdout
    assert "error" in proc.stderr.lower()


@pytest.mark.parametrize("source", ["file", "env", "cli"])
@pytest.mark.parametrize("value", ["invalid", ""])
def test_invalid_policy_cli_is_configuration_error(tmp_path, monkeypatch, source, value):
    args = ["run"]
    if source == "file":
        (tmp_path / ".dbt-plan.yml").write_text(f"fail_on: {value}\n")
    elif source == "env":
        monkeypatch.setenv("DBT_PLAN_FAIL_ON", value)
    else:
        args += ["--fail-on", value]
    # Windows putenv removes an empty native variable even though os.environ
    # retains it. Pass the mapping explicitly to exercise a present empty value.
    proc = invoke(tmp_path, *args, env=os.environ.copy())
    assert proc.returncode == 3
    assert "fail_on" in proc.stderr or "--fail-on" in proc.stderr
    assert not proc.stdout


@pytest.mark.parametrize("policy", ["warning", "destructive", "never"])
def test_reserved_warning_code_not_suppressed(tmp_path, policy):
    (tmp_path / ".dbt-plan.yml").write_text("warning_exit_code: 3\n")
    proc = invoke(tmp_path, "check", "--fail-on", policy)
    assert proc.returncode == 3
    assert "reserved" in proc.stderr


def test_snapshot_rejects_policy_flag(tmp_path):
    proc = invoke(tmp_path, "snapshot", "--fail-on", "never")
    assert proc.returncode == 3
    assert "unrecognized arguments" in proc.stderr


@pytest.mark.parametrize("source", ["file", "env", "cli"])
@pytest.mark.parametrize("policy,expected", [("warning", 2), ("destructive", 0), ("never", 0)])
def test_run_passes_resolved_policy_to_real_check(tmp_path, monkeypatch, source, policy, expected):
    project = project_with_risk(tmp_path, "warning")
    # Invalid lower layers must be overridden before run starts any subprocess.
    file_value = policy if source == "file" else "invalid"
    (project / ".dbt-plan.yml").write_text(f"fail_on: {file_value}\nwarning_exit_code: 0\n")
    if source != "file":
        monkeypatch.setenv("DBT_PLAN_FAIL_ON", policy if source == "env" else "invalid")
    process = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr("subprocess.run", process)
    monkeypatch.setattr(cli, "_do_snapshot", Mock())
    check = Mock(wraps=cli._do_check)
    monkeypatch.setattr(cli, "_do_check", check)
    assert (
        cli._do_run(
            argparse.Namespace(
                project_dir=str(project),
                fail_on=policy if source == "cli" else None,
                format="json",
            )
        )
        == expected
    )
    assert check.call_args.args[0].fail_on == policy
    assert process.call_count == 5  # version, git status, HEAD, baseline/current compile


@pytest.mark.parametrize("failed_compile", [1, 2])
def test_never_does_not_suppress_compile_failure(tmp_path, monkeypatch, failed_compile):
    calls = 0

    def process(argv, **kwargs):
        nonlocal calls
        if argv == ["dbt", "compile"]:
            calls += 1
            return subprocess.CompletedProcess(argv, int(calls == failed_compile), "", "failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", process)
    monkeypatch.setattr(cli, "_do_snapshot", Mock())
    check = Mock(side_effect=AssertionError("failed compile must not reach check"))
    monkeypatch.setattr(cli, "_do_check", check)
    assert cli._do_run(argparse.Namespace(project_dir=str(tmp_path), fail_on="never")) == 3
    check.assert_not_called()


@pytest.mark.parametrize(
    "policy,expected", [(None, 1), ("warning", 1), ("destructive", 1), ("never", 0)]
)
def test_policy_preserves_resource_waiver_scope(policy, expected):
    from dbt_plan.formatter import CheckResult
    from dbt_plan.predictor import DDLPrediction, DownstreamImpact, Safety

    prediction = DDLPrediction(
        model_name="up",
        materialization="view",
        on_schema_change=None,
        safety=Safety.DESTRUCTIVE,
        own_safety=Safety.SAFE,
        downstream_impacts=[
            DownstreamImpact("down", "incremental", "sync_all_columns", "inherited_drop", "lost")
        ],
    )
    result = CheckResult([prediction], acknowledge_models=["up"])
    assert cli._exit_code_for(result, 7, policy) == expected
    assert not result.impact_waived(prediction.downstream_impacts[0])
    assert prediction.safety == Safety.DESTRUCTIVE


@pytest.mark.parametrize(
    "field,value",
    [
        ("parse_failures", ["up"]),
        ("skipped_models", ["up"]),
        ("uncompiled_models", ["up"]),
        ("stale_sources", ["up"]),
        ("baseline_problem", "missing"),
    ],
)
def test_warning_policy_keeps_uncertainty_blocking(field, value):
    from dbt_plan.formatter import CheckResult

    result = CheckResult([], acknowledge_models=["up"], **{field: value})
    assert cli._exit_code_for(result, 0, "warning") == 2
    assert getattr(result, field) == value


def test_empty_result_path_obeys_policy(tmp_path):
    project = project_with_risk(tmp_path, "safe")
    (project / ".dbt-plan/base/manifest.json").unlink()
    for policy, code in [("warning", 2), ("destructive", 0), ("never", 0)]:
        proc = invoke(project, "check", "--format", "json", "--fail-on", policy)
        assert proc.returncode == code, proc.stderr + proc.stdout
        assert json.loads(proc.stdout)["baseline_problem"]
