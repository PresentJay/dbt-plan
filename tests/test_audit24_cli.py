"""Audit regressions exercised through the public check/agent-setup CLI helpers."""

import argparse
import copy
import json
import os

import pytest

from dbt_plan.cli import _do_agent_setup, _do_check, _do_snapshot


def project(tmp_path, before="select 1 as id, 2 as tax", after="select 1 as id", *, config=None):
    compiled = tmp_path / "target/compiled/bookshop/models"
    compiled.mkdir(parents=True)
    (compiled / "orders.sql").write_text(after)
    base = tmp_path / ".dbt-plan/base"
    (base / "compiled/models").mkdir(parents=True)
    (base / "compiled/models/orders.sql").write_text(before)
    manifest = {
        "metadata": {"project_name": "bookshop", "adapter_type": "duckdb"},
        "nodes": {
            "model.bookshop.orders": {
                "name": "orders",
                "resource_type": "model",
                "original_file_path": "models/orders.sql",
                "config": {"materialized": "incremental"},
                "unrendered_config": {"on_schema_change": "sync_all_columns"},
            }
        },
        "child_map": {},
    }
    if config:
        manifest["nodes"]["model.bookshop.orders"]["config"].update(config)
    for path in (tmp_path / "target/manifest.json", base / "manifest.json"):
        path.write_text(json.dumps(manifest))
    return manifest


def check(tmp_path, capsys, select=None):
    args = argparse.Namespace(
        project_dir=str(tmp_path),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format="json",
        no_color=True,
        select=select,
    )
    code = _do_check(args)
    output = capsys.readouterr().out
    return code, json.loads(output) if output else {}


def save(tmp_path, manifest, base=False):
    path = tmp_path / (".dbt-plan/base/manifest.json" if base else "target/manifest.json")
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "field,value",
    [("schema", "archive"), ("alias", "archived_orders"), ("database", "archive_db")],
)
def test_relation_movement_without_sql_change(tmp_path, capsys, field, value):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    m["nodes"]["model.bookshop.orders"][field] = value
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "RELATION CHANGED" in json.dumps(out)


@pytest.mark.parametrize(
    "before,after",
    [
        ("select id from raw", "select cast(id as int) as id from raw"),
        ("select cast(id as int) as id from raw", "select id from raw"),
    ],
)
def test_one_sided_cast_change(tmp_path, capsys, before, after):
    project(tmp_path, before, after)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "TYPE CHANGED" in json.dumps(out)


