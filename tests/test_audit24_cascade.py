"""Regressions for audit issues 143, 157, 167 and 169."""

import pytest

from dbt_plan.manifest import (
    ExposureNode,
    build_data_test_index,
    build_node_index,
    build_unit_test_index,
)
from dbt_plan.predictor import (
    Safety,
    analyze_cascade_impacts,
    attach_downstream_exposures,
    predict_ddl,
)


def prediction(name="orders", materialization="view"):
    return predict_ddl(name, materialization, None, ["id", "status"], ["id"])


def cascade(manifest, *, name="orders", nid="model.p.orders", downstream=(), **kwargs):
    return analyze_cascade_impacts(
        [prediction(name)],
        {name: nid},
        {name: (["id", "status"], ["id"])},
        {nid: list(downstream)},
        build_node_index(manifest),
        {},
        {},
        child_map=manifest.get("child_map", {}),
        data_test_index=build_data_test_index(manifest),
        unit_test_index=build_unit_test_index(manifest),
        **kwargs,
    )[0][0]


@pytest.mark.parametrize("condition", ["where", "row_condition", "expression"])
@pytest.mark.parametrize("sql_available", [True, False])
def test_generic_test_extra_sql_columns_are_checked(tmp_path, condition, sql_available):
    test = {
        "name": "not_null_orders_id",
        "column_name": "id",
        "attached_node": "model.p.orders",
        "depends_on": {"nodes": ["model.p.orders"]},
        "config": {"where": "status = 'active'"} if condition == "where" else {},
        "test_metadata": {"name": "not_null", "kwargs": {condition: "status = 'active'"}},
    }
    manifest = {"nodes": {"test.p.t": test}, "child_map": {"model.p.orders": ["test.p.t"]}}
    path = tmp_path / "test.sql"
    path.write_text("select id from orders where status = 'active'")
    result = cascade(manifest, test_sql_index={test["name"]: path} if sql_available else {})
    assert result.safety == Safety.WARNING
    assert result.downstream_impacts[0].risk == (
        "data_test_failure" if sql_available else "data_test_unreadable"
    )


@pytest.mark.parametrize(
    "ref",
    [
        "ref('orders', v=2)",
        "ref('p', 'orders', version=2)",
        "ref('orders', v='2')",
        "ref('orders')",
    ],
)
@pytest.mark.parametrize("path", ["orders_v2.sql", "orders_latest.sql"])
def test_unit_fixture_resolves_version_and_compiled_name(ref, path):
    name = path.removesuffix(".sql")
    manifest = {
        "nodes": {
            "model.p.orders.v2": {
                "name": "orders",
                "version": 2,
                "path": path,
                "config": {"materialized": "view"},
            }
        },
        "unit_tests": {
            "unit_test.p.fct.t": {
                "name": "t",
                "model": "fct",
                "depends_on": {"nodes": ["model.p.orders.v2", "model.p.fct"]},
                "expect": {"rows": [{"id": 1}]},
                "given": [{"input": ref, "rows": [{"id": 1, "status": "active"}]}],
            }
        },
        "child_map": {"model.p.orders.v2": ["unit_test.p.fct.t"]},
    }
    result = cascade(manifest, name=name, nid="model.p.orders.v2")
    assert result.safety == Safety.WARNING
    assert result.downstream_impacts[0].risk == "unit_test_failure"


def test_ephemeral_inherited_loss_reaches_its_data_tests():
    manifest = {
        "nodes": {
            "model.p.mid": {"name": "mid", "config": {"materialized": "ephemeral"}},
            "test.p.t": {
                "name": "t",
                "column_name": "status",
                "attached_node": "model.p.mid",
                "depends_on": {"nodes": ["model.p.mid"]},
            },
        },
        "child_map": {"model.p.orders": ["model.p.mid"], "model.p.mid": ["test.p.t"]},
    }
    result = cascade(
        manifest,
        downstream=["model.p.mid"],
        base_columns_of=lambda _: ["id", "status"],
        current_columns_of=lambda _: ["id"],
    )
    assert result.safety == Safety.WARNING
    assert [(i.model_name, i.risk) for i in result.downstream_impacts] == [
        ("t", "data_test_failure")
    ]


