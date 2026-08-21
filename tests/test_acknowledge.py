"""Acknowledged models: an escape hatch for intentional destructive changes.

Acknowledging a model does NOT hide it (that is what ignore_models does).
The change is still reported in full; it just stops driving the exit code,
so an intentional DROP COLUMN can land without disabling the check.
"""

from __future__ import annotations

import json

from dbt_plan.config import Config
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety


def _destructive(name="int_orders", cols_removed=("revenue",)):
    return DDLPrediction(
        model_name=name,
        materialization="incremental",
        on_schema_change="sync_all_columns",
        safety=Safety.DESTRUCTIVE,
        operations=[DDLOperation("DROP COLUMN", c) for c in cols_removed],
        columns_removed=list(cols_removed),
    )


def _safe(name="dim_customers"):
    return DDLPrediction(
        model_name=name,
        materialization="table",
        on_schema_change=None,
        safety=Safety.SAFE,
        operations=[DDLOperation("CREATE OR REPLACE TABLE", None)],
    )


class TestConfig:
    def test_defaults_to_empty(self):
        assert Config().acknowledge_models == []

    def test_reads_yaml_key(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [int_orders, fct_orders]\n")
        assert Config.load(tmp_path).acknowledge_models == ["int_orders", "fct_orders"]

    def test_env_var_overrides_file(self, tmp_path, monkeypatch):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [from_file]\n")
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", "from_env,other")
        assert Config.load(tmp_path).acknowledge_models == ["from_env", "other"]

    def test_env_var_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", " a , b ")
        assert Config.load(tmp_path).acknowledge_models == ["a", "b"]

    def test_empty_env_var_is_ignored(self, tmp_path, monkeypatch):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [from_file]\n")
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", "")
        assert Config.load(tmp_path).acknowledge_models == ["from_file"]


class TestTextOutput:
    def test_marks_the_model(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        out = format_text(r, color=False)
        assert "ACKNOWLEDGED" in out
        # still reported in full -- acknowledging is not hiding
        assert "int_orders" in out
        assert "DROP COLUMN  revenue" in out

    def test_unacknowledged_model_is_not_marked(self):
        out = format_text(CheckResult([_destructive()]), color=False)
        assert "ACKNOWLEDGED" not in out

    def test_summary_counts_acknowledged_separately(self):
        r = CheckResult([_destructive(), _safe()], acknowledge_models=["int_orders"])
        out = format_text(r, color=False)
        assert "1 destructive (1 acknowledged)" in out

    def test_summary_omits_note_when_none_acknowledged(self):
        out = format_text(CheckResult([_destructive(), _safe()]), color=False)
        assert "acknowledged" not in out

    def test_acknowledging_absent_model_changes_nothing(self):
        r = CheckResult([_destructive()], acknowledge_models=["some_other_model"])
        assert "ACKNOWLEDGED" not in format_text(r, color=False)


class TestGithubOutput:
    def test_marks_the_model(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert "ACKNOWLEDGED" in format_github(r)


class TestJsonOutput:
    def test_flags_the_model(self):
        r = CheckResult([_destructive(), _safe()], acknowledge_models=["int_orders"])
        data = json.loads(format_json(r))
        by_name = {m["model_name"]: m for m in data["models"]}
        assert by_name["int_orders"]["acknowledged"] is True
        assert by_name["dim_customers"]["acknowledged"] is False

    def test_summary_reports_count(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert json.loads(format_json(r))["summary"]["acknowledged"] == 1

    def test_safety_value_is_unchanged(self):
        """Acknowledging is a CI policy, not a re-classification of the risk."""
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        data = json.loads(format_json(r))
        assert data["models"][0]["safety"] == "destructive"
        assert data["summary"]["destructive"] == 1


class TestExitCode:
    """The whole point: an acknowledged destructive change stops failing CI."""

    def test_acknowledged_destructive_exits_zero(self):
        from dbt_plan.cli import _exit_code_for

        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 0

    def test_unacknowledged_destructive_still_exits_one(self):
        from dbt_plan.cli import _exit_code_for

        assert _exit_code_for(CheckResult([_destructive()]), warning_exit_code=2) == 1

    def test_one_acknowledged_does_not_excuse_another(self):
        """Named models only -- a second destructive model still fails the build."""
        from dbt_plan.cli import _exit_code_for

        r = CheckResult(
            [_destructive("int_orders"), _destructive("fct_orders")],
            acknowledge_models=["int_orders"],
        )
        assert _exit_code_for(r, warning_exit_code=2) == 1

    def test_acknowledging_does_not_mask_parse_failures(self):
        from dbt_plan.cli import _exit_code_for

        r = CheckResult(
            [_destructive()], parse_failures=["mystery_model"], acknowledge_models=["int_orders"]
        )
        assert _exit_code_for(r, warning_exit_code=2) == 2

    def test_acknowledging_does_not_mask_an_unrelated_warning(self):
        from dbt_plan.cli import _exit_code_for

        warned = DDLPrediction(
            model_name="snap_x",
            materialization="snapshot",
            on_schema_change=None,
            safety=Safety.WARNING,
        )
        r = CheckResult([_destructive(), warned], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 2

    def test_acknowledged_model_covers_its_own_cascade_block(self):
        """The reviewer approves the model block, cascade lines included."""
        from dbt_plan.cli import _exit_code_for

        pred = DDLPrediction(
            model_name="int_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            safety=Safety.DESTRUCTIVE,
            columns_removed=["revenue"],
            downstream_impacts=[
                DownstreamImpact(
                    model_name="fct_daily_sales",
                    materialization="incremental",
                    on_schema_change="append_new_columns",
                    risk="broken_ref",
                    reason="references dropped column(s): revenue",
                )
            ],
        )
        r = CheckResult([pred], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 0

    def test_safe_only_is_still_zero(self):
        from dbt_plan.cli import _exit_code_for

        assert _exit_code_for(CheckResult([_safe()]), warning_exit_code=2) == 0
