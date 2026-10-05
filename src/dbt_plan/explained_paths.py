"""Bounded evidence graph, independent of presentation and verdict policy.

No simple paths are enumerated. Root finding associations, direct read facts and
manifest dependency candidates are deliberately different objects. See
docs/causal-path-contract.md for the consumer contract and complexity bounds.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import cache

from dbt_plan.findings import (
    Evidence,
    Finding,
    InputRefusal,
    Resource,
    _Resources,
    findings_from_predictions,
)
from dbt_plan.predictor import DDLPrediction


@dataclass(frozen=True)
class CausalNode:
    resource: Resource
    materialization: str | None
    on_schema_change: str | None
    columns_removed: tuple[str, ...] = ()
    loss_evidence: Evidence | None = None


@dataclass(frozen=True)
class CausalEdge:
    source: Resource
    target: Resource
    kind: str  # read, read_check, dependency, declared_consumer
    evidence: Evidence
    columns_removed: tuple[str, ...] = ()
    root: Resource | None = None
    revisions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputLoss:
    root: Resource
    resource: Resource
    columns_removed: tuple[str, ...]
    evidence: Evidence


@dataclass(frozen=True)
class ReadCheckSummary:
    root: Resource
    resolved_empty_non_edges: int


@dataclass(frozen=True)
class CauseAssociation:
    finding_index: int
    source: Resource | None
    affected: Resource | None
    columns_added: tuple[str, ...]
    columns_removed: tuple[str, ...]
    evidence: Evidence
    cause_finding_indices: tuple[int, ...] = ()
    # Association is the analyzer's attribution, never a synthetic SQL edge.


@dataclass(frozen=True)
class CausalGraph:
    nodes: tuple[CausalNode, ...]
    edges: tuple[CausalEdge, ...]
    associations: tuple[CauseAssociation, ...]
    has_cycles: bool
    losses: tuple[OutputLoss, ...] = ()
    read_summaries: tuple[ReadCheckSummary, ...] = ()
    external_coverage: str = "unknown"


@dataclass(frozen=True)
class ExplainedAnalysis:
    findings: tuple[Finding, ...]
    graph: CausalGraph

    def to_dict(self) -> dict:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "graph": json.loads(json.dumps(asdict(self.graph))),
        }


def _key(value) -> str:
    return json.dumps(asdict(value), sort_keys=True)


def _walk(seeds, adjacency):
    seen = set(seeds)
    pending = deque(sorted(seen))
    while pending:
        for other in adjacency.get(pending.popleft(), ()):
            if other not in seen:
                seen.add(other)
                pending.append(other)
    return seen


def _cyclic(edges):
    adjacency: dict[str, set[str]] = {}
    indegree: dict[str, int] = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        indegree.setdefault(a, 0)
        indegree.setdefault(b, 0)
    for children in adjacency.values():
        for b in children:
            indegree[b] += 1
    pending = deque(a for a, count in indegree.items() if not count)
    visited = 0
    while pending:
        a = pending.popleft()
        visited += 1
        for b in adjacency.get(a, ()):
            indegree[b] -= 1
            if not indegree[b]:
                pending.append(b)
    return visited != len(indegree)


def _dependencies(manifest, base_manifest, resources):
    dependencies: dict[tuple[str, str], set[str]] = {}
    for revision_name, revision in (("base", base_manifest or {}), ("current", manifest)):
        for section in ("nodes", "unit_tests", "exposures", "sources"):
            for target, node in revision.get(section, {}).items():
                if (
                    target not in resources.nodes
                    or (node.get("config") or {}).get("enabled") is False
                ):
                    continue
                depends_on = node.get("depends_on", {})
                if not isinstance(depends_on, Mapping):
                    raise ValueError("invalid manifest depends_on")
                parents = depends_on.get("nodes", ())
                if not isinstance(parents, (list, tuple)):
                    raise ValueError("invalid manifest dependency list")
                for source in parents:
                    if not isinstance(source, str) or not source:
                        raise ValueError("invalid manifest dependency reference")
                    dependencies.setdefault((source, target), set()).add(revision_name)
    return dependencies


def explain_predictions(
    predictions: Sequence[DDLPrediction],
    manifest: Mapping,
    *,
    base_manifest: Mapping | None = None,
    refusals: Sequence[InputRefusal] = (),
    evidence: Mapping[tuple[str, str, str], Evidence] | None = None,
    ambiguous_resources: Collection[str] = (),
) -> ExplainedAnalysis:
    """Compose canonical facts and a matching lossless relevant evidence graph.

    Canonical findings are unchanged; explicit evidence has the same semantics
    as findings_from_predictions. Read certainty does not upgrade a root finding.
    Full manifests are mandatory: short-name indexes cannot prove uniqueness.
    """
    facts = findings_from_predictions(
        predictions,
        manifest,
        base_manifest=base_manifest,
        refusals=refusals,
        evidence=evidence,
        ambiguous_resources=ambiguous_resources,
    )
    return explain_findings(
        facts,
        predictions,
        manifest,
        base_manifest=base_manifest,
        ambiguous_resources=ambiguous_resources,
    )


def explain_findings(
    findings: Sequence[Finding],
    predictions: Sequence[DDLPrediction],
    manifest: Mapping,
    *,
    base_manifest: Mapping | None = None,
    ambiguous_resources: Collection[str] = (),
) -> ExplainedAnalysis:
    """Attach a graph without rewriting canonical transport facts.

    Supply facts and predictions from the same analysis before policy filtering.
    This retains #254's enriched table/view deltas. Indices address this exact
    tuple; they are not durable finding IDs.
    """
    if not isinstance(findings, (list, tuple)) or not all(
        isinstance(f, Finding) for f in findings
    ):
        raise TypeError("findings must be a list/tuple of Finding")
    if not isinstance(predictions, (list, tuple)) or not all(
        isinstance(p, DDLPrediction) for p in predictions
    ):
        raise TypeError("predictions must be a list/tuple of DDLPrediction")
    facts = tuple(findings)
    resources = _Resources(manifest, base_manifest or {}, ambiguous_resources)
    for fact in facts:
        for resource in (fact.source, fact.affected):
            if resource is not None and resource != resources.resolve(resource.name):
                raise ValueError("finding identity/path disagrees with composition manifests")
    # The existing reader uses bare relation names. A collision anywhere in
    # either full manifest therefore prevents certifying its attribution.
    relation_owners: dict[str, set[str]] = {}
    for revision in (base_manifest or {}, manifest):
        for section in ("nodes", "sources"):
            for resource_id, node in revision.get(section, {}).items():
                if (node.get("config") or {}).get("enabled") is False:
                    continue
                for name in (node.get("name"), node.get("alias"), node.get("identifier")):
                    if isinstance(name, str) and name:
                        relation_owners.setdefault(name.lower(), set()).add(resource_id)
    ambiguous_relations = {
        uid for owners in relation_owners.values() if len(owners) > 1 for uid in owners
    }
    dependencies = _dependencies(manifest, base_manifest, resources)

    # Normalize resolved spellings so every endpoint has exactly one graph node.
    # Resolve the original spelling FIRST: never certify a short-index winner.
    @cache
    def resolve(name):
        resource = resources.resolve(name)
        if resource.unique_id is None:
            return resource
        node = resources.nodes[resource.unique_id]
        return Resource(
            node.get("name") or name,
            resource.unique_id,
            resource.candidates,
            resource.original_file_path,
        )

    node_resources: dict[str, Resource] = {}
    losses: dict[str, set[str]] = {}
    output_losses: dict[str, OutputLoss] = {}
    empty_checks: dict[Resource, int] = {}
    edges: dict[str, CausalEdge] = {}
    seeds: set[str] = set()
    targets: set[str] = set()

    def remember(resource):
        if resource is not None:
            node_resources[_key(resource)] = resource

    def add_edge(edge):
        edges[_key(edge)] = edge
        remember(edge.source)
        remember(edge.target)
        remember(edge.root)

    own: dict[str, list[int]] = {}
    for index, fact in enumerate(facts):
        if fact.source and fact.source == fact.affected and fact.rule_code.startswith("ddl."):
            own.setdefault(_key(resolve(fact.source.name)), []).append(index)
    changes = {
        key: (
            tuple(indices),
            tuple(sorted({c for i in indices for c in facts[i].columns_added})),
            tuple(sorted({c for i in indices for c in facts[i].columns_removed})),
        )
        for key, indices in own.items()
    }
    associations = []
    for index, fact in enumerate(facts):
        source = resolve(fact.source.name) if fact.source else None
        affected = resolve(fact.affected.name) if fact.affected else None
        remember(source)
        remember(affected)
        if source:
            seeds.add(source.unique_id or source.name)
        if affected:
            targets.add(affected.unique_id or affected.name)
        cause_indices, added, removed = (
            changes.get(_key(source), ((), (), ())) if source else ((), (), ())
        )
        associations.append(
            CauseAssociation(index, source, affected, added, removed, fact.evidence, cause_indices)
        )

    for pred in predictions:
        if pred.provenance:
            for name, removed in pred.provenance.losses:
                resource = resolve(name)
                remember(resource)
                losses.setdefault(_key(resource), set()).update(removed)
                loss = OutputLoss(
                    resolve(pred.model_name),
                    resource,
                    removed,
                    Evidence(
                        "resolved_columns", "unknown", "output_loss_attribution_unproved", removed
                    ),
                )
                output_losses[_key(loss)] = loss
                targets.add(resource.unique_id or resource.name)
            for read in pred.provenance.reads:
                source, target = resolve(read.source), resolve(read.reader)
                if source.unique_id is None or target.unique_id is None:
                    state, origin, reason = (
                        "unknown",
                        "resolved_read" if read.columns_read is not None else "input",
                        "ambiguous_or_unresolved_resource",
                    )
                elif source.unique_id in ambiguous_relations:
                    state, origin, reason = (
                        "unknown",
                        "resolved_read"
                        if read.columns_read is not None
                        else "text_search"
                        if read.text_matches
                        else "input",
                        "ambiguous_relation",
                    )
                elif read.columns_read is not None:
                    state, origin, reason = "exact", "resolved_read", "read_checked"
                elif read.text_matches:
                    state, origin, reason = "conservative", "text_search", "read_fallback"
                else:
                    state, origin, reason = "unknown", "input", "read_unresolved"
                if (
                    state == "exact"
                    and read.columns_read == ()
                    and (source.unique_id, target.unique_id) not in dependencies
                ):
                    root = resolve(pred.model_name)
                    empty_checks[root] = empty_checks.get(root, 0) + 1
                    continue
                columns = read.columns_read if read.columns_read is not None else read.text_matches
                read_columns = {col.lower() for col in columns}
                removed = tuple(c for c in read.columns_removed if c.lower() in read_columns)
                add_edge(
                    CausalEdge(
                        source,
                        target,
                        "read" if removed else "read_check",
                        Evidence(origin, state, reason, columns),
                        removed,
                        resolve(pred.model_name),
                    )
                )
                targets.add(target.unique_id or target.name)

        # These are declared consumers, not additional safety findings.
        for exposure in pred.downstream_exposures:
            resource = resolve(exposure.node_id)
            remember(resource)
            targets.add(resource.unique_id or resource.name)

    forward: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for source, target in dependencies:
        forward.setdefault(source, set()).add(target)
        reverse.setdefault(target, set()).add(source)
    relevant = _walk(seeds, forward) & _walk(targets, reverse)
    retained = {(a, b) for a, b in dependencies if a in relevant and b in relevant}
    for a, b in sorted(retained):
        declared = b.startswith("exposure.")
        add_edge(
            CausalEdge(
                resolve(a),
                resolve(b),
                "declared_consumer" if declared else "dependency",
                Evidence(
                    "manifest",
                    "unknown",
                    "declared_consumer" if declared else "dependency_not_column_flow",
                ),
                revisions=tuple(sorted(dependencies[a, b])),
            )
        )

    nodes = []
    for key, resource in sorted(node_resources.items()):
        node = resources.nodes.get(resource.unique_id, {})
        config = node.get("config") or {}
        unrendered = node.get("unrendered_config") or {}
        nodes.append(
            CausalNode(
                resource,
                config.get("materialized"),
                unrendered.get("on_schema_change"),
                tuple(sorted(losses.get(key, ()))),
                Evidence(
                    "resolved_columns",
                    "unknown",
                    "output_loss_attribution_unproved",
                    tuple(sorted(losses[key])),
                )
                if losses.get(key)
                else None,
            )
        )
    return ExplainedAnalysis(
        facts,
        CausalGraph(
            tuple(nodes),
            tuple(edges[k] for k in sorted(edges)),
            tuple(associations),
            _cyclic(retained),
            tuple(output_losses[k] for k in sorted(output_losses)),
            tuple(
                ReadCheckSummary(root, empty_checks[root])
                for root in sorted(empty_checks, key=_key)
            ),
        ),
    )
