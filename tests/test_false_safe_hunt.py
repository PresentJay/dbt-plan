"""False-safe hunt: security audit for dbt-plan's #1 guarantee.

"False safe is never OK" -- dbt-plan must never report SAFE when dbt run
would cause data loss or build failure.

Each test class targets a specific attack vector. Every test documents its
verdict as one of:
  - TRUE SAFE: dbt-plan correctly identifies no schema risk
  - FALSE SAFE (BUG): dbt-plan says SAFE but there IS a real risk
  - KNOWN LIMITATION: dbt-plan says SAFE because the risk is out of scope
                       (data semantics, not schema structure)
  - CORRECTLY CAUGHT: dbt-plan already flags this appropriately
"""

from __future__ import annotations

from dbt_plan.columns import extract_columns
from dbt_plan.manifest import ModelNode
from dbt_plan.predictor import (
    Safety,
    analyze_cascade_impacts,
    predict_ddl,
)


# ---------------------------------------------------------------------------
# Helper to build ModelNode quickly
# ---------------------------------------------------------------------------
def _node(name: str, mat: str = "incremental", osc: str | None = "ignore") -> ModelNode:
    return ModelNode(
        node_id=f"model.test.{name}",
        name=name,
        materialization=mat,
        on_schema_change=osc,
    )


