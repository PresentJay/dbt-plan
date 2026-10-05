"""Canonical facts survive transports independently of exit/acknowledgement policy."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dbt_plan.cli import _do_check
from dbt_plan.findings import Evidence, findings_from_result
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety
from dbt_plan_mcp.report_validation import ReportValidationError, validate_report
from tests.test_cli import _make_check_args, _make_project


def manifest():
    return {
        "metadata": {"project_name": "shop"},
        "nodes": {
            f"model.shop.{name}": {
                "name": name,
                "resource_type": "model",
                "original_file_path": f"models/{name}.sql",
                "config": {"materialized": "incremental", "enabled": True},
                "unrendered_config": {"on_schema_change": "sync_all_columns"},
                "columns": {"id": {}},
            }
            for name in ("orders", "report")
        },
        "child_map": {},
    }


def canonical_result():
    result = CheckResult(
        predictions=[
            DDLPrediction(
                "orders",
                "incremental",
                "sync_all_columns",
                Safety.DESTRUCTIVE,
                operations=[DDLOperation("DROP COLUMN", "book_id")],
                columns_removed=["book_id"],
                downstream_impacts=[
                    DownstreamImpact("report", "view", None, "broken_ref", "reads book_id")
                ],
            )
        ],
        acknowledge_models=["orders"],
        stale_sources=["macros/shared.sql"],
    )
    result.findings = findings_from_result(
        result,
        manifest(),
        evidence={
            ("model.shop.orders", "model.shop.orders", "ddl.drop_column"): Evidence(
                "compiled_sql",
                "exact",
                "column_diff_checked",
                ("book_id",),
                "target/compiled/shop/models/orders.sql",
            )
        },
    )
    return result


def payload():
    result = canonical_result()
    data = json.loads(format_json(result))
    # Consumer controls must exercise canonical input even before formatter wiring.
    data["findings"] = [fact.to_dict() for fact in result.findings]
    return data


def mcp_plan(monkeypatch, data):
    server = pytest.importorskip("dbt_plan_mcp.server", reason="requires mcp extra")
    monkeypatch.setattr(
        server, "_run_cli", lambda args: subprocess.CompletedProcess(args, 0, json.dumps(data), "")
    )
    return server.plan("/unused")


def test_all_renderers_retain_facts_and_policy_separately(monkeypatch):
    result = canonical_result()
    facts = [fact.to_dict() for fact in result.findings]
    data = json.loads(format_json(result))
    assert data["findings"] == facts
    assert data["models"][0]["own_waived"] is True
    for rendered in (format_text(result, color=False), format_github(result)):
        for fact in facts:
            assert fact["rule_code"] in rendered
            assert fact["evidence"]["origin"] in rendered
            assert fact["evidence"]["state"] in rendered
        assert "model.shop.orders" in rendered
        assert "model.shop.report" in rendered
        assert "column: book_id" in rendered
        assert "ACKNOWLEDGED" in rendered
    out = mcp_plan(monkeypatch, data)
    assert out["findings"] == facts
    assert out["verdict"] == "destructive"


def test_legacy_missing_fields_remain_absent(monkeypatch):
    data = json.loads(format_json(CheckResult()))
    assert "findings" not in data
    for field in ("analysis", "ignored_models", "unmatched_ignore_models"):
        data.pop(field, None)
    out = mcp_plan(monkeypatch, data)
    assert out["verdict"] == "safe"
    for field in ("findings", "analysis", "ignored_models", "unmatched_ignore_models"):
        assert field not in out


def test_mcp_preserves_additive_fields(monkeypatch):
    data = payload()
    data.update(
        analysis={"future": {"value": 1}},
        ignored_models=["excluded"],
        unmatched_ignore_models=["typo"],
        future={"value": 2},
    )
    data["findings"][0]["future"] = {"opaque": True}
    out = mcp_plan(monkeypatch, data)
    for key, value in data.items():
        assert out[key] == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", True),
        ("contract_version", "1"),
        ("source", []),
        ("affected", {}),
        ("column", []),
        ("columns", [None]),
        ("columns_added", "id"),
        ("columns_removed", {}),
        ("severity", None),
        ("raw_risk", []),
        ("message", False),
        ("evidence", {}),
        ("uncertainty", "unknown"),
        ("waiver_allowed", 1),
    ],
)
def test_malformed_canonical_fields_fail_validation(field, value):
    data = payload()
    data["findings"][0][field] = value
    with pytest.raises(ReportValidationError):
        validate_report(data)


@pytest.mark.parametrize("value", [None, {}, [None], [{}]])
def test_malformed_findings_never_empty_safe(value):
    data = json.loads(format_json(CheckResult()))
    data["findings"] = value
    with pytest.raises(ReportValidationError):
        validate_report(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rule_code", "future.rule"),
        ("severity", "future"),
        ("raw_risk", "future"),
        ("contract_version", 2),
        (
            "evidence",
            {
                "origin": "future",
                "state": "exact",
                "reason_code": "future",
                "columns": [],
                "compiled_path": None,
            },
        ),
        ("uncertainty", ["future"]),
    ],
)
def test_unknown_semantics_require_review(monkeypatch, field, value):
    data = json.loads(format_json(CheckResult()))
    fact = next(f for f in payload()["findings"] if f["rule_code"] == "ddl.drop_column")
    fact.update(severity="safe", raw_risk="safe", rule_code="ddl.add_column")
    fact[field] = value
    data["findings"] = [fact]
    assert mcp_plan(monkeypatch, data)["verdict"] in {"review_required", "error"}


def test_schema_is_additive_and_checks_nested_findings():
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/check-report-v1.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    data = payload()
    data["findings"][0]["future"] = {"unknown": [1]}
    validator.validate(data)
    validator.validate(json.loads(format_json(CheckResult())))
    bad = copy.deepcopy(data)
    bad["findings"][0]["evidence"]["columns"] = "book_id"
    assert list(validator.iter_errors(bad))


def safe_payload():
    result = CheckResult(
        predictions=[
            DDLPrediction(
                "orders",
                "table",
                None,
                Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE TABLE")],
            )
        ]
    )
    result.findings = findings_from_result(result, manifest())
    return json.loads(format_json(result))


def test_safe_positive_control_and_unknown_reason_preserved(monkeypatch):
    data = safe_payload()
    assert mcp_plan(monkeypatch, data)["verdict"] == "safe"
    data["findings"][0]["evidence"]["reason_code"] = "future_reason"
    out = mcp_plan(monkeypatch, data)
    assert out["verdict"] == "review_required"
    assert out["findings"] == data["findings"]


@pytest.mark.parametrize(
    "reason", ["parse_failed", "partial_unknown", "manifest_fallback", "unresolved_input"]
)
def test_contradictory_exact_evidence_never_safe(monkeypatch, reason):
    data = safe_payload()
    data["findings"][0]["evidence"]["reason_code"] = reason
    assert mcp_plan(monkeypatch, data)["verdict"] != "safe"


def test_validator_imports_without_optional_packages():
    source = Path(__file__).parents[1] / "src"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "sys.modules['mcp'] = None; sys.modules['jsonschema'] = None; "
            "from dbt_plan_mcp.report_validation import validate_report, report_severity; "
            "assert sys.modules['mcp'] is None; print('STDLIB_VALIDATOR_OK')",
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "STDLIB_VALIDATOR_OK"


def test_actual_cli_to_mcp_preserves_report(tmp_path, capsys):
    server = pytest.importorskip("dbt_plan_mcp.server", reason="requires mcp extra")
    data = manifest()
    data["nodes"].pop("model.shop.report")
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select 1 as id"},
        base_sql={"orders": "select 1 as id, 2 as book_id"},
        manifest=data,
        base_manifest=data,
    )
    _do_check(_make_check_args(project, fmt="json"))
    raw = json.loads(capsys.readouterr().out)
    out = server.plan(str(project))
    assert out["verdict"] == "destructive"
    for key, value in raw.items():
        assert out[key] == value


def test_empty_diff_still_has_refusals(tmp_path, capsys):
    data = manifest()
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select 1 as id"},
        base_sql={"orders": "select 1 as id"},
        manifest=data,
        base_manifest=data,
    )
    _do_check(_make_check_args(project, fmt="json"))
    raw = json.loads(capsys.readouterr().out)
    assert raw["models"] == []
    assert any(
        f["rule_code"] == "input.refusal"
        and f["affected"]
        and f["affected"]["unique_id"] == "model.shop.report"
        for f in raw["findings"]
    )


@pytest.mark.parametrize(
    "rule,expected",
    [
        ("ddl.drop_column", "destructive"),
        ("ddl.model_removed", "destructive"),
        ("cascade.broken_ref", "destructive"),
        ("cascade.inherited_drop", "destructive"),
        ("ddl.build_failure", "review_required"),
        ("ddl.contract_violation", "review_required"),
        ("cascade.build_failure", "review_required"),
        ("input.refusal", "review_required"),
    ],
)
def test_rule_risk_cannot_be_forged_safe(monkeypatch, rule, expected):
    data = safe_payload()
    assert mcp_plan(monkeypatch, data)["verdict"] == "safe"
    data["findings"][0]["rule_code"] = rule
    assert mcp_plan(monkeypatch, data)["verdict"] == expected


@pytest.mark.parametrize(
    "uid",
    [
        "",
        "orders",
        "model..orders",
        "model.shop.",
        "model.shop.orders/escape",
        "model.shop. orders",
        "model.shop\\orders",
    ],
)
def test_malformed_identity_is_an_error(monkeypatch, uid):
    data = safe_payload()
    data["findings"][0]["source"]["unique_id"] = uid
    assert mcp_plan(monkeypatch, data)["verdict"] == "error"


@pytest.mark.parametrize(
    "base,current,expected",
    [
        ("select 1 as id", "select 1 as id, 2 as book_id", "exact"),
        ("select * from external_a", "select * from external_b", "conservative"),
        ("select 1 as id", "select 1 as id, count(*) from opaque", "unknown"),
        ("select 1 as id", "select (", "unknown"),
    ],
)
def test_actual_cli_evidence_and_policy_stability(tmp_path, capsys, base, current, expected):
    project = _make_project(
        tmp_path,
        models_sql={"orders": current},
        base_sql={"orders": base},
        manifest=manifest(),
        base_manifest=manifest(),
    )
    args = _make_check_args(project, fmt="json")
    _do_check(args)
    first = json.loads(capsys.readouterr().out)
    facts = first["findings"]
    own = [f for f in facts if f["rule_code"].startswith("ddl.")]
    assert own
    assert all(f["evidence"]["state"] == expected for f in own)
    assert all(
        f["evidence"]["origin"] == ("manifest" if expected == "conservative" else "compiled_sql")
        for f in own
    )
    assert all(f["source"]["unique_id"] == "model.shop.orders" for f in own)
    assert all(f["source"]["original_file_path"] == "models/orders.sql" for f in own)
    assert all("compiled" in f["evidence"]["compiled_path"] for f in own)
    assert all(f["evidence"]["compiled_path"].startswith("target/compiled/") for f in own)
    if expected == "unknown":
        assert any(f["rule_code"] == "input.refusal" for f in facts)
        reason = "partial_unknown" if "count(*)" in current else "parse_failed"
        assert all(f["evidence"]["reason_code"] == reason for f in own)
    args.fail_on = "never"
    args.acknowledge = "orders"
    assert _do_check(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["findings"] == facts


@pytest.mark.parametrize("mat", ["table", "view"])
def test_column_independent_rule_remains_raw_safe(tmp_path, capsys, mat):
    data = manifest()
    data["nodes"]["model.shop.orders"]["config"]["materialized"] = mat
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select * from b"},
        base_sql={"orders": "select * from a"},
        manifest=data,
        base_manifest=data,
    )
    _do_check(_make_check_args(project, fmt="json"))
    report = json.loads(capsys.readouterr().out)
    fact = next(f for f in report["findings"] if f["rule_code"] == f"ddl.replace_{mat}")
    assert fact["raw_risk"] == fact["severity"] == "safe"
    assert fact["evidence"]["state"] == "exact"


@pytest.mark.parametrize("mat", ["table", "view"])
def test_column_independent_rule_retains_proven_sql_delta(tmp_path, capsys, mat):
    data = manifest()
    data["nodes"].pop("model.shop.report")
    data["nodes"]["model.shop.orders"]["config"]["materialized"] = mat
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select 1 as id, 3 as tax"},
        base_sql={"orders": "select 1 as id, 2 as amount"},
        manifest=data,
        base_manifest=data,
    )
    assert _do_check(_make_check_args(project, fmt="json")) == 0
    report = json.loads(capsys.readouterr().out)
    fact = next(f for f in report["findings"] if f["rule_code"] == f"ddl.replace_{mat}")
    assert fact["columns_removed"] == ["amount"]
    assert fact["columns_added"] == ["tax"]
    assert fact["severity"] == fact["raw_risk"] == "safe"
    assert fact["source"]["original_file_path"] == "models/orders.sql"


def test_view_source_and_incremental_reader_keep_raw_loss(tmp_path, capsys):
    data = manifest()
    data["nodes"]["model.shop.orders"]["config"]["materialized"] = "view"
    data["child_map"] = {"model.shop.orders": ["model.shop.report"]}
    project = _make_project(
        tmp_path,
        models_sql={"orders": "select 1 as id", "report": "select id from orders"},
        base_sql={
            "orders": "select 1 as id, 2 as amount",
            "report": "select id, amount from orders",
        },
        manifest=data,
        base_manifest=data,
    )
    assert _do_check(_make_check_args(project, fmt="json")) == 1
    report = json.loads(capsys.readouterr().out)
    source = next(f for f in report["findings"] if f["rule_code"] == "ddl.replace_view")
    assert source["columns_removed"] == ["amount"]
    assert source["raw_risk"] == source["severity"] == "safe"
    assert any(
        f["rule_code"] == "ddl.drop_column"
        and f["column"] == "amount"
        and f["severity"] == "destructive"
        for f in report["findings"]
    )


def test_compiled_evidence_paths_are_portable_and_bounded(tmp_path):
    from dbt_plan.cli import _compiled_evidence_path

    project = tmp_path / "project"
    project.mkdir()
    compiled = project / "target" / "compiled" / "shop" / "orders.sql"
    # Resolving one side models /var versus /private/var on macOS; redundant
    # parent components exercise normalization on every platform.
    assert _compiled_evidence_path(compiled.resolve(), project / ".." / "project") == (
        "target/compiled/shop/orders.sql"
    )
    assert _compiled_evidence_path(project / ".dbt-plan/base/compiled/orders.sql", project) == (
        ".dbt-plan/base/compiled/orders.sql"
    )
    assert _compiled_evidence_path(tmp_path / "external-base/compiled/orders.sql", project) is None
    assert _compiled_evidence_path(None, project) is None
