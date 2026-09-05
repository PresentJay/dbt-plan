"""Tests for predict_ddl — DDL prediction rules."""

import pytest

from dbt_plan.predictor import Safety, predict_ddl


class TestTableAndView:
    def test_table_always_safe(self):
        """table materialization → CREATE OR REPLACE TABLE, always safe."""
        result = predict_ddl(
            model_name="dim_customers",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert result.safety == Safety.SAFE
        assert any(op.operation == "CREATE OR REPLACE TABLE" for op in result.operations)

    def test_view_always_safe(self):
        """view materialization → CREATE OR REPLACE VIEW, always safe."""
        result = predict_ddl(
            model_name="vw_report",
            materialization="view",
            on_schema_change=None,
            base_columns=["x"],
            current_columns=["x", "y"],
        )
        assert result.safety == Safety.SAFE
        assert any(op.operation == "CREATE OR REPLACE VIEW" for op in result.operations)


class TestIncrementalIgnoreAndFail:
    def test_incremental_ignore_safe(self):
        """incremental + ignore → NO DDL, safe."""
        result = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert result.safety == Safety.SAFE
        assert any(op.operation == "NO DDL" for op in result.operations)

    def test_incremental_fail_same_columns_safe(self):
        """incremental + fail + same columns → SAFE (no schema change)."""
        result = predict_ddl(
            model_name="fct_strict",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.SAFE

    def test_incremental_fail_warning(self):
        """incremental + fail + columns changed → WARNING, BUILD FAILURE."""
        result = predict_ddl(
            model_name="fct_strict",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert result.safety == Safety.WARNING
        assert any(op.operation == "BUILD FAILURE" for op in result.operations)


class TestIncrementalAppendAndSync:
    def test_append_add_only(self):
        """incremental + append_new_columns → ADD COLUMN only, safe."""
        result = predict_ddl(
            model_name="fct_metrics",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )
        assert result.safety == Safety.SAFE
        assert result.columns_added == ["c"]
        assert result.columns_removed == []
        add_ops = [op for op in result.operations if op.operation == "ADD COLUMN"]
        assert len(add_ops) == 1
        assert add_ops[0].column == "c"

    def test_sync_destructive_when_columns_removed(self):
        """incremental + sync_all_columns + columns removed → DESTRUCTIVE."""
        result = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a", "d"],
        )
        assert result.safety == Safety.DESTRUCTIVE
        assert sorted(result.columns_removed) == ["b", "c"]
        assert result.columns_added == ["d"]
        drop_ops = [op for op in result.operations if op.operation == "DROP COLUMN"]
        assert len(drop_ops) == 2

    def test_unknown_on_schema_change_shows_operation(self):
        """Unknown on_schema_change → WARNING with descriptive operation."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="custom_future_value",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert result.safety == Safety.WARNING
        assert any("UNKNOWN" in op.operation for op in result.operations)
        assert any("custom_future_value" in op.operation for op in result.operations)

    def test_duplicate_base_columns_trigger_warning(self):
        """Duplicate column names in base (from JOIN) → REVIEW REQUIRED, never FALSE SAFE."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["id", "id", "name"],
            current_columns=["id", "name"],
        )
        assert result.safety == Safety.WARNING
        assert any("duplicate" in op.operation.lower() for op in result.operations)

    def test_duplicate_current_columns_trigger_warning(self):
        """Duplicate column names in current → REVIEW REQUIRED."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["id", "name"],
            current_columns=["id", "id", "name"],
        )
        assert result.safety == Safety.WARNING

    def test_no_false_safe_with_duplicate_columns(self):
        """Core principle: duplicate columns must never produce FALSE SAFE."""
        # This exact scenario was the bug: set dedup hides the dropped column
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a_id", "b_id", "a_id"],  # duplicate from JOIN
            current_columns=["a_id", "b_id"],
        )
        # Must NOT be SAFE — we can't determine if a column was actually dropped
        assert result.safety != Safety.SAFE

    def test_sync_safe_when_no_changes(self):
        """incremental + sync_all_columns + same columns → SAFE, no ops."""
        result = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.SAFE
        assert result.operations == []
        assert result.columns_added == []
        assert result.columns_removed == []

    def test_sync_reorder_warning(self):
        """incremental + sync_all_columns + columns reordered → WARNING."""
        result = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any("REORDER" in op.operation for op in result.operations)


class TestNewModel:
    def test_new_model_added(self):
        """New model (status=added) → SAFE (no existing table to alter)."""
        result = predict_ddl(
            model_name="new_model",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=None,
            current_columns=["a", "b"],
            status="added",
        )
        assert result.safety == Safety.SAFE


class TestRemovedModel:
    def test_removed_model_destructive(self):
        """Removed model → DESTRUCTIVE regardless of materialization."""
        result = predict_ddl(
            model_name="old_model",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=None,
            status="removed",
        )
        assert result.safety == Safety.DESTRUCTIVE
        assert any(op.operation == "MODEL REMOVED" for op in result.operations)

    def test_removed_incremental_destructive(self):
        """Removed incremental model → DESTRUCTIVE."""
        result = predict_ddl(
            model_name="old_inc",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a"],
            current_columns=None,
            status="removed",
        )
        assert result.safety == Safety.DESTRUCTIVE


class TestParseFailure:
    def test_parse_failure_base_never_safe(self):
        """Modified model with base parse failure → WARNING (never safe)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=None,
            current_columns=["a", "b"],
            status="modified",
        )
        assert result.safety == Safety.WARNING
        assert any("REVIEW" in op.operation for op in result.operations)

    def test_parse_failure_current_never_safe(self):
        """Modified model with current parse failure → WARNING (never safe)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=None,
            status="modified",
        )
        assert result.safety == Safety.WARNING

    def test_parse_failure_both_never_safe(self):
        """Modified model with both parse failures → WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=None,
            current_columns=None,
            status="modified",
        )
        assert result.safety == Safety.WARNING


