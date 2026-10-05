"""Transport controls: original facts, bounded explanations, and failed reads."""

import json
from dataclasses import replace

import pytest

from dbt_plan.cli import _exit_code_for
from dbt_plan.explained_paths import explain_findings
from dbt_plan.findings import findings_from_result
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import CascadeProvenance, ReadProvenance, predict_ddl
from dbt_plan_mcp.report_validation import ReportValidationError, report_severity, validate_report


def fixture(read=None):
    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {
            f"model.shop.{name}": {
                "name": name,
                "original_file_path": f"models/{name}.sql",
                "config": {"materialized": "view"},
                "depends_on": {"nodes": ["model.shop.root"] if name == "reader" else []},
            }
            for name in ("root", "reader")
        },
    }
    pred = replace(
        predict_ddl("root", "view", None, ["id", "amount"], ["id"]),
        columns_removed=["amount"],
        provenance=CascadeProvenance(
            reads=(ReadProvenance("root", "reader", read, ("amount",), ()),),
            losses=(),
        ),
    )
    result = CheckResult(predictions=[pred])
    result.findings = findings_from_result(result, manifest)
    return result, manifest


def integrate(result, manifest):
    from dbt_plan.cli import _attach_causal_explanations

    _attach_causal_explanations(result, manifest, None)
    return result


def test_unresolved_read_is_separate_unwaivable_refusal():
    result, manifest = fixture()
    integrate(result, manifest)
    own, refusal = result.findings
    assert own.severity == "safe" and own.columns_removed == ("amount",)
    assert refusal.rule_code == "input.refusal"
    assert refusal.affected.unique_id == "model.shop.reader"
    assert refusal.severity == "warning" and not refusal.waiver_allowed
    assert _exit_code_for(result, 2) == 2
    result.acknowledge_models = ["root", "reader"]
    assert _exit_code_for(result, 2) == 2
    for policy in ("never", "destructive"):
        assert _exit_code_for(result, 2, policy) == 0
    assert _exit_code_for(result, 0) == 0
    for render in (format_text, format_github):
        output = render(result)
        assert "input.refusal" in output and "reader" in output
        assert "unknown" in output and "amount" in output
    report = json.loads(format_json(result))
    assert report_severity(validate_report(report)) == "warning"
    assert report["causal_graph"]["external_coverage"] == "unknown"


@pytest.mark.parametrize("read", [(), ("id",)])
def test_resolved_nonmatching_reads_and_dependency_are_not_warnings(read):
    result, manifest = fixture(read)
    integrate(result, manifest)
    assert len(result.findings) == 1
    assert _exit_code_for(result, 2) == 0
    assert report_severity(validate_report(json.loads(format_json(result)))) == "safe"


def test_legacy_absence_and_zero_impact_coverage():
    assert "causal_graph" not in json.loads(format_json(CheckResult()))
    result = integrate(CheckResult(findings=()), {"nodes": {}})
    assert not result.findings
    for render in (format_text, format_github):
        assert "External consumer coverage: unknown" in render(result)
    assert _exit_code_for(result, 2) == 0


def test_transport_keeps_relevant_graph_and_canonical_delta():
    result, manifest = fixture(("amount",))
    expected = explain_findings(result.findings, result.predictions, manifest).to_dict()
    integrate(result, manifest)
    report = json.loads(format_json(result))
    assert report["causal_graph"] == expected["graph"]
    assert report["findings"] == expected["findings"]
    assert validate_report(report) is report


