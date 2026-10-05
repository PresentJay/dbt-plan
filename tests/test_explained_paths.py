"""Independent graph expectations: evidence is not inferred from cascade prose."""

from dataclasses import replace

import pytest

from dbt_plan.columns import columns_read_from
from dbt_plan.explained_paths import explain_predictions
from dbt_plan.findings import Evidence
from dbt_plan.manifest import ModelNode
from dbt_plan.predictor import analyze_cascade_impacts, predict_ddl


def uid(name):
    return f"model.shop.{name}"


def manifest(names, edges=()):
    return {
        "metadata": {"project_name": "shop"},
        "nodes": {
            uid(n): {
                "name": n,
                "original_file_path": f"models/{n}.sql",
                "config": {"materialized": "table"},
                "depends_on": {"nodes": [uid(a) for a, b in edges if b == n]},
            }
            for n in names
        },
    }


def cascade(tmp_path, sqls, edges, *, losses=(), ephemeral=(), unknown=()):
    names = ["root", *sqls]
    m = manifest(names, edges)
    schema = {n: {"id": "INT", "book_id": "INT"} for n in names}
    paths = {}
    for n, sql in sqls.items():
        paths[n] = tmp_path / f"{n}.sql"
        paths[n].write_text(sql)
    nodes = {
        n: ModelNode(
            uid(n), n, "ephemeral" if n in ephemeral else "incremental", "sync_all_columns"
        )
        for n in sqls
    }
    for n, node in nodes.items():
        m["nodes"][uid(n)]["config"] = {
            "materialized": node.materialization,
            "on_schema_change": node.on_schema_change,
        }
        m["nodes"][uid(n)]["unrendered_config"] = {"on_schema_change": "sync_all_columns"}
    pred = predict_ddl("root", "incremental", "sync_all_columns", ["id", "book_id"], ["id"])
    predictions, _ = analyze_cascade_impacts(
        [pred],
        {"root": uid("root")},
        {"root": (["id", "book_id"], ["id"])},
        {uid("root"): [uid(n) for n in sqls]},
        nodes,
        {},
        paths,
        base_columns_of=lambda n: ["id", "book_id"],
        current_columns_of=lambda n: ["id"] if n in losses else ["id", "book_id"],
        columns_read_of=lambda reader, source: (
            None
            if (reader, source) in unknown
            else columns_read_from(sqls[reader], source, schema, dialect="duckdb")
        ),
    )
    return predictions, m


def edge_set(report, kind):
    return {
        (e.source.name, e.target.name, e.evidence.state)
        for e in report.graph.edges
        if e.kind == kind
    }


def test_direct_read_retains_actual_columns_and_cause(tmp_path):
    predictions, m = cascade(
        tmp_path, {"reader": "select id, book_id from root"}, [("root", "reader")]
    )
    report = explain_predictions(predictions, m)
    assert edge_set(report, "read") == {("root", "reader", "exact")}
    read = next(e for e in report.graph.edges if e.kind == "read")
    assert read.evidence.columns == ("book_id", "id")
    assert read.columns_removed == ("book_id",)
    assert report.graph.associations[-1].source is not None
    assert {n.resource.unique_id for n in report.graph.nodes} == {uid("root"), uid("reader")}
    assert all(n.resource.original_file_path for n in report.graph.nodes)


def test_multihop_ephemeral_star_is_not_exact_column_flow(tmp_path):
    p, m = cascade(
        tmp_path,
        {"mid": "select * from root", "reader": "select book_id from mid"},
        [("root", "mid"), ("mid", "reader")],
        losses=("mid",),
        ephemeral=("mid",),
    )
    report = explain_predictions(p, m)
    assert ("mid", "reader", "exact") in edge_set(report, "read")
    assert ("root", "reader", "exact") not in edge_set(report, "read")
    assert edge_set(report, "dependency") == {
        ("root", "mid", "unknown"),
        ("mid", "reader", "unknown"),
    }
    mid = next(n for n in report.graph.nodes if n.resource.name == "mid")
    assert mid.columns_removed == ("book_id",)
    assert mid.materialization == "ephemeral"
    impact = next(f for f in report.findings if f.rule_code == "cascade.broken_ref")
    assert impact.source.unique_id == uid("root")
    assert impact.evidence.state == "unknown"  # the full root association is not proved


