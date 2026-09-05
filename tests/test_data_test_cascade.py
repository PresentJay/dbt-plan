"""dbt data tests that read a column being removed.

A `not_null` on a dropped column is not a failing assertion, it is a broken
query. dbt 1.11.7 on duckdb:

    Failure in test not_null_stg_orders_customer_id (models/schema.yml)
      Binder Error: Referenced column "customer_id" not found in FROM clause!
      Candidate bindings: "order_id"

dbt-plan 0.12.0 reported `SAFE  stg_orders (view, ignore)` and exited 0. Generic
tests are in nearly every dbt project, which makes this the most common of the
gaps in this class -- unit tests (#43) are rare by comparison.
"""

from __future__ import annotations

import pytest

from dbt_plan.manifest import build_data_test_index
from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl


def _generic(name, model, column, *, test_name="not_null", kwargs=None, enabled=True):
    return {
        "name": name,
        "column_name": column,
        "attached_node": f"model.p.{model}",
        "test_metadata": {"name": test_name, "kwargs": kwargs or {"column_name": column}},
        "depends_on": {"nodes": [f"model.p.{model}"]},
        "config": {"enabled": enabled},
    }


def _singular(name, *models):
    return {
        "name": name,
        "column_name": None,
        "attached_node": None,
        "test_metadata": None,
        "depends_on": {"nodes": [f"model.p.{m}" for m in models]},
        "config": {"enabled": True},
    }


def _manifest(**tests):
    return {"nodes": {f"test.p.{k}": v for k, v in tests.items()}}


class TestBuildDataTestIndex:
    def test_a_generic_test_names_its_column_in_the_manifest(self):
        index = build_data_test_index(_manifest(t=_generic("t", "stg_orders", "customer_id")))
        node = index["test.p.t"]
        assert node.columns_by_model == {"stg_orders": frozenset({"customer_id"})}
        assert node.depends_on_models == ("stg_orders",)

    def test_relationships_also_names_the_far_side(self):
        """The only built-in test that reads a second model, and only kwargs says so."""
        index = build_data_test_index(
            _manifest(
                t=_generic(
                    "t",
                    "stg_orders",
                    "customer_id",
                    test_name="relationships",
                    kwargs={
                        "to": "ref('dim_customers')",
                        "field": "customer_id",
                        "column_name": "customer_id",
                    },
                )
            )
        )
        assert index["test.p.t"].columns_by_model == {
            "stg_orders": frozenset({"customer_id"}),
            "dim_customers": frozenset({"customer_id"}),
        }

    def test_a_singular_test_names_nothing_but_still_records_what_it_reads(self):
        index = build_data_test_index(_manifest(t=_singular("t", "dim_customers")))
        node = index["test.p.t"]
        assert node.columns_by_model == {}
        assert node.depends_on_models == ("dim_customers",)

    def test_disabled_tests_are_left_out(self):
        index = build_data_test_index(
            _manifest(t=_generic("t", "stg_orders", "customer_id", enabled=False))
        )
        assert index == {}

    def test_models_are_not_mistaken_for_tests(self):
        assert (
            build_data_test_index({"nodes": {"model.p.stg_orders": {"name": "stg_orders"}}}) == {}
        )


