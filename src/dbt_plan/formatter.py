"""Output formatting for DDL predictions."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from dbt_plan.predictor import DDLPrediction, Safety

_SAFETY_ORDER = {Safety.DESTRUCTIVE: 0, Safety.WARNING: 1, Safety.SAFE: 2}
_SAFETY_ICON = {
    Safety.DESTRUCTIVE: "\U0001f534",  # red circle
    Safety.WARNING: "\u26a0\ufe0f",  # warning
    Safety.SAFE: "\u2705",  # check mark
}

# ANSI color codes for terminal output
_SAFETY_COLOR = {
    Safety.DESTRUCTIVE: "\033[31m",  # red
    Safety.WARNING: "\033[33m",  # yellow
    Safety.SAFE: "\033[32m",  # green
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


_MAX_DOWNSTREAM_NAMES = 5  # Truncate long downstream lists for readability


def _format_downstream_line(downstream: list[str]) -> str:
    """Format downstream model list, truncating if too long."""
    total = len(downstream)
    if total <= _MAX_DOWNSTREAM_NAMES:
        names = ", ".join(downstream)
    else:
        shown = ", ".join(downstream[:_MAX_DOWNSTREAM_NAMES])
        names = f"{shown}, ... and {total - _MAX_DOWNSTREAM_NAMES} more"
    return f"  Downstream: {names} ({total} model(s))"


@dataclass
class CheckResult:
    """Full result of a dbt-plan check."""

    predictions: list[DDLPrediction] = field(default_factory=list)
    downstream_map: dict[str, list[str]] = field(default_factory=dict)
    parse_failures: list[str] = field(default_factory=list)
    skipped_models: list[str] = field(default_factory=list)


def format_text(result: CheckResult, *, color: bool | None = None) -> str:
    """Format result for terminal output.

    Args:
        color: Force color on/off. None = auto-detect from sys.stdout.isatty().
    """
    use_color = color if color is not None else sys.stdout.isatty()

    def _colored(text: str, safety: Safety) -> str:
        if not use_color:
            return text
        c = _SAFETY_COLOR.get(safety, "")
        return f"{c}{_BOLD}{text}{_RESET}"

    if not result.predictions:
        return "dbt-plan -- no model changes detected"

    sorted_preds = sorted(result.predictions, key=lambda p: _SAFETY_ORDER.get(p.safety, 9))
    lines = [f"dbt-plan -- {len(result.predictions)} model(s) changed", ""]

    for pred in sorted_preds:
        mat_info = pred.materialization
        if pred.on_schema_change:
            mat_info += f", {pred.on_schema_change}"
        label = _colored(pred.safety.value.upper(), pred.safety)
        lines.append(f"{label}  {pred.model_name} ({mat_info})")
        for op in pred.operations:
            if op.column:
                lines.append(f"  {op.operation}  {op.column}")
            else:
                lines.append(f"  {op.operation}")
        downstream = result.downstream_map.get(pred.model_name, [])
        if downstream:
            lines.append(_format_downstream_line(downstream))
        # Cascade impacts
        for impact in pred.downstream_impacts:
            risk_label = _colored(
                impact.risk.upper(),
                Safety.WARNING if impact.risk != "broken_ref" else Safety.DESTRUCTIVE,
            )
            lines.append(f"  >> {risk_label}  {impact.model_name}: {impact.reason}")
        lines.append("")

    if result.parse_failures:
        names = ", ".join(result.parse_failures)
        warn = _colored("WARNING", Safety.WARNING) if use_color else "WARNING"
        lines.append(f"{warn}: Could not extract columns for: {names} -- manual review required")

    if result.skipped_models:
        names = ", ".join(result.skipped_models)
        warn = _colored("WARNING", Safety.WARNING) if use_color else "WARNING"
        lines.append(
            f"{warn}: Skipped {len(result.skipped_models)} model(s) not found in manifest: {names}"
        )

    # Summary line (grepable for CI: grep "^dbt-plan:" output)
    lines.append(_summary_line(result))

    return "\n".join(lines)


def _summary_line(result: CheckResult) -> str:
    """One-line summary for CI pipeline grep."""
    n = len(result.predictions)
    safe = sum(1 for p in result.predictions if p.safety == Safety.SAFE)
    warn = sum(1 for p in result.predictions if p.safety == Safety.WARNING)
    dest = sum(1 for p in result.predictions if p.safety == Safety.DESTRUCTIVE)
    cascade = sum(len(p.downstream_impacts) for p in result.predictions)
    line = f"dbt-plan: {n} checked, {safe} safe, {warn} warning, {dest} destructive"
    if cascade:
        line += f", {cascade} cascade risk(s)"
    return line


def format_github(result: CheckResult) -> str:
    """Format result as GitHub-flavored markdown."""
    if not result.predictions:
        return "### dbt-plan -- no model changes detected"

    sorted_preds = sorted(result.predictions, key=lambda p: _SAFETY_ORDER.get(p.safety, 9))
    lines = [f"### dbt-plan -- {len(result.predictions)} model(s) changed", ""]

    for pred in sorted_preds:
        icon = _SAFETY_ICON.get(pred.safety, "")
        mat_info = pred.materialization
        if pred.on_schema_change:
            mat_info += f", {pred.on_schema_change}"
        lines.append(f"{icon} **{pred.safety.value.upper()}** `{pred.model_name}` ({mat_info})")
        for op in pred.operations:
            if op.column:
                lines.append(f"- `{op.operation}` {op.column}")
            else:
                lines.append(f"- {op.operation}")
        downstream = result.downstream_map.get(pred.model_name, [])
        if downstream:
            lines.append("- " + _format_downstream_line(downstream).lstrip())
        for impact in pred.downstream_impacts:
            risk_icon = "\u26a0\ufe0f" if impact.risk == "build_failure" else "\U0001f534"
            lines.append(
                f"- {risk_icon} **{impact.risk.upper()}** `{impact.model_name}`: {impact.reason}"
            )
        lines.append("")

    if result.parse_failures:
        names = ", ".join(result.parse_failures)
        lines.append(
            f"> **WARNING**: Could not extract columns for: {names} -- manual review required"
        )

    if result.skipped_models:
        names = ", ".join(result.skipped_models)
        lines.append(
            f"> **WARNING**: Skipped {len(result.skipped_models)} model(s)"
            f" not found in manifest: {names}"
        )

    lines.append(f"\n`{_summary_line(result)}`")

    return "\n".join(lines)


def format_json(result: CheckResult) -> str:
    """Format result as JSON for programmatic consumption."""
    from typing import Any

    models: list[dict[str, Any]] = []
    for pred in result.predictions:
        model: dict[str, Any] = {
            "model_name": pred.model_name,
            "materialization": pred.materialization,
            "on_schema_change": pred.on_schema_change,
            "safety": pred.safety.value,
            "operations": [
                {"operation": op.operation, "column": op.column} for op in pred.operations
            ],
            "columns_added": pred.columns_added,
            "columns_removed": pred.columns_removed,
        }
        downstream = result.downstream_map.get(pred.model_name, [])
        if downstream:
            model["downstream"] = downstream
        if pred.downstream_impacts:
            model["downstream_impacts"] = [
                {
                    "model_name": imp.model_name,
                    "risk": imp.risk,
                    "reason": imp.reason,
                }
                for imp in pred.downstream_impacts
            ]
        models.append(model)

    cascade_count = sum(len(p.downstream_impacts) for p in result.predictions)
    summary: dict = {
        "total": len(result.predictions),
        "safe": sum(1 for p in result.predictions if p.safety == Safety.SAFE),
        "warning": sum(1 for p in result.predictions if p.safety == Safety.WARNING),
        "destructive": sum(1 for p in result.predictions if p.safety == Safety.DESTRUCTIVE),
    }
    if cascade_count:
        summary["cascade_risks"] = cascade_count

    output = {
        "summary": summary,
        "models": models,
        "parse_failures": result.parse_failures,
        "skipped_models": result.skipped_models,
    }
    return json.dumps(output, indent=2)