def test_shared_parents_keep_distinct_targets(tmp_path):
    p, m = cascade(
        tmp_path,
        {
            "mid": "select * from root",
            "a": "select book_id from mid",
            "b": "select book_id from mid",
        },
        [("root", "mid"), ("mid", "a"), ("mid", "b")],
        losses=("mid",),
    )
    report = explain_predictions(p, m)
    assert {("mid", "a", "exact"), ("mid", "b", "exact")} <= edge_set(report, "read")
    assert {a.affected.unique_id for a in report.graph.associations} >= {uid("a"), uid("b")}


def test_cycles_are_finite_and_order_independent(tmp_path):
    p, m = cascade(
        tmp_path,
        {"mid": "select * from root", "reader": "select book_id from mid"},
        [("root", "mid"), ("mid", "reader"), ("reader", "mid")],
        losses=("mid",),
    )
    report = explain_predictions(p, m)
    assert report.graph.has_cycles
    m["nodes"] = dict(reversed(list(m["nodes"].items())))
    for node in m["nodes"].values():
        node["depends_on"]["nodes"].reverse()
    assert explain_predictions(p, m).to_dict() == report.to_dict()


def test_fallback_has_no_fabricated_exact_path(tmp_path):
    p, m = cascade(
        tmp_path,
        {"reader": "select book_id from root"},
        [("root", "reader")],
        unknown=(("reader", "root"),),
    )
    report = explain_predictions(p, m)
    assert edge_set(report, "read") == {("root", "reader", "conservative")}
    assert next(e for e in report.graph.edges if e.kind == "read").evidence.origin == "text_search"
    assert all(f.severity != "safe" for f in report.findings if f.rule_code.startswith("cascade."))


def test_unrelated_same_name_column_is_not_a_read(tmp_path):
    p, m = cascade(
        tmp_path,
        {"reader": "select other.book_id from root join other on root.id = other.id"},
        [("root", "reader")],
    )
    # The SQL resolver can resolve this single named other relation without its schema.
    report = explain_predictions(p, m)
    assert not any(
        e.columns_removed
        for e in report.graph.edges
        if e.kind == "read" and e.evidence.state == "exact"
    )


def test_collision_does_not_certify_index_winner(tmp_path):
    p, m = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    m["nodes"]["model.vendor.root"] = {"name": "root", "original_file_path": "models/root.sql"}
    report = explain_predictions(p, m)
    read = next(e for e in report.graph.edges if e.kind == "read")
    assert read.source.unique_id is None
    assert read.source.candidates == (uid("root"), "model.vendor.root")
    assert read.evidence.state == "unknown"
    assert read.source.original_file_path is None


def test_zero_impacts_never_assert_external_coverage():
    report = explain_predictions([], manifest(["unrelated"]))
    assert report.graph.nodes == ()
    assert report.graph.external_coverage == "unknown"
    assert report.findings == ()


def test_cause_is_actual_delta_not_all_evidence_columns():
    pred = predict_ddl("root", "incremental", "sync_all_columns", ["id", "book_id"], ["id"])
    report = explain_predictions(
        [pred],
        manifest(["root"]),
        evidence={
            (uid("root"), uid("root"), "ddl.drop_column"): Evidence(
                "compiled_sql", "exact", "column_diff_checked", ("id", "book_id")
            )
        },
    )
    assert report.graph.associations[0].columns_removed == ("book_id",)
    assert report.findings[0].columns == ("book_id", "id")


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/source.sql",
        "../escape.sql",
        "target/compiled/x.sql",
        "C:/source.sql",
        "dbt_packages/x.sql",
    ],
)
def test_original_path_trust_reused(path):
    p = predict_ddl("root", "view", None, ["id"], ["id"])
    m = manifest(["root"])
    m["nodes"][uid("root")]["original_file_path"] = path
    report = explain_predictions([p], m)
    assert report.graph.nodes[0].resource.original_file_path is None