class TestCascade:
    def _cascade(self, *, tests, child_map, test_sql_index=None, columns=None, downstream=()):
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert pred.safety == Safety.SAFE  # a view is CREATE OR REPLACE; the build is not
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": list(downstream)},
            node_index=columns or {},
            base_node_index={},
            compiled_sql_index={},
            child_map=child_map,
            data_test_index=build_data_test_index(_manifest(**tests)),
            test_sql_index=test_sql_index or {},
        )
        return updated[0]

    def test_a_generic_test_on_the_dropped_column_turns_a_safe_view_into_a_warning(self):
        updated = self._cascade(
            tests={"t": _generic("not_null_stg_orders_customer_id", "stg_orders", "customer_id")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "data_test_failure"
        assert impact.model_name == "not_null_stg_orders_customer_id"
        assert impact.reason == "tests dropped column(s): customer_id"
        assert updated.safety == Safety.WARNING

    def test_a_generic_test_on_a_surviving_column_is_not_a_finding(self):
        updated = self._cascade(
            tests={"t": _generic("not_null_stg_orders_order_id", "stg_orders", "order_id")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
        )
        assert updated.downstream_impacts == []
        assert updated.safety == Safety.SAFE

    def test_a_relationships_test_pointing_at_the_dropped_column_is_caught(self):
        """It is attached to another model, and reaches this one only through kwargs."""
        updated = self._cascade(
            tests={
                "t": _generic(
                    "relationships_fct_orders_customer_id",
                    "fct_orders",
                    "customer_id",
                    test_name="relationships",
                    kwargs={"to": "ref('stg_orders')", "field": "customer_id"},
                )
            },
            child_map={"model.p.stg_orders": ["test.p.t"]},
        )
        assert updated.downstream_impacts[0].risk == "data_test_failure"

    def test_a_singular_test_is_judged_from_its_compiled_sql(self, tmp_path):
        sql = tmp_path / "no_order_without_a_customer.sql"
        sql.write_text(
            "SELECT order_id FROM db.sch.stg_orders WHERE customer_id IS NULL", encoding="utf-8"
        )
        updated = self._cascade(
            tests={"t": _singular("no_order_without_a_customer", "stg_orders")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
            test_sql_index={"no_order_without_a_customer": sql},
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "data_test_failure"
        assert impact.reason == "its SQL names dropped column(s): customer_id"

    def test_a_singular_test_that_does_not_name_the_column_is_quiet(self, tmp_path):
        sql = tmp_path / "t.sql"
        sql.write_text(
            "SELECT order_id FROM db.sch.stg_orders WHERE order_id < 0", encoding="utf-8"
        )
        updated = self._cascade(
            tests={"t": _singular("t", "stg_orders")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
            test_sql_index={"t": sql},
        )
        assert updated.downstream_impacts == []

    def test_a_test_whose_sql_is_missing_is_reported_rather_than_assumed_clean(self):
        """Its compiled SQL is the only thing that could answer, and it is not there."""
        updated = self._cascade(
            tests={"t": _singular("t", "stg_orders")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
            test_sql_index={},
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "data_test_unreadable"
        assert "compiled SQL was not found" in impact.reason
        assert updated.safety == Safety.WARNING

    def test_a_test_reading_only_some_other_model_is_left_alone(self):
        updated = self._cascade(
            tests={"t": _singular("t", "dim_customers")},
            child_map={"model.p.stg_orders": ["test.p.t"]},
            test_sql_index={},
        )
        assert updated.downstream_impacts == []

    def test_without_a_data_test_index_nothing_changes(self):
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
            all_downstream={"model.p.stg_orders": []},
            node_index={},
            base_node_index={},
            compiled_sql_index={},
        )
        assert updated[0].downstream_impacts == []


class TestATestOnAModelThatInheritsTheLoss:
    """The composition of this with the `SELECT *` case, which neither covers alone.

        stg_orders:  SELECT order_id, customer_id  ->  SELECT order_id
        fct_orders:  SELECT * FROM ref('stg_orders'), materialized table
        not_null on fct_orders.customer_id

    `fct_orders` is a table, so rebuilding it with one column fewer is safe and the
    inherited-loss check says nothing. Its test still cannot bind. Measured: dbt
    reported `ERROR not_null_fct_orders_customer_id` while dbt-plan exited 0.
    """

    def test_the_test_on_the_downstream_model_is_reported(self):
        from dbt_plan.manifest import ModelNode

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
            node_index={
                "fct_orders": ModelNode(
                    node_id="model.p.fct_orders",
                    name="fct_orders",
                    materialization="table",
                    on_schema_change=None,
                )
            },
            base_node_index={},
            compiled_sql_index={},
            child_map={
                "model.p.stg_orders": ["model.p.fct_orders"],
                "model.p.fct_orders": ["test.p.t"],
            },
            data_test_index=build_data_test_index(
                _manifest(
                    t=_generic("not_null_fct_orders_customer_id", "fct_orders", "customer_id")
                )
            ),
            base_columns_of={"fct_orders": ["order_id", "customer_id"]}.get,
            current_columns_of={"fct_orders": ["order_id"]}.get,
        )
        risks = {(i.risk, i.model_name) for i in updated[0].downstream_impacts}
        assert ("data_test_failure", "not_null_fct_orders_customer_id") in risks
        # The table itself is fine -- CREATE OR REPLACE. Only its test is not.
        assert not any(r.startswith("inherited") for r, _ in risks)
        assert updated[0].safety == Safety.WARNING


class TestIterNonModelSql:
    def test_it_is_the_complement_of_iter_model_sql(self, tmp_path):
        from dbt_plan.diff import iter_model_sql, iter_non_model_sql

        for rel in (
            "models/staging/stg_orders.sql",
            "models/schema.yml/models/not_null_stg_orders_customer_id.sql",
            "tests/singular_customer_tier.sql",
        ):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SELECT 1", encoding="utf-8")

        assert [p.name for p in iter_model_sql(tmp_path / "models")] == ["stg_orders.sql"]
        assert sorted(p.name for p in iter_non_model_sql(tmp_path, "models")) == [
            "not_null_stg_orders_customer_id.sql",
            "singular_customer_tier.sql",
        ]

    @pytest.mark.parametrize("models_dir", ["models", "transformations"])
    def test_it_honours_a_renamed_model_path(self, tmp_path, models_dir):
        from dbt_plan.diff import iter_non_model_sql

        path = tmp_path / models_dir / "stg_orders.sql"
        path.parent.mkdir(parents=True)
        path.write_text("SELECT 1", encoding="utf-8")
        assert list(iter_non_model_sql(tmp_path, models_dir)) == []
