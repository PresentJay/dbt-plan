"""A model that loses a column without its own file changing.

    stg_orders:  SELECT order_id, customer_id, status  ->  SELECT order_id, status
    fct_orders:  SELECT * FROM {{ ref('stg_orders') }}  ->  byte-identical

`fct_orders` loses `customer_id` too, and on incremental + sync_all_columns dbt
issues a DROP COLUMN against a table that has data in it. Measured on duckdb:

    alter table "dev"."main"."fct_orders" drop column

dbt-plan 0.11.2 reported `SAFE  stg_orders (view, ignore)` and exited 0, because
the diff only carries models whose own file changed and the broken_ref check
looks for a column by name in SQL that never names it.
"""

from __future__ import annotations

import pytest

from dbt_plan.manifest import ModelNode
from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl


def _node(name, materialization="incremental", on_schema_change="sync_all_columns"):
    return ModelNode(
        node_id=f"model.p.{name}",
        name=name,
        materialization=materialization,
        on_schema_change=on_schema_change,
    )


def _resolver(columns: dict[str, list[str] | None]):
    return lambda name: columns.get(name)


class TestInheritedColumnLoss:
    """`stg_orders` is a view, so its own DDL is CREATE OR REPLACE and safe."""

    def _run(self, ds_node, base, current, *, changed=("stg_orders",)):
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert pred.safety == Safety.SAFE

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={name: f"model.p.{name}" for name in changed},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": [ds_node.node_id]},
            node_index={ds_node.name: ds_node},
            base_node_index={},
            compiled_sql_index={},
            base_columns_of=_resolver({ds_node.name: base}),
            current_columns_of=_resolver({ds_node.name: current}),
        )
        return updated[0]

    def test_sync_all_columns_downstream_is_a_drop_and_turns_the_change_destructive(self):
        updated = self._run(_node("fct_orders"), ["order_id", "customer_id"], ["order_id"])
        impact = updated.downstream_impacts[0]
        assert impact.risk == "inherited_drop"
        assert impact.model_name == "fct_orders"
        assert "file unchanged, loses customer_id from upstream" in impact.reason
        assert "DROP COLUMN customer_id" in impact.reason
        assert updated.safety == Safety.DESTRUCTIVE

    def test_append_new_columns_downstream_leaves_the_column_stale(self):
        updated = self._run(
            _node("fct_orders", on_schema_change="append_new_columns"),
            ["order_id", "customer_id"],
            ["order_id"],
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "inherited_change"
        assert "STALE COLUMNS" in impact.reason
        assert updated.safety == Safety.WARNING

    @pytest.mark.parametrize(
        "materialization,on_schema_change",
        [
            ("table", None),
            ("view", None),
            ("incremental", "ignore"),
        ],
    )
    def test_a_downstream_that_rebuilds_itself_is_not_a_finding(
        self, materialization, on_schema_change
    ):
        """Same rule as anywhere else: CREATE OR REPLACE and NO DDL are safe."""
        updated = self._run(
            _node("fct_orders", materialization, on_schema_change),
            ["order_id", "customer_id"],
            ["order_id"],
        )
        assert updated.downstream_impacts == []
        assert updated.safety == Safety.SAFE

    def test_a_downstream_whose_columns_are_unchanged_is_not_a_finding(self):
        """The ordinary case: it names its columns, so upstream cannot move them."""
        updated = self._run(_node("fct_orders"), ["order_id", "status"], ["order_id", "status"])
        assert updated.downstream_impacts == []

    def test_a_downstream_that_cannot_be_resolved_is_reported_rather_than_assumed_clean(self):
        """A `select *` off a source has no compiled SQL to expand from."""
        updated = self._run(_node("fct_orders"), None, None)
        impact = updated.downstream_impacts[0]
        assert impact.risk == "inherited_change"
        assert "cannot be resolved on both sides" in impact.reason
        assert "REVIEW REQUIRED" in impact.reason
        assert updated.safety == Safety.WARNING

    def test_an_unresolvable_downstream_that_rebuilds_itself_stays_quiet(self):
        """predict_ddl answers SAFE for a table whatever its columns do."""
        updated = self._run(_node("fct_orders", "table", None), None, None)
        assert updated.downstream_impacts == []

    def test_a_downstream_that_changed_on_its_own_is_left_to_its_own_verdict(self):
        """It has a row in the report already; a second line would double-count it."""
        updated = self._run(
            _node("fct_orders"),
            ["order_id", "customer_id"],
            ["order_id"],
            changed=("stg_orders", "fct_orders"),
        )
        assert updated.downstream_impacts == []

    def test_without_resolvers_the_check_is_skipped(self):
        """The parameters are optional, so an older caller keeps working."""
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": ["model.p.fct_orders"]},
            node_index={"fct_orders": _node("fct_orders")},
            base_node_index={},
            compiled_sql_index={},
        )
        assert updated[0].downstream_impacts == []

    def test_nothing_is_resolved_when_the_change_drops_no_columns(self):
        """Cost guard: the resolvers walk the DAG, so they are not called for an add."""
        called: list[str] = []

        def spy(name):
            called.append(name)
            return ["order_id"]

        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id"],
            current_columns=["order_id", "shipped_at"],
        )
        analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id"], ["order_id", "shipped_at"])},
            all_downstream={"model.p.stg_orders": ["model.p.fct_orders"]},
            node_index={"fct_orders": _node("fct_orders")},
            base_node_index={},
            compiled_sql_index={},
            base_columns_of=spy,
            current_columns_of=spy,
        )
        assert called == []


class TestTruncatingALongCascade:
    """One `select *` chain can put the whole project downstream of one change."""

    def _impacts(self, n_destructive, n_warning):
        from dbt_plan.predictor import DownstreamImpact

        return [
            DownstreamImpact(f"warn_{i}", "incremental", "fail", "build_failure", "r")
            for i in range(n_warning)
        ] + [
            DownstreamImpact(f"drop_{i}", "incremental", "sync_all_columns", "inherited_drop", "r")
            for i in range(n_destructive)
        ]

    def _result(self, impacts):
        from dataclasses import replace as _replace

        from dbt_plan.formatter import CheckResult

        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=["a"],
        )
        return CheckResult([_replace(pred, downstream_impacts=impacts)])

    def test_a_short_list_is_printed_whole(self):
        from dbt_plan.formatter import format_text

        out = format_text(self._result(self._impacts(2, 1)), color=False)
        assert "and 1 more" not in out
        assert out.count(">> ") == 3

    def test_a_long_list_keeps_the_destructive_ones_and_counts_the_rest(self):
        from dbt_plan.formatter import format_text

        out = format_text(self._result(self._impacts(3, 40)), color=False)
        assert out.count("INHERITED_DROP") == 3, "the cut must never remove the worst"
        assert ">> ... and 33 more -- use --format json for all of them" in out

    def test_json_still_carries_every_one(self):
        import json

        from dbt_plan.formatter import format_json

        out = json.loads(format_json(self._result(self._impacts(3, 40))))
        assert len(out["models"][0]["downstream_impacts"]) == 43
        assert out["summary"]["cascade_risks"] == 43

    def test_github_truncates_too(self):
        from dbt_plan.formatter import format_github

        out = format_github(self._result(self._impacts(3, 40)))
        assert "- ... and 33 more -- use `--format json` for all of them" in out
