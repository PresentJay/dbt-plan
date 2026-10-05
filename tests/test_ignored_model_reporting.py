"""Exact ignores report exclusions, never evidence of safety."""

import json
import os

import pytest

from dbt_plan.cli import _do_check
from tests.test_cli import _make_check_args, _make_project


def _manifest(names, children=None):
    return {
        "nodes": {
            f"model.my_project.{name}": {
                "name": name,
                "resource_type": "model",
                "config": {"materialized": "incremental"},
                "unrendered_config": {"on_schema_change": "sync_all_columns"},
            }
            for name in names
        },
        "child_map": children or {},
    }


def _project(tmp_path):
    return _make_project(
        tmp_path,
        models_sql={
            "modified": "select 1 as id",
            "added": "select 1 as id",
            "removed": "select 1 as id",  # stale compiled output
            "unchanged": "select 1 as id",
            "remaining": "select 1 as id",
        },
        base_sql={
            "modified": "select 1 as id, 2 as title",
            "removed": "select 1 as id",
            "unchanged": "select 1 as id",
            "remaining": "select 1 as id, 2 as title",
        },
        manifest=_manifest(["modified", "added", "unchanged", "remaining", "uncompiled"]),
        base_manifest=_manifest(["modified", "removed", "unchanged", "remaining"]),
    )


def _ignore(project, names):
    (project / ".dbt-plan.yml").write_text(f"ignore_models: [{', '.join(names)}]\n")


def _check(project, capsys, **kwargs):
    code = _do_check(_make_check_args(project, fmt="json", **kwargs))
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("source", ["file", "env"])
def test_actual_exclusions_and_remaining_findings(tmp_path, capsys, monkeypatch, source):
    project = _project(tmp_path)
    code, before = _check(project, capsys)
    assert code == 1
    assert before["uncompiled_models"] == ["uncompiled"]
    assert {m["model_name"] for m in before["models"] if m["safety"] == "destructive"} == {
        "modified",
        "removed",
        "remaining",
    }
    names = ["removed", "modified", "added", "uncompiled", "modified", "unknown", "unchanged"]
    if source == "file":
        _ignore(project, names)
    else:
        monkeypatch.setenv("DBT_PLAN_IGNORE_MODELS", ",".join(names))
    code, after = _check(project, capsys)
    assert code == 1
    assert after["ignored_models"] == ["added", "modified", "removed", "uncompiled"]
    assert after["unmatched_ignore_models"] == ["unknown"]
    assert after["uncompiled_models"] == []
    assert after["models"] == [m for m in before["models"] if m["model_name"] == "remaining"]
    assert after["summary"]["safe"] == 0


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("modified", ["modified"]),
        ("removed", ["removed"]),
        ("uncompiled", ["uncompiled"]),
        ("unchanged", []),
    ],
)
def test_selection_counts_only_actual_exclusions(tmp_path, capsys, selection, expected):
    project = _project(tmp_path)
    _ignore(project, ["modified", "removed", "uncompiled", "unchanged", "unknown"])
    code, data = _check(project, capsys, select=selection)
    assert code == 0
    assert data["models"] == []
    assert data["ignored_models"] == expected
    assert data["unmatched_ignore_models"] == ["unknown"]


@pytest.mark.parametrize("fmt", ["text", "github", "json"])
def test_zero_predictions_still_reports_exclusions(tmp_path, capsys, fmt):
    project = _project(tmp_path)
    _ignore(project, ["modified", "added", "removed", "remaining", "uncompiled", "unknown"])
    assert _do_check(_make_check_args(project, fmt=fmt)) == 0
    output = capsys.readouterr().out
    if fmt == "json":
        data = json.loads(output)
        assert data["models"] == []
        assert data["summary"]["safe"] == 0
        assert data["ignored_models"] == [
            "added",
            "modified",
            "remaining",
            "removed",
            "uncompiled",
        ]
        assert data["unmatched_ignore_models"] == ["unknown"]
    else:
        assert "Excluded by exact ignore policy (not checked)" in output
        assert "Unmatched ignore names (informational)" in output
        for name in ["modified", "added", "removed", "remaining", "uncompiled", "unknown"]:
            assert name in output
        assert "0 checked, 0 safe" in output


@pytest.mark.parametrize("problem", ["missing", "corrupt", "shared_source"])
def test_ignore_preserves_shared_refusals(tmp_path, capsys, problem):
    project = _project(tmp_path)
    if problem == "missing":
        (project / ".dbt-plan/base/manifest.json").unlink()
    elif problem == "corrupt":
        (project / ".dbt-plan/base/manifest.json").write_text("{")
    else:
        source = project / "dbt_project.yml"
        source.write_text("name: my_project\n")
        stamp = (project / "target/manifest.json").stat().st_mtime + 10
        os.utime(source, (stamp, stamp))
    _, before = _check(project, capsys)
    _ignore(project, ["modified", "added", "removed", "remaining", "uncompiled", "unchanged"])
    code, after = _check(project, capsys)
    assert code == 2
    key = "stale_sources" if problem == "shared_source" else "baseline_problem"
    assert after[key] == before[key]
    assert after[key]
    assert after["ignored_models"]
    assert after["summary"]["safe"] == 0


