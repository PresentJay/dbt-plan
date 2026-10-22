"""Human summaries must retain report-wide risks independently of model counts."""

import json
from dataclasses import replace

import pytest

from dbt_plan.cli import _exit_code_for
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety
from dbt_plan_mcp.report_validation import report_severity, validate_report
from tests.test_explained_path_rendering import fixture, integrate


@pytest.mark.parametrize("render", [format_text, format_github])
@pytest.mark.parametrize("policy,code", [(None, 2), ("never", 0), ("destructive", 0)])
def test_unresolved_downstream_read_is_visible_before_safe_model(render, policy, code):
    result, manifest = fixture()
    integrate(result, manifest)
    report_before = format_json(result)
    output = render(result)

    assert "Review required: WARNING" in output
    assert output.index("Review required: WARNING") < output.index("SAFE")
    assert "Next: Resolve the unreadable downstream SQL and recompile" in output
    summary = next(line for line in output.splitlines() if "dbt-plan: 1 checked" in line)
    assert "1 safe, 0 warning, 0 destructive" in summary
    assert "overall review: WARNING (before policy)" in summary
    assert _exit_code_for(result, 2, policy) == code
    assert format_json(result) == report_before
    report = validate_report(json.loads(report_before))
    assert report["summary"]["warning"] == 0
    assert report_severity(report) == "warning"


@pytest.mark.parametrize("render", [format_text, format_github])
@pytest.mark.parametrize(
    "field,value,advice",
    [
        ("parse_failures", ["orders"], "Use explicit output columns"),
        ("skipped_models", ["orders"], "Use matching compiled SQL and manifest artifacts"),
        ("uncompiled_models", ["orders"], "Complete compilation for the requested models"),
        ("stale_sources", ["macros/orders.sql"], "Recompile the current model and macro sources"),
        ("baseline_problem", "missing", "Capture a baseline from the intended base revision"),
        ("baseline_problem", "corrupt", "Capture a baseline from the intended base revision"),
    ],
)
def test_legacy_input_warning_without_model_changes_is_reviewable(render, field, value, advice):
    result = CheckResult(**{field: value})
    output = render(result)
    assert "Review required: WARNING" in output
    assert advice in output
    assert "overall review: WARNING (before policy)" in output
    assert "no model changes detected" not in output


@pytest.mark.parametrize("render", [format_text, format_github])
def test_acknowledgements_do_not_hide_raw_destructive_review(render):
    result = CheckResult(
        predictions=[
            DDLPrediction(
                "orders",
                "incremental",
                "sync_all_columns",
                Safety.DESTRUCTIVE,
                operations=[DDLOperation("DROP COLUMN", "amount")],
            )
        ],
        acknowledge_models=["orders"],
    )
    assert _exit_code_for(result, 2) == 0
    output = render(result)
    assert "Review required: DESTRUCTIVE" in output
    assert "exit policy is separate" in output
    assert "overall review: DESTRUCTIVE (before policy)" in output


@pytest.mark.parametrize("render", [format_text, format_github])
def test_unknown_fact_reason_keeps_review_and_generic_advice(render):
    result, manifest = fixture(())
    integrate(result, manifest)
    safe = result.findings[0]
    result.findings = (replace(safe, rule_code="future.rule"),)
    output = render(result)
    assert "Review required: WARNING" in output
    assert "Next: Review the finding evidence and affected source files" in output


@pytest.mark.parametrize("render", [format_text, format_github])
def test_safe_model_with_legacy_cascade_warning_is_not_an_all_clear(render):
    result = CheckResult(
        predictions=[
            DDLPrediction(
                "orders",
                "view",
                None,
                Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE VIEW")],
                downstream_impacts=[
                    DownstreamImpact("reader", "table", None, "data_test_unreadable", "bad SQL")
                ],
            )
        ],
    )
    assert "overall review: WARNING (before policy)" in render(result)


@pytest.mark.parametrize("render", [format_text, format_github])
def test_resolved_read_control_reports_no_risks_with_artifact_boundary(render):
    result, manifest = fixture(())
    integrate(result, manifest)
    output = render(result)
    assert "Review: NO REPORTED RISKS" in output
    assert "supplied artifacts only" in output
    assert "Review required:" not in output
    assert _exit_code_for(result, 2) == 0


@pytest.mark.parametrize("render", [format_text, format_github])
def test_all_ignored_reports_exclusions_before_model_details(render):
    output = render(CheckResult(ignored_models=["orders", "orders", "reader"]))
    assert "Excluded: 2 model(s) by ignore policy; not checked" in output
    assert "supplied artifacts only" in output


def test_remediation_is_deduplicated_for_repeated_canonical_and_legacy_refusals():
    result, manifest = fixture()
    integrate(result, manifest)
    result.findings = (*result.findings, result.findings[-1])
    output = format_text(result, color=False)
    assert output.count("Next: Resolve the unreadable downstream SQL and recompile") == 1


@pytest.mark.parametrize("render", [format_text, format_github])
def test_unknown_graph_semantics_require_review_even_with_no_predictions(render):
    result, manifest = fixture(())
    integrate(result, manifest)
    result.causal_graph["edges"][0]["kind"] = "future_link"
    result.predictions = []
    output = render(result)
    assert "Review required: WARNING" in output
    assert "Review: NO REPORTED RISKS" not in output


@pytest.mark.parametrize("render", [format_text, format_github])
def test_empty_report_does_not_claim_complete_project_safety(render):
    output = render(CheckResult())
    assert "no model changes detected" in output
    assert "supplied artifacts only" in output
    assert "Review required:" not in output
