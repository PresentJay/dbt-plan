"""Tests for _do_stats accuracy — materialization counts, SELECT * detection, coverage score."""

from __future__ import annotations

import argparse
import json

import pytest

from dbt_plan.cli import _do_stats
from dbt_plan.manifest import build_node_index


def _stats_report(project, capsys, *, target_dir="target", dialect=None):
    args = _make_args(str(project), target_dir=target_dir, dialect=dialect)
    args.format = "json"
    _do_stats(args)
    report = json.loads(capsys.readouterr().out)
    args.format = "text"
    _do_stats(args)
    return report, capsys.readouterr().out


def _write_stats_project(project, nodes, sql_files, *, target_dir="target"):
    target = project / target_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(_make_manifest(nodes, {"project_name": "proj", "adapter_type": "snowflake"}))
    )
    root = target / "compiled/proj"
    for name, sql in sql_files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql)
    return root


@pytest.mark.parametrize(
    "materialization,osc",
    [
        ("table", None),
        ("incremental", "fail"),
        ("custom", None),
    ],
)
def test_stats_counts_alias_once_in_every_manifest_counter(tmp_path, capsys, materialization, osc):
    nid, node = _model_node("orders", materialization=materialization, on_schema_change=osc)
    node.update(path="renamed.sql", original_file_path="models/renamed.sql")
    manifest = _make_manifest({nid: node}, {"project_name": "proj"})
    index = build_node_index(manifest)
    assert index["orders"].node_id == index["renamed"].node_id == nid
    _write_stats_project(tmp_path, {nid: node}, {"models/renamed.sql": "select 1 as id"})
    report, text = _stats_report(tmp_path, capsys)
    summary = report["summary"]
    assert summary["total"] == 1
    assert summary["materializations"] == {materialization: 1}
    assert summary["on_schema_change"] == ({"fail": 1} if osc else {})
    assert summary["cascade_risk"] == (1 if osc else 0)
    assert summary["ddl_rules"] == {"matched": 0 if materialization == "custom" else 1, "total": 1}
    assert sum(report["details"]["no_rule"].values()) == (1 if materialization == "custom" else 0)
    assert "1 model(s) in manifest" in text


def test_stats_separates_models_from_artifact_and_orphan_sql(tmp_path, capsys):
    nid, orders = _model_node("orders")
    orders.update(path="orders.sql", original_file_path="models/orders.sql")
    inline_id, inline = _model_node("inline")
    inline.update(path="inline.sql", original_file_path="target/inline.sql")
    _write_stats_project(
        tmp_path,
        {nid: orders, inline_id: inline},
        {
            "target/inline.sql": "select 1 as id",
            "target/orphan.sql": "select 2 as id",
        },
    )
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"] == {
        "total": 1,
        "compiled": 0,
        "readable": 0,
        "unreadable": 0,
        "missing": 1,
        "manifest_only": 0,
        "unclassified": 1,
    }
    assert report["details"]["missing_model_sql"] == [nid]
    assert report["details"]["unclassified_model_nodes"] == [inline_id]
    # Keep existing JSON fields as file measurements instead of changing their
    # public meaning without a version. Text makes the different denominator clear.
    assert report["summary"]["columns_readable"] == {"compiled": 2, "readable": 2, "unreadable": 0}
    assert "Model SQL present: 0/1" in text
    assert nid in text
    assert "Unclassified model nodes: 1" in text
    assert "2/2 compiled SQL file(s)" in text
    assert "2/2 compiled model(s)" not in text


