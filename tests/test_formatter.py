"""Tests for text, github, and json formatters."""

import json

from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety


class TestFormatText:
    def test_destructive_output(self):
        """Destructive prediction shows DROP COLUMN in terminal output."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="int_order_enriched",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE,
                    operations=[
                        DDLOperation("DROP COLUMN", "shipping_info"),
                        DDLOperation("ADD COLUMN", "shipping_city"),
                    ],
                    columns_added=["shipping_city"],
                    columns_removed=["shipping_info"],
                ),
            ],
            downstream_map={"int_order_enriched": ["dim_customers"]},
            parse_failures=[],
        )
        output = format_text(result)
        assert "DESTRUCTIVE" in output
        assert "int_order_enriched" in output
        assert "DROP COLUMN" in output
        assert "shipping_info" in output
        assert "dim_customers" in output

    def test_empty_no_changes(self):
        """No predictions → 'no model changes detected'."""
        result = CheckResult(predictions=[], downstream_map={}, parse_failures=[])
        output = format_text(result)
        assert "no model changes detected" in output


class TestFormatGithub:
    def test_safe_markdown(self):
        """Safe prediction uses markdown formatting."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="dim_customers",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
            downstream_map={},
            parse_failures=[],
        )
        output = format_github(result)
        assert "SAFE" in output
        assert "dim_customers" in output
        assert "CREATE OR REPLACE TABLE" in output


class TestFormatTextColor:
    def test_color_enabled_includes_ansi_codes(self):
        """color=True wraps safety labels with ANSI escape codes."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
        )
        output = format_text(result, color=True)
        assert "\033[32m" in output  # green
        assert "\033[1m" in output  # bold
        assert "\033[0m" in output  # reset

    def test_color_disabled_no_ansi_codes(self):
        """color=False produces plain text without ANSI escape codes."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.DESTRUCTIVE,
                    operations=[DDLOperation("MODEL REMOVED")],
                ),
            ],
        )
        output = format_text(result, color=False)
        assert "\033[" not in output
        assert "DESTRUCTIVE" in output


class TestFormatTextSkippedModels:
    def test_skipped_models_warning_shown(self):
        """Skipped models produce a WARNING line in text output."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
            skipped_models=["orphan_model", "unknown_model"],
        )
        output = format_text(result, color=False)
        assert "WARNING" in output
        assert "Skipped 2 model(s)" in output
        assert "orphan_model" in output
        assert "unknown_model" in output


class TestFormatTextParseFailuresColor:
    def test_parse_failures_warning_with_color(self):
        """Parse failures WARNING is colored when color=True."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.WARNING,
                    operations=[DDLOperation("REVIEW REQUIRED")],
                ),
            ],
            parse_failures=["broken_model"],
        )
        output = format_text(result, color=True)
        assert "\033[33m" in output  # yellow for WARNING
        assert "broken_model" in output
        assert "manual review required" in output


class TestFormatGithubParseFailures:
    def test_parse_failures_warning_in_github(self):
        """Parse failures produce a blockquote WARNING in github output."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.WARNING,
                ),
            ],
            parse_failures=["broken_sql_model"],
        )
        output = format_github(result)
        assert "> **WARNING**: Could not extract columns" in output
        assert "broken_sql_model" in output


class TestFormatGithubSkippedModels:
    def test_skipped_models_warning_in_github(self):
        """Skipped models produce a blockquote WARNING in github output."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="m",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
            skipped_models=["orphan_model"],
        )
        output = format_github(result)
        assert "> **WARNING**: Skipped 1 model(s)" in output
        assert "orphan_model" in output


