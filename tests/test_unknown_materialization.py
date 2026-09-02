"""A materialization dbt-plan does not model cannot be reported safe.

`predict_ddl` handles table, view, ephemeral and snapshot by name and then falls
through to the incremental branch. Anything else landed there with
`on_schema_change` defaulting to "ignore", which returns SAFE / NO DDL — so a
`materialized_view` dropping a column reported clean.

Two things are wrong with that. The verdict is a false all-clear, which is the
one this tool exists to prevent. And `on_schema_change` does not even apply to a
materialized view: dbt drives those with `on_configuration_change`, so the branch
was reasoning with a setting that has no meaning for the object in front of it.

Note the asymmetry this fixes. An unrecognized *on_schema_change* already
returned WARNING with `UNKNOWN on_schema_change: <value>`. An unrecognized
*materialization* did not.
"""

from __future__ import annotations

import pytest

from dbt_plan.predictor import Safety, predict_ddl

REMOVED = (["order_id", "customer_id"], ["order_id"])
UNCHANGED = (["order_id", "customer_id"], ["order_id", "customer_id"])
ADDED = (["order_id"], ["order_id", "customer_id"])


def verdict(materialization, columns=REMOVED, osc=None):
    base, current = columns
    return predict_ddl("m", materialization, osc, base, current, status="modified")


class TestTheFalseSafe:
    @pytest.mark.parametrize(
        "materialization",
        ["materialized_view", "dynamic_table", "external", "my_custom_materialization"],
    )
    def test_a_dropped_column_is_never_safe(self, materialization):
        assert verdict(materialization).safety != Safety.SAFE

    def test_the_reason_names_the_materialization(self):
        ops = [op.operation for op in verdict("my_custom_materialization").operations]
        assert any("my_custom_materialization" in op for op in ops)

    def test_the_column_diff_is_still_reported(self):
        """A bare "unknown" tells a reviewer nothing about what to look at."""
        result = verdict("dynamic_table")
        assert result.columns_removed == ["customer_id"]


class TestMaterializedViewIsNamedSpecifically:
    """dbt drives materialized views with on_configuration_change, not
    on_schema_change, and its docs do not say what a column change does. Saying
    that is more useful than a generic "unknown"."""

    def test_the_message_points_at_on_configuration_change(self):
        ops = [op.operation for op in verdict("materialized_view").operations]
        assert any("on_configuration_change" in op for op in ops)

    def test_it_is_a_warning_not_destructive(self):
        """Unknown means unknown. Claiming destructive would be a guess too."""
        assert verdict("materialized_view").safety == Safety.WARNING


class TestConsistencyWithTheExistingRules:
    def test_an_unknown_materialization_warns_like_an_unknown_osc(self):
        unknown_mat = verdict("dynamic_table")
        unknown_osc = verdict("incremental", osc="microbatch")
        assert unknown_mat.safety == unknown_osc.safety == Safety.WARNING

    @pytest.mark.parametrize("columns", [REMOVED, UNCHANGED, ADDED])
    def test_it_warns_regardless_of_the_column_diff(self, columns):
        """We do not know what DDL it emits, so no column diff makes it safe.

        This matches how `snapshot` is already treated. Whether that blanket
        treatment is too coarse is tracked separately in #30.
        """
        assert verdict("dynamic_table", columns=columns).safety == Safety.WARNING


class TestAnExplicitSettingIsStillHonoured:
    """The absence of on_schema_change is what was unsafe, not its presence.

    Setting it on a custom materialization is an assertion by the author about
    how their materialization behaves. Honouring it is strictly more useful than
    refusing: it keeps a DESTRUCTIVE verdict that refusing would have downgraded
    to a warning. `tests/test_predict_exhaustive.py::TestCustomMaterialization`
    pinned this deliberately and still passes.
    """

    def test_sync_all_columns_on_a_custom_materialization_stays_destructive(self):
        assert verdict("custom_materialization", osc="sync_all_columns").safety == (
            Safety.DESTRUCTIVE
        )

    def test_an_explicit_ignore_is_taken_at_its_word(self):
        assert verdict("custom_materialization", osc="ignore").safety == Safety.SAFE

    def test_but_an_absent_setting_is_not_read_as_ignore(self):
        """The exact false safe: None became "ignore" became SAFE."""
        assert verdict("custom_materialization", osc=None).safety == Safety.WARNING

    def test_this_applies_to_materialized_view_too(self):
        assert verdict("materialized_view", osc="sync_all_columns").safety == (Safety.DESTRUCTIVE)


class TestKnownMaterializationsAreUntouched:
    @pytest.mark.parametrize(
        "materialization,expected",
        [
            ("table", Safety.SAFE),
            ("view", Safety.SAFE),
            ("ephemeral", Safety.SAFE),
            ("snapshot", Safety.WARNING),
        ],
    )
    def test_no_new_noise(self, materialization, expected):
        assert verdict(materialization).safety == expected

    def test_incremental_still_reads_on_schema_change(self):
        assert verdict("incremental", osc="sync_all_columns").safety == Safety.DESTRUCTIVE
        assert verdict("incremental", osc="ignore").safety == Safety.SAFE

    def test_a_removed_model_is_still_destructive_whatever_it_was(self):
        result = predict_ddl("m", "materialized_view", None, ["a"], None, status="removed")
        assert result.safety == Safety.DESTRUCTIVE