def test_stats_model_sql_tracks_versions_paths_filters_and_missing_inputs(tmp_path, capsys):
    nodes = {}
    for version, filename, directory in [(1, "orders_v1", "transforms"), (2, "latest", "extras")]:
        nid, node = _model_node("orders")
        node.update(
            version=version,
            path=f"{filename}.sql",
            original_file_path=f"{directory}/{filename}.sql",
        )
        nodes[f"{nid}.v{version}"] = node
    # An inline_ prefix can be a perfectly ordinary user model.
    nid, node = _model_node("inline_user")
    node.update(path="inline_user.sql", original_file_path="transforms/inline_user.sql")
    nodes[nid] = node
    for name, language, materialization in [
        ("python", "python", "table"),
        ("temporary", "sql", "ephemeral"),
    ]:
        nid, node = _model_node(name, materialization=materialization)
        node.update(
            language=language,
            path=f"{name}.{'py' if language == 'python' else 'sql'}",
            original_file_path=f"transforms/{name}.{'py' if language == 'python' else 'sql'}",
        )
        nodes[nid] = node
    nid, node = _model_node("disabled")
    node.update(path="disabled.sql", original_file_path="transforms/disabled.sql")
    node["config"]["enabled"] = False
    nodes[nid] = node
    nid, node = _model_node("package", project="dep")
    node.update(path="package.sql", original_file_path="transforms/package.sql")
    nodes[nid] = node
    _write_stats_project(
        tmp_path,
        nodes,
        {
            "transforms/orders_v1.sql": "select 1 as id",
            "extras/latest.sql": "select * from unknown",
            "transforms/inline_user.sql": "select 2 as id",
            "transforms/temporary.sql": "select 3 as id",
            "transforms/disabled.sql": "select 4 as id",
            "transforms/package.sql": "select 5 as id",
            "extras/orphan.sql": "select 6 as id",
        },
    )
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["total"] == 5
    assert report["summary"]["model_sql"] == {
        "total": 4,
        "compiled": 4,
        "readable": 3,
        "unreadable": 1,
        "missing": 0,
        "manifest_only": 1,
        "unclassified": 0,
    }
    assert report["details"]["missing_model_sql"] == []
    assert "Model SQL present: 4/4" in text
    assert "Model columns readable: 3/4" in text


def test_stats_does_not_match_a_model_to_an_orphan_with_the_same_stem(tmp_path, capsys):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path="models/actual/orders.sql")
    _write_stats_project(tmp_path, {nid: node}, {"models/orphan/orders.sql": "select 1 as id"})
    report, _ = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["compiled"] == 0
    assert report["details"]["missing_model_sql"] == [nid]


@pytest.mark.parametrize(
    "declared", ["./models/orders.sql", "models//orders.sql", "models/./orders.sql"]
)
def test_stats_normalized_model_paths(tmp_path, capsys, declared):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path=declared)
    _write_stats_project(tmp_path, {nid: node}, {"models/orders.sql": "select 1 as id"})
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["compiled"] == 1
    assert report["summary"]["model_sql"]["readable"] == 1
    assert report["details"]["missing_model_sql"] == []
    assert "Model SQL present: 1/1" in text


@pytest.mark.parametrize("version_first", [False, True])
def test_stats_alias_collision_keeps_each_model_identity(tmp_path, capsys, version_first):
    nid, versioned = _model_node("orders", materialization="view")
    versioned.update(
        version=1,
        path="orders_def.sql",
        original_file_path="models/orders_def.sql",
        relation_name="db.main.orders_v1",
    )
    regular_id, regular = _model_node(
        "orders_v1", materialization="incremental", on_schema_change="fail"
    )
    regular.update(
        path="orders_v1.sql",
        original_file_path="models/orders_v1.sql",
        relation_name="db.main.regular_orders",
    )
    regular["unrendered_config"] = {"on_schema_change": "fail"}
    pairs = [(f"{nid}.v1", versioned), (regular_id, regular)]
    nodes = dict(pairs if version_first else reversed(pairs))
    for name in ("version_reader", "regular_reader"):
        reader_id, reader = _model_node(name, materialization="view")
        reader.update(path=f"{name}.sql", original_file_path=f"models/{name}.sql")
        nodes[reader_id] = reader
    _write_stats_project(
        tmp_path,
        nodes,
        {
            "models/orders_def.sql": "select 1 as version_id",
            "models/orders_v1.sql": "select 2 as regular_id",
            "models/version_reader.sql": "select * from db.main.orders_v1",
            "models/regular_reader.sql": "select * from db.main.regular_orders",
        },
    )
    report, _ = _stats_report(tmp_path, capsys)
    summary = report["summary"]
    assert summary["total"] == 4
    assert summary["materializations"] == {"view": 3, "incremental": 1}
    assert summary["on_schema_change"] == {"fail": 1}
    assert summary["cascade_risk"] == 1
    assert summary["ddl_rules"] == {"matched": 4, "total": 4}
    assert summary["model_sql"]["compiled"] == 4
    assert summary["model_sql"]["readable"] == 4


