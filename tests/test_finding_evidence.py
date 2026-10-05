"""Producer contract controls; no renderer or warehouse is involved."""

from dataclasses import replace

import pytest

from dbt_plan.findings import Evidence, InputRefusal, findings_from_predictions
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety


def manifest(*entries):
    return {"nodes": dict(entries)}


def node(name="orders", **extra):
    return {"name": name, "resource_type": "model", **extra}


def prediction(name="orders", **extra):
    return DDLPrediction(
        name,
        "incremental",
        "sync_all_columns",
        Safety.DESTRUCTIVE,
        operations=[DDLOperation("DROP COLUMN", "book_id")],
        **extra,
    )


def test_version_defined_in_and_package_ids_are_manifest_authority():
    m = manifest(("model.shop.orders.v2", node(path="models/orders_current.sql", version=2)))
    for name in ("orders_current", "orders_v2", "model.shop.orders.v2"):
        (fact,) = findings_from_predictions([prediction(name)], m)
        assert fact.source.unique_id == "model.shop.orders.v2"
        assert fact.affected == fact.source
        assert fact.column == "book_id"
        assert fact.rule_code == "ddl.drop_column"


def test_collisions_do_not_choose_first_or_guess_package():
    m = manifest(("model.a.orders", node()), ("model.b.orders", node()))
    (fact,) = findings_from_predictions([prediction()], m)
    assert fact.source.unique_id is None
    assert fact.source.candidates == ("model.a.orders", "model.b.orders")
    assert "ambiguous_resource" in fact.uncertainty
    assert fact.severity == "destructive"


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "../orders.sql",
        "/tmp/orders.sql",
        "C:\\orders.sql",
        "target/compiled/p/orders.sql",
    ],
)
def test_missing_or_untrustworthy_paths_never_become_source_locations(path):
    (fact,) = findings_from_predictions(
        [prediction()], manifest(("model.p.orders", node(original_file_path=path)))
    )
    assert fact.source.original_file_path is None
    assert "line" not in fact.to_dict()["source"]


def test_source_path_is_not_compiled_path_and_removed_nodes_use_base():
    m = manifest(
        (
            "model.p.orders",
            node(
                original_file_path="models/orders.sql", compiled_path="target/compiled/orders.sql"
            ),
        )
    )
    m["metadata"] = {"project_name": "p"}
    (fact,) = findings_from_predictions([prediction()], {}, base_manifest=m)
    assert fact.source.original_file_path == "models/orders.sql"


def test_cascade_without_provenance_is_unknown_and_shared_sources_survive():
    impact = DownstreamImpact("reader", "view", None, "broken_ref", "names book_id")
    m = manifest(
        ("model.p.orders", node()),
        ("model.p.books", node("books")),
        ("model.p.reader", node("reader")),
    )
    facts = findings_from_predictions(
        [
            prediction(downstream_impacts=[impact]),
            prediction("books", downstream_impacts=[impact]),
        ],
        m,
    )
    cascades = [f for f in facts if f.rule_code == "cascade.broken_ref"]
    assert len(cascades) == 2
    assert {f.source.unique_id for f in cascades} == {"model.p.orders", "model.p.books"}
    assert all(f.evidence.state == "unknown" and f.column is None for f in cascades)


@pytest.mark.parametrize(
    "state,origin",
    [("exact", "resolved_read"), ("conservative", "text_search"), ("unknown", "unresolved_input")],
)
def test_explicit_evidence_preserves_read_certainty(state, origin):
    impact = DownstreamImpact("reader", "view", None, "broken_ref", "read evidence")
    m = manifest(("model.p.orders", node()), ("model.p.reader", node("reader")))
    evidence = Evidence(
        origin, state, "read_checked", columns=("book_id",), compiled_path="target/reader.sql"
    )
    facts = findings_from_predictions(
        [prediction(downstream_impacts=[impact])],
        m,
        evidence={("model.p.orders", "model.p.reader", "cascade.broken_ref"): evidence},
    )
    fact = next(f for f in facts if f.rule_code == "cascade.broken_ref")
    assert fact.evidence == evidence
    assert fact.evidence.compiled_path == "target/reader.sql"