# ===================================================================
# Attack Vector 1: Column name aliasing tricks
#
# Base: SELECT user_id AS uid FROM t  -> columns=["uid"]
# Curr: SELECT customer_id AS uid FROM t -> columns=["uid"]
#
# Column NAME didn't change, CONTENT completely changed.
# With sync_all_columns: no add/remove -> SAFE.
# ===================================================================
class TestColumnAliasingTricks:
    """Alias swaps preserve column names but change underlying data."""

    def test_alias_swap_same_column_names_sync(self):
        """KNOWN LIMITATION: alias swap is invisible to schema-level analysis.

        Base: SELECT user_id AS uid
        Curr: SELECT customer_id AS uid
        Same column set -> no DDL -> SAFE.

        dbt-plan only checks column names, not source expressions.
        This is by design: dbt itself treats this as "no schema change."
        dbt run would succeed -- the physical table schema is unchanged.
        The data semantics change, but that's not a schema risk.
        """
        base_sql = "SELECT user_id AS uid FROM users"
        curr_sql = "SELECT customer_id AS uid FROM buyers"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["uid"]
        assert curr_cols == ["uid"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION -- dbt-plan only tracks schema, not data semantics.
        # dbt run WILL succeed (no schema change), so SAFE is technically correct
        # from a DDL perspective. The data corruption is a business logic issue.
        assert result.safety == Safety.SAFE

    def test_alias_swap_fail_mode(self):
        """incremental+fail with alias swap: same columns -> SAFE.

        dbt won't fail because the column names match. No false safe.
        """
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["uid", "ts"],
            current_columns=["uid", "ts"],
        )
        # Verdict: TRUE SAFE -- dbt run will succeed, no schema mismatch.
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 2: SQL logic change without column change
#
# Base: SELECT id, COALESCE(revenue, 0) AS revenue FROM t
# Curr: SELECT id, NULL AS revenue FROM t
# Same columns, different data.
# ===================================================================
class TestSQLLogicChangeNoColumnChange:
    """SQL expression changes that preserve column names."""

    def test_coalesce_to_null_same_columns(self):
        """KNOWN LIMITATION: expression change is invisible to schema analysis.

        COALESCE(revenue, 0) -> NULL AS revenue.
        Same column name, drastically different data. But schema unchanged.
        dbt run succeeds. dbt-plan reports SAFE.
        """
        base_sql = "SELECT id, COALESCE(revenue, 0) AS revenue FROM orders"
        curr_sql = "SELECT id, NULL AS revenue FROM orders"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["id", "revenue"]
        assert curr_cols == ["id", "revenue"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION -- logic changes are out of scope.
        assert result.safety == Safety.SAFE

    def test_aggregation_change_same_columns(self):
        """SUM -> COUNT on same alias: schema unchanged, data changed."""
        base_sql = "SELECT user_id, SUM(amount) AS metric FROM t GROUP BY 1"
        curr_sql = "SELECT user_id, COUNT(amount) AS metric FROM t GROUP BY 1"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["user_id", "metric"]
        assert curr_cols == ["user_id", "metric"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 3: WHERE clause change
#
# Base: SELECT id FROM t WHERE status = 'active'
# Curr: SELECT id FROM t  (no WHERE -- much more data)
# Same columns. Row count explodes.
# ===================================================================
class TestWhereClauseChange:
    """WHERE clause removal/change affects row count but not schema."""

    def test_where_clause_removed(self):
        """KNOWN LIMITATION: row count changes are out of scope.

        Removing WHERE doesn't change the schema. dbt run succeeds.
        Row explosion is a data quality concern, not a DDL risk.
        """
        base_sql = "SELECT id, name FROM users WHERE status = 'active'"
        curr_sql = "SELECT id, name FROM users"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["id", "name"]
        assert curr_cols == ["id", "name"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 4: Column type change (same name)
#
# Base: SELECT CAST(id AS VARCHAR) AS id FROM t
# Curr: SELECT CAST(id AS INTEGER) AS id FROM t
# Same column name, different type.
# ===================================================================
class TestColumnTypeChange:
    """Column type changes via CAST preserve column names."""

    def test_type_change_varchar_to_integer(self):
        """KNOWN LIMITATION: dbt-plan only checks column names, not types.

        CAST(id AS VARCHAR) -> CAST(id AS INTEGER).
        Same column name 'id'. dbt-plan sees no schema change.

        For incremental+sync_all_columns: dbt WILL execute the column,
        but the physical column type in the warehouse may or may not
        change depending on the warehouse. This is a real risk that
        dbt-plan cannot detect without type inference.
        """
        base_sql = "SELECT CAST(id AS VARCHAR) AS id FROM t"
        curr_sql = "SELECT CAST(id AS INTEGER) AS id FROM t"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["id"]
        assert curr_cols == ["id"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION -- type inference would require warehouse metadata
        # or sqlglot type resolution, which is beyond current scope.
        assert result.safety == Safety.SAFE

    def test_type_change_with_fail_mode(self):
        """Type change with on_schema_change=fail: dbt may or may not fail.

        Whether dbt detects a type change depends on the warehouse.
        Snowflake: VARCHAR(100) -> VARCHAR(200) may not fail.
        This is warehouse-specific behavior dbt-plan can't predict.
        """
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["id"],
            current_columns=["id"],
        )
        # Verdict: KNOWN LIMITATION -- same column names, type info not available
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 5: CTE column shadowing
#
# Base CTE has `data_region`, final SELECT uses it from CTE.
# Current removes `data_region` from CTE but adds it from different source.
# Column list identical. But data pipeline semantics changed.
# ===================================================================
class TestCTEColumnShadowing:
    """CTE internal changes that preserve the final SELECT column list."""

    def test_cte_source_change_same_output(self):
        """KNOWN LIMITATION: CTE internal restructuring is invisible.

        dbt-plan only looks at the final SELECT's output columns.
        Internal CTE structure changes don't affect the schema.
        dbt run will succeed -- the output schema is the same.
        """
        base_sql = """
        WITH base AS (
            SELECT user_id, customer_id AS data_region FROM raw_orders
        )
        SELECT user_id, data_region FROM base
        """
        curr_sql = """
        WITH base AS (
            SELECT user_id FROM raw_orders
        ),
        regions AS (
            SELECT user_id, browser AS data_region FROM raw_customers
        )
        SELECT base.user_id, regions.data_region FROM base JOIN regions USING(user_id)
        """

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["user_id", "data_region"]
        assert curr_cols == ["user_id", "data_region"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION -- CTE internals are out of scope
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 6: UNION ALL branch change
#
# Base: SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t2
# Curr: SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t3
# Same columns, different source.
# ===================================================================
class TestUnionAllBranchChange:
    """UNION ALL source changes that preserve column names."""

    def test_union_source_swap(self):
        """KNOWN LIMITATION: UNION ALL source changes are invisible.

        Different source table, same column names. Schema unchanged.
        dbt run succeeds. Data comes from a completely different table.
        """
        base_sql = "SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t2"
        curr_sql = "SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t3"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["a", "b"]
        assert curr_cols == ["a", "b"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION
        assert result.safety == Safety.SAFE

    def test_union_branch_added(self):
        """Adding a UNION ALL branch: same columns, more data."""
        base_sql = "SELECT a, b FROM t1"
        curr_sql = "SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t2"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["a", "b"]
        assert curr_cols == ["a", "b"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: KNOWN LIMITATION
        assert result.safety == Safety.SAFE

    def test_union_column_change_detected(self):
        """CORRECTLY CAUGHT: UNION ALL with column name change IS detected."""
        base_sql = "SELECT a, b FROM t1 UNION ALL SELECT a, b FROM t2"
        curr_sql = "SELECT a, c FROM t1 UNION ALL SELECT a, c FROM t2"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["a", "b"]
        assert curr_cols == ["a", "c"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.DESTRUCTIVE
        assert "b" in result.columns_removed
        assert "c" in result.columns_added


# ===================================================================
# Attack Vector 7: incremental + ignore with actual column removal
#
# Model: incremental, on_schema_change=ignore
# SQL changes from SELECT a, b, c to SELECT a, b (column c removed)
# dbt-plan says SAFE (ignore = no DDL).
# But column c is now stale (never populated for new rows).
# ===================================================================
class TestIncrementalIgnoreColumnRemoval:
    """incremental+ignore silently leaves stale columns in the physical table."""

    def test_ignore_with_column_removal_is_safe(self):
        """TRUE SAFE from DDL perspective: ignore means no DDL, no build failure.

        dbt run WILL succeed. Column c remains in the physical table
        but new rows won't populate it (NULL for new inserts).
        This is exactly what on_schema_change=ignore means -- the user
        has opted into this behavior.

        dbt-plan correctly reports SAFE because:
        1. No DDL will be executed
        2. dbt run will not fail
        3. The user explicitly chose "ignore" mode
        """
        result = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )
        # Verdict: TRUE SAFE -- ignore mode is explicit user choice.
        # No DDL, no build failure. Stale data is expected behavior.
        assert result.safety == Safety.SAFE
        assert any(op.operation == "NO DDL" for op in result.operations)

    def test_ignore_skips_cascade_analysis(self):
        """incremental+ignore: cascade analysis is correctly skipped.

        Since no physical schema change occurs (no DDL), downstream
        models see the OLD schema (column c still exists). So downstream
        won't break -- they can still SELECT c from the physical table.
        """
        pred = predict_ddl(
            model_name="inc_ignore",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )

        node_index = {"fct": _node("fct", "incremental", "fail")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"inc_ignore": "model.test.inc_ignore"},
            model_cols={"inc_ignore": (["a", "b", "c"], ["a", "b"])},
            all_downstream={"model.test.inc_ignore": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )
        # Verdict: TRUE SAFE -- no physical schema change means no cascade
        assert updated[0].downstream_impacts == []

    def test_ignore_default_none_on_schema_change(self):
        """on_schema_change=None defaults to 'ignore' -- ensure no surprise."""
        result = predict_ddl(
            model_name="fct",
            materialization="incremental",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a"],
        )
        # None -> ignore -> SAFE
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 8: Table materialization with column removal + cascade
#
# Parent: table, SQL changes from SELECT a, b, c to SELECT a, b
# dbt-plan says SAFE for the table itself (CREATE OR REPLACE).
# But downstream incremental+fail model referencing column c will break.
# ===================================================================
class TestTableColumnRemovalCascade:
    """Table is always SAFE for itself, but cascade should catch downstream breaks."""

    def test_table_itself_is_safe(self):
        """Table materialization: always SAFE (CREATE OR REPLACE TABLE).

        Even with column removal, the table is recreated from scratch.
        No data loss risk for the table itself.
        """
        result = predict_ddl(
            model_name="dim_users",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )
        # Verdict: TRUE SAFE for the table itself
        assert result.safety == Safety.SAFE

    def test_table_column_removal_cascade_to_incremental_fail(self, tmp_path):
        """CORRECTLY CAUGHT: cascade analysis detects downstream build failure.

        Parent table removes column c. Downstream incremental+fail model
        has column c in its schema -> build failure.
        """
        ds_sql = tmp_path / "fct_metrics.sql"
        ds_sql.write_text("SELECT a, b, c FROM {{ ref('dim_users') }}")

        pred = predict_ddl(
            model_name="dim_users",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )

        node_index = {"fct_metrics": _node("fct_metrics", "incremental", "fail")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"dim_users": "model.test.dim_users"},
            model_cols={"dim_users": (["a", "b", "c"], ["a", "b"])},
            all_downstream={"model.test.dim_users": ["model.test.fct_metrics"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct_metrics": ds_sql},
        )

        # Verdict: CORRECTLY CAUGHT -- cascade detects broken_ref and build_failure
        assert updated[0].safety != Safety.SAFE
        risks = {imp.risk for imp in updated[0].downstream_impacts}
        assert "broken_ref" in risks or "build_failure" in risks

    def test_table_column_removal_cascade_to_table_downstream(self, tmp_path):
        """CORRECTLY CAUGHT: even table downstream with broken ref is detected.

        Parent table drops column c. Downstream TABLE model references c.
        The downstream table's SELECT will fail at dbt run time.
        """
        ds_sql = tmp_path / "dim_derived.sql"
        ds_sql.write_text("SELECT a, c FROM {{ ref('dim_users') }}")

        pred = predict_ddl(
            model_name="dim_users",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )

        node_index = {"dim_derived": _node("dim_derived", "table", None)}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"dim_users": "model.test.dim_users"},
            model_cols={"dim_users": (["a", "b", "c"], ["a", "b"])},
            all_downstream={"model.test.dim_users": ["model.test.dim_derived"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"dim_derived": ds_sql},
        )

        # Verdict: CORRECTLY CAUGHT -- broken_ref detected on table downstream
        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"
        assert updated[0].safety == Safety.DESTRUCTIVE

    def test_table_column_addition_cascade_to_incremental_fail(self):
        """CORRECTLY CAUGHT: table adds column -> downstream incremental+fail breaks.

        This is the subtlest cascade: parent table ADDS a column.
        dbt-plan says SAFE for the table (CREATE OR REPLACE TABLE).
        But downstream incremental+fail will break because the upstream
        schema changed (added column) and fail mode doesn't tolerate that.
        """
        pred = predict_ddl(
            model_name="dim_users",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )
        assert pred.safety == Safety.SAFE

        node_index = {"fct_daily": _node("fct_daily", "incremental", "fail")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"dim_users": "model.test.dim_users"},
            model_cols={"dim_users": (["a", "b"], ["a", "b", "c"])},
            all_downstream={"model.test.dim_users": ["model.test.fct_daily"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        # Verdict: CORRECTLY CAUGHT -- cascade detects build_failure for inc+fail
        assert any(imp.risk == "build_failure" for imp in updated[0].downstream_impacts)
        assert updated[0].safety == Safety.WARNING


# ===================================================================
# Attack Vector 9: View with downstream incremental+fail
#
# View changes columns -> SAFE for view itself.
# But downstream incremental+fail model will break.
# ===================================================================
class TestViewCascadeToIncrementalFail:
    """View changes are SAFE for the view but can cascade to downstream."""

    def test_view_itself_always_safe(self):
        """View: always SAFE (CREATE OR REPLACE VIEW)."""
        result = predict_ddl(
            model_name="vw_report",
            materialization="view",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.SAFE

    def test_view_column_removal_cascade_to_incremental_fail(self, tmp_path):
        """CORRECTLY CAUGHT: view drops column -> downstream inc+fail breaks."""
        ds_sql = tmp_path / "fct.sql"
        ds_sql.write_text("SELECT a, b, c FROM {{ ref('vw_report') }}")

        pred = predict_ddl(
            model_name="vw_report",
            materialization="view",
            on_schema_change=None,
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )

        node_index = {"fct": _node("fct", "incremental", "fail")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"vw_report": "model.test.vw_report"},
            model_cols={"vw_report": (["a", "b", "c"], ["a", "b"])},
            all_downstream={"model.test.vw_report": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": ds_sql},
        )

        # Verdict: CORRECTLY CAUGHT
        risks = {imp.risk for imp in updated[0].downstream_impacts}
        assert "broken_ref" in risks or "build_failure" in risks
        assert updated[0].safety != Safety.SAFE

    def test_view_column_addition_cascade_to_incremental_fail(self):
        """CORRECTLY CAUGHT: view adds column -> downstream inc+fail gets build_failure."""
        pred = predict_ddl(
            model_name="vw_report",
            materialization="view",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )
        assert pred.safety == Safety.SAFE

        node_index = {"fct": _node("fct", "incremental", "fail")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"vw_report": "model.test.vw_report"},
            model_cols={"vw_report": (["a", "b"], ["a", "b", "c"])},
            all_downstream={"model.test.vw_report": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )

        # Verdict: CORRECTLY CAUGHT
        assert any(imp.risk == "build_failure" for imp in updated[0].downstream_impacts)
        assert updated[0].safety == Safety.WARNING


# ===================================================================
# Attack Vector 10: Manifest column fallback exploitation
#
# Model uses SELECT *, manifest has columns [a, b, c].
# Underlying table now has [a, b, c, d] (column added at source).
# dbt-plan uses manifest fallback [a, b, c] -- doesn't see d.
# ===================================================================
class TestManifestColumnFallback:
    """Manifest column fallback for SELECT * may be stale."""

    def test_select_star_without_manifest_fallback(self):
        """SELECT * without manifest fallback -> WARNING (correctly conservative).

        When SELECT * is used and no manifest columns are available,
        dbt-plan correctly returns WARNING -- it cannot determine
        the column diff.
        """
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["*"],
            current_columns=["*"],
        )
        # Verdict: CORRECTLY CAUGHT -- SELECT * gets WARNING
        assert result.safety == Safety.WARNING

    def test_stale_manifest_fallback_both_sides_same(self):
        """KNOWN LIMITATION: manifest fallback may be stale but both sides match.

        If both base and current manifest have the same stale columns,
        dbt-plan sees "no change" and reports SAFE. But in reality,
        the underlying source may have changed.

        This is a fundamental limitation: dbt-plan works on compiled SQL
        and manifest metadata, not live warehouse state.
        """
        # Simulate: both base and current use SELECT *, both manifests say [a, b, c]
        # In reality, source table now has [a, b, c, d] -- but dbt-plan can't know
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],  # manifest fallback
            current_columns=["a", "b", "c"],  # same manifest fallback
        )
        # Verdict: KNOWN LIMITATION -- manifest may be stale, but both sides agree
        assert result.safety == Safety.SAFE

    def test_manifest_fallback_column_change_detected(self):
        """CORRECTLY CAUGHT: manifest column difference IS detected.

        Base manifest: [a, b, c], Current manifest: [a, b, d].
        dbt-plan correctly detects the column change via manifest fallback.
        """
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],  # from base manifest
            current_columns=["a", "b", "d"],  # from current manifest
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.DESTRUCTIVE
        assert "c" in result.columns_removed
        assert "d" in result.columns_added

    def test_select_star_one_side_only(self):
        """SELECT * on one side, explicit on the other -> WARNING.

        This is the case where base used SELECT * (manifest [a,b,c]),
        but if manifest fallback is NOT available, the raw ["*"] is passed.
        """
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["*"],
            current_columns=["a", "b"],
        )
        # Verdict: CORRECTLY CAUGHT -- SELECT * on one side triggers WARNING
        assert result.safety == Safety.WARNING


# ===================================================================
# Attack Vector 11: Column extraction edge cases
#
# Ensure extract_columns correctly handles tricky SQL patterns
# that could lead to false column lists and then false SAFEs.
# ===================================================================
class TestColumnExtractionEdgeCases:
    """Ensure column extraction doesn't produce wrong results that hide changes."""

    def test_case_expression_without_alias_returns_none(self):
        """CASE without AS -> extract_columns returns None -> review required.

        This is critical: if we extracted a wrong column list, we might
        get a false SAFE. Returning None triggers WARNING.
        """
        sql = "SELECT id, CASE WHEN x > 0 THEN 'a' ELSE 'b' END FROM t"
        cols = extract_columns(sql)
        # sqlglot may or may not extract the CASE output_name.
        # If it can't -> None -> WARNING (good).
        # If it returns a name -> the column is still detected (also good).
        # The key guarantee: we never silently lose a column.
        if cols is not None:
            assert len(cols) == 2  # id + the CASE expression
        # If None, predict_ddl with this would give WARNING (safe fail)

    def test_nested_subquery_final_select(self):
        """Column extraction uses the outermost SELECT, not inner."""
        sql = """
        SELECT outer_a, outer_b FROM (
            SELECT inner_x AS outer_a, inner_y AS outer_b, inner_z
            FROM raw_table
        ) sub
        """
        cols = extract_columns(sql)
        assert cols == ["outer_a", "outer_b"]

    def test_malformed_sql_returns_none(self):
        """Malformed SQL -> None -> predict_ddl gives WARNING (never SAFE)."""
        sql = "THIS IS NOT VALID SQL AT ALL !!!"
        cols = extract_columns(sql)
        assert cols is None

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=None,  # parse failed
            current_columns=["a", "b"],
        )
        # Verdict: CORRECTLY CAUGHT -- parse failure -> WARNING
        assert result.safety == Safety.WARNING

    def test_empty_sql_returns_none(self):
        """Empty SQL -> None -> review required."""
        cols = extract_columns("")
        assert cols is None

    def test_select_star_correctly_detected(self):
        """SELECT * is correctly identified as ["*"]."""
        sql = "SELECT * FROM users"
        cols = extract_columns(sql)
        assert cols == ["*"]

    def test_qualified_star_detected(self):
        """t1.* is correctly identified as ["*"]."""
        sql = "SELECT t1.* FROM users t1"
        cols = extract_columns(sql)
        assert cols == ["*"]


# ===================================================================
# Attack Vector 12: Incremental + append_new_columns with column removal
#
# Model: incremental, on_schema_change=append_new_columns
# Columns removed from SQL: physical table keeps the column but
# it stops being populated. dbt-plan should warn.
# ===================================================================
class TestIncrementalAppendColumnRemoval:
    """append_new_columns only adds columns; removed columns become stale."""

    def test_append_with_removal_warns(self):
        """CORRECTLY CAUGHT: append_new_columns + column removal -> WARNING.

        Column c is removed from SQL but stays in the physical table.
        New rows will have NULL for column c. dbt-plan warns about stale data.
        """
        result = predict_ddl(
            model_name="fct",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a", "b"],
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.WARNING
        assert "c" in result.columns_removed
        assert any("STALE" in op.operation for op in result.operations)

    def test_append_with_only_addition_safe(self):
        """append_new_columns + only additions -> SAFE."""
        result = predict_ddl(
            model_name="fct",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b", "c"],
        )
        # Verdict: TRUE SAFE
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 13: Column rename (simultaneous add + remove)
#
# Base: [user_id, email]
# Curr: [user_id, email_address]
# Looks like: email removed, email_address added.
# With sync_all_columns: DROP email, ADD email_address -> DESTRUCTIVE
# ===================================================================
class TestColumnRename:
    """Column renames look like add+remove to dbt-plan."""

    def test_rename_detected_as_destructive(self):
        """CORRECTLY CAUGHT: column rename via sync_all_columns -> DESTRUCTIVE.

        dbt-plan can't distinguish rename from drop+add, which is correct:
        dbt itself treats it as DROP + ADD. The old column data is lost.
        """
        result = predict_ddl(
            model_name="dim_users",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "email"],
            current_columns=["user_id", "email_address"],
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.DESTRUCTIVE
        assert "email" in result.columns_removed
        assert "email_address" in result.columns_added

    def test_rename_detected_as_build_failure_with_fail(self):
        """CORRECTLY CAUGHT: column rename with fail mode -> WARNING (build failure)."""
        result = predict_ddl(
            model_name="dim_users",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["user_id", "email"],
            current_columns=["user_id", "email_address"],
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.WARNING
        assert any("BUILD FAILURE" in op.operation for op in result.operations)


# ===================================================================
# Attack Vector 14: Multi-level cascade (grandchild impact)
#
# A -> B -> C
# A (table) drops column. B (view) references it and will fail.
# C (incremental+fail) depends on B and will also fail.
# Does dbt-plan catch the full chain?
# ===================================================================
class TestMultiLevelCascade:
    """Multi-level cascade: parent -> child -> grandchild."""

    def test_direct_downstream_caught(self, tmp_path):
        """CORRECTLY CAUGHT: direct downstream broken ref detected."""
        ds_sql = tmp_path / "child_view.sql"
        ds_sql.write_text("SELECT a, dropped_col FROM {{ ref('parent_table') }}")

        pred = predict_ddl(
            model_name="parent_table",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "dropped_col"],
            current_columns=["a"],
        )

        {
            "child_view": _node("child_view", "view", None),
        }

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent_table": "model.test.parent_table"},
            model_cols={"parent_table": (["a", "dropped_col"], ["a"])},
            all_downstream={
                "model.test.parent_table": [
                    "model.test.child_view",
                    "model.test.grandchild_inc",
                ]
            },
            node_index={
                "child_view": _node("child_view", "view", None),
                "grandchild_inc": _node("grandchild_inc", "incremental", "fail"),
            },
            base_node_index={},
            compiled_sql_index={"child_view": ds_sql},
        )

        assert len(updated[0].downstream_impacts) >= 1
        # At minimum, child_view broken_ref is detected
        ref_names = {imp.model_name for imp in updated[0].downstream_impacts}
        assert "child_view" in ref_names

    def test_grandchild_detected_when_it_references_dropped_col(self, tmp_path):
        """Grandchild that also references the dropped column: detected.

        Note: dbt-plan's cascade analysis checks all downstream models
        (including indirect ones) against the parent's dropped columns.
        If grandchild SQL directly references the dropped column name,
        it gets caught. If it only references via the child, it may not
        (since the regex matches the SQL text, not the resolved lineage).
        """
        child_sql = tmp_path / "child_view.sql"
        child_sql.write_text("SELECT a, dropped_col FROM {{ ref('parent_table') }}")
        grandchild_sql = tmp_path / "grandchild_inc.sql"
        grandchild_sql.write_text("SELECT dropped_col FROM {{ ref('child_view') }}")

        pred = predict_ddl(
            model_name="parent_table",
            materialization="table",
            on_schema_change=None,
            base_columns=["a", "dropped_col"],
            current_columns=["a"],
        )

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent_table": "model.test.parent_table"},
            model_cols={"parent_table": (["a", "dropped_col"], ["a"])},
            all_downstream={
                "model.test.parent_table": [
                    "model.test.child_view",
                    "model.test.grandchild_inc",
                ]
            },
            node_index={
                "child_view": _node("child_view", "view", None),
                "grandchild_inc": _node("grandchild_inc", "incremental", "fail"),
            },
            base_node_index={},
            compiled_sql_index={
                "child_view": child_sql,
                "grandchild_inc": grandchild_sql,
            },
        )

        ref_names = {imp.model_name for imp in updated[0].downstream_impacts}
        # Both child and grandchild reference "dropped_col" in their SQL
        assert "child_view" in ref_names
        assert "grandchild_inc" in ref_names


# ===================================================================
# Attack Vector 15: Column case sensitivity
#
# Base: ["User_ID", "Email"]
# Curr: ["user_id", "email"]
# After lowercasing, these are the same. Ensure no false positive.
# ===================================================================
class TestColumnCaseSensitivity:
    """Column names are lowercased -- case changes should not trigger diff."""

    def test_case_change_no_diff(self):
        """Column case change: extract_columns lowercases, so no diff."""
        base_sql = "SELECT User_ID, Email FROM users"
        curr_sql = "SELECT user_id, email FROM users"

        base_cols = extract_columns(base_sql)
        curr_cols = extract_columns(curr_sql)

        assert base_cols == ["user_id", "email"]
        assert curr_cols == ["user_id", "email"]

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=curr_cols,
        )
        # Verdict: TRUE SAFE -- case normalization prevents false alarm
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 16: Table/view parse failure should NOT trigger WARNING
#
# Table and view are always CREATE OR REPLACE -- parse failure is
# irrelevant for the model itself. But what about cascade?
# ===================================================================
class TestTableViewParseFailure:
    """Table/view with parse failure: the model itself is SAFE."""

    def test_table_parse_failure_still_safe(self):
        """Table: parse failure doesn't matter (CREATE OR REPLACE TABLE)."""
        result = predict_ddl(
            model_name="dim",
            materialization="table",
            on_schema_change=None,
            base_columns=None,  # parse failed
            current_columns=["a", "b"],
        )
        # Verdict: TRUE SAFE -- table is always recreated
        assert result.safety == Safety.SAFE

    def test_view_parse_failure_still_safe(self):
        """View: parse failure doesn't matter (CREATE OR REPLACE VIEW)."""
        result = predict_ddl(
            model_name="vw",
            materialization="view",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=None,  # parse failed
        )
        # Verdict: TRUE SAFE -- view is always recreated
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 17: Removed model with ephemeral materialization
#
# Removing an ephemeral model has no physical impact, so SAFE.
# But what if a downstream model references it?
# ===================================================================
class TestRemovedEphemeralCascade:
    """Removed ephemeral: SAFE for itself, but downstream might break."""

    def test_removed_ephemeral_is_safe(self):
        """Removed ephemeral model -> SAFE (no physical object)."""
        result = predict_ddl(
            model_name="eph",
            materialization="ephemeral",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=None,
            status="removed",
        )
        assert result.safety == Safety.SAFE

    def test_removed_ephemeral_cascade_check(self, tmp_path):
        """Removed ephemeral with downstream: downstream will break at compile time.

        Note: ephemeral model removal means the CTE is removed from
        the compiled SQL. Any model referencing it via {{ ref('eph') }}
        will fail at dbt compile time, not dbt run time.

        dbt-plan's cascade analysis skips ephemeral downstream models
        (they're CTEs), but a removed ephemeral parent is different --
        it's a compile error. The cascade analysis checks the parent model,
        and for removed models it uses status="removed" which is SAFE
        for ephemeral. The downstream would need to be caught at compile
        time by dbt itself.
        """
        # The ephemeral model is removed
        pred = predict_ddl(
            model_name="eph_parent",
            materialization="ephemeral",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=None,
            status="removed",
        )

        # A downstream model references it
        ds_sql = tmp_path / "fct.sql"
        ds_sql.write_text("SELECT a FROM {{ ref('eph_parent') }}")

        node_index = {"fct": _node("fct", "table", None)}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"eph_parent": "model.test.eph_parent"},
            model_cols={"eph_parent": (["a", "b"], None)},
            all_downstream={"model.test.eph_parent": ["model.test.fct"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"fct": ds_sql},
        )

        # The cascade detects broken_ref because "a" and "b" columns from
        # the ephemeral parent are treated as removed (base cols exist, current is None)
        # and the downstream SQL references "a"
        # This should be caught because stored_curr is None and stored_base has columns
        assert len(updated[0].downstream_impacts) >= 1
        assert updated[0].safety == Safety.DESTRUCTIVE