@pytest.mark.parametrize("materialization", ["table", "view"])
@pytest.mark.parametrize(
    "current,expected",
    [(["id"], True), (["id", "status", "amount"], False), (["id", "status"], False)],
)
def test_safe_replacement_names_exposure_only_on_column_loss(materialization, current, expected):
    result = attach_downstream_exposures(
        [prediction(materialization=materialization)],
        {"orders": "model.p.orders"},
        {},
        {"model.p.orders": ["exposure.p.dashboard"]},
        {"exposure.p.dashboard": ExposureNode("exposure.p.dashboard", "dashboard", "dashboard")},
        model_cols={"orders": (["id", "status"], current)},
    )[0]
    assert result.safety == Safety.SAFE
    assert bool(result.downstream_exposures) is expected


def test_sync_all_columns_ignores_projection_order():
    result = predict_ddl(
        "orders", "incremental", "sync_all_columns", ["id", "status"], ["status", "id"]
    )
    assert result.safety == Safety.SAFE
    assert not result.operations


def test_manifest_preserves_source_provenance_without_dependency_files(tmp_path):
    import json

    from dbt_plan.manifest import load_manifest

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"project_name": "p"},
                "nodes": {
                    "model.p.orders": {
                        "original_file_path": "models/orders.sql",
                        "raw_code": "select 1 as id",
                    },
                    "model.p.missing": {"original_file_path": "models/missing.sql"},
                    "test.p.generic": {"original_file_path": "models/schema.yml", "raw_code": ""},
                    "model.dep.orders": {
                        "original_file_path": "models/dep.sql",
                        "raw_code": "select 2",
                    },
                },
                "macros": {
                    "macro.p.a": {
                        "original_file_path": "macros/helpers.sql",
                        "macro_sql": "{% macro a() %}1{% endmacro %}",
                    },
                    "macro.p.b": {
                        "original_file_path": "macros/helpers.sql",
                        "macro_sql": "{% macro b() %}2{% endmacro %}",
                    },
                    "macro.dep.c": {"original_file_path": "macros/dep.sql", "macro_sql": "other"},
                },
            }
        )
    )
    loaded = load_manifest(path)
    assert loaded["source_files"] == {"models/orders.sql": "select 1 as id"}
    assert loaded["source_macros"] == {
        "macros/helpers.sql": ["{% macro a() %}1{% endmacro %}", "{% macro b() %}2{% endmacro %}"]
    }


def test_extra_predicate_without_dropped_reference_stays_safe(tmp_path):
    manifest = {
        "nodes": {
            "test.p.t": {
                "name": "t",
                "column_name": "id",
                "attached_node": "model.p.orders",
                "depends_on": {"nodes": ["model.p.orders"]},
                "config": {"where": "id > 0"},
            }
        },
        "child_map": {"model.p.orders": ["test.p.t"]},
    }
    path = tmp_path / "t.sql"
    path.write_text("select id from orders where id > 0")
    assert cascade(manifest, test_sql_index={"t": path}).safety == Safety.SAFE


def test_ephemeral_inherited_loss_reaches_precise_downstream_reader():
    manifest = {
        "nodes": {
            "model.p.mid": {"name": "mid", "config": {"materialized": "ephemeral"}},
            "model.p.fct": {"name": "fct", "config": {"materialized": "table"}},
        }
    }
    result = cascade(
        manifest,
        downstream=["model.p.mid", "model.p.fct"],
        base_columns_of=lambda _: ["id", "status"],
        current_columns_of=lambda name: ["id"] if name == "mid" else ["id", "status"],
        columns_read_of=lambda name, parent: (
            ["status"] if (name, parent) == ("fct", "mid") else []
        ),
    )
    assert result.safety == Safety.DESTRUCTIVE
    assert [(i.model_name, i.risk) for i in result.downstream_impacts] == [("fct", "broken_ref")]


def test_unversioned_fixture_uses_latest_when_two_versions_are_dependencies():
    manifest = {
        "nodes": {
            f"model.p.orders.v{v}": {
                "name": "orders",
                "version": v,
                "latest_version": 2,
                "path": f"orders_v{v}.sql",
            }
            for v in (1, 2)
        },
        "unit_tests": {
            "unit_test.p.fct.t": {
                "model": "fct",
                "depends_on": {"nodes": ["model.p.orders.v1", "model.p.orders.v2"]},
                "given": [{"input": "ref('orders')", "rows": [{"status": "active"}]}],
            }
        },
    }
    fixture = build_unit_test_index(manifest)["unit_test.p.fct.t"].fixtures[1]
    assert fixture.model == "orders_v2"