@pytest.mark.parametrize("graph", [None, [], {}, {"nodes": []}])
def test_malformed_graph_cannot_become_all_clear(graph):
    report = json.loads(format_json(CheckResult()))
    report["causal_graph"] = graph
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_mcp_forwards_refusals_and_graph(monkeypatch):
    pytest.importorskip("mcp")
    from subprocess import CompletedProcess

    from dbt_plan_mcp import server

    result, manifest = fixture()
    report = format_json(integrate(result, manifest))
    monkeypatch.setattr(server, "_run_cli", lambda *a, **k: CompletedProcess([], 0, report, ""))
    response = server.plan(".")
    assert response["verdict"] == "review_required"
    assert response["refusals"]
    assert response["causal_graph"] == json.loads(report)["causal_graph"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("finding_index", 900),
        ("finding_index", True),
        ("cause_finding_indices", [900]),
        ("cause_finding_indices", [True]),
    ],
)
def test_graph_local_indices_are_checked(field, value):
    result, manifest = fixture(())
    report = json.loads(format_json(integrate(result, manifest)))
    report["causal_graph"]["associations"][0][field] = value
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_safe_candidate_dependency_then_unknown_kind_requires_review():
    result, manifest = fixture(())
    report = json.loads(format_json(integrate(result, manifest)))
    graph = report["causal_graph"]
    graph["edges"] = [edge for edge in graph["edges"] if edge["kind"] == "dependency"]
    assert len(graph["edges"]) == 1
    assert report_severity(validate_report(report)) == "safe"
    graph["edges"][0]["kind"] = "future_link"
    graph["future_extension"] = {"preserve": True}
    assert report_severity(validate_report(report)) == "warning"
    assert report["causal_graph"]["future_extension"] == {"preserve": True}


def test_read_checks_are_observations_not_exact_routes():
    result, manifest = fixture(())
    output = format_text(integrate(result, manifest))
    assert "resolved read check; not a column-flow edge" in output
    assert "exact direct compiled read" not in output


def test_trusted_original_filename_is_encoded_without_compiled_line():
    result, manifest = fixture(("amount",))
    path = "models/a [book]#`<x>.sql"
    manifest["nodes"]["model.shop.root"]["original_file_path"] = path
    result.findings = findings_from_result(result, manifest)
    output = format_github(integrate(result, manifest))
    assert "./models/a%20%5Bbook%5D%23%60%3Cx%3E.sql" in output
    assert "<x>" not in output


def test_shared_targets_cycles_and_human_bounds_preserve_json():
    from dbt_plan.predictor import DownstreamImpact

    result, manifest = fixture(("amount",))
    pred = result.predictions[0]
    impacts = []
    for i in range(30):
        name = f"target_{i:02}"
        manifest["nodes"][f"model.shop.{name}"] = {
            "name": name,
            "config": {"materialized": "incremental"},
            "unrendered_config": {"on_schema_change": "sync_all_columns"},
            "depends_on": {"nodes": ["model.shop.reader"]},
        }
        impacts.append(
            DownstreamImpact(
                name, "incremental", "sync_all_columns", "broken_ref", "reads removed amount"
            )
        )
    manifest["nodes"]["model.shop.reader"]["depends_on"]["nodes"].append("model.shop.target_00")
    result.predictions = [replace(pred, downstream_impacts=impacts)]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    before = format_json(result)
    output = format_text(result)
    assert "21 associations" in output
    assert "Candidate dependency graph has cycles" in output
    assert "candidate dependency; column flow unknown" in output
    assert "exact direct compiled read" in output
    assert len(result.causal_graph["associations"]) == 31
    assert len(output) < 25000
    assert format_json(result) == before
    assert _exit_code_for(result, 2) == 1
    assert format_text(result) == output


def test_schema_rejects_malformed_graph_and_accepts_real_graph():
    from pathlib import Path

    from jsonschema import Draft202012Validator, ValidationError

    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/check-report-v1.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    result, manifest = fixture(())
    report = json.loads(format_json(integrate(result, manifest)))
    validator.validate(report)
    report["causal_graph"]["associations"][0]["finding_index"] = "0"
    with pytest.raises(ValidationError):
        validator.validate(report)