def test_acknowledgements_cannot_hide_raw_findings_or_refusals():
    from dbt_plan.formatter import CheckResult

    pred = prediction()
    result = CheckResult(predictions=[pred], acknowledge_models=["orders"])
    assert result.own_waived(pred)
    refusal = InputRefusal("missing_compiled_sql", "current SQL missing", resource="orders")
    facts = findings_from_predictions(
        result.predictions, manifest(("model.p.orders", node())), refusals=[refusal]
    )
    assert {f.severity for f in facts} == {"warning", "destructive"}
    assert next(f for f in facts if f.rule_code == "input.refusal").waiver_allowed is False


def test_unknown_operation_and_risk_never_default_to_safe():
    pred = replace(
        prediction(),
        safety=Safety.SAFE,
        operations=[DDLOperation("FUTURE")],
        downstream_impacts=[DownstreamImpact("orders", "view", None, "future_risk", "new")],
    )
    facts = findings_from_predictions([pred], manifest(("model.p.orders", node())))
    assert all(f.severity == "warning" and not f.waiver_allowed for f in facts)
    assert {f.raw_risk for f in facts} == {"safe", "future_risk"}


def test_equivalent_iteration_order_has_identical_serialization():
    entries = [("model.p.orders", node()), ("model.p.books", node("books"))]
    preds = [prediction(), prediction("books")]
    first = findings_from_predictions(preds, manifest(*entries))
    second = findings_from_predictions(preds[::-1], manifest(*entries[::-1]))
    assert [f.to_dict() for f in first] == [f.to_dict() for f in second]


@pytest.mark.parametrize("bad", [None, {}, [None], [{"safety": "safe"}]])
def test_malformed_producer_inputs_raise_instead_of_empty_safe_report(bad):
    with pytest.raises((ValueError, TypeError)):
        findings_from_predictions(bad, {})


def test_unknown_evidence_cannot_certify_safe_prediction():
    pred = replace(prediction(), safety=Safety.SAFE, operations=[DDLOperation("NO DDL")])
    facts = findings_from_predictions(
        [pred],
        manifest(("model.p.orders", node())),
        evidence={
            ("model.p.orders", "model.p.orders", "ddl.no_ddl"): Evidence(
                "parser", "unknown", "parse_failed"
            )
        },
    )
    assert facts[0].severity == "warning"
    assert not facts[0].waiver_allowed


def test_result_adapter_preserves_every_input_refusal_and_raw_acknowledged_risk():
    from dbt_plan.findings import findings_from_result
    from dbt_plan.formatter import CheckResult

    result = CheckResult(
        predictions=[prediction()],
        acknowledge_models=["orders"],
        parse_failures=["orders"],
        skipped_models=["missing"],
        uncompiled_models=["orders"],
        stale_sources=["models/orders.sql"],
        baseline_problem="corrupt",
    )
    facts = findings_from_result(result, manifest(("model.p.orders", node())))
    refusals = [f for f in facts if f.rule_code == "input.refusal"]
    assert {f.evidence.reason_code for f in refusals} == {
        "parse_failed",
        "missing_manifest_resource",
        "missing_compiled_sql",
        "stale_source",
        "baseline_corrupt",
    }
    assert all(f.severity == "warning" and not f.waiver_allowed for f in refusals)
    assert any(f.severity == "destructive" for f in facts)


def test_result_adapter_accepts_node_index_without_inventing_paths():
    from dbt_plan.findings import findings_from_result
    from dbt_plan.formatter import CheckResult
    from dbt_plan.manifest import build_node_index

    m = manifest(("model.p.orders.v2", node(path="models/current_orders.sql", version=2)))
    facts = findings_from_result(
        CheckResult(predictions=[prediction("current_orders")]), node_index=build_node_index(m)
    )
    assert facts[0].source.unique_id == "model.p.orders.v2"
    assert facts[0].source.original_file_path is None


