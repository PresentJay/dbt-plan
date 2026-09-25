"""Malformed child output must never become permission to run dbt."""

import copy
import json
import subprocess

import pytest

server = pytest.importorskip("dbt_plan_mcp.server", reason="needs the optional mcp extra")


def report(safety=None):
    models = []
    if safety:
        models.append(
            {
                "model_name": "orders",
                "materialization": "incremental",
                "on_schema_change": "sync_all_columns",
                "safety": safety,
                "operations": [
                    {"operation": "DROP COLUMN", "column": "tax"}
                    if safety == "destructive"
                    else {"operation": "NO SCHEMA CHANGE", "column": None}
                ],
                "columns_added": [],
                "columns_removed": ["tax"] if safety == "destructive" else [],
                "acknowledged": False,
            }
        )
    return {
        "summary": {
            "total": len(models),
            **{
                severity: int(safety == severity)
                for severity in ("safe", "warning", "destructive")
            },
        },
        "models": models,
        "parse_failures": [],
        "skipped_models": [],
        "uncompiled_models": [],
        "stale_sources": [],
    }


def plan_output(monkeypatch, payload, code=0):
    monkeypatch.setattr(
        server,
        "_run_cli",
        lambda args: subprocess.CompletedProcess(args, code, payload, ""),
    )
    return server.plan("/unused")


@pytest.mark.parametrize("payload", ["{}", "[]", "null", '"secret-stdout"', "not JSON"])
def test_invalid_top_level_is_a_structured_error(monkeypatch, capsys, payload):
    out = plan_output(monkeypatch, payload)
    assert out["verdict"] == "error"
    assert out["error"]
    assert "secret-stdout" not in json.dumps(out)
    assert capsys.readouterr().out == ""


_MISSING = object()


def malformed_cases():
    base = report("safe")
    replacements = []
    for field in base:
        replacements.append(((field,), _MISSING))
        replacements.append(((field,), None))
    for field in ("total", "safe", "warning", "destructive"):
        for value in (_MISSING, True, -1, 1.5, "0", 3):
            replacements.append((("summary", field), value))
    for field in base["models"][0]:
        replacements.append((("models", 0, field), _MISSING))
    for field, values in {
        "model_name": [None, 7],
        "materialization": [None, []],
        "on_schema_change": [False, {}],
        "safety": ["future", [], None],
        "operations": [
            None,
            {},
            [None],
            [{}],
            [{"operation": 7, "column": None}],
            [{"operation": "DDL", "column": []}],
            [{"operation": "DDL"}],
        ],
        "columns_added": [None, "tax", [1]],
        "columns_removed": [False, [None]],
        "acknowledged": [None, 1, "yes"],
        "downstream_impacts": [
            None,
            {},
            [None],
            [{}],
            [{"model_name": "child", "risk": [], "reason": "new"}],
            [{"model_name": None, "risk": "build_failure", "reason": "new"}],
            [{"model_name": "child", "risk": "build_failure", "reason": 1}],
        ],
    }.items():
        replacements.extend((("models", 0, field), value) for value in values)
    for field in ("parse_failures", "skipped_models", "uncompiled_models", "stale_sources"):
        replacements.extend(((field,), value) for value in ("orders", {}, [None], [1], [[]]))
    replacements.extend((("baseline_problem",), value) for value in (None, False, {}, []))
    replacements.extend((("models",), value) for value in ([None], ["orders"], [{}]))
    for field in ("acknowledged", "cascade_risks"):
        replacements.extend((("summary", field), value) for value in (True, -1, "0", 1))
    for path, value in replacements:
        data = copy.deepcopy(base)
        parent = data
        for part in path[:-1]:
            parent = parent[part]
        if value is _MISSING:
            del parent[path[-1]]
        else:
            parent[path[-1]] = value
        label = "missing" if value is _MISSING else repr(value)
        yield pytest.param(data, id=f"{'.'.join(map(str, path))}={label}")