def test_graph_of_diamonds_is_linear_not_all_paths():
    layers = [["root"], *[[f"a{i}", f"b{i}"] for i in range(40)], ["target"]]
    names = [n for layer in layers for n in layer]
    edges = [
        (a, b) for prev, nxt in zip(layers, layers[1:], strict=False) for a in prev for b in nxt
    ]
    from dbt_plan.predictor import DownstreamImpact

    p = predict_ddl("root", "view", None, ["id"], ["id"])
    p = replace(
        p, downstream_impacts=[DownstreamImpact("target", "table", None, "broken_ref", "opaque")]
    )
    report = explain_predictions([p], manifest([*names, "unrelated"], edges))
    assert len(report.graph.nodes) == 82
    assert len(report.graph.edges) == 160
    assert not report.graph.has_cycles
    assert len(report.graph.associations) == 2


def test_provenance_does_not_change_legacy_comparisons(tmp_path):
    p, _ = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    assert p[0].provenance is not None
    assert p[0] == replace(p[0], provenance=None)


def test_relation_alias_collision_cannot_certify_read(tmp_path):
    p, m = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    m["nodes"][uid("root")]["alias"] = "shared_relation"
    m["nodes"][uid("other")] = {"name": "other", "alias": "shared_relation"}
    report = explain_predictions(p, m)
    read = next(e for e in report.graph.edges if e.kind == "read")
    assert read.evidence.state == "unknown"
    assert read.evidence.reason_code == "ambiguous_relation"
    assert read.evidence.columns == ("book_id",)


def test_existing_canonical_transport_facts_are_preserved(tmp_path):
    from dbt_plan.explained_paths import explain_findings
    from dbt_plan.findings import findings_from_predictions

    p, m = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    facts = findings_from_predictions(p, m)
    report = explain_findings(facts, p, m)
    assert report.findings is facts
    for a in report.graph.associations:
        assert report.findings[a.finding_index].evidence == a.evidence
        assert a.columns_removed == ("book_id",)
        assert all(
            report.findings[i].rule_code.startswith("ddl.") for i in a.cause_finding_indices
        )


def test_base_dependency_is_not_lost_when_current_link_removed():
    from dbt_plan.predictor import DownstreamImpact

    base = manifest(["root", "mid", "target"], [("root", "mid"), ("mid", "target")])
    current = manifest(["root", "mid", "target"])
    p = replace(
        predict_ddl("root", "view", None, ["id"], ["id"]),
        downstream_impacts=[DownstreamImpact("target", "view", None, "broken_ref", "opaque")],
    )
    report = explain_predictions([p], current, base_manifest=base)
    assert edge_set(report, "dependency") == {
        ("root", "mid", "unknown"),
        ("mid", "target", "unknown"),
    }
    assert all(e.revisions == ("base",) for e in report.graph.edges)


def test_exposures_are_declared_and_external_coverage_stays_unknown():
    from dbt_plan.manifest import ExposureNode

    m = manifest(["root"])
    m["exposures"] = {
        "exposure.shop.dashboard": {"name": "dashboard", "depends_on": {"nodes": [uid("root")]}}
    }
    p = replace(
        predict_ddl("root", "view", None, ["id"], ["id"]),
        downstream_exposures=[ExposureNode("exposure.shop.dashboard", "dashboard", "dashboard")],
    )
    report = explain_predictions([p], m)
    assert edge_set(report, "declared_consumer") == {("root", "dashboard", "unknown")}
    assert report.graph.external_coverage == "unknown"
    assert len(report.findings) == 1


def test_unknown_read_without_sql_survives(tmp_path):
    p, m = cascade(
        tmp_path,
        {"reader": "select id from root"},
        [("root", "reader")],
        unknown=(("reader", "root"),),
    )
    report = explain_predictions(p, m)
    checks = [e for e in report.graph.edges if e.kind == "read_check"]
    assert len(checks) == 1
    assert checks[0].evidence.state == "unknown"
    assert checks[0].evidence.columns == ()