@pytest.mark.parametrize("excluded", ["package", "disabled", "artifact", "python"])
@pytest.mark.parametrize(
    "foreign_name,relation_alias",
    [("invoices", "invoices_v1"), ("orders", "orders"), ("invoices", "orders")],
)
def test_stats_excluded_model_relations(tmp_path, capsys, excluded, foreign_name, relation_alias):
    nid, root = _model_node("orders")
    root.update(
        version=1,
        path="shared.sql",
        original_file_path="models/shared.sql",
        relation_name="db.public.orders_v1",
    )
    foreign_id, foreign = _model_node(
        foreign_name, project="dep" if excluded == "package" else "proj"
    )
    foreign.update(
        version=2,
        path="shared.sql",
        original_file_path="models/shared.sql",
        relation_name=f"db.dep.{relation_alias}",
    )
    if excluded == "disabled":
        foreign["config"]["enabled"] = False
    elif excluded == "artifact":
        foreign["original_file_path"] = "target/shared.sql"
    elif excluded == "python":
        foreign.update(language="python", path="shared.py", original_file_path="models/shared.py")
    nodes = {f"{nid}.v1": root, f"{foreign_id}.v2": foreign}
    for name in ("known_reader", "foreign_reader"):
        reader_id, reader = _model_node(name)
        reader.update(path=f"{name}.sql", original_file_path=f"models/{name}.sql")
        nodes[reader_id] = reader
    _write_stats_project(
        tmp_path,
        nodes,
        {
            "models/shared.sql": "select 1 as local_id",
            "models/known_reader.sql": "select * from db.public.orders_v1",
            "models/foreign_reader.sql": f"select * from db.dep.{relation_alias}",
        },
    )
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["total"] == 3
    assert report["summary"]["model_sql"]["readable"] == 2
    assert report["summary"]["model_sql"]["unreadable"] == 1
    assert "Model columns readable: 2/3" in text


def test_stats_custom_artifact_directory_does_not_exclude_target_named_model_path(
    tmp_path, capsys
):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path="target/orders.sql")
    _write_stats_project(
        tmp_path, {nid: node}, {"target/orders.sql": "select 1 as id"}, target_dir="build"
    )
    report, _ = _stats_report(tmp_path, capsys, target_dir="build")
    assert report["summary"]["model_sql"]["compiled"] == 1
    assert report["summary"]["model_sql"]["unclassified"] == 0


def test_stats_absent_compiled_input_exposes_missing_sql_without_counting_python(tmp_path, capsys):
    nid, node = _model_node("orders")
    node.update(original_file_path="models/orders.sql", path="orders.sql")
    python_id, python = _model_node("python")
    python.update(language="python", original_file_path="models/python.py", path="python.py")
    _write_stats_project(tmp_path, {nid: node, python_id: python}, {})
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"] == {
        "total": 1,
        "compiled": 0,
        "readable": 0,
        "unreadable": 0,
        "missing": 1,
        "manifest_only": 1,
        "unclassified": 0,
    }
    assert report["summary"]["columns_readable"] is None
    assert report["details"]["missing_model_sql"] == [nid]
    assert "Model SQL present: 0/1" in text


