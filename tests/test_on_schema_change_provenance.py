"""`on_schema_change` as the author wrote it, not as dbt resolved it.

0.11.0 added a guard so a materialization dbt-plan has no rule for stops reporting
NO DDL:

    if materialization != "incremental" and on_schema_change is None:

It never fired. dbt resolves `on_schema_change` for every model, so a view, a
materialized view and a custom materialization all carry `'ignore'` whether or not
anyone wrote it. Measured on dbt 1.11.7, a materialized view dropping a column:

    SAFE  mv_thing (materialized_view, ignore)
      NO DDL
    exit=0

`unrendered_config` carries only what a human wrote, from the model file or from
`dbt_project.yml`. That is the assertion; the resolved value is not one.
"""

from __future__ import annotations

import pytest

from dbt_plan.manifest import build_node_index
from dbt_plan.predictor import Safety, has_ddl_rule, predict_ddl


def _manifest(materialized, *, resolved_osc="ignore", unrendered=None):
    node = {
        "name": "m",
        "path": "m.sql",
        "config": {"materialized": materialized, "on_schema_change": resolved_osc},
    }
    if unrendered is not None:
        node["unrendered_config"] = unrendered
    return {"metadata": {"project_name": "p"}, "nodes": {"model.p.m": node}}


class TestWhatTheIndexCarries:
    def test_dbts_own_default_is_not_read_as_an_assertion(self):
        index = build_node_index(
            _manifest("materialized_view", unrendered={"materialized": "materialized_view"})
        )
        assert index["m"].on_schema_change is None

    def test_a_value_the_author_wrote_is_kept(self):
        """An explicit setting is a claim about how that materialization behaves."""
        index = build_node_index(
            _manifest(
                "materialized_view",
                resolved_osc="sync_all_columns",
                unrendered={
                    "materialized": "materialized_view",
                    "on_schema_change": "sync_all_columns",
                },
            )
        )
        assert index["m"].on_schema_change == "sync_all_columns"

    def test_an_incremental_default_still_behaves_as_ignore(self):
        """predict_ddl reads `on_schema_change or "ignore"`, which is dbt's real default."""
        index = build_node_index(
            _manifest("incremental", unrendered={"materialized": "incremental"})
        )
        node = index["m"]
        assert node.on_schema_change is None
        assert (
            predict_ddl("m", node.materialization, node.on_schema_change, ["a", "b"], ["a"]).safety
            == Safety.SAFE
        )

    @pytest.mark.parametrize(
        "materialized,expected",
        [("incremental", "sync_all_columns"), ("materialized_view", None), ("table", None)],
    )
    def test_a_manifest_without_unrendered_config_refuses_outside_incremental(
        self, materialized, expected
    ):
        """The resolved value cannot say who set it. Refuse where a rule is missing."""
        index = build_node_index(_manifest(materialized, resolved_osc="sync_all_columns"))
        assert index["m"].on_schema_change == expected


class TestTheGuardNowFires:
    def _verdict(self, materialized, unrendered):
        node = build_node_index(_manifest(materialized, unrendered=unrendered))["m"]
        return predict_ddl(
            model_name="m",
            materialization=node.materialization,
            on_schema_change=node.on_schema_change,
            base_columns=["order_id", "amount"],
            current_columns=["order_id"],
        )

    def test_a_materialized_view_losing_a_column_is_no_longer_safe(self):
        prediction = self._verdict("materialized_view", {"materialized": "materialized_view"})
        assert prediction.safety == Safety.WARNING
        assert prediction.operations[0].operation.startswith(
            "REVIEW REQUIRED (materialized_view is driven by on_configuration_change"
        )
        # The diff is carried anyway: "review required" with nothing attached tells
        # a reviewer nothing about what to look at.
        assert prediction.columns_removed == ["amount"]

    def test_a_custom_materialization_is_named_rather_than_assumed(self):
        prediction = self._verdict("my_custom_thing", {"materialized": "my_custom_thing"})
        assert prediction.safety == Safety.WARNING
        assert prediction.operations[0].operation == "UNKNOWN materialization: my_custom_thing"

    def test_an_author_who_declares_sync_all_columns_is_believed(self):
        prediction = self._verdict(
            "my_custom_thing",
            {"materialized": "my_custom_thing", "on_schema_change": "sync_all_columns"},
        )
        assert prediction.safety == Safety.DESTRUCTIVE
        assert [op.operation for op in prediction.operations] == ["DROP COLUMN"]


class TestHasDdlRule:
    """The count `dbt-plan stats` prints, derived from predict_ddl rather than restated."""

    @pytest.mark.parametrize(
        "materialization,on_schema_change,expected",
        [
            ("table", None, True),
            ("view", None, True),
            ("ephemeral", None, True),
            ("snapshot", None, False),
            ("incremental", "ignore", True),
            ("incremental", "fail", True),
            ("incremental", "append_new_columns", True),
            ("incremental", "sync_all_columns", True),
            ("incremental", None, True),
            ("incremental", "something_dbt_added_later", False),
            ("materialized_view", None, False),
            ("materialized_view", "sync_all_columns", True),
            ("a_custom_materialization", None, False),
        ],
    )
    def test_it_matches_the_rules_table_in_the_readme(
        self, materialization, on_schema_change, expected
    ):
        assert has_ddl_rule(materialization, on_schema_change) is expected