def test_read_root_and_loss_context_survive_shared_sources(tmp_path):
    p, m = cascade(
        tmp_path,
        {"mid": "select * from root", "reader": "select book_id from mid"},
        [("root", "mid"), ("mid", "reader")],
        losses=("mid",),
    )
    other = replace(p[0], model_name="other")
    m["nodes"][uid("other")] = {"name": "other"}
    report = explain_predictions([p[0], other], m)
    reads = [e for e in report.graph.edges if e.kind == "read"]
    assert {e.root.unique_id for e in reads} == {uid("root"), uid("other")}
    assert {(loss.root.unique_id, loss.resource.unique_id) for loss in report.graph.losses} >= {
        (uid("root"), uid("mid")),
        (uid("other"), uid("mid")),
    }


def test_resolved_empty_read_retained_as_check_not_path(tmp_path):
    p, m = cascade(
        tmp_path, {"reader": "select * from root"}, [("root", "reader")], losses=("reader",)
    )
    report = explain_predictions(p, m)
    assert edge_set(report, "read") == set()
    assert edge_set(report, "read_check") == {("root", "reader", "exact")}
    assert next(e for e in report.graph.edges if e.kind == "read_check").evidence.columns == ()


@pytest.mark.parametrize("mutation", ["id", "metadata", "collision"])
def test_mismatched_composition_is_rejected(mutation):
    from dbt_plan.explained_paths import explain_findings
    from dbt_plan.findings import findings_from_predictions

    p = [predict_ddl("root", "view", None, ["id"], ["id"])]
    m = manifest(["root"])
    facts = findings_from_predictions(p, m)
    if mutation == "id":
        m["nodes"][uid("root")]["unique_id"] = "model.other.root"
    elif mutation == "metadata":
        m["metadata"] = {}
    else:
        m["nodes"]["model.other.root"] = {"name": "root"}
    with pytest.raises(ValueError):
        explain_findings(facts, p, m)


@pytest.mark.parametrize("bad", [None, {}, [None], ["legacy"]])
def test_malformed_findings_are_errors(bad):
    from dbt_plan.explained_paths import explain_findings

    with pytest.raises(TypeError):
        explain_findings(bad, [], {})


def test_json_is_fresh_and_contains_all_facts(tmp_path):
    import json

    p, m = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    report = explain_predictions(p, m)
    original = report.to_dict()
    edited = report.to_dict()
    edited["graph"]["edges"].clear()
    edited["findings"].clear()
    assert report.to_dict() == original
    assert json.loads(json.dumps(original)) == original


def test_known_ambiguity_and_missing_metadata_never_certify_paths(tmp_path):
    p, m = cascade(tmp_path, {"reader": "select book_id from root"}, [("root", "reader")])
    m.pop("metadata")
    report = explain_predictions(p, m, ambiguous_resources=("root",))
    assert all(n.resource.original_file_path is None for n in report.graph.nodes)
    assert next(e for e in report.graph.edges if e.kind == "read").evidence.state == "unknown"


def test_provenance_capture_preserves_policy_and_callback_count(tmp_path):
    from dbt_plan.predictor import Safety

    calls = []
    p = predict_ddl("root", "view", None, ["id", "book_id"], ["id"])

    def read(reader, source):
        calls.append((reader, source))
        return ["book_id"]

    result, _ = analyze_cascade_impacts(
        [p],
        {"root": uid("root")},
        {"root": (["id", "book_id"], ["id"])},
        {uid("root"): [uid("reader")]},
        {"reader": ModelNode(uid("reader"), "reader", "table", None)},
        {},
        {},
        columns_read_of=read,
    )
    assert calls == [("reader", "root")]
    assert result[0].own_verdict == Safety.SAFE
    assert result[0].safety == Safety.DESTRUCTIVE
    assert [(i.risk, i.reason) for i in result[0].downstream_impacts] == [
        ("broken_ref", "reads dropped column(s): book_id")
    ]