def test_stats_model_resolver_uses_only_identified_model_paths(tmp_path, capsys):
    base_id, base = _model_node("orders")
    base.update(
        path="orders.sql",
        original_file_path="models/actual/orders.sql",
        alias="orders",
        relation_name='"db"."public"."orders"',
    )
    reader_id, reader = _model_node("reader")
    reader.update(path="reader.sql", original_file_path="models/reader.sql")
    _write_stats_project(
        tmp_path,
        {base_id: base, reader_id: reader},
        {
            "models/orphan/orders.sql": "select 1 as id",
            "models/reader.sql": "select * from db.public.orders",
        },
    )
    report, _ = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["compiled"] == 1
    assert report["summary"]["model_sql"]["readable"] == 0
    assert report["summary"]["model_sql"]["unreadable"] == 1
    assert report["details"]["missing_model_sql"] == [base_id]


def test_stats_invalid_sql_encoding_is_unreadable(tmp_path, capsys):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path="models/orders.sql")
    root = _write_stats_project(tmp_path, {nid: node}, {"models/orders.sql": "select 1 as id"})
    (root / "models/orders.sql").write_bytes(b"select \xff as id")
    report, _ = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["readable"] == 0
    assert report["summary"]["model_sql"]["unreadable"] == 1
    assert report["summary"]["columns_readable"]["unreadable"] == 1


@pytest.mark.parametrize("foreign_relation", ["db.raw.orders", "other.main.orders"])
def test_stats_relation_identity_cannot_borrow_model_columns(tmp_path, capsys, foreign_relation):
    nodes = {}
    for name in ("orders", "known_reader", "foreign_reader"):
        nid, node = _model_node(name)
        node.update(
            path=f"{name}.sql",
            original_file_path=f"models/{name}.sql",
            relation_name=f"db.main.{name}",
        )
        nodes[nid] = node
    _write_stats_project(
        tmp_path,
        nodes,
        {
            "models/orders.sql": "select 1 as local_id",
            "models/known_reader.sql": "select * from db.main.orders",
            "models/foreign_reader.sql": f"select * from {foreign_relation}",
        },
    )
    report, text = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["readable"] == 2
    assert report["summary"]["model_sql"]["unreadable"] == 1
    assert report["summary"]["columns_readable"] == {"readable": 2, "unreadable": 1, "compiled": 3}
    assert "Model columns readable: 2/3" in text


@pytest.mark.parametrize(
    "dialect,known,foreign",
    [
        ("snowflake", '"DB"."MAIN"."orders"', '"DB"."MAIN"."Orders"'),
        ("postgres", '"db"."main"."orders"', '"db"."main"."Orders"'),
        ("bigquery", "project.dataset.orders", "project.dataset.Orders"),
    ],
)
def test_stats_relation_identity_preserves_table_case(tmp_path, capsys, dialect, known, foreign):
    nodes = {}
    for name in ("orders", "known_reader", "foreign_reader"):
        nid, node = _model_node(name)
        node.update(path=f"{name}.sql", original_file_path=f"models/{name}.sql")
        if name == "orders":
            node["relation_name"] = known
        nodes[nid] = node
    _write_stats_project(
        tmp_path,
        nodes,
        {
            "models/orders.sql": "select 1 as local_id",
            "models/known_reader.sql": f"select * from {known}",
            "models/foreign_reader.sql": f"select * from {foreign}",
        },
    )
    report, _ = _stats_report(tmp_path, capsys, dialect=dialect)
    assert report["summary"]["model_sql"]["readable"] == 2
    assert report["summary"]["model_sql"]["unreadable"] == 1


@pytest.mark.parametrize("has_relation", [False, True])
@pytest.mark.parametrize("reference,readable", [("customers", 2), ("orders", 1)])
def test_stats_relation_identity_uses_physical_alias(
    tmp_path, capsys, has_relation, reference, readable
):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path="models/orders.sql", alias="customers")
    if has_relation:
        node["relation_name"] = "db.main.customers"
    reader_id, reader = _model_node("reader")
    reader.update(path="reader.sql", original_file_path="models/reader.sql")
    _write_stats_project(
        tmp_path,
        {nid: node, reader_id: reader},
        {
            "models/orders.sql": "select 1 as local_id",
            "models/reader.sql": f"select * from {reference}",
        },
    )
    report, _ = _stats_report(tmp_path, capsys)
    assert report["summary"]["model_sql"]["readable"] == readable
    assert report["summary"]["model_sql"]["unreadable"] == 2 - readable


