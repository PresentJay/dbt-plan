"""Quality checks for dbt-plan's --format github markdown output as PR comments.

Validates that format_github produces well-structured, readable GitHub-flavored
markdown suitable for posting as a pull request comment.
"""

from __future__ import annotations

from dbt_plan.formatter import CheckResult, format_github, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _complex_scenario() -> CheckResult:
    """Build a realistic PR comment scenario with mixed severity models.

    - 1 destructive model (incremental+sync_all_columns, 3 DROP COLUMNs)
      with cascade impacts: 2 broken_ref + 1 build_failure
    - 1 warning model (snapshot)
    - 2 safe models (table + view)
    - 1 parse failure
    - 1 skipped model
    """
    destructive = DDLPrediction(
        model_name="int_orders_merged",
        materialization="incremental",
        on_schema_change="sync_all_columns",
        safety=Safety.DESTRUCTIVE,
        operations=[
            DDLOperation("DROP COLUMN", "legacy_customer_id"),
            DDLOperation("DROP COLUMN", "old_channel"),
            DDLOperation("DROP COLUMN", "deprecated_flag"),
            DDLOperation("ADD COLUMN", "customer_uuid"),
        ],
        columns_added=["customer_uuid"],
        columns_removed=["legacy_customer_id", "old_channel", "deprecated_flag"],
        downstream_impacts=[
            DownstreamImpact(
                model_name="fct_daily_sales",
                materialization="incremental",
                on_schema_change="ignore",
                risk="broken_ref",
                reason="references dropped column(s): legacy_customer_id",
            ),
            DownstreamImpact(
                model_name="dim_customer_history",
                materialization="table",
                on_schema_change=None,
                risk="broken_ref",
                reason="references dropped column(s): old_channel",
            ),
            DownstreamImpact(
                model_name="fct_hourly_agg",
                materialization="incremental",
                on_schema_change="fail",
                risk="build_failure",
                reason="upstream schema changed, on_schema_change=fail",
            ),
        ],
    )
    warning_snapshot = DDLPrediction(
        model_name="snap_user_status",
        materialization="snapshot",
        on_schema_change=None,
        safety=Safety.WARNING,
        operations=[DDLOperation("REVIEW REQUIRED (snapshot)")],
    )
    safe_table = DDLPrediction(
        model_name="dim_customers",
        materialization="table",
        on_schema_change=None,
        safety=Safety.SAFE,
        operations=[DDLOperation("CREATE OR REPLACE TABLE")],
    )
    safe_view = DDLPrediction(
        model_name="rpt_summary",
        materialization="view",
        on_schema_change=None,
        safety=Safety.SAFE,
        operations=[DDLOperation("CREATE OR REPLACE VIEW")],
    )

    return CheckResult(
        predictions=[destructive, warning_snapshot, safe_table, safe_view],
        downstream_map={
            "int_orders_merged": [
                "fct_daily_sales",
                "dim_customer_history",
                "fct_hourly_agg",
            ],
        },
        parse_failures=["broken_cte_model"],
        skipped_models=["orphan_staging"],
    )


# ---------------------------------------------------------------------------
# 1. Valid markdown structure
# ---------------------------------------------------------------------------