def test_snapshot_change_from_manifests(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    m["nodes"]["snapshot.bookshop.history"] = {
        "name": "history",
        "resource_type": "snapshot",
        "raw_code": "select id, tax from orders",
        "config": {"materialized": "snapshot", "check_cols": ["tax"]},
    }
    save(tmp_path, m, True)
    m["nodes"]["snapshot.bookshop.history"]["raw_code"] = "select id from orders"
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 2 and "history" in json.dumps(out)


@pytest.mark.parametrize("changed", [False, True])
def test_python_is_not_missing_sql(tmp_path, capsys, changed):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    m["nodes"]["model.bookshop.py_orders"] = {
        "name": "py_orders",
        "resource_type": "model",
        "language": "python",
        "raw_code": "return df",
        "original_file_path": "models/py_orders.py",
        "config": {"materialized": "table"},
    }
    save(tmp_path, m, True)
    if changed:
        m["nodes"]["model.bookshop.py_orders"]["raw_code"] = 'return df.drop("tax")'
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert out.get("uncompiled_models", []) == []
    assert code == (2 if changed else 0)
    if changed:
        assert "Python" in json.dumps(out)


@pytest.mark.parametrize("selection", ["orders+2", "@orders", "2+orders", "orders++", "typo"])
def test_unsupported_or_unknown_selection_never_passes(tmp_path, capsys, selection):
    project(tmp_path)
    assert check(tmp_path, capsys, selection)[0] != 0


def test_selection_ignores_unrelated_missing_sql_but_checks_dependencies(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    missing = copy.deepcopy(m["nodes"]["model.bookshop.orders"])
    missing["name"] = "missing"
    m["nodes"]["model.bookshop.missing"] = missing
    save(tmp_path, m)
    save(tmp_path, m, True)
    code, out = check(tmp_path, capsys, "orders")
    assert code == 0
    m["child_map"] = {"model.bookshop.missing": ["model.bookshop.orders"]}
    save(tmp_path, m)
    save(tmp_path, m, True)
    assert check(tmp_path, capsys, "orders")[0] == 2


def test_partial_compile_stale_file_detected(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    m["metadata"]["invocation_id"] = "current"
    save(tmp_path, m)
    (tmp_path / "target/run_results.json").write_text(
        json.dumps({"metadata": {"invocation_id": "current"}, "results": []})
    )
    assert check(tmp_path, capsys)[0] == 2


def test_deleted_macro_detected_with_base_provenance(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    (tmp_path / "dbt_project.yml").write_text("name: bookshop\n")
    m["macros"] = {
        "macro.bookshop.cols": {
            "original_file_path": "custom_macros/cols.sql",
            "macro_sql": "{% macro cols() %}id{% endmacro %}",
        }
    }
    save(tmp_path, m, True)
    assert check(tmp_path, capsys)[0] == 2


def test_contract_unresolved_star_does_not_prove_itself(tmp_path, capsys):
    m = project(
        tmp_path,
        "select * from external_orders",
        "select * from external_orders -- edit",
        config={"contract": {"enforced": True}},
    )
    m["nodes"]["model.bookshop.orders"]["columns"] = {"id": {"name": "id"}, "tax": {"name": "tax"}}
    save(tmp_path, m)
    save(tmp_path, m, True)
    assert check(tmp_path, capsys)[0] == 2


def test_agent_setup_explicit_file_preserves_existing_text(tmp_path, capsys):
    target = tmp_path / ".cursor/rules/dbt-plan.mdc"
    target.parent.mkdir(parents=True)
    target.write_text("Existing instructions\n")
    _do_agent_setup(
        argparse.Namespace(project_dir=str(tmp_path), file=".cursor/rules/dbt-plan.mdc")
    )
    assert target.read_text().startswith("Existing instructions\n")
    assert "dbt-plan" in target.read_text()
    assert not (tmp_path / "AGENTS.md").exists()


def test_report_provenance_in_json(tmp_path, capsys):
    project(tmp_path)
    _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
    capsys.readouterr()
    code, out = check(tmp_path, capsys)
    assert code == 0
    assert out["analysis"]["dialect"] == "duckdb"
    assert out["analysis"]["baseline"]["created_at"]
    assert out["analysis"]["baseline"]["revision"] is None


def test_unchanged_downstream_star_contract_is_checked(tmp_path, capsys):
    m = project(tmp_path, config={"materialized": "view"})
    m["nodes"]["model.bookshop.orders"]["alias"] = "orders"
    child = {
        "name": "reader",
        "resource_type": "model",
        "original_file_path": "models/reader.sql",
        "config": {"materialized": "view", "contract": {"enforced": True}},
        "columns": {"id": {"name": "id"}, "tax": {"name": "tax"}},
    }
    m["nodes"]["model.bookshop.reader"] = child
    m["child_map"] = {"model.bookshop.orders": ["model.bookshop.reader"]}
    for folder in ("target/compiled/bookshop/models", ".dbt-plan/base/compiled/models"):
        (tmp_path / folder / "reader.sql").write_text("select * from orders")
    save(tmp_path, m)
    save(tmp_path, m, True)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "contract_violation" in json.dumps(out)


def test_snapshot_only_project_can_snapshot_and_check(tmp_path, capsys):
    (tmp_path / "target").mkdir()
    m = {
        "metadata": {"project_name": "bookshop"},
        "nodes": {
            "snapshot.bookshop.history": {
                "name": "history",
                "resource_type": "snapshot",
                "raw_code": "select id, tax from orders",
                "config": {"materialized": "snapshot"},
            }
        },
    }
    save(tmp_path, m)
    _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
    capsys.readouterr()
    m["nodes"]["snapshot.bookshop.history"]["raw_code"] = "select id from orders"
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "snapshot changed" in json.dumps(out)


def test_successful_compile_allows_a_deleted_macro(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    (tmp_path / "dbt_project.yml").write_text("name: bookshop\n")
    old = copy.deepcopy(m)
    old["macros"] = {
        "macro.bookshop.cols": {
            "original_file_path": "macros/cols.sql",
            "macro_sql": "{% macro cols() %}id{% endmacro %}",
        }
    }
    save(tmp_path, old, True)
    m["metadata"]["invocation_id"] = "fresh"
    save(tmp_path, m)
    (tmp_path / "target/run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "fresh"},
                "results": [{"unique_id": "model.bookshop.orders", "status": "success"}],
            }
        )
    )
    assert check(tmp_path, capsys)[0] == 0


def test_subsecond_source_edit_is_stale(tmp_path, capsys):
    project(tmp_path, after="select 1 as id, 2 as tax")
    (tmp_path / "models").mkdir()
    source = tmp_path / "models/orders.sql"
    source.write_text("select 1 as id")
    manifest = tmp_path / "target/manifest.json"
    timestamp = manifest.stat().st_mtime
    os.utime(source, (timestamp + 0.25, timestamp + 0.25))
    assert check(tmp_path, capsys)[0] == 2


def test_partial_projection_keeps_known_dropped_columns(tmp_path, capsys):
    project(
        tmp_path,
        "select id, tax, count(*) from raw group by id, tax",
        "select id, count(*) from raw group by id",
    )
    code, report = check(tmp_path, capsys)
    assert code != 0
    model = report["models"][0]
    assert model["columns_removed"] == ["tax"]
    assert {"operation": "DROP COLUMN", "column": "tax"} in model["operations"]
    assert report["parse_failures"] == ["orders"]


@pytest.mark.parametrize("dialect", ["snowflake", "postgres"])
def test_quoted_contract_metadata_is_not_folded_into_proof(tmp_path, capsys, dialect):
    m = project(
        tmp_path,
        "select id from raw",
        "select id from raw -- edit",
        config={"contract": {"enforced": True}},
    )
    m["metadata"]["adapter_type"] = dialect
    m["nodes"]["model.bookshop.orders"]["columns"] = {"Id": {"name": "Id", "quote": True}}
    save(tmp_path, m)
    save(tmp_path, m, True)
    assert check(tmp_path, capsys)[0] == 2


def test_versioned_defined_in_selection_keeps_source_refusal(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    old = m["nodes"].pop("model.bookshop.orders")
    old.update(
        path="models/orders_current.sql", original_file_path="models/orders_current.sql", version=2
    )
    m["nodes"]["model.bookshop.orders.v2"] = old
    for folder in ("target/compiled/bookshop/models", ".dbt-plan/base/compiled/models"):
        (tmp_path / folder / "orders.sql").rename(tmp_path / folder / "orders_current.sql")
    save(tmp_path, m)
    save(tmp_path, m, True)
    (tmp_path / "models").mkdir()
    source = tmp_path / "models/orders_current.sql"
    source.write_text("select 1 as id")
    # Force the stale-input precondition independently of Windows timestamp
    # resolution; this test measures alias scoping, not write scheduling.
    timestamp = (tmp_path / "target/manifest.json").stat().st_mtime + 2
    os.utime(source, (timestamp, timestamp))
    assert check(tmp_path, capsys, "orders_v2")[0] == 2