def test_relation_alias_is_not_a_model_name():
    m = manifest(("model.p.orders", node(alias="books")), ("model.p.books", node("books")))
    (fact,) = findings_from_predictions([prediction("books")], m)
    assert fact.source.unique_id == "model.p.books"


def test_unknown_safety_and_malformed_manifest_are_errors():
    with pytest.raises(ValueError):
        findings_from_predictions([replace(prediction(), safety="future")], {})
    with pytest.raises(ValueError):
        findings_from_predictions([], {"nodes": []})


def test_aggregate_columns_are_sorted_and_not_parsed_from_messages():
    (fact,) = findings_from_predictions(
        [prediction(columns_removed=["z", "a", "z"])], manifest(("model.p.orders", node()))
    )
    assert fact.columns_removed == ("a", "z")
    assert fact.columns == ("book_id",)


def test_refusal_cannot_be_overridden_by_exact_evidence():
    (fact,) = findings_from_predictions(
        [],
        manifest(("model.p.orders", node())),
        refusals=[InputRefusal("parse_failed", "cannot parse", "orders")],
        evidence={
            ("model.p.orders", "model.p.orders", "input.refusal"): Evidence(
                "parser", "exact", "read_checked"
            )
        },
    )
    assert fact.evidence.state == "unknown"
    assert "parse_failed" in fact.uncertainty


@pytest.mark.parametrize("operations", [[DDLOperation("")], [None], "NO DDL"])
def test_malformed_operations_raise(operations):
    with pytest.raises((TypeError, ValueError)):
        findings_from_predictions([replace(prediction(), operations=operations)], {})


def test_dependency_package_path_is_not_a_root_project_source_link():
    m = manifest(("model.dependency.orders", node(original_file_path="models/orders.sql")))
    m["metadata"] = {"project_name": "shop"}
    (fact,) = findings_from_predictions([prediction()], m)
    assert fact.source.unique_id == "model.dependency.orders"
    assert fact.source.original_file_path is None


def test_test_resource_ids_and_version_collision_are_preserved():
    m = manifest(
        ("model.p.orders.v1", node(version=1)),
        ("model.p.orders.v2", node(version=2)),
        ("test.p.check_orders.hash", {"name": "check_orders", "resource_type": "test"}),
    )
    impact = DownstreamImpact("check_orders", "data_test", None, "data_test_failure", "fixture")
    facts = findings_from_predictions([prediction(downstream_impacts=[impact])], m)
    fact = next(f for f in facts if f.rule_code == "cascade.data_test_failure")
    assert fact.source.unique_id is None
    assert fact.affected.unique_id == "test.p.check_orders.hash"
    assert fact.source.candidates == ("model.p.orders.v1", "model.p.orders.v2")


def test_result_malformed_input_is_rejected():
    from dbt_plan.findings import findings_from_result

    with pytest.raises(ValueError):
        findings_from_result({"predictions": []})


def test_result_known_ambiguity_survives_a_lossy_node_index():
    from dbt_plan.findings import findings_from_result
    from dbt_plan.formatter import CheckResult
    from dbt_plan.manifest import build_node_index

    result = CheckResult(
        predictions=[prediction()], ambiguous_resources={"orders"}, acknowledge_models=["orders"]
    )
    (fact,) = findings_from_result(
        result, node_index=build_node_index(manifest(("model.p.orders", node())))
    )
    assert fact.source.unique_id is None
    assert "ambiguous_resource" in fact.uncertainty
    assert not fact.waiver_allowed


