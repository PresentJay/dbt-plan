"""Bookstore reproductions of false warnings observed in external projects."""

import copy
import hashlib
import os

import pytest

from tests.test_audit24_cli import check, project, save


def snapshot_project(tmp_path, *, normalized=True, identical=False, wrapper=False):
    manifest = project(tmp_path, after="select 1 as id, 2 as tax")
    bodies = {
        "books_history": "\nselect 'a  b' as title -- keep this newline\n, 1 as id\n",
        "customers_history": "\nselect 'customer' as title, 2 as id\n",
    }
    if identical:
        bodies["customers_history"] = bodies["books_history"]
    content = "{# History of bookstore records. #}\n" + "\n".join(
        f"{{% snapshot {name} %}}{body}{{% endsnapshot %}}" for name, body in bodies.items()
    )
    if wrapper:
        content = "{% if true %}\n" + content + "\n{% endif %}"
    source = tmp_path / "snapshots/history.sql"
    source.parent.mkdir()
    source.write_text(content)
    hashed = " ".join(content.split()) if normalized else content.strip()
    for name, body in bodies.items():
        manifest["nodes"][f"snapshot.bookshop.{name}"] = {
            "name": name,
            "resource_type": "snapshot",
            "original_file_path": "snapshots/history.sql",
            "raw_code": body,
            "checksum": {
                "name": "sha256",
                "checksum": hashlib.sha256(hashed.encode()).hexdigest(),
            },
            "config": {"materialized": "snapshot", "check_cols": ["title"]},
        }
    save(tmp_path, manifest, True)
    save(tmp_path, manifest)
    return source, manifest, bodies


@pytest.mark.parametrize("normalized", [False, True])
def test_unchanged_snapshot_with_original_or_normalized_checksum(tmp_path, capsys, normalized):
    snapshot_project(tmp_path, normalized=normalized)
    code, report = check(tmp_path, capsys)
    assert code == 0
    assert report["summary"]["total"] == 0
    assert not report.get("stale_sources")


@pytest.mark.parametrize(
    "damage",
    [
        "literal-space",
        "comment-newline",
        "identical-bodies",
        "rename",
        "delete",
        "duplicate",
        "missing-checksum",
        "missing-body",
        "all-bodies-missing",
    ],
)
def test_normalized_snapshot_hash_cannot_hide_source_damage(tmp_path, capsys, damage):
    source, manifest, bodies = snapshot_project(tmp_path, identical=damage == "identical-bodies")
    original = source.read_text()
    content = original
    if damage in {"literal-space", "identical-bodies"}:
        content = content.replace("'a  b'", "'a b'", 1)
    elif damage == "comment-newline":
        content = content.replace("-- keep this newline\n", "-- keep this newline ", 1)
    elif damage == "rename":
        content = content.replace("snapshot books_history", "snapshot renamed", 1)
    elif damage == "delete":
        content = content[: content.index("{% snapshot customers_history")]
    elif damage == "duplicate":
        content += f"\n{{% snapshot books_history %}}{bodies['books_history']}{{% endsnapshot %}}"
    elif damage == "missing-checksum":
        manifest["nodes"]["snapshot.bookshop.books_history"].pop("checksum")
        save(tmp_path, manifest)
    elif damage == "missing-body":
        manifest["nodes"]["snapshot.bookshop.books_history"].pop("raw_code")
        save(tmp_path, manifest)
    elif damage == "all-bodies-missing":
        for node_id, node in manifest["nodes"].items():
            if node_id.startswith("snapshot."):
                node.pop("raw_code")
        save(tmp_path, manifest)
    if damage in {"literal-space", "comment-newline", "identical-bodies"}:
        assert " ".join(content.split()) == " ".join(original.split())
    if damage == "identical-bodies":
        assert bodies["books_history"] in content  # Containment alone would falsely pass.
    source.write_text(content)
    os.utime(source, (1, 1))
    code, report = check(tmp_path, capsys)
    assert code == 2
    assert report["stale_sources"] == ["snapshots/history.sql"]


def test_normalized_snapshot_with_unverified_outer_template_still_warns(tmp_path, capsys):
    snapshot_project(tmp_path, wrapper=True)
    assert check(tmp_path, capsys)[0] == 2