class TestSelectStarWildcard:
    def test_select_star_base_warning(self):
        """SELECT * in base → WARNING (cannot diff columns)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["*"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any("SELECT *" in op.operation for op in result.operations)

    def test_select_star_current_warning(self):
        """SELECT * in current → WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["*"],
        )
        assert result.safety == Safety.WARNING


class TestStarExceptPredictor:
    """Predictor handles SELECT * EXCEPT sentinel values."""

    def test_star_except_current_warning(self):
        """base=["*"], current=["* except(revenue)"] → WARNING (column likely removed)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["*"],
            current_columns=["* except(revenue)"],
        )
        assert result.safety == Safety.WARNING
        assert any("SELECT *" in op.operation for op in result.operations)

    def test_star_except_base_warning(self):
        """base=["* except(revenue)"], current=explicit → WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["* except(revenue)"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any("EXCEPT" in op.operation for op in result.operations)

    def test_star_except_both_same_exclusions(self):
        """Both sides same EXCEPT → WARNING (same exclusions, can't enumerate)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["* except(revenue)"],
            current_columns=["* except(revenue)"],
        )
        assert result.safety == Safety.WARNING
        assert any("same exclusions" in op.operation for op in result.operations)

    def test_star_except_both_different_exclusions(self):
        """Different EXCEPT sets → WARNING (exclusions changed)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["* except(old_col)"],
            current_columns=["* except(new_col)"],
        )
        assert result.safety == Safety.WARNING
        assert any("exclusions changed" in op.operation for op in result.operations)

    def test_star_except_vs_explicit_warning(self):
        """base=["* except(col)"], current=explicit columns → WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["* except(old_col)"],
            current_columns=["a", "b", "c"],
        )
        assert result.safety == Safety.WARNING
        assert any("column removal likely" in op.operation for op in result.operations)

    def test_star_except_table_safe(self):
        """Table materialization with * except → still SAFE (CREATE OR REPLACE)."""
        result = predict_ddl(
            model_name="m",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["* except(revenue)"],
        )
        assert result.safety == Safety.SAFE

    def test_star_except_ignore_safe(self):
        """Incremental + ignore with * except → SAFE (no DDL)."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b"],
            current_columns=["* except(revenue)"],
        )
        assert result.safety == Safety.SAFE


