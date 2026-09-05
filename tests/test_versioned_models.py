"""Versioned models.

    models/fct_orders_v1.sql    node model.p.fct_orders.v1, name "fct_orders"
    models/fct_orders_v2.sql    node model.p.fct_orders.v2, name "fct_orders"

Both versions share the dbt name, and the node_id carries the version where every
other model carries its name. dbt-plan is keyed by the compiled file throughout,
because the diff is a comparison of files, so reading `v2` as the model name meant:

    dbt-plan -- 0 model(s) changed
    WARNING: Skipped 1 model(s) not found in manifest: fct_orders_v2
    WARNING: The compile is incomplete -- 1 model(s) in the manifest have no
             compiled SQL: fct_orders

A `sync_all_columns` DROP COLUMN went entirely unanalysed under those two warnings.
"""

from __future__ import annotations

import pytest

from dbt_plan.manifest import build_node_index, model_key


def _manifest(*nodes):
    return {
        "metadata": {"project_name": "p"},
        "nodes": {node_id: node for node_id, node in nodes},
    }


def _model(name, *, version=None, path=None, materialized="incremental", osc="sync_all_columns"):
    node_id = f"model.p.{name}" + (f".v{version}" if version is not None else "")
    return node_id, {
        "name": name,
        "path": path
        if path is not None
        else f"{name}_v{version}.sql"
        if version
        else f"{name}.sql",
        "version": version,
        "config": {"materialized": materialized, "on_schema_change": osc},
    }


class TestModelKey:
    @pytest.mark.parametrize(
        "node_id,expected",
        [
            ("model.p.fct_orders", "fct_orders"),
            ("model.p.fct_orders.v2", "fct_orders_v2"),
            ("model.p.fct_orders.v10", "fct_orders_v10"),
            # Not a version tail: a model whose name really does end in a dot-segment
            # cannot happen, but a two-segment id must not be mangled.
            ("model.fct_orders", "fct_orders"),
            # `v` alone, or `version` spelled out, is not the pattern dbt uses.
            ("model.p.fct_orders.vnext", "vnext"),
        ],
    )
    def test_it_names_the_file_dbt_wrote(self, node_id, expected):
        assert model_key(node_id) == expected


class TestBuildNodeIndex:
    def test_each_version_gets_its_own_entry(self):
        """They used to collapse: both are named `fct_orders`, first one wins."""
        index = build_node_index(
            _manifest(
                _model("fct_orders", version=1, osc="ignore"),
                _model("fct_orders", version=2, osc="sync_all_columns"),
            )
        )
        assert index["fct_orders_v1"].on_schema_change == "ignore"
        assert index["fct_orders_v2"].on_schema_change == "sync_all_columns"
        assert index["fct_orders_v1"].node_id == "model.p.fct_orders.v1"
        assert index["fct_orders_v2"].version == "2"

    def test_an_unversioned_model_is_unchanged(self):
        index = build_node_index(_manifest(_model("stg_orders", materialized="view", osc=None)))
        assert set(index) == {"stg_orders"}
        assert index["stg_orders"].name == "stg_orders"
        assert index["stg_orders"].version is None

    def test_defined_in_is_registered_under_both_spellings(self):
        """`defined_in:` renames the file; the node_id still says v2."""
        index = build_node_index(
            _manifest(_model("fct_orders", version=2, path="fct_orders_new.sql"))
        )
        assert index["fct_orders_new"].node_id == "model.p.fct_orders.v2"
        assert index["fct_orders_v2"] is index["fct_orders_new"]

    def test_a_manifest_with_no_path_falls_back_to_the_node_id(self):
        node_id, node = _model("fct_orders", version=2)
        del node["path"]
        assert set(build_node_index(_manifest((node_id, node)))) == {"fct_orders_v2"}


class TestCascadeReachesAVersionedDownstream:
    def test_a_downstream_version_is_looked_up_by_its_file(self):
        """`ds_nid.split(".")[-1]` used to hand the cascade the string `v2`."""
        from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl

        node_index = build_node_index(_manifest(_model("fct_orders", version=2, osc="fail")))
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": ["model.p.fct_orders.v2"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )
        assert downstream_map["stg_orders"] == ["fct_orders_v2"]
        impact = updated[0].downstream_impacts[0]
        assert impact.model_name == "fct_orders_v2"
        assert impact.risk == "build_failure"
        assert updated[0].safety == Safety.WARNING


class TestDataTestsOnAVersionedModel:
    def test_the_attached_node_is_read_the_same_way(self):
        from dbt_plan.manifest import build_data_test_index

        index = build_data_test_index(
            {
                "nodes": {
                    "test.p.not_null_fct_orders_v2_customer_id.abc": {
                        "name": "not_null_fct_orders_v2_customer_id",
                        "column_name": "customer_id",
                        "attached_node": "model.p.fct_orders.v2",
                        "test_metadata": {"name": "not_null", "kwargs": {}},
                        "depends_on": {"nodes": ["model.p.fct_orders.v2"]},
                        "config": {"enabled": True},
                    }
                }
            }
        )
        node = index["test.p.not_null_fct_orders_v2_customer_id.abc"]
        assert node.columns_by_model == {"fct_orders_v2": frozenset({"customer_id"})}
        assert node.depends_on_models == ("fct_orders_v2",)