@pytest.mark.parametrize("data", list(malformed_cases()))
def test_malformed_fields_are_errors(monkeypatch, data):
    out = plan_output(monkeypatch, json.dumps(data))
    assert out["verdict"] == "error"
    assert out["error"].startswith("Invalid dbt-plan report:")


@pytest.mark.parametrize(
    "safety,expected",
    [(None, "safe"), ("warning", "review_required"), ("destructive", "destructive")],
)
def test_valid_reports_keep_findings_separate_from_exit_policy(monkeypatch, safety, expected):
    data = report(safety)
    if safety == "destructive":
        data["models"][0]["acknowledged"] = True
        data["summary"]["acknowledged"] = 1
    data["future_metadata"] = {"anything": [1, None]}
    data["summary"]["future_count"] = "additive"
    if safety:
        data["models"][0]["future"] = True
        data["models"][0]["operations"][0]["future"] = {}
    out = plan_output(monkeypatch, json.dumps(data))
    assert out["verdict"] == expected
    assert out["exit_code"] == 0
    assert out["summary"] == data["summary"]
    assert out["models"] == data["models"]
    assert out["refusals"] == []


@pytest.mark.parametrize(
    "field", ["parse_failures", "skipped_models", "uncompiled_models", "stale_sources"]
)
def test_valid_refusals_survive_zero_total_and_exit_zero(monkeypatch, field):
    data = report()
    data[field] = ["orders"]
    out = plan_output(monkeypatch, json.dumps(data))
    assert out["verdict"] == "review_required"
    assert out["refusals"][0]["models"] == ["orders"]


@pytest.mark.parametrize(
    "risk,expected",
    [
        ("future_risk", "review_required"),
        ("build_failure", "review_required"),
        ("broken_ref", "destructive"),
        ("inherited_drop", "destructive"),
    ],
)
def test_raw_cascade_risk_cannot_be_hidden_by_parent_safety(monkeypatch, risk, expected):
    data = report("safe")
    data["models"][0]["own_safety"] = "safe"
    data["models"][0]["waiver_detail"] = {"future": True}
    data["models"][0]["downstream_impacts"] = [
        {"model_name": "child", "risk": risk, "reason": "finding", "future": []}
    ]
    data["summary"]["cascade_risks"] = 1
    out = plan_output(monkeypatch, json.dumps(data))
    assert out["verdict"] == expected
    assert out["models"] == data["models"]


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_validation_precedes_exit_policy(monkeypatch, code):
    out = plan_output(monkeypatch, "{}", code)
    assert out["verdict"] == "error"
    assert out["error"].startswith("Invalid dbt-plan report:")


def test_valid_report_does_not_mask_execution_error(monkeypatch):
    out = plan_output(monkeypatch, json.dumps(report()), 3)
    assert out == {"verdict": "error", "error": "dbt-plan exited 3"}


def test_deeply_nested_json_is_a_structured_error(monkeypatch):
    out = plan_output(monkeypatch, "[" * 2000 + "]" * 2000)
    assert out["verdict"] == "error"


def test_real_cli_unchanged_report_passes_validation(tmp_path):
    compiled = tmp_path / "target" / "compiled" / "p" / "models"
    compiled.mkdir(parents=True)
    (compiled / "orders.sql").write_text("select 1 as id", encoding="utf-8")
    (tmp_path / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.orders": {
                        "name": "orders",
                        "config": {"materialized": "table", "enabled": True},
                        "columns": {},
                    }
                },
                "child_map": {},
                "metadata": {"project_name": "p"},
            }
        ),
        encoding="utf-8",
    )
    assert server.snapshot(str(tmp_path))["ok"]
    out = server.plan(str(tmp_path))
    assert out["verdict"] == "safe"
    assert out["summary"] == {"total": 0, "safe": 0, "warning": 0, "destructive": 0}
    assert out["models"] == out["refusals"] == []