def test_fresh_normalized_snapshot_definition_change_requires_review(tmp_path, capsys):
    _, manifest, _ = snapshot_project(tmp_path)
    manifest["nodes"]["snapshot.bookshop.books_history"]["config"]["check_cols"] = "all"
    save(tmp_path, manifest)
    code, report = check(tmp_path, capsys)
    assert code == 2
    assert not report.get("stale_sources")
    assert [(m["model_name"], m["safety"]) for m in report["models"]] == [
        ("books_history", "warning")
    ]


def comment_project(tmp_path, *, kind="comment", aliased=False):
    manifest = project(tmp_path)
    parent = '"main"."physical_orders"' if aliased else '"main"."orders"'
    manifest["nodes"]["model.bookshop.orders"].update(
        relation_name=parent, compiled=True, compiled_code="select 1 as id"
    )
    if aliased:
        manifest["nodes"]["model.bookshop.orders"]["alias"] = "physical_orders"
    comment = f"/* Disabled query: select tax from {parent} */"
    bridge = f'select b.book_id as id from "main"."books" b {comment}'
    if kind == "actual":
        bridge = f"select o.tax as id from {parent} o {comment}"
    elif kind == "star":
        bridge = f"select * from {parent} {comment}"
    elif kind == "depends-on":
        bridge = f'select b.book_id as id from "main"."books" b -- depends_on: {parent}'
    elif kind == "udf":
        bridge = f'select hidden_read(b.book_id) as id from "main"."books" b {comment}'
    elif kind == "qualified-udf":
        bridge = f'select custom.abs(b.book_id) as id from "main"."books" b {comment}'
    elif kind == "dynamic":
        bridge = f"select 1 as id from read_csv('books.csv') {comment}"
    elif kind == "unknown-relation":
        bridge = f'select b.book_id as id from "main"."external_view" b {comment}'
    elif kind == "unknown-qualified-relation":
        bridge = f'select b.book_id as id from "other"."books" b {comment}'
    elif kind == "multi-statement":
        bridge = f"select 1 as id {comment}; select tax from {parent}"
    elif kind == "parse-error":
        bridge = f"select from ( {comment}"
    elif kind == "string":
        bridge = f"select '{parent}' as id"
    nodes = {
        "books": "select 1 as book_id",
        "bridge": bridge,
        "far": 'select hidden_read(b.id) as id from "main"."bridge" b',
        "actual_reader": f"select o.tax from {parent} o",
    }
    compiled = tmp_path / "target/compiled/bookshop/models"
    base = tmp_path / ".dbt-plan/base/compiled/models"
    for name, sql in nodes.items():
        manifest["nodes"][f"model.bookshop.{name}"] = {
            "name": name,
            "resource_type": "model",
            "language": "sql",
            "original_file_path": f"models/{name}.sql",
            "path": f"models/{name}.sql",
            "relation_name": f'"main"."{name}"',
            "config": {"materialized": "view"},
            "raw_code": sql,
            "compiled": True,
            "compiled_code": sql,
            "depends_on": {"nodes": []},
        }
        (compiled / f"{name}.sql").write_text(sql)
        (base / f"{name}.sql").write_text(sql)
    manifest["nodes"]["model.bookshop.bridge"]["depends_on"]["nodes"] = [
        "model.bookshop.orders",
        "model.bookshop.books",
    ]
    manifest["nodes"]["model.bookshop.far"]["depends_on"]["nodes"] = ["model.bookshop.bridge"]
    manifest["nodes"]["model.bookshop.actual_reader"]["depends_on"]["nodes"] = [
        "model.bookshop.orders"
    ]
    manifest["child_map"] = {
        "model.bookshop.orders": ["model.bookshop.bridge", "model.bookshop.actual_reader"],
        "model.bookshop.books": ["model.bookshop.bridge"],
        "model.bookshop.bridge": ["model.bookshop.far"],
    }
    node = manifest["nodes"]["model.bookshop.bridge"]
    if kind == "python":
        node.update(language="python", raw_code="return df")
    elif kind == "ephemeral":
        node["config"]["materialized"] = "ephemeral"
    elif kind == "hook":
        node["config"]["post-hook"] = [f"select * from {parent}"]
    elif kind == "custom":
        node["config"]["materialized"] = "custom"
    elif kind == "parent-uncompiled":
        manifest["nodes"]["model.bookshop.orders"]["compiled"] = False
    elif kind == "uncompiled":
        node["compiled"] = False
    elif kind == "mismatch":
        node["compiled_code"] = "select 99 as id"
    old = copy.deepcopy(manifest)
    old["nodes"]["model.bookshop.orders"]["compiled_code"] = "select 1 as id, 2 as tax"
    save(tmp_path, old, True)
    save(tmp_path, manifest)
    return manifest