@pytest.mark.parametrize(
    "declared", ["../models/orders.sql", "/models/orders.sql", r"C:\models\orders.sql"]
)
def test_stats_model_paths_cannot_escape_the_project(tmp_path, capsys, declared):
    nid, node = _model_node("orders")
    node.update(path="orders.sql", original_file_path=declared)
    _write_stats_project(tmp_path, {nid: node}, {"models/orders.sql": "select 1 as id"})
    args = _make_args(str(tmp_path))
    args.format = "json"
    with pytest.raises(SystemExit) as exc:
        _do_stats(args)
    assert exc.value.code == 3
    output = capsys.readouterr()
    assert not output.out
    assert "original_file_path" in output.err


def _make_manifest(nodes: dict, metadata: dict | None = None) -> dict:
    """Build a minimal manifest dict."""
    return {
        "nodes": nodes,
        "child_map": {},
        "metadata": metadata or {},
    }


def _model_node(
    name: str,
    project: str = "proj",
    materialization: str = "table",
    on_schema_change: str | None = None,
    columns: dict | None = None,
) -> tuple[str, dict]:
    """Return (node_id, node_dict) for a model node."""
    node_id = f"model.{project}.{name}"
    node = {
        "name": name,
        "config": {
            "materialized": materialization,
            "on_schema_change": on_schema_change,
        },
    }
    if columns is not None:
        node["columns"] = columns
    return node_id, node