class TestMarkdownStructure:
    """Verify structural correctness of generated GitHub markdown."""

    def test_starts_with_h3_heading(self):
        """Output must begin with a ### heading."""
        result = _complex_scenario()
        output = format_github(result)
        assert output.startswith("### ")

    def test_empty_result_starts_with_heading(self):
        """Empty result also starts with ### heading."""
        output = format_github(CheckResult())
        assert output.startswith("### ")

    def test_destructive_emoji(self):
        """Destructive models use the red circle emoji."""
        result = _complex_scenario()
        output = format_github(result)
        assert "\U0001f534" in output  # red circle

    def test_warning_emoji(self):
        """Warning models use the warning emoji."""
        result = _complex_scenario()
        output = format_github(result)
        assert "\u26a0\ufe0f" in output  # warning sign

    def test_safe_emoji(self):
        """Safe models use the check mark emoji."""
        result = _complex_scenario()
        output = format_github(result)
        assert "\u2705" in output  # check mark

    def test_model_names_in_backticks(self):
        """All model names should appear in backtick code spans."""
        result = _complex_scenario()
        output = format_github(result)
        for name in ["int_orders_merged", "snap_user_status", "dim_customers", "rpt_summary"]:
            assert f"`{name}`" in output, f"Model {name} not in backticks"

    def test_operations_formatted_with_backtick_code(self):
        """Operations like DROP COLUMN should appear in backtick code spans."""
        result = _complex_scenario()
        output = format_github(result)
        # Operations with columns use backtick formatting: `DROP COLUMN` col
        assert "`DROP COLUMN`" in output
        assert "`ADD COLUMN`" in output

    def test_operations_without_column_no_backtick(self):
        """Operations without a column (e.g. REVIEW REQUIRED) are plain list items."""
        result = _complex_scenario()
        output = format_github(result)
        # REVIEW REQUIRED (snapshot) is rendered as a plain list item
        assert "- REVIEW REQUIRED (snapshot)" in output

    def test_no_unbalanced_backticks(self):
        """Count of backticks should be even (all opened are closed)."""
        result = _complex_scenario()
        output = format_github(result)
        backtick_count = output.count("`")
        assert backtick_count % 2 == 0, f"Unbalanced backticks: {backtick_count} total"


# ---------------------------------------------------------------------------
# 2. Summary line format
# ---------------------------------------------------------------------------


class TestSummaryLine:
    """Verify the summary line is in backtick code format on the last line."""

    def test_summary_in_backtick_code(self):
        """Summary line should be wrapped in backticks."""
        result = _complex_scenario()
        output = format_github(result)
        last_line = output.strip().splitlines()[-1]
        assert last_line.startswith("`") and last_line.endswith("`"), (
            f"Summary not in backticks: {last_line}"
        )

    def test_summary_contains_dbt_plan_prefix(self):
        """Summary line must start with 'dbt-plan:' inside the backticks."""
        result = _complex_scenario()
        output = format_github(result)
        last_line = output.strip().splitlines()[-1]
        inner = last_line.strip("`")
        assert inner.startswith("dbt-plan:"), f"Summary doesn't start with dbt-plan: {inner}"

    def test_summary_counts_match(self):
        """Summary counts must match the actual predictions."""
        result = _complex_scenario()
        output = format_github(result)
        last_line = output.strip().splitlines()[-1]
        inner = last_line.strip("`")
        # 4 checked, 2 safe, 1 warning, 1 destructive, 3 cascade risk(s)
        assert "4 checked" in inner
        assert "2 safe" in inner
        assert "1 warning" in inner
        assert "1 destructive" in inner
        assert "3 cascade risk(s)" in inner

    def test_summary_no_cascade_when_absent(self):
        """Summary omits cascade count when no cascade impacts exist."""
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
        output = format_github(result)
        last_line = output.strip().splitlines()[-1]
        assert "cascade" not in last_line


# ---------------------------------------------------------------------------
# 3. Realistic PR comment content
# ---------------------------------------------------------------------------


class TestRealisticPRComment:
    """Verify a complex scenario produces correct, complete output."""

    def test_all_models_appear(self):
        """All 4 models appear in the output."""
        result = _complex_scenario()
        output = format_github(result)
        for name in ["int_orders_merged", "snap_user_status", "dim_customers", "rpt_summary"]:
            assert name in output, f"Model {name} missing from output"

    def test_severity_ordering(self):
        """Models are sorted: destructive first, then warning, then safe."""
        result = _complex_scenario()
        output = format_github(result)
        lines = output.splitlines()
        # Model header lines start with an emoji, not with '>' (blockquote warnings)
        model_lines = [
            ln
            for ln in lines
            if ("**DESTRUCTIVE**" in ln or "**WARNING**" in ln or "**SAFE**" in ln)
            and not ln.startswith(">")
        ]
        assert len(model_lines) == 4
        assert "DESTRUCTIVE" in model_lines[0]
        assert "WARNING" in model_lines[1]
        # The last two should be SAFE
        assert "SAFE" in model_lines[2]
        assert "SAFE" in model_lines[3]

    def test_cascade_broken_ref_emoji(self):
        """broken_ref cascade impacts use red circle emoji."""
        result = _complex_scenario()
        output = format_github(result)
        # Find lines with BROKEN_REF
        lines = [ln for ln in output.splitlines() if "BROKEN_REF" in ln]
        assert len(lines) == 2
        for line in lines:
            assert "\U0001f534" in line, f"broken_ref line missing red circle: {line}"

    def test_cascade_build_failure_emoji(self):
        """build_failure cascade impacts use warning emoji."""
        result = _complex_scenario()
        output = format_github(result)
        lines = [ln for ln in output.splitlines() if "BUILD_FAILURE" in ln]
        assert len(lines) == 1
        assert "\u26a0\ufe0f" in lines[0], f"build_failure line missing warning emoji: {lines[0]}"

    def test_parse_failure_blockquote(self):
        """Parse failures appear as > blockquote WARNING."""
        result = _complex_scenario()
        output = format_github(result)
        assert "> **WARNING**: Could not extract columns for: broken_cte_model" in output

    def test_skipped_models_blockquote(self):
        """Skipped models appear as > blockquote WARNING."""
        result = _complex_scenario()
        output = format_github(result)
        assert "> **WARNING**: Skipped 1 model(s) not found in manifest: orphan_staging" in output

    def test_heading_count_matches(self):
        """The heading line mentions the correct total count."""
        result = _complex_scenario()
        output = format_github(result)
        first_line = output.splitlines()[0]
        assert "4 model(s) changed" in first_line


