"""Versioned layout selection must never silently discard baseline models."""

import json
import shutil
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot
from tests.test_dbt_e2e import (
    _dbt_compile,
    _dbt_plan,
    _missing_requirement,
    renamed_paths_project,  # noqa: F401 -- shared pytest fixture
)
from tests.test_snapshot_lifecycle import (
    _check_args,
    _minimal_manifest,
    _setup_target,
    _snapshot_args,
)


@pytest.fixture
def project(tmp_path, capsys):
    manifest = _minimal_manifest(
        {"orders": {"materialized": "incremental", "on_schema_change": "sync_all_columns"}}
    )
    manifest["nodes"]["model.my_project.orders"]["original_file_path"] = "models/orders.sql"
    _setup_target(tmp_path, {"orders": "select 1 as id, 2 as title"}, manifest)
    _do_snapshot(_snapshot_args(tmp_path))
    capsys.readouterr()
    return tmp_path


def metadata(project, value):
    (project / ".dbt-plan/base/provenance.json").write_text(json.dumps(value), encoding="utf-8")


def baseline_bytes(project):
    base = project / ".dbt-plan/base"
    return {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file()}


def test_new_snapshot_has_layout_version(project, capsys):
    stored = json.loads((project / ".dbt-plan/base/provenance.json").read_text())
    assert type(stored["layout_version"]) is int
    assert stored["layout_version"] == 1
    assert _do_check(_check_args(project)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["baseline"]["layout_status"] == "versioned"


@pytest.mark.parametrize("layout", ["project-root", "model-root", "direct"])
@pytest.mark.parametrize("provenance", [False, True])
def test_legacy_layouts_are_read_only_and_detect_drops(project, capsys, layout, provenance):
    base = project / ".dbt-plan/base"
    if provenance:
        metadata(project, {"dbt_plan_version": "0.13.0"})
    else:
        (base / "provenance.json").unlink()
    if layout != "project-root":
        dest = base if layout == "direct" else base / "compiled"
        shutil.move(str(base / "compiled/models/orders.sql"), str(dest / "orders.sql"))
        (base / "compiled/models").rmdir()
        if layout == "direct":
            (base / "compiled").rmdir()
    before = baseline_bytes(project)
    (project / "target/compiled/my_project/models/orders.sql").write_text("select 1 as id")
    assert _do_check(_check_args(project)) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["models"][0]["safety"] == "destructive"
    assert result["analysis"]["baseline"]["layout_status"] == "legacy"
    assert result["analysis"]["baseline"]["layout_version"] is None
    assert "legacy/unversioned" in output.err
    assert baseline_bytes(project) == before


@pytest.mark.parametrize("version", [2, -1, 0, True, False, None, "1", 1.0, [], {}])
def test_invalid_explicit_version_fails_before_analysis(project, capsys, version):
    metadata(project, {"layout_version": version})
    before = baseline_bytes(project)
    args = _check_args(project)
    args.select = "does_not_exist"
    assert _do_check(args) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "layout_version" in output.err
    assert "dbt-plan snapshot" in output.err
    assert baseline_bytes(project) == before


@pytest.mark.parametrize("raw", ['{"layout_version":', "[]", "null", "\xff"])
def test_unreadable_metadata_cannot_be_treated_as_legacy(project, capsys, raw):
    (project / ".dbt-plan/base/provenance.json").write_bytes(raw.encode("latin-1"))
    assert _do_check(_check_args(project)) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "dbt-plan snapshot" in output.err


@pytest.mark.parametrize("mismatch", ["flattened", "missing-root", "nested-project", "mixed"])
def test_declared_layout_mismatch_is_error(project, capsys, mismatch):
    metadata(project, {"layout_version": 1})
    base = project / ".dbt-plan/base"
    compiled = base / "compiled"
    if mismatch == "missing-root":
        compiled.rename(base / "elsewhere")
    elif mismatch == "nested-project":
        (compiled / "my_project").mkdir()
        (compiled / "models").rename(compiled / "my_project/models")
    elif mismatch == "mixed":
        (compiled / "hidden.sql").write_text("select 1 as hidden")
    else:
        (compiled / "models/orders.sql").rename(compiled / "orders.sql")
        (compiled / "models").rmdir()
    assert _do_check(_check_args(project)) == 3
    output = capsys.readouterr()
    assert not output.out
    assert "dbt-plan snapshot" in output.err


def test_baseline_uses_its_own_model_paths(project, capsys):
    target = project / "target"
    (target / "compiled/my_project/models").rename(target / "compiled/my_project/transforms")
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["nodes"]["model.my_project.orders"]["original_file_path"] = "transforms/orders.sql"
    (target / "manifest.json").write_text(json.dumps(manifest))
    (target / "compiled/my_project/transforms/orders.sql").write_text("select 1 as id")
    assert _do_check(_check_args(project)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["models"][0]["safety"] == "destructive"


@pytest.mark.parametrize("resource", ["snapshot", "python"])
def test_versioned_manifest_only_snapshot(project, capsys, resource):
    shutil.rmtree(project / "target/compiled")
    manifest = {
        "metadata": {"project_name": "my_project"},
        "nodes": {
            "snapshot.my_project.orders": {
                "name": "orders",
                "resource_type": "snapshot",
                "config": {"enabled": True, "materialized": "snapshot"},
            }
        },
    }
    if resource == "python":
        node = manifest["nodes"].pop("snapshot.my_project.orders")
        node.update(
            resource_type="model", language="python", original_file_path="models/orders.py"
        )
        node["config"]["materialized"] = "table"
        manifest["nodes"]["model.my_project.orders"] = node
    (project / "target/manifest.json").write_text(json.dumps(manifest))
    _do_snapshot(_snapshot_args(project))
    capsys.readouterr()
    assert _do_check(_check_args(project)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["baseline"]["layout_version"] == 1


def test_metadata_failure_preserves_versioned_baseline(project, monkeypatch):
    before = baseline_bytes(project)
    original = Path.write_text

    def fail(path, *args, **kwargs):
        if path.name == "provenance.json":
            raise OSError("metadata publication failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError, match="metadata publication failed"):
        _do_snapshot(_snapshot_args(project))
    assert baseline_bytes(project) == before


def test_declared_sql_layout_requires_model_directory(project, capsys):
    shutil.rmtree(project / ".dbt-plan/base/compiled/models")
    assert _do_check(_check_args(project)) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "dbt-plan snapshot" in output.err


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("legacy", [False, True])
def test_baseline_manifest_problem_remains_warning(project, capsys, damage, legacy):
    if legacy:
        (project / ".dbt-plan/base/provenance.json").unlink()
    path = project / ".dbt-plan/base/manifest.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{")
    assert _do_check(_check_args(project)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["baseline_problem"] == damage


def test_custom_non_model_sql_is_not_a_layout_mismatch(project, capsys):
    base = project / ".dbt-plan/base"
    tests = base / "compiled/checks"
    tests.mkdir()
    (tests / "assert_orders.sql").write_text("select 1")
    manifest = json.loads((base / "manifest.json").read_text())
    manifest["nodes"]["test.my_project.assert_orders"] = {
        "resource_type": "test",
        "original_file_path": "checks/assert_orders.sql",
    }
    (base / "manifest.json").write_text(json.dumps(manifest))
    assert _do_check(_check_args(project)) == 0


def test_layout_status_is_derived_not_trusted(project, capsys):
    metadata(project, {"layout_status": "versioned", "revision": "abc"})
    assert _do_check(_check_args(project)) == 0
    baseline = json.loads(capsys.readouterr().out)["analysis"]["baseline"]
    assert baseline["layout_status"] == "legacy"
    assert baseline["layout_version"] is None
    assert baseline["revision"] == "abc"


@pytest.mark.parametrize(
    "declared",
    [
        ".",
        "/",
        "///",
        "../orders.sql",
        r"..\outside\orders.sql",
        r"C:\outside\orders.sql",
        r"\outside\orders.sql",
        "C:outside/orders.sql",
        "C:/outside/orders.sql",
        "models/\norders.sql",
        "models/\torders.sql",
        "models/\x00orders.sql",
        "models/\x7forders.sql",
    ],
)
def test_invalid_manifest_model_path_is_clear_error(project, capsys, declared):
    path = project / ".dbt-plan/base/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["nodes"]["model.my_project.orders"]["original_file_path"] = declared
    path.write_text(json.dumps(manifest))
    assert _do_check(_check_args(project)) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "original_file_path" in output.err
    assert "dbt-plan snapshot" in output.err


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_custom_model_path_manifest_problem_remains_warning(project, capsys, damage):
    target = project / "target"
    (target / "compiled/my_project/models").rename(target / "compiled/my_project/transforms")
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["nodes"]["model.my_project.orders"]["original_file_path"] = "transforms/orders.sql"
    (target / "manifest.json").write_text(json.dumps(manifest))
    _do_snapshot(_snapshot_args(project))
    baseline_manifest = project / ".dbt-plan/base/manifest.json"
    if damage == "missing":
        baseline_manifest.unlink()
    else:
        baseline_manifest.write_text("{")
    capsys.readouterr()
    before = baseline_bytes(project)
    assert _do_check(_check_args(project)) == 2
    assert json.loads(capsys.readouterr().out)["baseline_problem"] == damage
    assert baseline_bytes(project) == before


@pytest.mark.parametrize("raw", ["null", "[]", '{"nodes": []}', '{"nodes": {"model.p.x": null}}'])
def test_malformed_manifest_shape_refuses_without_traceback(project, capsys, raw):
    (project / ".dbt-plan/base/manifest.json").write_text(raw)
    assert _do_check(_check_args(project)) in (2, 3)
    output = capsys.readouterr()
    assert "Traceback" not in output.err


def test_explicit_empty_model_directory_is_supported(project, capsys):
    for base in [project / "target", project / ".dbt-plan/base"]:
        (base / "manifest.json").write_text(json.dumps(_minimal_manifest()))
    (project / "target/compiled/my_project/models/orders.sql").unlink()
    _do_snapshot(_snapshot_args(project))
    capsys.readouterr()
    assert _do_check(_check_args(project)) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["total"] == 0


@pytest.mark.skipif(_missing_requirement() is not None, reason=_missing_requirement() or "")
@pytest.mark.parametrize("flatten", [False, True])
def test_real_dbt_unversioned_layouts(renamed_paths_project, flatten):  # noqa: F811
    _dbt_compile(renamed_paths_project)
    result = _dbt_plan(["snapshot", "--project-dir", str(renamed_paths_project)])
    assert result.returncode == 0, result.stderr
    base = renamed_paths_project / ".dbt-plan/base"
    stored = json.loads((base / "provenance.json").read_text())
    del stored["layout_version"]
    (base / "provenance.json").write_text(json.dumps(stored))
    if flatten:
        for name in ("transformations", "extras"):
            folder = base / "compiled" / name
            for sql in folder.glob("*.sql"):
                sql.rename(base / "compiled" / sql.name)
            folder.rmdir()
    before = baseline_bytes(renamed_paths_project)
    result = _dbt_plan(["check", "--project-dir", str(renamed_paths_project), "--format", "json"])
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["total"] == 0
    assert report["analysis"]["baseline"]["layout_status"] == "legacy"
    assert baseline_bytes(renamed_paths_project) == before