def _write_manifest(tmp_path, manifest_data: dict):
    """Write manifest.json to tmp_path and return its path."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_data))
    return path


def _setup_compiled_dir(tmp_path, sql_files: dict[str, str]):
    """Create target/compiled/{project}/models/ with SQL files.

    sql_files: mapping of model_name -> SQL content.
    Returns the target dir path.
    """
    target_dir = tmp_path / "target"
    compiled_dir = target_dir / "compiled" / "proj" / "models"
    compiled_dir.mkdir(parents=True)
    for name, sql in sql_files.items():
        (compiled_dir / f"{name}.sql").write_text(sql)
    return target_dir


def _make_args(
    project_dir: str,
    target_dir: str = "target",
    manifest: str | None = None,
    dialect: str | None = None,
) -> argparse.Namespace:
    """Build args namespace for _do_stats."""
    return argparse.Namespace(
        project_dir=project_dir,
        target_dir=target_dir,
        manifest=manifest,
        dialect=dialect,
    )


class TestStatsOutputCorrectness:
    """Scenario 1: Stats output correctness with known model counts."""

    def test_materialization_and_osc_counts(self, tmp_path, capsys):
        """5 tables, 3 views, 4 incremental (2 fail, 1 sync_all, 1 ignore), 2 ephemeral, 1 snapshot."""
        nodes = {}

        # 5 tables
        for i in range(5):
            nid, node = _model_node(f"tbl_{i}", materialization="table")
            nodes[nid] = node

        # 3 views
        for i in range(3):
            nid, node = _model_node(f"vw_{i}", materialization="view")
            nodes[nid] = node

        # 4 incremental: 2 fail, 1 sync_all_columns, 1 ignore
        nid, node = _model_node(
            "inc_fail_0", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_fail_1", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_sync", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_ign", materialization="incremental", on_schema_change="ignore"
        )
        nodes[nid] = node

        # 2 ephemeral
        for i in range(2):
            nid, node = _model_node(f"eph_{i}", materialization="ephemeral")
            nodes[nid] = node

        # 1 snapshot
        nid, node = _model_node("snap_0", materialization="snapshot")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        # No compiled dir needed for pure manifest stats
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # Total: 5+3+4+2+1 = 15
        assert "15 model(s) in manifest" in out

        # Materialization counts
        assert "table" in out
        assert "view" in out
        assert "incremental" in out
        assert "ephemeral" in out
        assert "snapshot" in out

        # on_schema_change (incremental only) section
        assert "on_schema_change (incremental only):" in out
        assert "fail" in out
        assert "sync_all_columns" in out
        assert "ignore" in out

    def test_osc_incremental_breakdown(self, tmp_path, capsys):
        """Verify incremental on_schema_change section shows correct counts."""
        nodes = {}
        nid, node = _model_node("inc_f1", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("inc_f2", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node(
            "inc_s", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node("inc_i", materialization="incremental", on_schema_change="ignore")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        lines = out.splitlines()

        # Find the incremental osc section
        osc_section = False
        osc_lines = []
        for line in lines:
            if "on_schema_change (incremental only):" in line:
                osc_section = True
                continue
            if osc_section:
                if line.strip() and line.startswith("  "):
                    osc_lines.append(line)
                elif line.strip() == "":
                    continue
                else:
                    break

        # fail:2, sync_all_columns:1, ignore:1
        fail_line = [ln for ln in osc_lines if "fail" in ln and "sync" not in ln]
        assert len(fail_line) >= 1
        assert "2" in fail_line[0]

        sync_line = [ln for ln in osc_lines if "sync_all_columns" in ln]
        assert len(sync_line) >= 1
        assert "1" in sync_line[0]

        # fail and sync_all_columns should be marked as monitored
        assert "dbt-plan monitors this" in fail_line[0]
        assert "dbt-plan monitors this" in sync_line[0]

    def test_cascade_risk_mentions_fail_models(self, tmp_path, capsys):
        """Cascade risk count mentions fail models."""
        nodes = {}
        nid, node = _model_node("inc_f1", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("inc_f2", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("tbl", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "Cascade risk: 2 incremental model(s) with on_schema_change=fail" in out


class TestSelectStarCounting:
    """Scenario 2: SELECT * counting in compiled SQL."""

    def test_star_count_correct(self, tmp_path, capsys):
        """3 models use SELECT *, 2 use explicit columns -> 3/5 (60%)."""
        nodes = {}
        for name in ("star1", "star2", "star3", "explicit1", "explicit2"):
            nid, node = _model_node(name, materialization="table")
            nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "star1": "SELECT * FROM raw.users",
            "star2": "SELECT * FROM raw.events",
            "star3": "SELECT * FROM raw.orders",
            "explicit1": "SELECT id, name, email FROM raw.users",
            "explicit2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 3/5 SQL files (60%)" in out


class TestManifestColumnFallback:
    """Scenario 3: Manifest column fallback info."""

    def test_fallback_is_counted_only_for_models_it_could_help(self, tmp_path, capsys):
        """It used to count every documented model, including ones already readable."""
        nodes = {}
        # star1 and star2 have columns in manifest
        nid, node = _model_node("star1", materialization="table", columns={"id": {}, "name": {}})
        nodes[nid] = node
        nid, node = _model_node(
            "star2", materialization="table", columns={"order_id": {}, "total": {}}
        )
        nodes[nid] = node
        # star3 has no columns in manifest
        nid, node = _model_node("star3", materialization="table")
        nodes[nid] = node
        # explicit models
        nid, node = _model_node("explicit1", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("explicit2", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "star1": "SELECT * FROM raw.users",
            "star2": "SELECT * FROM raw.events",
            "star3": "SELECT * FROM raw.orders",
            "explicit1": "SELECT id, name FROM raw.users",
            "explicit2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 3/5 SQL files (60%)" in out
        # None of the three stars can be resolved: they select from raw relations,
        # which are not models and have no compiled SQL to expand from.
        assert "Columns readable: 2/5 compiled SQL file(s)" in out
        assert "unresolved: 3 -- these report review required" in out
        assert "manifest columns documented for 2 of them" in out
        assert "no fallback for 1 (add column docs to resolve)" in out


class TestDdlRuleCount:
    """Scenario 4: which models dbt-plan has a rule for, rather than a readiness score.

    The old "Coverage: N/N models fully analyzed" counted materializations. It
    excluded `append_new_columns` and `ignore`, which dbt-plan analyses exactly,
    and it could print `SELECT * usage: 1/5` and `Coverage: 5/5 fully analyzed`
    one line apart.
    """

    def test_only_materializations_with_no_rule_are_excluded(self, tmp_path, capsys):
        """Derived from predict_ddl itself, so the count cannot drift from the rules."""
        nodes = {}
        # 3 tables
        for i in range(3):
            nid, node = _model_node(f"tbl_{i}", materialization="table")
            nodes[nid] = node
        # 2 views
        for i in range(2):
            nid, node = _model_node(f"vw_{i}", materialization="view")
            nodes[nid] = node
        # 1 ephemeral
        nid, node = _model_node("eph_0", materialization="ephemeral")
        nodes[nid] = node
        # 2 incremental sync_all_columns (monitorable)
        nid, node = _model_node(
            "inc_sync_0", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_sync_1", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        # 1 incremental fail (monitorable)
        nid, node = _model_node(
            "inc_fail_0", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        # 1 incremental ignore (NOT monitorable)
        nid, node = _model_node(
            "inc_ign_0", materialization="incremental", on_schema_change="ignore"
        )
        nodes[nid] = node
        # 1 snapshot (NOT covered)
        nid, node = _model_node("snap_0", materialization="snapshot")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # Every model here has an exact rule except the snapshot -- including the
        # append_new_columns and ignore ones, which the old count left out.
        assert "DDL rules: 10/11 model(s)" in out
        assert "no rule, always review required:" in out
        assert "snapshot" in out


class TestEdgeCases:
    """Scenario 5: Edge cases."""

    def test_empty_manifest(self, tmp_path, capsys):
        """Empty manifest (0 models) should handle gracefully."""
        manifest = _make_manifest({})
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "0 model(s) in manifest" in out
        assert "DDL rules: 0/0 model(s)" in out

    def test_only_test_source_nodes(self, tmp_path, capsys):
        """Manifest with only test/source nodes — 0 models."""
        manifest_data = {
            "nodes": {
                "test.proj.not_null_id": {
                    "name": "not_null_id",
                    "config": {"materialized": "test"},
                },
                "source.proj.raw_users": {
                    "name": "raw_users",
                    "config": {},
                },
                "seed.proj.countries": {
                    "name": "countries",
                    "config": {},
                },
            },
            "child_map": {},
            "metadata": {},
        }
        _write_manifest(tmp_path, manifest_data)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "0 model(s) in manifest" in out

    def test_all_incremental_ignore(self, tmp_path, capsys):
        """All models are incremental+ignore -> cascade risk = 0."""
        nodes = {}
        for i in range(3):
            nid, node = _model_node(
                f"inc_{i}", materialization="incremental", on_schema_change="ignore"
            )
            nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # No "Cascade risk:" line should appear
        assert "Cascade risk:" not in out

    def test_on_schema_change_null_defaults_to_ignore(self, tmp_path, capsys):
        """on_schema_change: null should default to 'ignore'."""
        nodes = {}
        nid, node = _model_node("inc_null", materialization="incremental", on_schema_change=None)
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # Should show "ignore" in the incremental osc breakdown
        assert "ignore" in out
        # No cascade risk
        assert "Cascade risk:" not in out

    def test_materialized_null_defaults_to_table(self, tmp_path, capsys):
        """materialized: null should default to 'table'."""
        manifest_data = {
            "nodes": {
                "model.proj.m": {
                    "name": "m",
                    "config": {"materialized": None},
                },
            },
            "child_map": {},
            "metadata": {},
        }
        _write_manifest(tmp_path, manifest_data)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "table" in out


class TestStatsWithDialect:
    """Scenario 6: Stats with --dialect flag."""

    def test_bigquery_dialect_select_star_detection(self, tmp_path, capsys):
        """SELECT * detection works with bigquery dialect."""
        nodes = {}
        nid, node = _model_node("bq_star", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("bq_explicit", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "bq_star": "SELECT * FROM `project.dataset.table`",
            "bq_explicit": "SELECT id, name FROM `project.dataset.table`",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(
            str(tmp_path), manifest=str(tmp_path / "manifest.json"), dialect="bigquery"
        )
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/2 SQL files (50%)" in out

    def test_snowflake_dialect_select_star_detection(self, tmp_path, capsys):
        """SELECT * detection works with default snowflake dialect."""
        nodes = {}
        nid, node = _model_node("sf_star", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("sf_explicit", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "sf_star": 'SELECT * FROM "RAW"."SCHEMA"."TABLE"',
            "sf_explicit": 'SELECT ID, NAME FROM "RAW"."SCHEMA"."TABLE"',
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/2 SQL files (50%)" in out

    def test_dialect_none_defaults_to_snowflake(self, tmp_path, capsys):
        """dialect=None should default to snowflake."""
        nodes = {}
        nid, node = _model_node("model_a", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "model_a": "SELECT * FROM raw.users",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"), dialect=None)
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/1 SQL files (100%)" in out


class TestStatsWithCorruptManifest:
    """Scenario 7: Stats with corrupt manifest."""

    def test_invalid_json_exits_3(self, tmp_path, capsys):
        """manifest.json with invalid JSON should exit 3 with error message."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{invalid json!!!")

        args = _make_args(str(tmp_path), manifest=str(manifest_path))
        with pytest.raises(SystemExit) as exc:
            _do_stats(args)

        assert exc.value.code == 3
        err = capsys.readouterr().err
        assert "Could not parse manifest.json" in err

    def test_missing_manifest_exits_3(self, tmp_path, capsys):
        """Non-existent manifest should exit 3."""
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "nonexistent_manifest.json"))
        with pytest.raises(SystemExit) as exc:
            _do_stats(args)

        assert exc.value.code == 3
        err = capsys.readouterr().err
        assert "manifest.json not found" in err


