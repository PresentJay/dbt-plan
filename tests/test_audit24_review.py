"""Independent adversarial review of audit CLI changes."""

import copy
import json
import os

import pytest

from tests.test_audit24_cli import check, project, save


def successful_invocation(tmp_path, manifest):
    manifest["metadata"]["invocation_id"] = "review-current"
    save(tmp_path, manifest)
    (tmp_path / "target/run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "review-current"},
                "results": [{"unique_id": "model.bookshop.orders", "status": "success"}],
            }
        )
    )


def test_successful_invocation_cannot_validate_mismatched_compiled_sql(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    node = m["nodes"]["model.bookshop.orders"]
    node["compiled"] = True
    node["compiled_code"] = "select 1 as id"
    successful_invocation(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code != 0, out
    assert out["stale_sources"]


def test_recent_artifact_without_node_compilation_evidence_is_not_safe(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    m["metadata"]["invocation_id"] = "partial-compile"
    # An unselected node has raw source but no compiled flag/code. Its previous
    # file may be less than one second old, so mtime cannot prove compilation.
    m["nodes"]["model.bookshop.orders"]["raw_code"] = "select 1 as id"
    save(tmp_path, m)
    timestamp = (tmp_path / "target/manifest.json").stat().st_mtime
    artifact = tmp_path / "target/compiled/bookshop/models/orders.sql"
    os.utime(artifact, (timestamp, timestamp))
    code, out = check(tmp_path, capsys)
    assert code != 0, out


def test_content_edit_with_restored_mtime_requires_review(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    node = m["nodes"]["model.bookshop.orders"]
    node["raw_code"] = "select 1 as id, 2 as tax"
    (tmp_path / "models").mkdir()
    path = tmp_path / "models/orders.sql"
    path.write_text("select 1 as id")
    save(tmp_path, m)
    os.utime(path, (1, 1))
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "models/orders.sql" in out["stale_sources"]


def test_current_macro_deletion_not_hidden_by_successful_old_compile(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    (tmp_path / "dbt_project.yml").write_text("name: bookshop")
    m["macros"] = {
        "macro.bookshop.columns": {
            "original_file_path": "macros/columns.sql",
            "macro_sql": "{% macro columns() %}id, tax{% endmacro %}",
        }
    }
    successful_invocation(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert "macros/columns.sql" in out["stale_sources"]


def test_stale_successful_results_do_not_validate_new_manifest(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    successful_invocation(tmp_path, m)
    m["metadata"]["invocation_id"] = "new-failed-compile"
    m["nodes"]["model.bookshop.orders"]["compiled"] = False
    save(tmp_path, m)
    assert check(tmp_path, capsys)[0] == 2


def test_relation_move_does_not_downgrade_destructive_drop(tmp_path, capsys):
    m = project(tmp_path)
    m["nodes"]["model.bookshop.orders"]["schema"] = "archive"
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 1
    assert out["models"][0]["safety"] == "destructive"


@pytest.mark.parametrize("kind", ["python", "snapshot"])
def test_manifest_only_config_change_requires_review(tmp_path, capsys, kind):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    nid = "snapshot.bookshop.history" if kind == "snapshot" else "model.bookshop.history"
    m["nodes"][nid] = {
        "name": "history",
        "resource_type": "snapshot" if kind == "snapshot" else "model",
        "language": "python" if kind == "python" else "sql",
        "raw_code": "return df" if kind == "python" else "select id from orders",
        "config": {"materialized": "snapshot" if kind == "snapshot" else "table"},
    }
    save(tmp_path, copy.deepcopy(m), True)
    m["nodes"][nid]["config"]["schema"] = "archive"
    save(tmp_path, m)
    code, out = check(tmp_path, capsys)
    assert code == 2
    assert any(n["model_name"] == "history" for n in out["models"])


def test_mixed_known_and_unknown_selection_refuses(tmp_path, capsys):
    project(tmp_path, after="select 1 as id, 2 as tax")
    code, _ = check(tmp_path, capsys, "orders,typo")
    assert code != 0


def test_selection_checks_transitive_upstream_compilation(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    for name in ("parent", "grandparent"):
        node = copy.deepcopy(m["nodes"]["model.bookshop.orders"])
        node["name"] = name
        node["original_file_path"] = f"models/{name}.sql"
        m["nodes"][f"model.bookshop.{name}"] = node
    m["child_map"] = {
        "model.bookshop.grandparent": ["model.bookshop.parent"],
        "model.bookshop.parent": ["model.bookshop.orders"],
    }
    save(tmp_path, m)
    save(tmp_path, m, True)
    code, out = check(tmp_path, capsys, "orders")
    assert code == 2
    assert set(out["uncompiled_models"]) == {"parent", "grandparent"}