def test_manifest_reference_to_missing_source_is_explicitly_unknown():
    from dbt_plan.predictor import DownstreamImpact

    m = manifest(["reader"])
    m["nodes"][uid("reader")]["depends_on"] = {"nodes": [uid("missing")]}
    p = replace(
        predict_ddl(uid("missing"), "view", None, ["id"], ["id"]),
        downstream_impacts=[DownstreamImpact("reader", "view", None, "broken_ref", "opaque")],
    )
    report = explain_predictions([p], m)
    assert len(report.graph.edges) == 1
    edge = report.graph.edges[0]
    assert edge.source.name == uid("missing")
    assert edge.source.unique_id is None
    assert edge.evidence.state == "unknown"


def test_real_producer_chain_excludes_only_empty_non_relationship_checks():
    import json

    names = ["root", *[f"m{i}" for i in range(1, 200)]]
    dependencies = list(zip(names, names[1:], strict=False))
    m = manifest(names, dependencies)
    nodes = {n: ModelNode(uid(n), n, "incremental", "sync_all_columns") for n in names}
    calls = []

    def read(reader, source):
        calls.append((reader, source))
        return []

    p, _ = analyze_cascade_impacts(
        [predict_ddl("root", "view", None, ["id", "book_id"], ["id"])],
        {"root": uid("root")},
        {"root": (["id", "book_id"], ["id"])},
        {uid("root"): [uid(n) for n in names[1:]]},
        nodes,
        {},
        {},
        base_columns_of=lambda _: ["id", "book_id"],
        current_columns_of=lambda _: ["id"],
        columns_read_of=read,
    )
    report = explain_predictions(p, m)
    assert len(calls) == 199 * 199
    assert len(p[0].provenance.reads) == len(calls)
    assert len(report.graph.nodes) == 200
    assert len(report.graph.losses) == 200
    assert len(report.findings) == 200
    assert len(report.graph.edges) == 398
    assert sum(s.resolved_empty_non_edges for s in report.graph.read_summaries) == 39601 - 199
    assert len(json.dumps(report.to_dict()).encode()) < 1_000_000


def test_unknown_non_dependency_checks_are_not_pruned(tmp_path):
    p, m = cascade(tmp_path, {"reader": "select id from root"}, [], unknown=(("reader", "root"),))
    report = explain_predictions(p, m)
    assert edge_set(report, "read_check") == {("root", "reader", "unknown")}
    assert report.graph.read_summaries == ()


@pytest.mark.parametrize("unknown", [False, True])
def test_positive_and_fallback_without_manifest_edge_are_retained(tmp_path, unknown):
    p, m = cascade(
        tmp_path,
        {"reader": "select book_id from root"},
        [],
        unknown=(("reader", "root"),) if unknown else (),
    )
    report = explain_predictions(p, m)
    assert edge_set(report, "read") == {("root", "reader", "conservative" if unknown else "exact")}
    assert report.graph.read_summaries == ()


@pytest.mark.parametrize(
    "depends_on", [{"nodes": "model.shop.root"}, {"nodes": [None]}, [], {"nodes": [""]}]
)
def test_malformed_dependency_is_not_silently_discarded(depends_on):
    m = manifest(["root", "reader"])
    m["nodes"][uid("reader")]["depends_on"] = depends_on
    p = predict_ddl("root", "view", None, ["id"], ["id"])
    with pytest.raises(ValueError):
        explain_predictions([p], m)


def test_deep_cycle_uses_no_recursive_path_enumeration():
    from dbt_plan.predictor import DownstreamImpact

    names = [f"m{i}" for i in range(1500)]
    edges = [*zip(names, names[1:], strict=False), (names[-1], names[0])]
    p = replace(
        predict_ddl(names[0], "view", None, ["id"], ["id"]),
        downstream_impacts=[DownstreamImpact(names[-1], "view", None, "broken_ref", "opaque")],
    )
    report = explain_predictions([p], manifest(names, edges))
    assert len(report.graph.nodes) == 1500
    assert len(report.graph.edges) == 1500
    assert report.graph.has_cycles