# ---------------------------------------------------------------------------
# 4. Edge case: long and special model names
# ---------------------------------------------------------------------------


class TestEdgeCaseModelNames:
    """Model names that could break markdown formatting."""

    def test_very_long_model_name(self):
        """100-character model name should not break output."""
        long_name = "a" * 100
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name=long_name,
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
        )
        output = format_github(result)
        assert f"`{long_name}`" in output
        # Output should still be valid (not crash, contain heading and summary)
        assert output.startswith("### ")
        assert output.strip().splitlines()[-1].startswith("`dbt-plan:")

    def test_model_name_with_asterisk(self):
        """Model name containing * should be safely rendered in backticks."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="model_*_test",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
        )
        output = format_github(result)
        # Inside backticks, * is safe from markdown bold interpretation
        assert "`model_*_test`" in output

    def test_model_name_with_pipe(self):
        """Model name containing | should be safely rendered."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="model|pipe",
                    materialization="view",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE VIEW")],
                ),
            ],
        )
        output = format_github(result)
        assert "`model|pipe`" in output

    def test_model_name_with_brackets(self):
        """Model name containing [] should be safely rendered."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="model[bracket]",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                ),
            ],
        )
        output = format_github(result)
        assert "`model[bracket]`" in output


# ---------------------------------------------------------------------------
# 5. Downstream list formatting
# ---------------------------------------------------------------------------


class TestDownstreamListGithub:
    """Downstream list rendering in github format."""

    def test_three_downstream_shows_all(self):
        """3 downstream models should all appear by name."""
        downstream = ["model_a", "model_b", "model_c"]
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
        output = format_github(result)
        assert "model_a, model_b, model_c" in output
        assert "(3 model(s))" in output
        assert "more" not in output

    def test_ten_downstream_truncated(self):
        """10 downstream models should be truncated with '... and N more'."""
        downstream = [f"model_{i}" for i in range(10)]
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
        output = format_github(result)
        # First 5 shown, remaining 5 truncated
        assert "... and 5 more" in output
        assert "(10 model(s))" in output
        # First 5 names present
        for i in range(5):
            assert f"model_{i}" in output

    def test_downstream_as_bullet_item(self):
        """Downstream line in github format should be a markdown list item."""
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
            downstream_map={"parent": ["child_a"]},
        )
        output = format_github(result)
        downstream_lines = [ln for ln in output.splitlines() if "Downstream:" in ln]
        assert len(downstream_lines) == 1
        assert downstream_lines[0].startswith("- Downstream:")


# ---------------------------------------------------------------------------
# 6. Multiple cascade impacts on same model
# ---------------------------------------------------------------------------


class TestMultipleCascadeImpacts:
    """Model with both broken_ref and build_failure on different downstream models."""

    def test_both_impact_types_appear(self):
        """Both broken_ref and build_failure should appear with correct emojis."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="stg_orders",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.DESTRUCTIVE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                    downstream_impacts=[
                        DownstreamImpact(
                            model_name="fct_conversions",
                            materialization="table",
                            on_schema_change=None,
                            risk="broken_ref",
                            reason="references dropped column(s): channel",
                        ),
                        DownstreamImpact(
                            model_name="int_daily_rollup",
                            materialization="incremental",
                            on_schema_change="fail",
                            risk="build_failure",
                            reason="upstream schema changed, on_schema_change=fail",
                        ),
                    ],
                ),
            ],
            downstream_map={"stg_orders": ["fct_conversions", "int_daily_rollup"]},
        )
        output = format_github(result)

        # Both impacts present
        assert "**BROKEN_REF**" in output
        assert "**BUILD_FAILURE**" in output

        # Correct model names in backticks
        assert "`fct_conversions`" in output
        assert "`int_daily_rollup`" in output

        # Correct emojis per risk type
        broken_ref_line = [ln for ln in output.splitlines() if "BROKEN_REF" in ln][0]
        build_fail_line = [ln for ln in output.splitlines() if "BUILD_FAILURE" in ln][0]
        assert "\U0001f534" in broken_ref_line  # red circle for broken_ref
        assert "\u26a0\ufe0f" in build_fail_line  # warning for build_failure

    def test_cascade_count_reflects_both(self):
        """Summary should count all cascade impacts."""
        result = CheckResult(
            predictions=[
                DDLPrediction(
                    model_name="stg_orders",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.DESTRUCTIVE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                    downstream_impacts=[
                        DownstreamImpact(
                            model_name="a",
                            materialization="table",
                            on_schema_change=None,
                            risk="broken_ref",
                            reason="dropped col",
                        ),
                        DownstreamImpact(
                            model_name="b",
                            materialization="incremental",
                            on_schema_change="fail",
                            risk="build_failure",
                            reason="schema changed",
                        ),
                    ],
                ),
            ],
        )
        output = format_github(result)
        last_line = output.strip().splitlines()[-1]
        assert "2 cascade risk(s)" in last_line


