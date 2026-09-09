"""Regression controls for the four findings in the PR 200 review."""

import copy
import hashlib
import json
import os

import pytest

from tests.test_audit24_cascade import cascade
from tests.test_audit24_cli import check, project, save


@pytest.mark.parametrize("name", ["active_id", "not_null"])
@pytest.mark.parametrize("available", [True, False])
def test_local_generic_test_and_builtin_override_inspect_sql(tmp_path, name, available):
    test = {
        "name": "test_orders_id",
        "column_name": "id",
        "attached_node": "model.p.orders",
        "depends_on": {"nodes": ["model.p.orders"]},
        "test_metadata": {
            "name": name,
            "namespace": None,
            "kwargs": {"model": "ref('orders')", "column_name": "id"},
        },
    }
    manifest = {"nodes": {"test.p.t": test}, "child_map": {"model.p.orders": ["test.p.t"]}}
    sql = tmp_path / "test.sql"
    sql.write_text("select id from orders where status < 0")
    result = cascade(manifest, test_sql_index={test["name"]: sql} if available else {})
    assert result.safety.value == "warning"
    assert result.downstream_impacts[0].risk == (
        "data_test_failure" if available else "data_test_unreadable"
    )


@pytest.mark.parametrize("evidence", ["uncompiled", "mismatch", "failed", "valid"])
def test_data_test_sql_requires_current_compilation_evidence(tmp_path, capsys, evidence):
    m = project(tmp_path, config={"materialized": "view"})
    m["metadata"]["invocation_id"] = "current"
    model = m["nodes"]["model.bookshop.orders"]
    model.update(compiled=True, compiled_code="select 1 as id")
    test = {
        "name": "test_orders_id",
        "column_name": "id",
        "attached_node": "model.bookshop.orders",
        "depends_on": {"nodes": ["model.bookshop.orders"]},
        "config": {"where": "tax > 0" if evidence != "valid" else "id > 0"},
        "raw_code": "{{ test_not_null(**_dbt_generic_test_kwargs) }}",
        "original_file_path": "models/schema.yml",
        "test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}},
    }
    sql = "select id from orders where id > 0"
    if evidence != "uncompiled":
        test.update(
            compiled=True,
            compiled_code=sql if evidence != "mismatch" else "select tax from orders",
        )
    m["nodes"]["test.bookshop.t"] = test
    m["child_map"] = {"model.bookshop.orders": ["test.bookshop.t"]}
    save(tmp_path, m)
    folder = tmp_path / "target/compiled/bookshop/models/schema.yml"
    folder.mkdir()
    (folder / "test_orders_id.sql").write_text(sql)
    results = [{"unique_id": "model.bookshop.orders", "status": "success"}]
    if evidence != "uncompiled":
        results.append(
            {
                "unique_id": "test.bookshop.t",
                "status": "error" if evidence == "failed" else "success",
            }
        )
    (tmp_path / "target/run_results.json").write_text(
        json.dumps({"metadata": {"invocation_id": "current"}, "results": results})
    )
    code, out = check(tmp_path, capsys)
    assert code == (0 if evidence == "valid" else 2)
    if evidence != "valid":
        assert "data_test_unreadable" in json.dumps(out)


@pytest.mark.parametrize("change", [None, "first", "second", "wrapper", "deleted"])
def test_snapshot_file_checksum_covers_all_blocks(tmp_path, capsys, change):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    folder = tmp_path / "snapshots"
    folder.mkdir()
    content = "{% snapshot first %}\nselect 1 as id\n{% endsnapshot %}\n{% snapshot second %}\nselect 2 as id\n{% endsnapshot %}\n"
    source = folder / "history.sql"
    source.write_text(content)
    for name, raw in [("first", "\nselect 1 as id\n"), ("second", "\nselect 2 as id\n")]:
        m["nodes"][f"snapshot.bookshop.{name}"] = {
            "name": name,
            "resource_type": "snapshot",
            "original_file_path": "snapshots/history.sql",
            "raw_code": raw,
            "checksum": {
                "name": "sha256",
                "checksum": hashlib.sha256(content.strip().encode()).hexdigest(),
            },
            "config": {"materialized": "snapshot"},
        }
    save(tmp_path, m, True)
    save(tmp_path, m)
    if change == "deleted":
        source.unlink()
    else:
        if change in {"first", "second"}:
            content = content.replace("select 1" if change == "first" else "select 2", "select 3")
        elif change == "wrapper":
            content = content.replace("snapshot first", "snapshot renamed")
        source.write_text(content)
        os.utime(source, (1, 1))  # Content checks, independent of mtime.
    code, out = check(tmp_path, capsys)
    assert code == (0 if change is None else 2)
    assert bool(out.get("stale_sources")) == (change is not None)


def test_missing_snapshot_checksum_is_uncertain(tmp_path, capsys):
    m = project(tmp_path, after="select 1 as id, 2 as tax")
    (tmp_path / "snapshots").mkdir()
    source = tmp_path / "snapshots/history.sql"
    source.write_text("{% snapshot history %}select 1 as id{% endsnapshot %}")
    m["nodes"]["snapshot.bookshop.history"] = {
        "name": "history",
        "resource_type": "snapshot",
        "original_file_path": "snapshots/history.sql",
        "raw_code": "select 1 as id",
        "config": {"materialized": "snapshot"},
    }
    save(tmp_path, m)
    save(tmp_path, copy.deepcopy(m), True)
    assert check(tmp_path, capsys)[0] == 2
