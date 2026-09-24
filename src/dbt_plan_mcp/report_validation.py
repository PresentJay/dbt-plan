"""Validate the CLI report contract before MCP interprets findings or exit policy.

Keep this boundary standard-library-only and independent of the analysis process.
Unknown object keys are extensions, not errors. Unknown risk strings are retained
and treated conservatively by the caller; unknown model severities are errors.
"""

from __future__ import annotations

from typing import Any


class ReportValidationError(ValueError):
    """A report is incomplete, incorrectly typed, or internally inconsistent."""


def _typed(value: Any, expected: type | tuple[type, ...], path: str) -> None:
    if not isinstance(value, expected):
        raise ReportValidationError(f"{path}: invalid type")


def _fields(obj: Any, fields: dict[str, Any], path: str) -> None:
    _typed(obj, dict, path)
    for key, expected in fields.items():
        if key not in obj:
            raise ReportValidationError(f"{path}.{key}: required field missing")
        _typed(obj[key], expected, f"{path}.{key}")


def _strings(value: Any, path: str) -> None:
    _typed(value, list, path)
    for index, item in enumerate(value):
        _typed(item, str, f"{path}[{index}]")


def _count(value: Any, expected: int, path: str) -> None:
    # bool is an int subclass, but cannot be a report count.
    if type(value) is not int or value < 0:
        raise ReportValidationError(f"{path}: expected a nonnegative integer")
    if value != expected:
        raise ReportValidationError(f"{path}: count does not match model entries")


def validate_report(report: Any) -> dict[str, Any]:
    """Return the original report, or raise a diagnostic without echoing its data."""
    _fields(
        report,
        {
            "summary": dict,
            "models": list,
            "parse_failures": list,
            "stale_sources": list,
            "skipped_models": list,
            "uncompiled_models": list,
        },
        "report",
    )
    for field in ("parse_failures", "stale_sources", "skipped_models", "uncompiled_models"):
        _strings(report[field], field)
    if "baseline_problem" in report:
        _typed(report["baseline_problem"], str, "baseline_problem")
        if not report["baseline_problem"]:
            raise ReportValidationError("baseline_problem: expected a nonempty string")

    counts = {
        "total": len(report["models"]),
        "safe": 0,
        "warning": 0,
        "destructive": 0,
        "acknowledged": 0,
        "cascade_risks": 0,
    }
    for index, model in enumerate(report["models"]):
        path = f"models[{index}]"
        _fields(
            model,
            {
                "model_name": str,
                "materialization": str,
                "on_schema_change": (str, type(None)),
                "safety": str,
                "operations": list,
                "columns_added": list,
                "columns_removed": list,
                "acknowledged": bool,
            },
            path,
        )
        if model["safety"] not in ("safe", "warning", "destructive"):
            raise ReportValidationError(f"{path}.safety: unknown severity")
        counts[model["safety"]] += 1
        counts["acknowledged"] += model["acknowledged"]
        for field in ("columns_added", "columns_removed"):
            _strings(model[field], f"{path}.{field}")
        for op_index, operation in enumerate(model["operations"]):
            _fields(
                operation,
                {"operation": str, "column": (str, type(None))},
                f"{path}.operations[{op_index}]",
            )
        if "downstream_impacts" in model:
            _typed(model["downstream_impacts"], list, f"{path}.downstream_impacts")
            for impact_index, impact in enumerate(model["downstream_impacts"]):
                _fields(
                    impact,
                    {"model_name": str, "risk": str, "reason": str},
                    f"{path}.downstream_impacts[{impact_index}]",
                )
            counts["cascade_risks"] += len(model["downstream_impacts"])

    for field, expected in counts.items():
        if field not in report["summary"]:
            if field in ("acknowledged", "cascade_risks"):
                continue  # Optional counts do not determine the findings.
            raise ReportValidationError(f"summary.{field}: required field missing")
        _count(report["summary"][field], expected, f"summary.{field}")
    return report