@pytest.mark.parametrize("aliased", [False, True])
def test_comment_only_edge_does_not_spread_cascade_uncertainty(tmp_path, capsys, aliased):
    comment_project(tmp_path, aliased=aliased)
    code, report = check(tmp_path, capsys)
    assert code == 1
    changed = next(m for m in report["models"] if m["model_name"] == "orders")
    assert "bridge" not in changed["downstream"]
    assert "far" not in changed["downstream"]
    assert any(
        f["rule_code"] == "cascade.broken_ref" and f["affected"]["name"] == "actual_reader"
        for f in report["findings"]
    )
    assert not any(
        f["rule_code"] == "input.refusal" and f["affected"]["name"] == "far"
        for f in report["findings"]
    )


@pytest.mark.parametrize(
    "kind",
    [
        "actual",
        "star",
        "depends-on",
        "udf",
        "qualified-udf",
        "dynamic",
        "unknown-relation",
        "unknown-qualified-relation",
        "multi-statement",
        "parse-error",
        "string",
        "python",
        "ephemeral",
        "hook",
        "custom",
        "parent-uncompiled",
    ],
)
def test_unproven_or_real_dependencies_remain_in_cascade(tmp_path, capsys, kind):
    comment_project(tmp_path, kind=kind)
    code, report = check(tmp_path, capsys)
    assert code in (1, 2)
    changed = next(m for m in report["models"] if m["model_name"] == "orders")
    assert "bridge" in changed["downstream"] and "far" in changed["downstream"]


@pytest.mark.parametrize("kind", ["uncompiled", "mismatch"])
def test_unverified_compilation_cannot_prune_dependencies(tmp_path, capsys, kind):
    comment_project(tmp_path, kind=kind)
    code, report = check(tmp_path, capsys)
    assert code in (1, 2)
    changed = next(m for m in report["models"] if m["model_name"] == "orders")
    assert "bridge" in changed["downstream"] and "far" in changed["downstream"]


def test_comment_filter_does_not_change_dbt_graph_selection(tmp_path, capsys):
    manifest = comment_project(tmp_path)
    path = tmp_path / "target/compiled/bookshop/models/far.sql"
    sql = path.read_text().replace("as id", "as id, 2 as marker")
    path.write_text(sql)
    manifest["nodes"]["model.bookshop.far"].update(compiled_code=sql, raw_code=sql)
    save(tmp_path, manifest)
    # The manifest's real selection still reaches bridge/far, even though its
    # comment edge cannot transmit this SQL-level column loss.
    code, report = check(tmp_path, capsys, "orders+")
    assert code == 1
    assert report["analysis"]["selection"] == "orders+"
    assert {"orders", "far"}.issubset(m["model_name"] for m in report["models"])


def test_comment_filter_preserves_alternative_real_dependency_path(tmp_path, capsys):
    manifest = comment_project(tmp_path)
    # orders -> bridge is disabled, but orders -> books -> bridge is a real path.
    sql = 'select o.tax as book_id from "main"."orders" o'
    path = tmp_path / "target/compiled/bookshop/models/books.sql"
    path.write_text(sql)
    (tmp_path / ".dbt-plan/base/compiled/models/books.sql").write_text(sql)
    node = manifest["nodes"]["model.bookshop.books"]
    node.update(raw_code=sql, compiled_code=sql, depends_on={"nodes": ["model.bookshop.orders"]})
    manifest["child_map"]["model.bookshop.orders"].append("model.bookshop.books")
    old = copy.deepcopy(manifest)
    old["nodes"]["model.bookshop.orders"]["compiled_code"] = "select 1 as id, 2 as tax"
    save(tmp_path, old, True)
    save(tmp_path, manifest)
    code, report = check(tmp_path, capsys)
    assert code == 1
    changed = next(m for m in report["models"] if m["model_name"] == "orders")
    assert {"books", "bridge", "far"}.issubset(changed["downstream"])
    assert any(
        f["rule_code"] == "cascade.broken_ref" and f["affected"]["name"] == "books"
        for f in report["findings"]
    )
