"""Changes an enforced contract will reject.

A contract is the author stating what a model's shape is, which is exactly the
kind of claim dbt-plan can check against the SQL without running anything. dbt
checks the same thing at build time and refuses to create the relation:

    Compilation Error in model fct_contract (models/fct_contract.sql)
      This model has an enforced contract that failed.
      | column_name | definition_type | contract_type | mismatch_reason       |
      | customer_id |                 | VARCHAR       | missing in definition |

dbt-plan 0.12.0 read no contract information at all and reported
`SAFE  fct_contract (table, ignore)`.
"""

from __future__ import annotations

import argparse

import pytest

from dbt_plan.manifest import ModelNode, build_node_index
from dbt_plan.predictor import DDLOperation, Safety, apply_contract, predict_ddl


def _node(columns=("order_id", "customer_id"), *, enforced=True, materialization="table"):
    return ModelNode(
        node_id="model.p.fct_contract",
        name="fct_contract",
        materialization=materialization,
        on_schema_change=None,
        columns=tuple(columns),
        contract_enforced=enforced,
    )


def _prediction(materialization="table"):
    return predict_ddl(
        model_name="fct_contract",
        materialization=materialization,
        on_schema_change=None,
        base_columns=["order_id", "customer_id"],
        current_columns=["order_id"],
    )


def _operations(prediction):
    return [op.operation for op in prediction.operations]


class TestBuildNodeIndexReadsTheContract:
    def test_enforced_is_picked_up_from_config(self):
        index = build_node_index(
            {
                "metadata": {"project_name": "p"},
                "nodes": {
                    "model.p.fct_contract": {
                        "name": "fct_contract",
                        "config": {"materialized": "table", "contract": {"enforced": True}},
                        "columns": {"order_id": {}, "customer_id": {}},
                    }
                },
            }
        )
        assert index["fct_contract"].contract_enforced is True
        assert index["fct_contract"].columns == ("order_id", "customer_id")

    @pytest.mark.parametrize("contract", [{"enforced": False}, {}, None])
    def test_anything_but_enforced_is_false(self, contract):
        index = build_node_index(
            {
                "metadata": {"project_name": "p"},
                "nodes": {
                    "model.p.m": {
                        "name": "m",
                        "config": {"materialized": "table", "contract": contract},
                    }
                },
            }
        )
        assert index["m"].contract_enforced is False