# ---------------------------------------------------------------------------
# 7. Compare text vs github output
# ---------------------------------------------------------------------------


class TestTextVsGithubConsistency:
    """Same CheckResult should produce consistent content in both formats."""

    def test_same_model_names(self):
        """Both formats should contain identical model names."""
        result = _complex_scenario()
        text_out = format_text(result, color=False)
        gh_out = format_github(result)

        for name in ["int_orders_merged", "snap_user_status", "dim_customers", "rpt_summary"]:
            assert name in text_out, f"{name} missing from text output"
            assert name in gh_out, f"{name} missing from github output"

    def test_same_safety_levels(self):
        """Both formats should show the same safety level labels."""
        result = _complex_scenario()
        text_out = format_text(result, color=False)
        gh_out = format_github(result)

        for level in ["DESTRUCTIVE", "WARNING", "SAFE"]:
            assert level in text_out, f"{level} missing from text output"
            assert level in gh_out, f"{level} missing from github output"

    def test_github_has_markdown_formatting(self):
        """GitHub format should have markdown-specific elements."""
        result = _complex_scenario()
        gh_out = format_github(result)
        text_out = format_text(result, color=False)

        # GitHub should have markdown elements
        assert "###" in gh_out
        assert "**" in gh_out
        assert "`" in gh_out
        assert ">" in gh_out  # blockquote for warnings

        # Text should NOT have markdown elements
        assert "###" not in text_out
        assert "**" not in text_out

    def test_same_summary_content(self):
        """Both formats should produce the same summary counts."""
        result = _complex_scenario()
        text_out = format_text(result, color=False)
        gh_out = format_github(result)

        summary_text = "dbt-plan: 4 checked, 2 safe, 1 warning, 1 destructive, 3 cascade risk(s)"
        assert summary_text in text_out
        assert summary_text in gh_out

    def test_both_show_parse_failures(self):
        """Both formats mention parse failures."""
        result = _complex_scenario()
        text_out = format_text(result, color=False)
        gh_out = format_github(result)

        assert "broken_cte_model" in text_out
        assert "broken_cte_model" in gh_out

    def test_both_show_skipped_models(self):
        """Both formats mention skipped models."""
        result = _complex_scenario()
        text_out = format_text(result, color=False)
        gh_out = format_github(result)

        assert "orphan_staging" in text_out
        assert "orphan_staging" in gh_out
