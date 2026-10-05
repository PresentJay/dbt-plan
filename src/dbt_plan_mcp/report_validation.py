"""Validate the CLI report contract before MCP interprets findings or exit policy.

Keep this boundary standard-library-only and independent of the analysis process.
Unknown object keys are extensions, not errors. Unknown risk strings are retained
and treated conservatively by the caller; unknown model severities are errors.
"""

from __future__ import annotations

from typing import Any


class ReportValidationError(ValueError):
    """A report is incomplete, incorrectly typed, or internally inconsistent."""


# Only these rules can establish safety; every unfamiliar rule requires review.
# Known destructive rules impose a floor even on contradictory raw fields.
_DESTRUCTIVE_RULES = {
    "ddl.drop_column",
    "ddl.model_removed",
    "cascade.broken_ref",
    "cascade.inherited_drop",
}
_SAFE_RULES = {
    "ddl.replace_table",
    "ddl.replace_view",
    "ddl.no_ddl",
    "ddl.add_column",
    "ddl.verdict",
}


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


def _validate_finding(fact: Any, path: str) -> None:
    _fields(
        fact,
        {
            "contract_version": int,
            "rule_code": str,
            "source": (dict, type(None)),
            "affected": (dict, type(None)),
            "column": (str, type(None)),
            "columns": list,
            "columns_added": list,
            "columns_removed": list,
            "severity": str,
            "raw_risk": str,
            "message": str,
            "evidence": dict,
            "uncertainty": list,
            "waiver_allowed": bool,
        },
        path,
    )
    if type(fact["contract_version"]) is not int or fact["contract_version"] < 1:
        raise ReportValidationError(f"{path}.contract_version: invalid version")
    for field in ("columns", "columns_added", "columns_removed", "uncertainty"):
        _strings(fact[field], f"{path}.{field}")
    for field in ("source", "affected"):
        ref = fact[field]
        if ref is not None:
            _fields(
                ref,
                {
                    "name": str,
                    "unique_id": (str, type(None)),
                    "candidates": list,
                },
                f"{path}.{field}",
            )
            _strings(ref["candidates"], f"{path}.{field}.candidates")
            if "original_file_path" in ref:
                _typed(
                    ref["original_file_path"],
                    (str, type(None)),
                    f"{path}.{field}.original_file_path",
                )
            if not ref["name"]:
                raise ReportValidationError(f"{path}.{field}.name: empty resource name")
            ids = ref["candidates"] + ([ref["unique_id"]] if ref["unique_id"] is not None else [])
            for uid in ids:
                if (
                    len(uid.split(".")) < 3
                    or not all(uid.split("."))
                    or any(c.isspace() or ord(c) < 32 or c in "/\\:" for c in uid)
                ):
                    raise ReportValidationError(f"{path}.{field}: invalid qualified resource ID")
            if ref["unique_id"] is not None and ref["candidates"] != [ref["unique_id"]]:
                raise ReportValidationError(f"{path}.{field}: inconsistent resource candidates")
    _fields(
        fact["evidence"],
        {
            "origin": str,
            "state": str,
            "reason_code": str,
            "columns": list,
        },
        f"{path}.evidence",
    )
    _strings(fact["evidence"]["columns"], f"{path}.evidence.columns")
    if "compiled_path" in fact["evidence"]:
        _typed(
            fact["evidence"]["compiled_path"], (str, type(None)), f"{path}.evidence.compiled_path"
        )


def finding_severity(fact: dict) -> str:
    """Interpret a validated fact conservatively without rewriting its raw data."""
    raw = fact["raw_risk"]
    if (
        fact["severity"] == "destructive"
        or raw in {"destructive", "broken_ref", "inherited_drop"}
        or fact["rule_code"] in _DESTRUCTIVE_RULES
    ):
        return "destructive"
    ev = fact["evidence"]
    if (
        fact["severity"] != "safe"
        or raw != "safe"
        or fact["contract_version"] != 1
        or fact["rule_code"] not in _SAFE_RULES
        or ev["state"] != "exact"
        or ev["reason_code"] not in {"ddl_rule", "column_diff_checked", "read_checked"}
        or ev["origin"] not in {"prediction", "compiled_sql", "resolved_columns", "resolved_read"}
        or fact["uncertainty"]
        or any(ref is None or not ref["unique_id"] for ref in (fact["source"], fact["affected"]))
    ):
        return "warning"
    return "safe"


def report_severity(report: dict) -> str:
    """Raw verdict only; exit policy and acknowledgements stay with their owners."""
    levels = {model["safety"] for model in report["models"]}
    for model in report["models"]:
        for impact in model.get("downstream_impacts", []):
            levels.add(
                "destructive" if impact["risk"] in {"broken_ref", "inherited_drop"} else "warning"
            )
    for fact in report.get("findings", []):
        levels.add(finding_severity(fact))
    if any(
        report.get(field)
        for field in (
            "parse_failures",
            "skipped_models",
            "uncompiled_models",
            "stale_sources",
            "baseline_problem",
        )
    ):
        levels.add("warning")
    return (
        "destructive" if "destructive" in levels else "warning" if "warning" in levels else "safe"
    )


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
    for field in ("ignored_models", "unmatched_ignore_models"):
        if field in report:
            _strings(report[field], field)
    if "analysis" in report:
        _typed(report["analysis"], dict, "analysis")
    if "findings" in report:
        _typed(report["findings"], list, "findings")
        for index, fact in enumerate(report["findings"]):
            _validate_finding(fact, f"findings[{index}]")
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
