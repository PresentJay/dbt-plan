"""Canonical, policy-independent facts. See docs/finding-contract.md.

This producer adapter performs no IO and does not import a transport or renderer.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from dbt_plan.manifest import model_key
from dbt_plan.predictor import RISK_SAFETY, DDLOperation, DDLPrediction, DownstreamImpact, Safety

if TYPE_CHECKING:
    from dbt_plan.formatter import CheckResult
    from dbt_plan.manifest import ModelNode

CONTRACT_VERSION = 1


@dataclass(frozen=True)
class Resource:
    name: str
    unique_id: str | None
    candidates: tuple[str, ...] = ()
    original_file_path: str | None = None


@dataclass(frozen=True)
class Evidence:
    origin: str
    state: str  # exact, conservative, unknown
    reason_code: str
    columns: tuple[str, ...] = ()
    compiled_path: str | None = None

    def __post_init__(self):
        if self.state not in {"exact", "conservative", "unknown"}:
            raise ValueError("unknown evidence state")
        if not all(isinstance(s, str) and s for s in (self.origin, self.reason_code)):
            raise ValueError("evidence requires origin and reason_code")
        if self.compiled_path is not None and not isinstance(self.compiled_path, str):
            raise ValueError("compiled_path must be a string or None")
        if not isinstance(self.columns, tuple) or not all(
            isinstance(c, str) and c for c in self.columns
        ):
            raise ValueError("evidence columns must be a tuple of names")
        object.__setattr__(self, "columns", tuple(sorted(set(self.columns))))


@dataclass(frozen=True)
class InputRefusal:
    reason_code: str
    message: str
    resource: str | None = None


@dataclass(frozen=True)
class Finding:
    rule_code: str
    source: Resource | None
    affected: Resource | None
    column: str | None
    severity: str
    raw_risk: str
    message: str
    evidence: Evidence
    uncertainty: tuple[str, ...]
    waiver_allowed: bool
    columns_added: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()
    contract_version: int = CONTRACT_VERSION

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.evidence.columns) | ({self.column} if self.column else set()))
        )

    def to_dict(self) -> dict:
        """Return a fresh JSON-compatible object; callers may add transport fields."""
        return json.loads(json.dumps({**asdict(self), "columns": self.columns}))


def _source_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or not path.parts
        or path.parts[0] in {"target", "dbt_packages"}
        or ":" in value
    ):
        return None
    return value


class _Resources:
    def __init__(self, current: Mapping, base: Mapping, ambiguous: Collection[str]):
        self.nodes: dict[str, dict] = {}
        self.aliases: dict[str, set[str]] = {}
        self.projects: dict[str, str | None] = {}
        self.ambiguous = ambiguous
        # Keep aliases from both revisions, but current source paths take precedence.
        for manifest in (base, current):
            if not isinstance(manifest, Mapping):
                raise TypeError("manifest must be a mapping")
            metadata = manifest.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise ValueError("manifest metadata must be a mapping")
            for section in ("nodes", "unit_tests", "exposures", "sources"):
                entries = manifest.get(section, {})
                if not isinstance(entries, Mapping):
                    raise ValueError(f"manifest {section} must be a mapping")
                for uid, node in entries.items():
                    if (
                        not isinstance(uid, str)
                        or len(uid.split(".")) < 3
                        or not isinstance(node, dict)
                    ):
                        raise ValueError("invalid manifest resource")
                    if node.get("unique_id", uid) != uid:
                        raise ValueError("manifest unique_id disagrees with its key")
                    config = node.get("config") or {}
                    if not isinstance(config, Mapping):
                        raise ValueError("invalid resource config")
                    if config.get("enabled") is False:
                        continue
                    self.nodes[uid] = node
                    self.projects[uid] = metadata.get("project_name")
                    aliases = [uid, node.get("name"), model_key(uid)]
                    if isinstance(node.get("path"), str):
                        aliases.append(PurePosixPath(node["path"]).stem)
                    # Relation aliases are deliberately not resource identifiers.
                    for alias in aliases:
                        if isinstance(alias, str) and alias:
                            self.aliases.setdefault(alias, set()).add(uid)

    def resolve(self, name: str) -> Resource:
        if not isinstance(name, str) or not name:
            raise ValueError("resource name must be nonempty")
        candidates = (name,) if name in self.nodes else tuple(sorted(self.aliases.get(name, ())))
        uid = candidates[0] if len(candidates) == 1 and name not in self.ambiguous else None
        path = _source_path(self.nodes[uid].get("original_file_path")) if uid else None
        if uid:
            project = self.projects[uid]
            if not isinstance(project, str) or not project or uid.split(".")[1] != project:
                path = None
        return Resource(name, uid, candidates, path)


_OPERATIONS = {
    "MODEL REMOVED": "model_removed",
    "CREATE OR REPLACE TABLE": "replace_table",
    "CREATE OR REPLACE VIEW": "replace_view",
    "NO DDL": "no_ddl",
    "ADD COLUMN": "add_column",
    "DROP COLUMN": "drop_column",
    "BUILD FAILURE": "build_failure",
    "STALE COLUMNS (not populated)": "stale_columns",
    "BUILD FAILURE RISK: removed columns remain in the target under on_schema_change=ignore": "ignored_removal",
    "REVIEW REQUIRED: added columns are not written to the target under on_schema_change=ignore; downstream readers may fail": "ignored_addition",
}
_PREFIXES = {
    "CONTRACT VIOLATION:": "contract_violation",
    "MATERIALIZATION CHANGED:": "materialization_changed",
    "on_schema_change CHANGED:": "schema_policy_changed",
    "RELATION CHANGED (": "relation_changed",
}


def _operation_code(operation: str) -> str:
    if operation in _OPERATIONS:
        return "ddl." + _OPERATIONS[operation]
    for prefix, code in _PREFIXES.items():
        if operation.startswith(prefix):
            return "ddl." + code
    if operation.startswith("REVIEW REQUIRED"):
        return "ddl.review_required"
    if operation.startswith("UNKNOWN"):
        return "ddl.unknown_configuration"
    return "ddl.unknown_operation"


def findings_from_predictions(
    predictions: Sequence[DDLPrediction],
    manifest: Mapping,
    *,
    base_manifest: Mapping | None = None,
    refusals: Sequence[InputRefusal] = (),
    evidence: Mapping[tuple[str, str, str], Evidence] | None = None,
    ambiguous_resources: Collection[str] = (),
) -> tuple[Finding, ...]:
    """Adapt raw predictions, cascades and explicit input refusals without policy.

    Evidence keys are (source unique_id, affected unique_id, rule_code). Missing
    cascade provenance stays unknown. An ambiguous name never selects a node.
    Invalid input raises TypeError/ValueError; it never produces an empty success.
    This is a typed producer API, not a parser for legacy JSON reports.
    """
    if not isinstance(predictions, (list, tuple)) or not all(
        isinstance(p, DDLPrediction) for p in predictions
    ):
        raise TypeError("predictions must be a list/tuple of DDLPrediction")
    if not isinstance(refusals, (list, tuple)) or not all(
        isinstance(r, InputRefusal) for r in refusals
    ):
        raise TypeError("refusals must be a list/tuple of InputRefusal")
    if not isinstance(ambiguous_resources, (list, tuple, set, frozenset)) or not all(
        isinstance(name, str) and name for name in ambiguous_resources
    ):
        raise ValueError("invalid ambiguous_resources")
    resources = _Resources(
        manifest, {} if base_manifest is None else base_manifest, ambiguous_resources
    )
    evidence = {} if evidence is None else evidence
    if not isinstance(evidence, Mapping) or not all(
        isinstance(k, tuple)
        and len(k) == 3
        and all(isinstance(s, str) and s for s in k)
        and isinstance(v, Evidence)
        for k, v in evidence.items()
    ):
        raise ValueError("invalid evidence mapping")
    facts: list[Finding] = []

    def emit(
        code,
        source,
        affected,
        column,
        severity,
        raw,
        message,
        default,
        allowed,
        added=(),
        removed=(),
    ):
        ev = (
            evidence.get((source.unique_id, affected.unique_id, code), default)
            if source and affected and code != "input.refusal"
            else default
        )
        uncertainty = set()
        for resource in (source, affected):
            if resource and resource.unique_id is None:
                uncertainty.add(
                    "ambiguous_resource"
                    if resource.candidates or resource.name in ambiguous_resources
                    else "unresolved_resource"
                )
        if ev.state != "exact":
            uncertainty.add(ev.reason_code)
        if code in {"ddl.unknown_operation", "cascade.unknown_risk"}:
            uncertainty.add("unknown_rule")
        if code in {"ddl.review_required", "ddl.unknown_configuration"}:
            uncertainty.add("unresolved_prediction")
        if code in {"cascade.data_test_unreadable", "cascade.unit_test_unreadable"}:
            uncertainty.add("unresolved_input")
        if uncertainty and severity == "safe":
            severity = "warning"
        facts.append(
            Finding(
                code,
                source,
                affected,
                column,
                severity,
                raw,
                message,
                ev,
                tuple(sorted(uncertainty)),
                allowed and not uncertainty,
                tuple(sorted(set(added))),
                tuple(sorted(set(removed))),
            )
        )

    for pred in predictions:
        if not isinstance(pred.safety, Safety) or not isinstance(pred.own_verdict, Safety):
            raise ValueError("invalid prediction safety")
        if not isinstance(pred.operations, (list, tuple)) or not all(
            isinstance(op, DDLOperation)
            and isinstance(op.operation, str)
            and op.operation
            and (op.column is None or isinstance(op.column, str) and op.column)
            for op in pred.operations
        ):
            raise ValueError("invalid prediction operations")
        for columns in (pred.columns_added, pred.columns_removed):
            if not isinstance(columns, (list, tuple)) or not all(
                isinstance(c, str) and c for c in columns
            ):
                raise ValueError("invalid prediction columns")
        if not isinstance(pred.downstream_impacts, (list, tuple)) or not all(
            isinstance(impact, DownstreamImpact)
            and isinstance(impact.risk, str)
            and isinstance(impact.reason, str)
            and isinstance(impact.waiver_allowed, bool)
            for impact in pred.downstream_impacts
        ):
            raise ValueError("invalid downstream impacts")
        source = resources.resolve(pred.model_name)
        for op in pred.operations or [DDLOperation("")]:
            code = _operation_code(op.operation) if op.operation else "ddl.verdict"
            uncertain = code in {
                "ddl.review_required",
                "ddl.unknown_configuration",
                "ddl.unknown_operation",
            }
            default = Evidence(
                "prediction",
                "unknown" if uncertain else "conservative",
                "unresolved_prediction" if uncertain else "provenance_unavailable",
            )
            emit(
                code,
                source,
                source,
                op.column,
                pred.own_verdict.value,
                pred.own_verdict.value,
                op.operation or pred.own_verdict.value,
                default,
                pred.known_operations,
                pred.columns_added,
                pred.columns_removed,
            )
        for impact in pred.downstream_impacts:
            code = (
                "cascade." + impact.risk if impact.risk in RISK_SAFETY else "cascade.unknown_risk"
            )
            emit(
                code,
                source,
                resources.resolve(impact.model_name),
                None,
                RISK_SAFETY.get(impact.risk, Safety.WARNING).value,
                impact.risk,
                impact.reason,
                Evidence("legacy_cascade", "unknown", "provenance_unavailable"),
                impact.waiver_allowed
                and impact.risk not in {"data_test_unreadable", "unit_test_unreadable"},
            )
    for refusal in refusals:
        if (
            not isinstance(refusal.reason_code, str)
            or not refusal.reason_code
            or not isinstance(refusal.message, str)
        ):
            raise ValueError("refusal requires reason_code and message")
        resource = resources.resolve(refusal.resource) if refusal.resource is not None else None
        emit(
            "input.refusal",
            resource,
            resource,
            None,
            "warning",
            "warning",
            refusal.message,
            Evidence("input", "unknown", refusal.reason_code),
            False,
        )
    return tuple(sorted(facts, key=lambda fact: json.dumps(fact.to_dict(), sort_keys=True)))


def findings_from_result(
    result: CheckResult,
    manifest: Mapping | None = None,
    *,
    node_index: Mapping[str, ModelNode] | None = None,
    base_manifest: Mapping | None = None,
    evidence: Mapping[tuple[str, str, str], Evidence] | None = None,
) -> tuple[Finding, ...]:
    """Adapt CheckResult without importing formatter at runtime or applying waivers.

    Prefer full manifests: a legacy node index may already have lost colliding
    names and has no source paths or test nodes. Its actual node_id values are
    retained; no IDs or paths are constructed from model names.
    """
    if manifest is not None and node_index is not None:
        raise ValueError("supply manifest or node_index, not both")
    if manifest is None:
        manifest = {"nodes": {}}
        if node_index is not None:
            if not isinstance(node_index, Mapping):
                raise TypeError("node_index must be a mapping")
            for alias, node in node_index.items():
                if not isinstance(alias, str) or not hasattr(node, "node_id"):
                    raise ValueError("invalid node_index entry")
                manifest["nodes"][node.node_id] = {"name": node.name}
    refusals = []
    for field, code in (
        ("parse_failures", "parse_failed"),
        ("skipped_models", "missing_manifest_resource"),
        ("uncompiled_models", "missing_compiled_sql"),
        ("stale_sources", "stale_source"),
    ):
        values = getattr(result, field, None)
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(v, str) and v for v in values
        ):
            raise ValueError(f"invalid CheckResult.{field}")
        for value in values:
            # Stale sources are paths, not model names. Keep them in the message.
            refusals.append(InputRefusal(code, value, None if field == "stale_sources" else value))
    problem = getattr(result, "baseline_problem", None)
    if problem is not None:
        if not isinstance(problem, str) or not problem:
            raise ValueError("invalid CheckResult.baseline_problem")
        code = "baseline_" + problem if problem in {"missing", "corrupt"} else "baseline_unknown"
        refusals.append(InputRefusal(code, problem))
    return findings_from_predictions(
        getattr(result, "predictions", None),
        manifest,
        base_manifest=base_manifest,
        refusals=refusals,
        evidence=evidence,
        ambiguous_resources=getattr(result, "ambiguous_resources", ()),
    )