class TestFormatJson:
    def test_json_output_structure(self):
        """JSON output has summary, models, parse_failures, skipped_models."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="int_order_enriched",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE,
                    operations=[
                        DDLOperation("DROP COLUMN", "old_col"),
                        DDLOperation("ADD COLUMN", "new_col"),
                    ],
                    columns_added=["new_col"],
                    columns_removed=["old_col"],
                ),
                DDLPrediction(
                    model_name="dim_customers",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
            downstream_map={"int_order_enriched": ["dim_customers"]},
            parse_failures=["broken_model"],
            skipped_models=["unknown_model"],
        )
        raw = format_json(result)
        data = json.loads(raw)

        assert data["summary"]["total"] == 2
        assert data["summary"]["destructive"] == 1
        assert data["summary"]["safe"] == 1
        assert len(data["models"]) == 2
        assert data["models"][0]["model_name"] == "int_order_enriched"
        assert data["models"][0]["safety"] == "destructive"
        assert data["models"][0]["downstream"] == ["dim_customers"]
        assert data["parse_failures"] == ["broken_model"]
        assert data["skipped_models"] == ["unknown_model"]

    def test_json_empty_result(self):
        """Empty result produces valid JSON with zero counts."""
        result = CheckResult()
        raw = format_json(result)
        data = json.loads(raw)
        assert data["summary"]["total"] == 0
        assert data["models"] == []


class TestCascadeImpactFormatting:
    """Tests for cascade impact output in all three formatters."""

    def _make_cascade_result(self):
        impact = DownstreamImpact(
            model_name="fct_metrics",
            materialization="incremental",
            on_schema_change="ignore",
            risk="broken_ref",
            reason="references dropped column(s): old_col",
        )
        return CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="int_order_enriched",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE,
                    operations=[DDLOperation("DROP COLUMN", "old_col")],
                    columns_removed=["old_col"],
                    downstream_impacts=[impact],
                ),
            ],
            downstream_map={"int_order_enriched": ["fct_metrics", "dim_customers"]},
        )

    def test_text_shows_cascade_broken_ref(self):
        """Text format shows >> BROKEN_REF line."""
        result = self._make_cascade_result()
        output = format_text(result, color=False)
        assert "BROKEN_REF" in output
        assert "fct_metrics" in output
        assert "references dropped column(s): old_col" in output

    def test_github_shows_cascade_broken_ref(self):
        """GitHub format shows risk icon and bold label."""
        result = self._make_cascade_result()
        output = format_github(result)
        assert "**BROKEN_REF**" in output
        assert "fct_metrics" in output

    def test_json_includes_downstream_impacts(self):
        """JSON format includes downstream_impacts array."""
        result = self._make_cascade_result()
        raw = format_json(result)
        data = json.loads(raw)
        model = data["models"][0]
        assert "downstream_impacts" in model
        assert len(model["downstream_impacts"]) == 1
        assert model["downstream_impacts"][0]["risk"] == "broken_ref"
        assert model["downstream_impacts"][0]["model_name"] == "fct_metrics"

    def test_summary_line_includes_cascade_count(self):
        """Summary line shows cascade risk count when present."""
        result = self._make_cascade_result()
        output = format_text(result, color=False)
        assert "1 cascade risk(s)" in output

    def test_summary_line_no_cascade_when_none(self):
        """Summary line omits cascade count when there are no cascade risks."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="safe_model",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                ),
            ],
        )
        output = format_text(result, color=False)
        assert "cascade" not in output

    def test_json_includes_cascade_risks_in_summary(self):
        """JSON summary includes cascade_risks count when present."""
        result = self._make_cascade_result()
        raw = format_json(result)
        data = json.loads(raw)
        assert data["summary"]["cascade_risks"] == 1

    def test_json_no_cascade_risks_key_when_none(self):
        """JSON summary omits cascade_risks key when there are no cascade risks."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="safe_model",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                ),
            ],
        )
        raw = format_json(result)
        data = json.loads(raw)
        assert "cascade_risks" not in data["summary"]

    def test_downstream_list_truncated_when_many(self):
        """Long downstream lists are truncated for readability."""
        downstream = [f"model_{i}" for i in range(20)]
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="parent",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
            downstream_map={"parent": downstream},
        )
        # Text format
        text = format_text(result, color=False)
        assert "... and 15 more" in text
        assert "(20 model(s))" in text
        # No single line should exceed 120 chars (reasonable terminal width)
        for line in text.splitlines():
            assert len(line) <= 120, f"Line too long ({len(line)} chars): {line[:80]}..."

        # GitHub format
        gh = format_github(result)
        assert "... and 15 more" in gh

    def test_downstream_list_not_truncated_when_few(self):
        """Short downstream lists show all names."""
        downstream = ["model_a", "model_b", "model_c"]
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="parent",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                ),
            ],
            downstream_map={"parent": downstream},
        )
        text = format_text(result, color=False)
        assert "model_a, model_b, model_c" in text
        assert "more" not in text

    def test_text_shows_build_failure(self):
        """Text format shows >> BUILD_FAILURE for incremental+fail downstream."""
        impact = DownstreamImpact(
            model_name="fct_daily",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream schema changed, on_schema_change=fail",
        )
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="parent",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.WARNING,
                    downstream_impacts=[impact],
                ),
            ],
            downstream_map={"parent": ["fct_daily"]},
        )
        output = format_text(result, color=False)
        assert "BUILD_FAILURE" in output
        assert "fct_daily" in output
