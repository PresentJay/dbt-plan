"""Tests for column ordering behavior in predictor and column extraction."""

from dbt_plan.columns import extract_columns
from dbt_plan.predictor import Safety, predict_ddl


class TestColumnReorderSyncAllColumns:
    """Scenario 1: Column reorder with sync_all_columns."""

    def test_reorder_only_is_warning(self):
        """Same columns in different order → WARNING with COLUMNS REORDERED."""
        result = predict_ddl(
            model_name="fct_ordered",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any(op.operation == "COLUMNS REORDERED" for op in result.operations)

    def test_reorder_no_add_no_remove(self):
        """Reorder only → columns_added and columns_removed should be empty."""
        result = predict_ddl(
            model_name="fct_ordered",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.columns_added == []
        assert result.columns_removed == []


class TestColumnReorderOtherOSC:
    """Scenario 2: Column reorder with non-sync_all_columns values."""

    def test_append_new_columns_reorder_only_safe(self):
        """append_new_columns + reorder only (no add/remove) → SAFE."""
        result = predict_ddl(
            model_name="fct_metrics",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.safety == Safety.SAFE

    def test_fail_reorder_only_safe(self):
        """fail + reorder only (same column set) → SAFE (no schema diff)."""
        result = predict_ddl(
            model_name="fct_strict",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.safety == Safety.SAFE

    def test_ignore_reorder_always_safe(self):
        """ignore + reorder → always SAFE (no DDL at all)."""
        result = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert result.safety == Safety.SAFE
        assert any(op.operation == "NO DDL" for op in result.operations)


class TestColumnReorderPlusAdd:
    """Scenario 3: Column reorder + add with sync_all_columns."""

    def test_reorder_plus_add_shows_add_column(self):
        """Reorder + new column → ADD COLUMN c, not COLUMNS REORDERED."""
        result = predict_ddl(
            model_name="fct_growing",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["b", "a", "c"],
        )
        # Should detect ADD COLUMN c (set diff), not reorder
        assert result.columns_added == ["c"]
        assert result.columns_removed == []
        assert any(op.operation == "ADD COLUMN" and op.column == "c" for op in result.operations)
        # Should NOT be flagged as COLUMNS REORDERED
        assert not any(op.operation == "COLUMNS REORDERED" for op in result.operations)
        # ADD only → SAFE
        assert result.safety == Safety.SAFE


class TestColumnReorderPlusRemove:
    """Scenario 4: Column reorder + remove with sync_all_columns."""

    def test_reorder_plus_remove_is_destructive(self):
        """Reorder + dropped column → DROP COLUMN b, DESTRUCTIVE."""
        result = predict_ddl(
            model_name="fct_shrinking",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a"],
        )
        assert result.columns_removed == ["b"]
        assert any(op.operation == "DROP COLUMN" and op.column == "b" for op in result.operations)
        assert result.safety == Safety.DESTRUCTIVE


class TestExtractColumnsPreservesOrder:
    """Scenario 5: extract_columns preserves SELECT order."""

    def test_simple_reordered_select(self):
        """SELECT c, a, b FROM t should return ['c', 'a', 'b'], not sorted."""
        result = extract_columns("SELECT c, a, b FROM t")
        assert result == ["c", "a", "b"]

    def test_cte_final_select_order(self):
        """CTE with final SELECT reordering should preserve outer SELECT order."""
        sql = """
        WITH src AS (
            SELECT a, b, c FROM raw_table
        )
        SELECT c, a, b FROM src
        """
        result = extract_columns(sql)
        assert result == ["c", "a", "b"]

    def test_union_all_preserves_first_branch_order(self):
        """UNION ALL — should preserve first branch column order."""
        sql = "SELECT c, a, b FROM t1 UNION ALL SELECT x, y, z FROM t2"
        result = extract_columns(sql)
        # First branch order should be preserved
        assert result == ["c", "a", "b"]


class TestDuplicateColumns:
    """Scenario 6: Duplicate columns → WARNING."""

    def test_base_has_duplicates(self):
        """Base columns with duplicates → WARNING (set diff unreliable)."""
        result = predict_ddl(
            model_name="fct_dupes",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "a", "b"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any("duplicate" in op.operation.lower() for op in result.operations)

    def test_current_has_duplicates(self):
        """Current columns with duplicates → WARNING."""
        result = predict_ddl(
            model_name="fct_dupes",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a", "a", "b"],
        )
        assert result.safety == Safety.WARNING
        assert any("duplicate" in op.operation.lower() for op in result.operations)

    def test_both_have_same_duplicates(self):
        """Both sides have same duplicates → still WARNING."""
        result = predict_ddl(
            model_name="fct_dupes",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["a", "a", "b"],
            current_columns=["a", "a", "b"],
        )
        assert result.safety == Safety.WARNING

    def test_duplicates_with_append_new_columns(self):
        """Duplicate check applies regardless of on_schema_change value."""
        result = predict_ddl(
            model_name="fct_dupes",
            materialization="incremental",
            on_schema_change="append_new_columns",
            base_columns=["a", "a", "b"],
            current_columns=["a", "b", "c"],
        )
        assert result.safety == Safety.WARNING

    def test_duplicates_with_fail(self):
        """Duplicate check applies with on_schema_change=fail too."""
        result = predict_ddl(
            model_name="fct_dupes",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["a", "a", "b"],
            current_columns=["a", "b"],
        )
        assert result.safety == Safety.WARNING


class TestCaseOnlyReorder:
    """Scenario 7: Case-only reorder (lowercased columns, different order)."""

    def test_case_only_reorder_detected(self):
        """Lowercased columns in different order → COLUMNS REORDERED."""
        result = predict_ddl(
            model_name="fct_users",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "name"],
            current_columns=["name", "user_id"],
        )
        assert result.safety == Safety.WARNING
        assert any(op.operation == "COLUMNS REORDERED" for op in result.operations)

    def test_case_only_reorder_three_columns(self):
        """Three-column reorder with realistic names."""
        result = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "user_id", "created_at"],
            current_columns=["created_at", "order_id", "user_id"],
        )
        assert result.safety == Safety.WARNING
        assert any(op.operation == "COLUMNS REORDERED" for op in result.operations)

    def test_extract_columns_lowercases_preserving_order(self):
        """extract_columns lowercases but preserves ordering."""
        sql = "SELECT Name, User_ID FROM users"
        result = extract_columns(sql)
        assert result == ["name", "user_id"]

    def test_mixed_case_reorder_via_extract(self):
        """Mixed-case SELECT reorder: extract_columns feeds into predict_ddl."""
        base_sql = "SELECT user_id, name FROM users"
        current_sql = "SELECT name, user_id FROM users"
        base_cols = extract_columns(base_sql)
        current_cols = extract_columns(current_sql)
        assert base_cols == ["user_id", "name"]
        assert current_cols == ["name", "user_id"]

        result = predict_ddl(
            model_name="dim_users",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=base_cols,
            current_columns=current_cols,
        )
        assert result.safety == Safety.WARNING
        assert any(op.operation == "COLUMNS REORDERED" for op in result.operations)