def test_cascade_escalation_does_not_relabel_own_safe_operation():
    pred = replace(
        prediction(),
        own_safety=Safety.SAFE,
        operations=[DDLOperation("CREATE OR REPLACE VIEW")],
        downstream_impacts=[DownstreamImpact("reader", "view", None, "broken_ref", "reads")],
    )
    m = manifest(("model.p.orders", node()), ("model.p.reader", node("reader")))
    facts = findings_from_predictions(
        [pred],
        m,
        evidence={
            ("model.p.orders", "model.p.orders", "ddl.replace_view"): Evidence(
                "resolved_columns", "exact", "ddl_rule"
            )
        },
    )
    assert next(f for f in facts if f.rule_code == "ddl.replace_view").severity == "safe"
    assert next(f for f in facts if f.rule_code == "cascade.broken_ref").severity == "destructive"


def test_serialization_is_json_compatible_and_independent():
    import json

    (fact,) = findings_from_predictions([prediction()], manifest(("model.p.orders", node())))
    payload = fact.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    payload["source"]["candidates"].clear()
    assert fact.source.candidates == ("model.p.orders",)


def test_input_and_nested_lists_are_not_mutated():
    import copy

    preds = [prediction(columns_removed=["z", "a"])]
    m = manifest(("model.p.orders", node()))
    original = copy.deepcopy((preds, m))
    findings_from_predictions(preds, m)
    assert (preds, m) == original


@pytest.mark.parametrize("safety", [Safety.SAFE, Safety.DESTRUCTIVE])
def test_ddl_without_producer_provenance_cannot_claim_exact_or_be_waived(safety):
    pred = replace(prediction(), safety=safety)
    (fact,) = findings_from_predictions([pred], manifest(("model.p.orders", node())))
    assert fact.evidence.state == "conservative"
    assert "provenance_unavailable" in fact.uncertainty
    assert not fact.waiver_allowed
    assert fact.raw_risk == safety.value
    assert fact.severity == ("warning" if safety == Safety.SAFE else "destructive")


def test_explicit_ddl_producer_evidence_can_establish_exact_fact():
    (fact,) = findings_from_predictions(
        [prediction()],
        manifest(("model.p.orders", node())),
        evidence={
            ("model.p.orders", "model.p.orders", "ddl.drop_column"): Evidence(
                "resolved_columns", "exact", "column_diff_checked", columns=("book_id",)
            )
        },
    )
    assert fact.evidence.state == "exact"
    assert fact.waiver_allowed
    assert fact.uncertainty == ()


@pytest.mark.parametrize("project", [None, "", "other", 1])
def test_single_package_cannot_prove_source_path_without_declared_matching_root(project):
    m = manifest(("model.p.orders", node(original_file_path="models/orders.sql")))
    if project is not None:
        m["metadata"] = {"project_name": project}
    (fact,) = findings_from_predictions([prediction()], m)
    assert fact.source.unique_id == "model.p.orders"
    assert fact.source.original_file_path is None


def test_declared_matching_root_proves_source_path():
    m = manifest(("model.p.orders", node(original_file_path="models/orders.sql")))
    m["metadata"] = {"project_name": "p"}
    (fact,) = findings_from_predictions([prediction()], m)
    assert fact.source.original_file_path == "models/orders.sql"


def test_manifest_fallback_review_and_column_operation_both_retain_uncertainty():
    from dbt_plan.predictor import predict_ddl

    pred = predict_ddl("orders", "incremental", "sync_all_columns", ["id", "book_id"], ["id"])
    pred = replace(
        pred,
        operations=[
            DDLOperation("REVIEW REQUIRED (columns came from the manifest, not the SQL)"),
            *pred.operations,
        ],
    )
    facts = findings_from_predictions([pred], manifest(("model.p.orders", node())))
    drop = next(f for f in facts if f.rule_code == "ddl.drop_column")
    review = next(f for f in facts if f.rule_code == "ddl.review_required")
    assert drop.evidence.state == "conservative"
    assert review.evidence.state == "unknown"
    assert all(
        f.severity == "destructive" and f.uncertainty and not f.waiver_allowed for f in facts
    )