def test_globs_are_informational_not_waivers(tmp_path, capsys):
    project = _project(tmp_path)
    _ignore(project, ["*"])
    code, data = _check(project, capsys)
    assert code == 1
    assert data["ignored_models"] == []
    assert data["unmatched_ignore_models"] == ["*"]
    assert data["uncompiled_models"] == ["uncompiled"]


@pytest.mark.parametrize("problem", ["invalid_manifest", "unknown_selection"])
def test_ignores_do_not_change_error_exit_three(tmp_path, capsys, problem):
    project = _project(tmp_path)
    _ignore(project, ["modified", "added", "removed", "remaining", "uncompiled"])
    if problem == "invalid_manifest":
        (project / "target/manifest.json").write_text("{")
    args = _make_check_args(project, select="unknown" if problem == "unknown_selection" else None)
    assert _do_check(args) == 3
    assert "Error:" in capsys.readouterr().err


@pytest.mark.parametrize("missing_side", ["current", "baseline"])
def test_related_missing_sql_counts_but_unrelated_changes_do_not(tmp_path, capsys, missing_side):
    graph = {"model.my_project.upstream": ["model.my_project.selected"]}
    manifest = _manifest(["upstream", "selected", "unrelated"], graph)
    sql = {name: "select 1 as id" for name in ["upstream", "selected", "unrelated"]}
    partial = {name: query for name, query in sql.items() if name != "upstream"}
    project = _make_project(
        tmp_path,
        models_sql=partial if missing_side == "current" else sql,
        base_sql=partial if missing_side == "baseline" else sql,
        manifest=manifest,
        base_manifest=manifest,
    )
    code, before = _check(project, capsys, select="selected")
    assert code == 2
    if missing_side == "current":
        assert before["uncompiled_models"] == ["upstream"]
    else:
        assert "upstream" in before["baseline_problem"]
    _ignore(project, ["upstream", "unrelated"])
    code, data = _check(project, capsys, select="selected")
    assert code == 0
    assert data["ignored_models"] == ["upstream"]
    assert data["unmatched_ignore_models"] == []
    assert data["models"] == []


def test_metadata_only_changes_are_actual_exclusions(tmp_path, capsys):
    base = _manifest(["orders"])
    current = _manifest(["orders"])
    current["nodes"]["model.my_project.orders"]["config"]["materialized"] = "table"
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select 1 as id"},
        base_sql={"orders": "select 1 as id"},
        manifest=current,
        base_manifest=base,
    )
    _, before = _check(project, capsys)
    assert [m["model_name"] for m in before["models"]] == ["orders"]
    _ignore(project, ["orders"])
    code, data = _check(project, capsys)
    assert code == 0
    assert data["ignored_models"] == ["orders"]
    assert data["models"] == []


@pytest.mark.parametrize("fmt", ["text", "github", "json"])
def test_unmatched_only_is_visible_and_informational(tmp_path, capsys, fmt):
    project = _project(tmp_path)
    _ignore(project, ["unknown", "unknown"])
    assert _do_check(_make_check_args(project, fmt=fmt, select="unchanged")) == 0
    output = capsys.readouterr().out
    if fmt == "json":
        data = json.loads(output)
        assert data["ignored_models"] == []
        assert data["unmatched_ignore_models"] == ["unknown"]
        assert data["models"] == []
    else:
        assert "Unmatched ignore names (informational)" in output
        assert "unknown" in output
        assert "Excluded by exact ignore policy" not in output


def test_remaining_parse_failure_is_never_safe(tmp_path, capsys):
    project = _project(tmp_path)
    (project / "target/compiled/my_project/models/remaining.sql").write_text("select (")
    _, before = _check(project, capsys)
    remaining = [m for m in before["models"] if m["model_name"] == "remaining"]
    assert remaining[0]["safety"] == "warning"
    _ignore(project, ["modified", "added", "removed", "uncompiled"])
    code, after = _check(project, capsys)
    assert code == 2
    assert after["models"] == remaining
    assert after["parse_failures"] == before["parse_failures"] == ["remaining"]
    assert after["summary"]["safe"] == 0


def test_defaults_add_empty_arrays_without_changing_verdict(tmp_path, capsys):
    project = _project(tmp_path)
    code, data = _check(project, capsys, select="unchanged")
    assert code == 0
    assert data["ignored_models"] == data["unmatched_ignore_models"] == []
    assert data["models"] == []