# ===================================================================
# Attack Vector 18: Snapshot materialization
#
# Snapshots always get WARNING regardless of column changes.
# Verify this conservative behavior.
# ===================================================================
class TestSnapshotAlwaysWarning:
    """Snapshot materialization should always produce WARNING."""

    def test_snapshot_no_column_change(self):
        """Snapshot with no column change: still WARNING."""
        result = predict_ddl(
            model_name="snap",
            materialization="snapshot",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        # Verdict: CORRECTLY CAUGHT -- conservative WARNING
        assert result.safety == Safety.WARNING

    def test_snapshot_with_column_change(self):
        """Snapshot with column change: still WARNING (not DESTRUCTIVE)."""
        result = predict_ddl(
            model_name="snap",
            materialization="snapshot",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        # Verdict: CORRECTLY CAUGHT
        assert result.safety == Safety.WARNING


# ===================================================================
# Attack Vector 19: incremental + sync_all_columns column reorder
#
# Same column set but different order.
# sync_all_columns may reorder columns (drop + re-add).
# ===================================================================
class TestSyncColumnReorder:
    """sync_all_columns detects column reordering."""

    def test_reorder_detected(self):
        """CORRECTLY CAUGHT: column reorder with sync_all_columns -> WARNING."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "b", "a"],
        )
        assert result.safety == Safety.WARNING
        assert any("REORDER" in op.operation for op in result.operations)

    def test_no_reorder_when_same_order(self):
        """Same order -> SAFE, no reorder warning."""
        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a", "b", "c"],
        )
        assert result.safety == Safety.SAFE


# ===================================================================
# Attack Vector 20: Downstream model not in current manifest
#   but exists in base manifest
#
# When a downstream model is removed from current manifest but
# exists in base, the cascade analysis should still find it.
# ===================================================================
class TestDownstreamInBaseManifestOnly:
    """Cascade analysis should use base_node_index for fallback lookups."""

    def test_downstream_from_base_manifest(self, tmp_path):
        """Downstream only in base manifest: still analyzed for cascade."""
        ds_sql = tmp_path / "old_fct.sql"
        ds_sql.write_text("SELECT dropped_col FROM {{ ref('parent') }}")

        pred = predict_ddl(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["dropped_col", "kept_col"],
            current_columns=["kept_col"],
        )

        # old_fct is NOT in current node_index, only in base
        base_node_index = {"old_fct": _node("old_fct", "incremental", "ignore")}

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.test.parent"},
            model_cols={"parent": (["dropped_col", "kept_col"], ["kept_col"])},
            all_downstream={"model.test.parent": ["model.test.old_fct"]},
            node_index={},  # empty current
            base_node_index=base_node_index,
            compiled_sql_index={"old_fct": ds_sql},
        )

        # Should find old_fct via base_node_index fallback
        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].model_name == "old_fct"
        assert updated[0].downstream_impacts[0].risk == "broken_ref"


# ===================================================================
# Attack Vector 21: Window function / complex SQL patterns
#
# Ensure that complex SQL patterns don't cause extract_columns
# to return wrong results that could lead to false SAFE.
# ===================================================================
class TestComplexSQLPatterns:
    """Complex SQL patterns should not confuse column extraction."""

    def test_window_function(self):
        """Window function with alias: correctly extracted."""
        sql = """
        SELECT
            user_id,
            ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) AS rn
        FROM events
        """
        cols = extract_columns(sql)
        assert cols == ["user_id", "rn"]

    def test_lateral_flatten(self):
        """Snowflake LATERAL FLATTEN (if supported by sqlglot)."""
        sql = """
        SELECT
            user_id,
            f.value::string AS tag
        FROM users,
        LATERAL FLATTEN(input => tags) f
        """
        cols = extract_columns(sql, dialect="snowflake")
        # We just verify it doesn't crash and returns something reasonable
        if cols is not None:
            assert "user_id" in cols

    def test_deeply_nested_cte(self):
        """Deeply nested CTEs: final SELECT is what matters."""
        sql = """
        WITH cte1 AS (SELECT a, b, c FROM raw),
             cte2 AS (SELECT a, b FROM cte1),
             cte3 AS (SELECT a FROM cte2)
        SELECT a FROM cte3
        """
        cols = extract_columns(sql)
        assert cols == ["a"]

    def test_multiple_statements_first_wins(self):
        """Multiple SQL statements: sqlglot parse_one takes the first."""
        sql = "SELECT a, b FROM t1; SELECT x, y FROM t2"
        cols = extract_columns(sql)
        # parse_one takes the first statement
        if cols is not None:
            assert cols == ["a", "b"]
