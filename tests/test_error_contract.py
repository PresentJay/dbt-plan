"""Process failures must not masquerade as completed analysis verdicts."""

import json
import os
import subprocess
import sys

import pytest

from dbt_plan.cli import main
from tests.test_cli import _make_project


@pytest.fixture
def project(tmp_path):
    manifest = {
        "nodes": {
            "model.p.orders": {
                "name": "orders",
                "resource_type": "model",
                "config": {"materialized": "incremental"},
                "unrendered_config": {"on_schema_change": "sync_all_columns"},
                "columns": {"id": {"name": "id", "data_type": "INT"}},
            }
        },
        "child_map": {},
    }
    return _make_project(
        tmp_path,
        models_sql={"orders": "SELECT 1 AS id"},
        base_sql={"orders": "SELECT 1 AS id, 2 AS amount"},
        manifest=manifest,
        base_manifest=manifest,
    )


def invoke(project, *args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", *args, "--project-dir", str(project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize("failure", ["base", "compiled", "manifest", "corrupt", "config", "args"])
def test_failed_invocation_has_no_verdict(project, failure):
    import shutil

    args = ["check", "--format", "json"]
    if failure == "base":
        shutil.rmtree(project / ".dbt-plan")
    elif failure == "compiled":
        shutil.rmtree(project / "target/compiled")
    elif failure == "manifest":
        (project / "target/manifest.json").unlink()
    elif failure == "corrupt":
        (project / "target/manifest.json").write_text("{")
    elif failure == "config":
        (project / ".dbt-plan.yml").write_bytes(b"\xff")
    else:
        args += ["--not-a-real-flag"]
    proc = invoke(project, *args)
    assert proc.returncode == 3, proc.stderr
    assert proc.stdout == ""
    assert "error" in proc.stderr.lower()
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("source", ["file", "env"])
def test_error_code_cannot_be_configured_as_warning(project, source):
    env = os.environ.copy()
    if source == "file":
        (project / ".dbt-plan.yml").write_text("warning_exit_code: 3\n")
        env.pop("DBT_PLAN_WARNING_EXIT_CODE", None)
    else:
        env["DBT_PLAN_WARNING_EXIT_CODE"] = "3"
    proc = invoke(project, "check", "--format", "json", env=env)
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert "reserved" in proc.stderr.lower()


@pytest.mark.parametrize("side", ["base", "current"])
@pytest.mark.parametrize("materialization", ["incremental", "table", "view"])
@pytest.mark.parametrize("contract", [False, True])
def test_non_utf8_sql_is_reported_for_review(project, side, materialization, contract):
    for path in [project / "target/manifest.json", project / ".dbt-plan/base/manifest.json"]:
        manifest = json.loads(path.read_text())
        node = manifest["nodes"]["model.p.orders"]
        node["config"].update(materialized=materialization, contract={"enforced": contract})
        path.write_text(json.dumps(manifest))
    path = (
        project / ".dbt-plan/base/compiled/orders.sql"
        if side == "base"
        else project / "target/compiled/my_project/models/orders.sql"
    )
    path.write_bytes(b"SELECT \xff AS id")
    proc = invoke(project, "check", "--format", "json")
    assert proc.returncode == 2, proc.stderr
    report = json.loads(proc.stdout)
    assert report["parse_failures"] == ["orders"]
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("status,code", [("safe", 0), ("destructive", 1), ("warning", 2)])
def test_completed_verdict_codes_are_unchanged(project, status, code):
    current = project / "target/compiled/my_project/models/orders.sql"
    if status == "safe":
        current.write_text("SELECT 1 AS id, 2 AS amount")
    elif status == "warning":
        current.write_bytes(b"SELECT \xff")
    proc = invoke(project, "check", "--format", "json")
    assert proc.returncode == code, proc.stderr
    assert isinstance(json.loads(proc.stdout)["models"], list)


def test_warning_opt_out_never_suppresses_execution_failure(project):
    (project / ".dbt-plan.yml").write_text("warning_exit_code: 0\n")
    current = project / "target/compiled/my_project/models/orders.sql"
    current.write_bytes(b"SELECT \xff")
    warning = invoke(project, "check", "--format", "json")
    assert warning.returncode == 0, warning.stderr
    assert json.loads(warning.stdout)["parse_failures"] == ["orders"]
    (project / "target/manifest.json").unlink()
    failure = invoke(project, "check", "--format", "json")
    assert failure.returncode == 3
    assert failure.stdout == ""


def test_unexpected_exception_is_not_destructive(monkeypatch, capsys):
    def fail(args):
        raise RuntimeError("analysis unexpectedly failed")

    monkeypatch.setattr("dbt_plan.cli._do_check", fail)
    monkeypatch.setattr(sys, "argv", ["dbt-plan", "check"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    assert "analysis unexpectedly failed" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("status", ["added", "removed"])
def test_non_utf8_sql_without_matching_revision(project, status):
    base_path = project / ".dbt-plan/base/compiled/orders.sql"
    current_path = project / "target/compiled/my_project/models/orders.sql"
    if status == "added":
        base_path.unlink()
        (project / ".dbt-plan/base/manifest.json").write_text('{"nodes": {}, "child_map": {}}')
        current_path.write_bytes(b"SELECT \xff")
    else:
        current_path.unlink()
        (project / "target/manifest.json").write_text('{"nodes": {}, "child_map": {}}')
        base_path.write_bytes(b"SELECT \xff")
    proc = invoke(project, "check", "--format", "json")
    assert proc.returncode == (2 if status == "added" else 1), proc.stderr
    report = json.loads(proc.stdout)
    if status == "added":
        assert report["parse_failures"] == ["orders"]
    else:
        assert report["models"][0]["safety"] == "destructive"
    assert "Traceback" not in proc.stderr


def test_environment_override_precedes_reserved_code_validation(project):
    (project / ".dbt-plan.yml").write_text("warning_exit_code: 3\n")
    env = {**os.environ, "DBT_PLAN_WARNING_EXIT_CODE": "4"}
    (project / "target/compiled/my_project/models/orders.sql").write_bytes(b"SELECT \xff")
    proc = invoke(project, "check", "--format", "json", env=env)
    assert proc.returncode == 4
    assert json.loads(proc.stdout)["parse_failures"] == ["orders"]


@pytest.mark.parametrize("exception", [SystemExit(2), KeyboardInterrupt()])
def test_process_control_is_not_reclassified(monkeypatch, exception):
    def stop(args):
        raise exception

    monkeypatch.setattr("dbt_plan.cli._do_check", stop)
    monkeypatch.setattr(sys, "argv", ["dbt-plan", "check"])
    with pytest.raises(type(exception)) as exc:
        main()
    assert exc.value is exception
