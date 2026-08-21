"""Exhaustive path coverage tests for predict_ddl.

Every code path through predict_ddl is tested here, organized by the
branch conditions in predictor.py. The matrix covers:

- status: removed, added, modified
- materialization: table, view, incremental, ephemeral, snapshot, custom
- on_schema_change: ignore, fail, append_new_columns, sync_all_columns, None, unknown
- column scenarios: None, ["*"], ["* except(...)"], duplicates, identical,
  added-only, removed-only, both, reordered
"""

import pytest

from dbt_plan.predictor import DDLOperation, DDLPrediction, Safety, predict_ddl

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _predict(
    materialization="incremental",
    on_schema_change="ignore",
    base_columns=None,
    current_columns=None,
    status="modified",
    model_name="test_model",
):
    return predict_ddl(
        model_name=model_name,
        materialization=materialization,
        on_schema_change=on_schema_change,
        base_columns=base_columns,
        current_columns=current_columns,
        status=status,
    )


def _op_names(pred: DDLPrediction) -> list[str]:
    return [op.operation for op in pred.operations]


# ===========================================================================
# 1. status="removed" × every materialization
# ===========================================================================


class TestRemovedByMaterialization:
    """Removed model: destructive for physical objects, safe for ephemeral."""

    @pytest.mark.parametrize("mat", ["table", "view", "incremental", "snapshot"])
    def test_removed_physical_materialization_destructive(self, mat):
        pred = _predict(materialization=mat, status="removed")
        assert pred.safety == Safety.DESTRUCTIVE
        assert "MODEL REMOVED" in _op_names(pred)

    def test_removed_ephemeral_safe(self):
        pred = _predict(materialization="ephemeral", status="removed")
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_removed_custom_materialization_destructive(self):
        """Unknown/custom materialization falls through — still DESTRUCTIVE on removal."""
        pred = _predict(materialization="custom_materialization", status="removed")
        assert pred.safety == Safety.DESTRUCTIVE
        assert "MODEL REMOVED" in _op_names(pred)

    def test_removed_preserves_on_schema_change(self):
        """on_schema_change is stored as-is for removed models."""
        pred = _predict(
            materialization="incremental",
            on_schema_change="sync_all_columns",
            status="removed",
        )
        assert pred.on_schema_change == "sync_all_columns"

    def test_removed_with_none_on_schema_change(self):
        pred = _predict(
            materialization="table",
            on_schema_change=None,
            status="removed",
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert pred.on_schema_change is None


# ===========================================================================
# 2. status="added" × every materialization × on_schema_change
#    (After the removed block, added hits mat-specific branches first)
# ===========================================================================


class TestAddedTableViewEphemeralSnapshot:
    """For table/view/ephemeral/snapshot, status=added hits the mat branch before osc."""

    def test_added_table_safe(self):
        pred = _predict(materialization="table", status="added", current_columns=["a", "b"])
        assert pred.safety == Safety.SAFE
        assert "CREATE OR REPLACE TABLE" in _op_names(pred)

    def test_added_view_safe(self):
        pred = _predict(materialization="view", status="added", current_columns=["a", "b"])
        assert pred.safety == Safety.SAFE
        assert "CREATE OR REPLACE VIEW" in _op_names(pred)

    def test_added_ephemeral_safe(self):
        pred = _predict(materialization="ephemeral", status="added", current_columns=["a", "b"])
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_added_snapshot_warning(self):
        pred = _predict(materialization="snapshot", status="added", current_columns=["a", "b"])
        assert pred.safety == Safety.WARNING
        assert any("snapshot" in op.lower() for op in _op_names(pred))


class TestAddedIncrementalByOsc:
    """Incremental added: osc=ignore returns early; others hit status=added branch."""

    def test_added_incremental_ignore_safe(self):
        pred = _predict(
            materialization="incremental",
            on_schema_change="ignore",
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert "NO DDL" in _op_names(pred)

    def test_added_incremental_none_osc_defaults_to_ignore(self):
        """on_schema_change=None → defaults to 'ignore'."""
        pred = _predict(
            materialization="incremental",
            on_schema_change=None,
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.on_schema_change == "ignore"  # normalized

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_added_incremental_non_ignore_safe(self, osc):
        """Added incremental with non-ignore osc → SAFE (no existing table)."""
        pred = _predict(
            materialization="incremental",
            on_schema_change=osc,
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_added_incremental_unknown_osc_safe(self):
        """Added with unknown osc → still SAFE (new model, no existing table)."""
        pred = _predict(
            materialization="incremental",
            on_schema_change="unknown_value",
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE


class TestAddedCustomMaterialization:
    """Custom materialization + added → falls through to incremental path."""

    def test_added_custom_ignore_safe(self):
        pred = _predict(
            materialization="my_custom_mat",
            on_schema_change="ignore",
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert "NO DDL" in _op_names(pred)

    def test_added_custom_sync_safe(self):
        pred = _predict(
            materialization="my_custom_mat",
            on_schema_change="sync_all_columns",
            status="added",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE


# ===========================================================================
# 3. status="modified" × table / view / ephemeral / snapshot
#    (These materializations return before checking columns)
# ===========================================================================


class TestModifiedNonIncremental:
    """Table, view, ephemeral, snapshot don't check columns on modified."""

    def test_modified_table_safe_regardless_of_column_changes(self):
        pred = _predict(
            materialization="table",
            status="modified",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.SAFE
        assert "CREATE OR REPLACE TABLE" in _op_names(pred)

    def test_modified_table_safe_with_parse_failure(self):
        """Table doesn't care about parse failures."""
        pred = _predict(
            materialization="table",
            status="modified",
            base_columns=None,
            current_columns=None,
        )
        assert pred.safety == Safety.SAFE

    def test_modified_view_safe_regardless_of_column_changes(self):
        pred = _predict(
            materialization="view",
            status="modified",
            base_columns=["a"],
            current_columns=["a", "b", "c"],
        )
        assert pred.safety == Safety.SAFE
        assert "CREATE OR REPLACE VIEW" in _op_names(pred)

    def test_modified_ephemeral_safe(self):
        pred = _predict(
            materialization="ephemeral",
            status="modified",
            base_columns=["a"],
            current_columns=["b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_modified_snapshot_warning(self):
        pred = _predict(
            materialization="snapshot",
            status="modified",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.WARNING
        assert any("snapshot" in op.lower() for op in _op_names(pred))


# ===========================================================================
# 4. status="modified", incremental, osc="ignore"
# ===========================================================================


class TestModifiedIncrementalIgnore:
    def test_ignore_safe_even_with_column_changes(self):
        pred = _predict(
            on_schema_change="ignore",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.SAFE
        assert "NO DDL" in _op_names(pred)

    def test_none_osc_defaults_to_ignore(self):
        """on_schema_change=None → 'ignore', stored in result."""
        pred = _predict(
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["x"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.on_schema_change == "ignore"


# ===========================================================================
# 5. Parse failure scenarios (base=None / current=None / both=None)
#    Only reached for incremental with osc != "ignore" and status="modified"
# ===========================================================================


class TestParseFailure:
    """Parse failure: None columns → WARNING (never safe)."""

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_base_none_current_valid(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=None,
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert "REVIEW REQUIRED" in _op_names(pred)

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_base_valid_current_none(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b"],
            current_columns=None,
        )
        assert pred.safety == Safety.WARNING
        assert "REVIEW REQUIRED" in _op_names(pred)

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_both_none(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=None,
            current_columns=None,
        )
        assert pred.safety == Safety.WARNING

    def test_unknown_osc_with_parse_failure(self):
        """Unknown osc + parse failure → WARNING (parse failure branch wins)."""
        pred = _predict(
            on_schema_change="unknown_value",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING
        assert "REVIEW REQUIRED" in _op_names(pred)


# ===========================================================================
# 6. SELECT * scenarios
# ===========================================================================


class TestSelectStar:
    """SELECT * on either side → WARNING."""

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_base_star_current_valid(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["*"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert any("SELECT *" in op for op in _op_names(pred))

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_base_valid_current_star(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b"],
            current_columns=["*"],
        )
        assert pred.safety == Safety.WARNING
        assert any("SELECT *" in op for op in _op_names(pred))

    @pytest.mark.parametrize("osc", ["fail", "append_new_columns", "sync_all_columns"])
    def test_both_star(self, osc):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["*"],
            current_columns=["*"],
        )
        assert pred.safety == Safety.WARNING
        assert any("SELECT *" in op for op in _op_names(pred))


# ===========================================================================
# 7. SELECT * EXCEPT scenarios
# ===========================================================================


class TestStarExcept:
    """SELECT * EXCEPT(...) sentinel handling."""

    def test_both_star_except_same_exclusions(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["* except(a)"],
            current_columns=["* except(a)"],
        )
        assert pred.safety == Safety.WARNING
        assert any("same exclusions" in op for op in _op_names(pred))

    def test_both_star_except_different_exclusions(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["* except(a)"],
            current_columns=["* except(b)"],
        )
        assert pred.safety == Safety.WARNING
        assert any("exclusions changed" in op for op in _op_names(pred))

    def test_base_star_except_current_explicit(self):
        """One side star except, other explicit → column removal likely."""
        pred = _predict(
            on_schema_change="fail",
            base_columns=["* except(a)"],
            current_columns=["x", "y"],
        )
        assert pred.safety == Safety.WARNING
        assert any("column removal likely" in op for op in _op_names(pred))

    def test_base_explicit_current_star_except(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["x", "y"],
            current_columns=["* except(a)"],
        )
        assert pred.safety == Safety.WARNING
        assert any("column removal likely" in op for op in _op_names(pred))

    def test_base_plain_star_current_star_except(self):
        """base=["*"] hits the SELECT * branch BEFORE star-except."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["*"],
            current_columns=["* except(a)"],
        )
        assert pred.safety == Safety.WARNING
        # This hits the SELECT * branch (line 160), not the star-except branch
        assert any("SELECT *" in op for op in _op_names(pred))

    def test_base_star_except_current_plain_star(self):
        """current=["*"] hits SELECT * branch first."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["* except(a)"],
            current_columns=["*"],
        )
        assert pred.safety == Safety.WARNING
        assert any("SELECT *" in op for op in _op_names(pred))

    def test_star_except_with_multiple_exclusions(self):
        """Star except with multiple excluded columns."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["* except(a, b, c)"],
            current_columns=["* except(a, b, c)"],
        )
        assert pred.safety == Safety.WARNING
        assert any("same exclusions" in op for op in _op_names(pred))

    def test_star_except_across_different_osc(self):
        """Star except behavior is the same regardless of osc (it's checked before osc)."""
        for osc in ["fail", "append_new_columns", "sync_all_columns", "unknown_value"]:
            pred = _predict(
                on_schema_change=osc,
                base_columns=["* except(a)"],
                current_columns=["* except(b)"],
            )
            assert pred.safety == Safety.WARNING


# ===========================================================================
# 8. Duplicate column scenarios
# ===========================================================================


class TestDuplicateColumns:
    """Duplicate columns → WARNING (set diff unreliable)."""

    def test_base_has_duplicates(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["id", "id", "name"],
            current_columns=["id", "name"],
        )
        assert pred.safety == Safety.WARNING
        assert any("duplicate" in op.lower() for op in _op_names(pred))

    def test_current_has_duplicates(self):
        pred = _predict(
            on_schema_change="fail",
            base_columns=["id", "name"],
            current_columns=["id", "id", "name"],
        )
        assert pred.safety == Safety.WARNING
        assert any("duplicate" in op.lower() for op in _op_names(pred))

    def test_both_have_duplicates(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a", "a"],
            current_columns=["b", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert any("duplicate" in op.lower() for op in _op_names(pred))

    def test_duplicates_never_produce_safe(self):
        """Core principle: duplicates must never return SAFE."""
        for osc in ["fail", "append_new_columns", "sync_all_columns"]:
            pred = _predict(
                on_schema_change=osc,
                base_columns=["a", "a"],
                current_columns=["a"],
            )
            assert pred.safety != Safety.SAFE, f"SAFE with duplicates for osc={osc}"


# ===========================================================================
# 9. osc="fail" × column scenarios
# ===========================================================================


class TestOscFail:
    """on_schema_change='fail': columns changed → BUILD FAILURE WARNING."""

    def test_identical_columns_safe(self):
        pred = _predict(
            on_schema_change="fail",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_columns_added_only(self):
        pred = _predict(
            on_schema_change="fail",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert "BUILD FAILURE" in _op_names(pred)
        assert pred.columns_added == ["b"]
        assert pred.columns_removed == []

    def test_columns_removed_only(self):
        pred = _predict(
            on_schema_change="fail",
            base_columns=["a", "b"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING
        assert "BUILD FAILURE" in _op_names(pred)
        assert pred.columns_added == []
        assert pred.columns_removed == ["b"]

    def test_columns_added_and_removed(self):
        pred = _predict(
            on_schema_change="fail",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.WARNING
        assert "BUILD FAILURE" in _op_names(pred)
        assert pred.columns_added == ["c"]
        assert pred.columns_removed == ["b"]

    def test_columns_reordered_only_is_safe(self):
        """fail + reorder only → SAFE (set comparison, order doesn't matter)."""
        pred = _predict(
            on_schema_change="fail",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []


# ===========================================================================
# 10. osc="append_new_columns" × column scenarios
# ===========================================================================


class TestOscAppendNewColumns:
    """append_new_columns: ADD COLUMN for new, stale warning for removed."""

    def test_identical_columns_safe(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []  # no ADD ops when nothing added

    def test_columns_added_only_safe(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a"],
            current_columns=["a", "b", "c"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.columns_added == ["b", "c"]
        assert pred.columns_removed == []
        add_ops = [op for op in pred.operations if op.operation == "ADD COLUMN"]
        assert len(add_ops) == 2
        assert {op.column for op in add_ops} == {"b", "c"}

    def test_columns_removed_only_warning(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING
        assert pred.columns_removed == ["b", "c"]
        assert any("STALE" in op for op in _op_names(pred))

    def test_columns_added_and_removed_warning(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.WARNING
        assert pred.columns_added == ["c"]
        assert pred.columns_removed == ["b"]
        assert any(op.operation == "ADD COLUMN" for op in pred.operations)
        assert any("STALE" in op for op in _op_names(pred))

    def test_columns_reordered_only_safe(self):
        """append + reorder only → SAFE (set comparison, no diff)."""
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "b", "a"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []


# ===========================================================================
# 11. osc="sync_all_columns" × column scenarios
# ===========================================================================


class TestOscSyncAllColumns:
    """sync_all_columns: ADD + DROP, destructive if removed, reorder detection."""

    def test_identical_columns_safe(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_columns_added_only_safe(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.columns_added == ["b"]
        assert pred.columns_removed == []
        add_ops = [op for op in pred.operations if op.operation == "ADD COLUMN"]
        assert len(add_ops) == 1
        assert add_ops[0].column == "b"

    def test_columns_removed_only_destructive(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert sorted(pred.columns_removed) == ["b", "c"]
        drop_ops = [op for op in pred.operations if op.operation == "DROP COLUMN"]
        assert len(drop_ops) == 2

    def test_columns_added_and_removed_destructive(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert pred.columns_added == ["c"]
        assert pred.columns_removed == ["b"]
        assert any(op.operation == "ADD COLUMN" for op in pred.operations)
        assert any(op.operation == "DROP COLUMN" for op in pred.operations)

    def test_columns_reordered_warning(self):
        """Same column set, different order → WARNING + COLUMNS REORDERED."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert "COLUMNS REORDERED" in _op_names(pred)
        # Should NOT have ADD/DROP ops
        assert not any(op.operation in ("ADD COLUMN", "DROP COLUMN") for op in pred.operations)

    def test_single_column_no_change(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []


# ===========================================================================
# 12. Unknown on_schema_change
# ===========================================================================


class TestUnknownOsc:
    """Unknown on_schema_change → WARNING with descriptive operation."""

    def test_unknown_osc_with_no_column_changes(self):
        pred = _predict(
            on_schema_change="custom_future_value",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING
        ops = _op_names(pred)
        assert any("UNKNOWN" in op for op in ops)
        assert any("custom_future_value" in op for op in ops)

    def test_unknown_osc_with_column_changes(self):
        pred = _predict(
            on_schema_change="new_strategy",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert any("UNKNOWN" in op for op in _op_names(pred))

    def test_unknown_osc_with_columns_removed(self):
        pred = _predict(
            on_schema_change="weird_value",
            base_columns=["a", "b"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING

    def test_unknown_osc_exact_message(self):
        pred = _predict(
            on_schema_change="xyz_mode",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert any(op == "UNKNOWN on_schema_change: xyz_mode" for op in _op_names(pred))


# ===========================================================================
# 13. Custom materialization (not in known set) → falls through to incremental
# ===========================================================================


class TestCustomMaterialization:
    """Any materialization not in {table, view, ephemeral, snapshot} uses incremental path."""

    def test_custom_mat_ignore_safe(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="ignore",
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == Safety.SAFE
        assert "NO DDL" in _op_names(pred)

    def test_custom_mat_fail_with_changes(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="fail",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING
        assert "BUILD FAILURE" in _op_names(pred)

    def test_custom_mat_sync_with_removal(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert any(op.operation == "DROP COLUMN" for op in pred.operations)

    def test_custom_mat_append_with_addition(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="append_new_columns",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert any(op.operation == "ADD COLUMN" for op in pred.operations)

    def test_custom_mat_none_osc_defaults_ignore(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=["x"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.on_schema_change == "ignore"

    def test_custom_mat_parse_failure_warning(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="sync_all_columns",
            base_columns=None,
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING

    def test_custom_mat_select_star_warning(self):
        pred = _predict(
            materialization="custom_materialization",
            on_schema_change="fail",
            base_columns=["*"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING
        assert any("SELECT *" in op for op in _op_names(pred))


# ===========================================================================
# 14. Edge cases and field correctness
# ===========================================================================


class TestFieldCorrectness:
    """Verify all DDLPrediction fields are populated correctly."""

    def test_model_name_preserved(self):
        pred = _predict(model_name="my_model")
        assert pred.model_name == "my_model"

    def test_materialization_preserved(self):
        pred = _predict(materialization="incremental")
        assert pred.materialization == "incremental"

    def test_osc_normalized_for_incremental(self):
        """None osc is normalized to 'ignore' for incremental path."""
        pred = _predict(
            materialization="incremental",
            on_schema_change=None,
            base_columns=["a"],
            current_columns=["a"],
        )
        assert pred.on_schema_change == "ignore"

    def test_osc_not_normalized_for_table(self):
        """Table materialization returns before normalizing osc."""
        pred = _predict(materialization="table", on_schema_change=None)
        assert pred.on_schema_change is None

    def test_osc_not_normalized_for_view(self):
        pred = _predict(materialization="view", on_schema_change=None)
        assert pred.on_schema_change is None

    def test_osc_not_normalized_for_ephemeral(self):
        pred = _predict(materialization="ephemeral", on_schema_change=None)
        assert pred.on_schema_change is None

    def test_osc_not_normalized_for_snapshot(self):
        pred = _predict(materialization="snapshot", on_schema_change=None)
        assert pred.on_schema_change is None

    def test_columns_added_sorted(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a"],
            current_columns=["a", "c", "b"],
        )
        assert pred.columns_added == ["b", "c"]

    def test_columns_removed_sorted(self):
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "c", "b"],
            current_columns=["a"],
        )
        assert pred.columns_removed == ["b", "c"]

    def test_default_downstream_impacts_empty(self):
        pred = _predict(
            materialization="table",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert pred.downstream_impacts == []

    def test_default_status_is_modified(self):
        """Default status parameter is 'modified'."""
        pred = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change="fail",
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.WARNING  # would be SAFE if status=added


# ===========================================================================
# 15. Full matrix: incremental × osc × all column scenarios
#     Validates exhaustive combination coverage
# ===========================================================================


class TestFullIncrementalMatrix:
    """Cross-product of osc × column scenario for incremental modified."""

    # -- Scenario: empty column lists (both have columns, but identical) --

    @pytest.mark.parametrize(
        "osc,expected_safety",
        [
            ("fail", Safety.SAFE),
            ("append_new_columns", Safety.SAFE),
            ("sync_all_columns", Safety.SAFE),
        ],
    )
    def test_no_change(self, osc, expected_safety):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b"],
            current_columns=["a", "b"],
        )
        assert pred.safety == expected_safety
        assert pred.columns_added == []
        assert pred.columns_removed == []

    # -- Scenario: add only --

    @pytest.mark.parametrize(
        "osc,expected_safety",
        [
            ("fail", Safety.WARNING),
            ("append_new_columns", Safety.SAFE),
            ("sync_all_columns", Safety.SAFE),
        ],
    )
    def test_add_only(self, osc, expected_safety):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a"],
            current_columns=["a", "b"],
        )
        assert pred.safety == expected_safety
        assert pred.columns_added == ["b"]
        assert pred.columns_removed == []

    # -- Scenario: remove only --

    @pytest.mark.parametrize(
        "osc,expected_safety",
        [
            ("fail", Safety.WARNING),
            ("append_new_columns", Safety.WARNING),
            ("sync_all_columns", Safety.DESTRUCTIVE),
        ],
    )
    def test_remove_only(self, osc, expected_safety):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b"],
            current_columns=["a"],
        )
        assert pred.safety == expected_safety
        assert pred.columns_removed == ["b"]

    # -- Scenario: add + remove --

    @pytest.mark.parametrize(
        "osc,expected_safety",
        [
            ("fail", Safety.WARNING),
            ("append_new_columns", Safety.WARNING),
            ("sync_all_columns", Safety.DESTRUCTIVE),
        ],
    )
    def test_add_and_remove(self, osc, expected_safety):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b"],
            current_columns=["a", "c"],
        )
        assert pred.safety == expected_safety

    # -- Scenario: reorder only --

    @pytest.mark.parametrize(
        "osc,expected_safety",
        [
            ("fail", Safety.SAFE),
            ("append_new_columns", Safety.SAFE),
            ("sync_all_columns", Safety.WARNING),
        ],
    )
    def test_reorder_only(self, osc, expected_safety):
        pred = _predict(
            on_schema_change=osc,
            base_columns=["a", "b", "c"],
            current_columns=["c", "a", "b"],
        )
        assert pred.safety == expected_safety


# ===========================================================================
# 16. Boundary / degenerate cases
# ===========================================================================


class TestBoundaryCases:
    """Edge and degenerate inputs."""

    def test_empty_column_lists(self):
        """Both empty lists → no diff, SAFE."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=[],
            current_columns=[],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_base_empty_current_has_columns(self):
        """Empty base, columns in current → all added."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=[],
            current_columns=["a", "b"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.columns_added == ["a", "b"]

    def test_base_has_columns_current_empty(self):
        """Columns in base, empty current → all removed."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "b"],
            current_columns=[],
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert sorted(pred.columns_removed) == ["a", "b"]

    def test_single_column_added(self):
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=[],
            current_columns=["new_col"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.columns_added == ["new_col"]

    def test_many_columns_performance(self):
        """Large column lists work correctly."""
        base = [f"col_{i}" for i in range(100)]
        current = [f"col_{i}" for i in range(50, 150)]  # 50 removed, 50 added
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=base,
            current_columns=current,
        )
        assert pred.safety == Safety.DESTRUCTIVE
        assert len(pred.columns_removed) == 50
        assert len(pred.columns_added) == 50

    def test_star_is_only_element(self):
        """["*"] specifically — not ["*", "a"]."""
        pred = _predict(
            on_schema_change="fail",
            base_columns=["*"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.WARNING

    def test_star_in_multi_element_list_not_caught(self):
        """["*", "a"] is NOT treated as SELECT * — it's treated as column names."""
        pred = _predict(
            on_schema_change="fail",
            base_columns=["*", "a"],
            current_columns=["a"],
        )
        # "*" is just a regular column name here, not the SELECT * sentinel
        # It should be treated as a removed column named "*"
        assert pred.safety == Safety.WARNING
        assert "*" in pred.columns_removed

    def test_star_except_not_in_single_element(self):
        """["* except(a)", "b"] — multi-element, star-except check skipped."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["* except(a)", "b"],
            current_columns=["c"],
        )
        # Not treated as star-except because len > 1
        # Regular diff: removed=["* except(a)", "b"], added=["c"]
        assert pred.safety == Safety.DESTRUCTIVE

    def test_append_no_columns_no_ops(self):
        """append_new_columns with no changes → no operations."""
        pred = _predict(
            on_schema_change="append_new_columns",
            base_columns=["a"],
            current_columns=["a"],
        )
        assert pred.safety == Safety.SAFE
        assert pred.operations == []

    def test_sync_add_column_has_column_name(self):
        """ADD COLUMN operations carry the column name."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a"],
            current_columns=["a", "new_col"],
        )
        add_ops = [op for op in pred.operations if op.operation == "ADD COLUMN"]
        assert len(add_ops) == 1
        assert add_ops[0].column == "new_col"

    def test_sync_drop_column_has_column_name(self):
        """DROP COLUMN operations carry the column name."""
        pred = _predict(
            on_schema_change="sync_all_columns",
            base_columns=["a", "old_col"],
            current_columns=["a"],
        )
        drop_ops = [op for op in pred.operations if op.operation == "DROP COLUMN"]
        assert len(drop_ops) == 1
        assert drop_ops[0].column == "old_col"


# ===========================================================================
# 17. DDLPrediction / DDLOperation frozen dataclass behavior
# ===========================================================================


class TestDataclassProperties:
    """Verify frozen dataclass constraints and defaults."""

    def test_prediction_is_frozen(self):
        pred = _predict(materialization="table")
        with pytest.raises(AttributeError):
            pred.safety = Safety.WARNING  # type: ignore[misc]

    def test_operation_is_frozen(self):
        op = DDLOperation("ADD COLUMN", "col")
        with pytest.raises(AttributeError):
            op.operation = "DROP COLUMN"  # type: ignore[misc]

    def test_operation_column_default_none(self):
        op = DDLOperation("NO DDL")
        assert op.column is None

    def test_prediction_defaults(self):
        pred = DDLPrediction(
            model_name="m",
            materialization="table",
            on_schema_change=None,
            safety=Safety.SAFE,
        )
        assert pred.operations == []
        assert pred.columns_added == []
        assert pred.columns_removed == []
        assert pred.downstream_impacts == []