class TestStatsWithManifestFlag:
    """Scenario 8: Stats with --manifest flag."""

    def test_custom_manifest_path(self, tmp_path, capsys):
        """Custom manifest path should be used."""
        # Create manifest in a non-default location
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        nodes = {}
        nid, node = _model_node("custom_model", materialization="view")
        nodes[nid] = node
        manifest = _make_manifest(nodes)
        custom_manifest = custom_dir / "my_manifest.json"
        custom_manifest.write_text(json.dumps(manifest))

        args = _make_args(str(tmp_path), manifest=str(custom_manifest))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "view" in out

    def test_default_manifest_path_from_target(self, tmp_path, capsys):
        """Without --manifest, stats reads from target/manifest.json."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        nodes = {}
        nid, node = _model_node("default_model", materialization="table")
        nodes[nid] = node
        manifest = _make_manifest(nodes)
        (target_dir / "manifest.json").write_text(json.dumps(manifest))

        args = _make_args(str(tmp_path))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out


class TestStatsNoCompiledDir:
    """No compiled directory -> SELECT * section should be skipped."""

    def test_no_compiled_dir_skips_star_count(self, tmp_path, capsys):
        """When compiled SQL directory does not exist, SELECT * section is absent."""
        nodes = {}
        nid, node = _model_node("model_a", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        # Don't create target/compiled
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "SELECT * usage" not in out


class TestStatsSelectStarZero:
    """All models have explicit columns — star count should be 0."""

    def test_zero_star_usage(self, tmp_path, capsys):
        """0 SELECT * models should display 0% usage and no fallback info."""
        nodes = {}
        nid, node = _model_node("m1", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("m2", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "m1": "SELECT id, name FROM raw.users",
            "m2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 0/2 SQL files (0%)" in out
        assert "Columns readable: 2/2 compiled SQL file(s)" in out
        # Nothing unresolved, so no advice about how to resolve it.
        assert "unresolved" not in out
        assert "add column docs" not in out