def test_published_examples_reproduce_actual_cli_outputs(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parents[1]
    examples = root / "examples/explained-findings"
    process = subprocess.run(
        [sys.executable, str(examples / "generate.py"), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    for scenario in ("direct", "multihop", "unknown"):
        for suffix in ("txt", "md", "json"):
            name = f"{scenario}.{suffix}"
            assert (tmp_path / name).read_text(encoding="utf-8") == (examples / name).read_text(
                encoding="utf-8"
            )
        report = json.loads((tmp_path / f"{scenario}.json").read_text())
        assert report_severity(validate_report(report)) == (
            "warning" if scenario == "unknown" else "destructive"
        )
        own = next(f for f in report["findings"] if f["rule_code"] == "ddl.replace_view")
        assert own["columns_removed"] == ["amount"]
    unknown = json.loads((tmp_path / "unknown.json").read_text())
    assert unknown["findings"][0]["severity"] == "safe"
    assert any(f["rule_code"] == "input.refusal" for f in unknown["findings"])
    multi = json.loads((tmp_path / "multihop.json").read_text())
    assert any(n["materialization"] == "ephemeral" for n in multi["causal_graph"]["nodes"])
    assert len(multi["causal_graph"]["associations"]) >= 3


@pytest.mark.parametrize("where", ["nodes", "losses"])
@pytest.mark.parametrize("field", ["origin", "state", "reason_code"])
def test_unfamiliar_loss_evidence_requires_review_with_known_unknown_control(where, field):
    result, manifest = fixture(())
    report = json.loads(format_json(integrate(result, manifest)))
    evidence = {
        "origin": "resolved_columns",
        "state": "unknown",
        "reason_code": "output_loss_attribution_unproved",
        "columns": ["amount"],
        "compiled_path": None,
    }
    node = report["causal_graph"]["nodes"][0]
    if where == "nodes":
        node["loss_evidence"] = evidence
    else:
        report["causal_graph"]["losses"] = [
            {
                "root": node["resource"],
                "resource": node["resource"],
                "columns_removed": ["amount"],
                "evidence": evidence,
            }
        ]
    assert report_severity(validate_report(report)) == "safe"
    evidence[field] = "future_value"
    assert report_severity(validate_report(report)) == "warning"


def test_refusal_roots_do_not_depend_on_canonical_sort_order():
    from dbt_plan.predictor import DownstreamImpact

    result, manifest = fixture()
    root = result.predictions[0]
    manifest["nodes"]["model.shop.zroot"] = {"name": "zroot", "config": {"materialized": "view"}}
    zroot = replace(
        root,
        model_name="zroot",
        downstream_impacts=[DownstreamImpact("reader", "view", None, "broken_ref", "known risk")],
    )
    result.predictions = [zroot, root]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    refusals = [f for f in result.findings if f.rule_code == "input.refusal"]
    assert len(refusals) == 1
    assert "while checking root;" in refusals[0].message


def test_additive_nested_graph_fields_survive_validation():
    result, manifest = fixture(())
    report = json.loads(format_json(integrate(result, manifest)))
    association = report["causal_graph"]["associations"][0]
    association["source"]["future_metadata"] = "retained"
    association["evidence"]["future_metadata"] = "retained"
    assert validate_report(report) is report
    assert report_severity(report) == "safe"


def test_operation_column_is_shown_even_outside_column_list_cap():
    result, manifest = fixture(())
    original = result.predictions[0]
    pred = replace(
        predict_ddl(
            "root",
            "incremental",
            "sync_all_columns",
            [f"a{i:02}" for i in range(20)] + ["zz_removed"],
            [f"a{i:02}" for i in range(20)],
        ),
        provenance=original.provenance,
    )
    result.predictions = [pred]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    assert "column: zz_removed" in format_text(result)


@pytest.mark.parametrize("shape", ["chain", "diamond"])
def test_representative_routes_do_not_enumerate_alternative_paths(shape):
    from dbt_plan.predictor import DownstreamImpact

    result, manifest = fixture(())
    if shape == "chain":
        names = ["root", *[f"m{i:03}" for i in range(1, 200)]]
        edges = list(zip(names, names[1:], strict=False))
        target = names[-1]
    else:
        layers = [["root"], *[[f"l{i}a", f"l{i}b"] for i in range(40)], ["target"]]
        names = [name for layer in layers for name in layer]
        edges = [
            (a, b)
            for left, right in zip(layers, layers[1:], strict=False)
            for a in left
            for b in right
        ]
        target = "target"
    manifest["nodes"] = {
        f"model.shop.{name}": {
            "name": name,
            "config": {"materialized": "ephemeral"},
            "depends_on": {"nodes": [f"model.shop.{a}" for a, b in edges if b == name]},
        }
        for name in names
    }
    result.predictions = [
        replace(
            result.predictions[0],
            provenance=None,
            downstream_impacts=[
                DownstreamImpact(target, "view", None, "broken_ref", "affected target")
            ],
        )
    ]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    before = format_json(result)
    for render in (format_text, format_github):
        text = render(result)
        assert f"model.shop.{target}" in text
        assert f"{len(edges) - 8} graph edges not displayed" in text
        assert (
            "191 route steps omitted" in text
            if shape == "chain"
            else "33 route steps omitted" in text
        )
        assert len(text) < 15000
    assert len(result.causal_graph["edges"]) == len(edges)
    assert before == format_json(result)


def test_existing_known_risk_waiver_and_destructive_precedence_survive():
    from dbt_plan.predictor import DownstreamImpact

    result, manifest = fixture()
    result.predictions = [
        replace(
            result.predictions[0],
            downstream_impacts=[
                DownstreamImpact("reader", "view", None, "broken_ref", "known affected column")
            ],
        )
    ]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    assert not any(f.rule_code == "input.refusal" for f in result.findings)
    assert _exit_code_for(result, 2) == 1
    result.acknowledge_models = ["reader", "root"]
    assert _exit_code_for(result, 2) == 0
    assert report_severity(validate_report(json.loads(format_json(result)))) == "destructive"


def test_graph_consumer_imports_without_optional_packages():
    import subprocess
    import sys
    from pathlib import Path

    source = str(Path(__file__).parents[1] / "src")
    script = f"import sys; sys.path.insert(0, {source!r}); sys.modules['mcp'] = None; from dbt_plan_mcp.report_validation import validate_report; assert sys.modules['mcp'] is None"
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_versioned_defined_in_alias_is_same_qualified_resource():
    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {
            "model.shop.orders.v2": {
                "name": "orders",
                "version": 2,
                "path": "models/orders_current.sql",
                "original_file_path": "models/orders_current.sql",
                "config": {"materialized": "view"},
            }
        },
    }
    result = CheckResult(predictions=[predict_ddl("orders_current", "view", None, ["id"], ["id"])])
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    report = json.loads(format_json(result))
    assert report["findings"][0]["source"]["name"] == "orders_current"
    assert report["causal_graph"]["associations"][0]["source"]["name"] == "orders"
    assert report_severity(validate_report(report)) == "safe"
    report["causal_graph"]["associations"][0]["source"]["unique_id"] = "model.shop.other.v2"
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_known_risk_and_unresolved_read_aliases_share_affected_identity():
    from dbt_plan.predictor import DownstreamImpact

    result, manifest = fixture()
    reader = manifest["nodes"].pop("model.shop.reader")
    reader.update(name="orders", version=2, path="models/orders_current.sql")
    manifest["nodes"]["model.shop.orders.v2"] = reader
    pred = result.predictions[0]
    result.predictions = [
        replace(
            pred,
            downstream_impacts=[
                DownstreamImpact("orders_current", "view", None, "broken_ref", "known risk")
            ],
            provenance=CascadeProvenance(
                reads=(ReadProvenance("root", "model.shop.orders.v2", None, ("amount",)),)
            ),
        )
    ]
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    assert not any(f.rule_code == "input.refusal" for f in result.findings)


def test_input_refusal_without_change_does_not_suggest_a_deletion():
    result = CheckResult(uncompiled_models=["reader"])
    _, manifest = fixture(())
    result.findings = findings_from_result(result, manifest)
    integrate(result, manifest)
    for render in (format_text, format_github):
        output = render(result).lower()
        assert "compile" in output
        assert "removed" not in output
        assert "source change: not recorded" in output