class TestEphemeralAndUnknown:
    def test_ephemeral_safe(self):
        """Ephemeral model → SAFE, no operations."""
        result = predict_ddl(
            model_name="eph",
            materialization="ephemeral",
            on_schema_change=None,
            base_columns=None,
            current_columns=None,
            status="added",
        )
        assert result.safety == Safety.SAFE
        assert result.operations == []

    def test_unknown_on_schema_change_warning(self):
        """Unknown on_schema_change value → WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="unknown_value",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.WARNING

    def test_removed_ephemeral_safe(self):
        """Removed ephemeral model → SAFE (no physical object in Snowflake)."""
        result = predict_ddl(
            model_name="eph_model",
            materialization="ephemeral",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=None,
            status="removed",
        )
        assert result.safety == Safety.SAFE
        assert not any(op.operation == "MODEL REMOVED" for op in result.operations)

    def test_snapshot_materialization_warning(self):
        """Snapshot materialization → WARNING (schema not auto-managed)."""
        result = predict_ddl(
            model_name="snap_model",
            materialization="snapshot",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert result.safety == Safety.WARNING
        assert any("snapshot" in op.operation.lower() for op in result.operations)


class TestAppendStaleColumns:
    def test_append_with_removed_columns_warns(self):
        """append_new_columns + columns removed from SQL → WARNING about stale data."""
        result = predict_ddl(
            model_name="inc_model",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a", "d"],
        )
        assert result.safety == Safety.WARNING
        assert result.columns_added == ["d"]
        assert sorted(result.columns_removed) == ["b", "c"]
        assert any("STALE" in op.operation for op in result.operations)

    def test_append_add_only_still_safe(self):
        """append_new_columns + only additions → SAFE (no stale columns)."""
        result = predict_ddl(
            model_name="inc_model",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )
        assert result.safety == Safety.SAFE
        assert result.columns_added == ["c"]
        assert result.columns_removed == []


class TestDownstreamImpact:
    def test_downstream_impact_dataclass(self):
        """DownstreamImpact stores risk and reason."""
        from dbt_plan.predictor import DownstreamImpact

        impact = DownstreamImpact(
            model_name="fct_metrics",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream schema changed",
        )
        assert impact.risk == "build_failure"
        assert impact.model_name == "fct_metrics"

    def test_prediction_with_downstream_impacts(self):
        """DDLPrediction can hold downstream_impacts."""
        from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact

        impact = DownstreamImpact(
            model_name="fct_metrics",
            materialization="incremental",
            on_schema_change="fail",
            risk="broken_ref",
            reason="references dropped column(s): old_col",
        )
        pred = DDLPrediction(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            safety=Safety.DESTRUCTIVE,
            operations=[DDLOperation("DROP COLUMN", "old_col")],
            columns_removed=["old_col"],
            downstream_impacts=[impact],
        )
        assert len(pred.downstream_impacts) == 1
        assert pred.downstream_impacts[0].risk == "broken_ref"

    def test_default_empty_downstream_impacts(self):
        """DDLPrediction defaults to empty downstream_impacts."""
        pred = predict_ddl(
            model_name="m",
            materialization="table",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.downstream_impacts == []


class TestAnalyzeCascadeImpacts:
    """Tests for analyze_cascade_impacts extracted function."""

    def _make_node(self, name, materialization="incremental", on_schema_change="ignore"):
        from dbt_plan.manifest import ModelNode

        return ModelNode(
            node_id=f"model.test.{name}",
            name=name,
            materialization=materialization,
            on_schema_change=on_schema_change,
        )

    def test_broken_ref_detected(self, tmp_path):
        """Downstream SQL referencing a dropped column → broken_ref."""
        from dbt_plan.predictor import analyze_cascade_impacts

        # Write downstream SQL that references the dropped column
        ds_sql = tmp_path / "fct_metrics.sql"
        ds_sql.write_text("SELECT user_id, shipping_info FROM {{ ref('int_order_enriched') }}")

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "shipping_info"],
            current_columns=["user_id"],
        )

        node_index = {
            "fct_metrics": self._make_node("fct_metrics", "incremental", "ignore"),
        }

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["user_id", "shipping_info"], ["user_id"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.fct_metrics"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct_metrics": ds_sql},
        )

        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"
        assert updated[0].safety == Safety.DESTRUCTIVE
        assert "int_order_enriched" in downstream_map
        assert "fct_metrics" in downstream_map["int_order_enriched"]

    def test_build_failure_for_incremental_fail(self, tmp_path):
        """Downstream incremental+fail → build_failure on any schema change."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )

        node_index = {
            "fct_daily": self._make_node("fct_daily", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["a", "b"], ["a", "b", "c"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.fct_daily"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert any(imp.risk == "build_failure" for imp in updated[0].downstream_impacts)

    def test_ephemeral_skipped(self):
        """Ephemeral downstream → no cascade impact (CTE, no physical table)."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a"],
        )

        node_index = {
            "eph_safe": self._make_node("eph_safe", "ephemeral"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["a", "b"], ["a"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.eph_safe"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].downstream_impacts == []

    def test_table_view_broken_ref_detected(self, tmp_path):
        """Table/view downstream with broken column ref → detected.

        Even though table/view DDL is safe (CREATE OR REPLACE),
        the SELECT will fail if it references a dropped upstream column.
        """
        from dbt_plan.predictor import analyze_cascade_impacts

        ds_sql = tmp_path / "dim_customers.sql"
        ds_sql.write_text("SELECT user_id, dropped_col FROM {{ ref('parent') }}")

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "dropped_col"],
            current_columns=["user_id"],
        )

        node_index = {
            "dim_customers": self._make_node("dim_customers", "table"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["user_id", "dropped_col"], ["user_id"])},
            all_downstream={"model.test.parent": ["model.test.dim_customers"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"dim_customers": ds_sql},
        )

        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"
        assert updated[0].downstream_impacts[0].model_name == "dim_customers"

    def test_table_view_no_broken_ref_when_safe(self, tmp_path):
        """Table/view downstream without broken ref → no cascade impact."""
        from dbt_plan.predictor import analyze_cascade_impacts

        ds_sql = tmp_path / "dim_customers.sql"
        ds_sql.write_text("SELECT user_id FROM {{ ref('parent') }}")

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "dropped_col"],
            current_columns=["user_id"],
        )

        node_index = {
            "dim_customers": self._make_node("dim_customers", "table"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["user_id", "dropped_col"], ["user_id"])},
            all_downstream={"model.test.parent": ["model.test.dim_customers"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"dim_customers": ds_sql},
        )

        assert updated[0].downstream_impacts == []

    def test_no_cascade_when_no_column_changes(self):
        """Same columns before and after → no cascade."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["a", "b"], ["a", "b"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].downstream_impacts == []

    def test_safety_escalation_safe_to_warning_for_build_failure(self):
        """SAFE prediction with build_failure downstream → WARNING."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="table",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE

        node_index = {
            "fct": self._make_node("fct", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["a"], ["a", "b"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].safety == Safety.WARNING

    def test_build_failure_does_not_downgrade_destructive(self):
        """DESTRUCTIVE prediction stays DESTRUCTIVE even with only build_failure downstream."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="int_order_enriched",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.DESTRUCTIVE

        node_index = {
            "fct": self._make_node("fct", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_order_enriched": "model.test.int_order_enriched"},
            model_cols={"int_order_enriched": (["a", "b"], ["a"])},
            all_downstream={"model.test.int_order_enriched": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].safety == Safety.DESTRUCTIVE

    def test_incremental_fail_also_checks_broken_ref(self, tmp_path):
        """incremental+fail downstream should detect broken_ref too (not just build_failure)."""
        from dbt_plan.predictor import analyze_cascade_impacts

        ds_sql = tmp_path / "fct.sql"
        ds_sql.write_text("SELECT dropped_col FROM {{ ref('parent') }}")

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["dropped_col", "kept_col"],
            current_columns=["kept_col"],
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["dropped_col", "kept_col"], ["kept_col"])},
            all_downstream={"model.test.parent": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": ds_sql},
        )

        risks = [imp.risk for imp in updated[0].downstream_impacts]
        assert "build_failure" in risks
        assert "broken_ref" in risks
        # broken_ref should escalate to DESTRUCTIVE
        assert updated[0].safety == Safety.DESTRUCTIVE

    def test_no_downstream_no_impact(self):
        """Model with no downstream → no cascade analysis."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="leaf",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a"],
        )

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"leaf": "model.test.leaf"},
            model_cols={"leaf": (["a", "b"], ["a"])},
            all_downstream={},
            node_index={},
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].downstream_impacts == []
        assert "leaf" not in downstream_map

    def test_removed_model_cascade(self, tmp_path):
        """Removed model → all base columns treated as removed for cascade."""
        from dbt_plan.predictor import analyze_cascade_impacts

        ds_sql = tmp_path / "fct.sql"
        ds_sql.write_text("SELECT col_a, col_b FROM {{ ref('removed_model') }}")

        pred = predict_ddl(
            model_name="removed_model",
            materialization="table",
            on_schema_change=None,
            base_columns=["col_a", "col_b"],
            current_columns=None,
            status="removed",
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "ignore"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"removed_model": "model.test.removed_model"},
            model_cols={"removed_model": (["col_a", "col_b"], None)},
            all_downstream={"model.test.removed_model": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": ds_sql},
        )

        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"
        assert "col_a" in updated[0].downstream_impacts[0].reason
        assert "col_b" in updated[0].downstream_impacts[0].reason

    def test_incremental_ignore_skips_cascade(self):
        """Incremental+ignore → no cascade (no physical schema change)."""
        from dbt_plan.predictor import analyze_cascade_impacts

        pred = predict_ddl(
            model_name="inc_ignore",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "fail"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"inc_ignore": "model.test.inc_ignore"},
            model_cols={"inc_ignore": (["a", "b"], ["a", "c"])},
            all_downstream={"model.test.inc_ignore": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        assert updated[0].downstream_impacts == []

    def test_unreadable_downstream_sql_skips_broken_ref(self, tmp_path):
        """If downstream SQL file is unreadable, skip broken_ref check (no crash)."""
        from dbt_plan.predictor import analyze_cascade_impacts

        # Create a file path that doesn't exist (simulates deleted file)
        nonexistent = tmp_path / "does_not_exist.sql"

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a"],
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "ignore"),
        }

        # compiled_sql_index points to a nonexistent file
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["a", "b"], ["a"])},
            all_downstream={"model.test.parent": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": nonexistent},
        )

        # Should not crash, and no broken_ref since file was unreadable
        assert updated[0].downstream_impacts == []

    def test_incremental_sync_does_cascade(self, tmp_path):
        """Incremental+sync_all_columns → cascade analysis runs normally."""
        from dbt_plan.predictor import analyze_cascade_impacts

        ds_sql = tmp_path / "fct.sql"
        ds_sql.write_text("SELECT dropped_col FROM {{ ref('parent') }}")

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["dropped_col", "kept_col"],
            current_columns=["kept_col"],
        )

        node_index = {
            "fct": self._make_node("fct", "incremental", "ignore"),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["dropped_col", "kept_col"], ["kept_col"])},
            all_downstream={"model.test.parent": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": ds_sql},
        )

        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"


class TestSnapshotsCarryTheirColumnDiff:
    """The verdict stays review -- schema changes on a snapshot are not auto-managed.

    But the diff was computed and then thrown away, so a reviewer was told "review
    required" without being told what to review, on a change dbt-plan had already
    worked out.
    """

    def test_a_dropped_column_is_named(self):
        prediction = predict_ddl("s", "snapshot", None, ["a", "b"], ["a"])
        assert prediction.safety == Safety.WARNING
        assert [(op.operation, op.column) for op in prediction.operations] == [
            ("REVIEW REQUIRED (snapshot)", None),
            ("DROP COLUMN", "b"),
        ]
        assert prediction.columns_removed == ["b"]

    def test_an_added_column_is_named(self):
        prediction = predict_ddl("s", "snapshot", None, ["a"], ["a", "b"])
        assert prediction.columns_added == ["b"]
        assert ("ADD COLUMN", "b") in [(op.operation, op.column) for op in prediction.operations]

    def test_an_unchanged_column_set_still_warns_with_nothing_attached(self):
        """A `where` clause edit. The verdict question is deliberately left alone."""
        prediction = predict_ddl("s", "snapshot", None, ["a", "b"], ["a", "b"])
        assert prediction.safety == Safety.WARNING
        assert [op.operation for op in prediction.operations] == ["REVIEW REQUIRED (snapshot)"]

    @pytest.mark.parametrize(
        "base,current",
        [(["*"], ["a"]), (["a"], ["*"]), (None, ["a"]), (["a"], None), (["* except(b)"], ["a"])],
    )
    def test_a_star_is_never_differenced_into_a_column_name(self, base, current):
        """`["*"]` stands for columns it could not enumerate. `DROP COLUMN *` is not a finding."""
        prediction = predict_ddl("s", "snapshot", None, base, current)
        assert [op.operation for op in prediction.operations] == ["REVIEW REQUIRED (snapshot)"]

    def test_the_same_guard_covers_an_unknown_materialization(self):
        prediction = predict_ddl("m", "some_custom_thing", None, ["*"], ["a"])
        assert [op.operation for op in prediction.operations] == [
            "UNKNOWN materialization: some_custom_thing"
        ]