class TestApplyContract:
    def test_a_column_the_contract_declares_and_the_sql_no_longer_produces(self):
        """`table` is CREATE OR REPLACE and safe. The build still stops."""
        prediction = _prediction()
        assert prediction.safety == Safety.SAFE

        updated = apply_contract(prediction, _node(), ["order_id"])
        assert updated.safety == Safety.WARNING
        assert _operations(updated) == [
            "CONTRACT VIOLATION: customer_id missing in definition",
            "CREATE OR REPLACE TABLE",
        ]

    def test_a_column_the_sql_produces_and_the_contract_does_not_declare(self):
        """The half worth stating: everywhere else in dbt-plan, adding is safe."""
        updated = apply_contract(_prediction(), _node(), ["order_id", "customer_id", "note"])
        assert _operations(updated)[0] == "CONTRACT VIOLATION: note missing in contract"
        assert updated.safety == Safety.WARNING

    def test_both_directions_are_listed_together(self):
        updated = apply_contract(_prediction(), _node(), ["order_id", "note"])
        assert _operations(updated)[:2] == [
            "CONTRACT VIOLATION: customer_id missing in definition",
            "CONTRACT VIOLATION: note missing in contract",
        ]

    def test_a_matching_shape_is_left_exactly_as_it_was(self):
        prediction = _prediction()
        assert apply_contract(prediction, _node(), ["order_id", "customer_id"]) is prediction

    def test_case_is_not_a_violation(self):
        """dbt lowercases neither side; the manifest and sqlglot disagree often enough."""
        prediction = _prediction()
        assert apply_contract(prediction, _node(), ["ORDER_ID", "Customer_Id"]) is prediction

    @pytest.mark.parametrize("columns", [None, ["*"], ["* except(customer_id)"]])
    def test_columns_it_cannot_read_are_review_required(self, columns):
        """A contract makes the refusal more important: the one stated thing is unchecked."""
        updated = apply_contract(_prediction(), _node(), columns)
        assert updated.safety == Safety.WARNING
        assert _operations(updated)[0].startswith("REVIEW REQUIRED (contract enforced")

    def test_an_unenforced_contract_changes_nothing(self):
        prediction = _prediction()
        assert apply_contract(prediction, _node(enforced=False), ["order_id"]) is prediction

    def test_enforcement_with_nothing_declared_is_dbts_error_to_report(self):
        prediction = _prediction()
        assert apply_contract(prediction, _node(columns=()), ["order_id"]) is prediction

    def test_ephemeral_has_no_relation_to_enforce_against(self):
        prediction = _prediction("ephemeral")
        assert apply_contract(prediction, _node(materialization="ephemeral"), ["order_id"]) is (
            prediction
        )

    def test_it_never_downgrades_a_destructive_verdict(self):
        prediction = predict_ddl(
            model_name="fct_contract",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert prediction.safety == Safety.DESTRUCTIVE
        updated = apply_contract(prediction, _node(materialization="incremental"), ["order_id"])
        assert updated.safety == Safety.DESTRUCTIVE
        assert _operations(updated)[0] == "CONTRACT VIOLATION: customer_id missing in definition"

    def test_the_original_operations_are_kept_underneath(self):
        updated = apply_contract(_prediction(), _node(), ["order_id"])
        assert DDLOperation("CREATE OR REPLACE TABLE") in updated.operations


class TestTheManifestColumnsAreTrustedUnderAContract:
    """#21 stopped a verdict built from manifest columns being reported SAFE.

    Its reasoning was that `schema.yml` conventionally documents only the columns
    you test, so the same partial list gets substituted on both sides and the diff
    comes out zero whether or not the SQL changed. An enforced contract is exactly
    the case where that does not hold: dbt requires every column to be declared and
    fails the build otherwise, so the list is the shape dbt checks the SQL against.
    """

    def _check(self, tmp_path, *, enforced):
        import json

        from dbt_plan.cli import _do_check

        project = tmp_path / "project"
        base = project / ".dbt-plan" / "base" / "compiled"
        current = project / "target" / "compiled" / "p" / "models"
        base.mkdir(parents=True)
        current.mkdir(parents=True)
        # `select *` off a relation with no compiled SQL: the manifest columns are
        # the only thing that can answer, on both sides.
        (base / "fct_orders.sql").write_text("SELECT * FROM raw.orders", encoding="utf-8")
        (current / "fct_orders.sql").write_text(
            "SELECT * FROM raw.orders WHERE 1 = 1", encoding="utf-8"
        )
        manifest = {
            "metadata": {"project_name": "p"},
            "nodes": {
                "model.p.fct_orders": {
                    "name": "fct_orders",
                    "path": "fct_orders.sql",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                        "contract": {"enforced": enforced},
                    },
                    "unrendered_config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "columns": {"order_id": {"data_type": "integer"}},
                }
            },
            "child_map": {},
        }
        (project / "target" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (project / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        args = argparse.Namespace(
            project_dir=str(project),
            target_dir="target",
            base_dir=".dbt-plan/base",
            manifest=None,
            format="text",
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            acknowledge=None,
        )
        return _do_check(args)

    def test_without_a_contract_the_clean_bill_is_still_escalated(self, tmp_path, capsys):
        assert self._check(tmp_path, enforced=False) == 2
        assert "REVIEW REQUIRED (columns came from the manifest, not the SQL)" in (
            capsys.readouterr().out
        )

    def test_with_one_the_declared_columns_are_the_answer(self, tmp_path, capsys):
        assert self._check(tmp_path, enforced=True) == 0
        out = capsys.readouterr().out
        assert "columns came from the manifest" not in out
        assert "SAFE" in out
