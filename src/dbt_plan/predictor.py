"""DDL risk assessment rules based on materialization and on_schema_change."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path


class Safety(Enum):
    SAFE = "safe"
    WARNING = "warning"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class DDLOperation:
    """A single DDL operation that will be executed."""

    operation: str  # "CREATE OR REPLACE TABLE", "ADD COLUMN", "DROP COLUMN", etc.
    column: str | None = None  # column name for ADD/DROP


@dataclass(frozen=True)
class DownstreamImpact:
    """Predicted impact on a downstream model."""

    model_name: str
    materialization: str
    on_schema_change: str | None
    risk: str  # "build_failure", "broken_ref"
    reason: str  # human-readable explanation


@dataclass(frozen=True)
class DDLPrediction:
    """Complete DDL prediction for a model."""

    model_name: str
    materialization: str
    on_schema_change: str | None
    safety: Safety
    operations: list[DDLOperation] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    downstream_impacts: list[DownstreamImpact] = field(default_factory=list)


def predict_ddl(
    model_name: str,
    materialization: str,
    on_schema_change: str | None,
    base_columns: list[str] | None,
    current_columns: list[str] | None,
    status: str = "modified",
) -> DDLPrediction:
    """Predict DDL operations from materialization config and column diff.

    Args:
        model_name: dbt model name.
        materialization: "table", "view", "incremental", "ephemeral".
        on_schema_change: "ignore", "fail", "append_new_columns",
                          "sync_all_columns", or None.
        base_columns: Columns from base (None if parse failed).
        current_columns: Columns from current (None if parse failed).
        status: "added", "modified", or "removed" from diff.

    Returns:
        DDLPrediction with safety level and predicted operations.
    """
    # Removed model → destructive (physical table/view will be orphaned)
    # Exception: ephemeral models have no physical object
    if status == "removed":
        if materialization == "ephemeral":
            return DDLPrediction(
                model_name=model_name,
                materialization=materialization,
                on_schema_change=on_schema_change,
                safety=Safety.SAFE,
            )
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.DESTRUCTIVE,
            operations=[DDLOperation("MODEL REMOVED")],
        )

    if materialization == "table":
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.SAFE,
            operations=[DDLOperation("CREATE OR REPLACE TABLE")],
        )

    if materialization == "view":
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.SAFE,
            operations=[DDLOperation("CREATE OR REPLACE VIEW")],
        )

    if materialization == "ephemeral":
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.SAFE,
        )

    if materialization == "snapshot":
        # dbt snapshots use CREATE TABLE IF NOT EXISTS + MERGE
        # Schema changes are not auto-managed, so warn for review
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.WARNING,
            operations=[DDLOperation("REVIEW REQUIRED (snapshot)")],
        )

    # Incremental: depends on on_schema_change
    osc = on_schema_change or "ignore"

    if osc == "ignore":
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.SAFE,
            operations=[DDLOperation("NO DDL")],
        )

    # New model (status=added, no base) → safe, no existing table to alter
    if status == "added":
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.SAFE,
        )

    # From here: status == "modified"
    # Parse failure on either side → WARNING (절대 safe 반환 금지)
    if base_columns is None or current_columns is None:
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.WARNING,
            operations=[DDLOperation("REVIEW REQUIRED")],
        )

    # SELECT * on either side → cannot determine column diff
    if base_columns == ["*"] or current_columns == ["*"]:
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.WARNING,
            operations=[DDLOperation("REVIEW REQUIRED (SELECT *)")],
        )

    # SELECT * EXCEPT(...) sentinel — columns excluded but full set unknown
    base_is_star_except = len(base_columns) == 1 and base_columns[0].startswith("* except(")
    current_is_star_except = len(current_columns) == 1 and current_columns[0].startswith(
        "* except("
    )
    if base_is_star_except or current_is_star_except:
        # Both are * except with identical exclusions → no change, still WARNING
        # because we can't enumerate the full column set
        if base_is_star_except and current_is_star_except:
            if base_columns[0] == current_columns[0]:
                return DDLPrediction(
                    model_name=model_name,
                    materialization=materialization,
                    on_schema_change=osc,
                    safety=Safety.WARNING,
                    operations=[
                        DDLOperation("REVIEW REQUIRED (SELECT * EXCEPT — same exclusions)")
                    ],
                )
            else:
                return DDLPrediction(
                    model_name=model_name,
                    materialization=materialization,
                    on_schema_change=osc,
                    safety=Safety.WARNING,
                    operations=[
                        DDLOperation("REVIEW REQUIRED (SELECT * EXCEPT — exclusions changed)")
                    ],
                )
        # One side is * except, other is explicit or plain * → column diff impossible
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.WARNING,
            operations=[DDLOperation("REVIEW REQUIRED (SELECT * EXCEPT — column removal likely)")],
        )

    # Detect duplicate column names (e.g., from JOINs without aliases)
    # Duplicates make set diff unreliable — we can't determine the real schema change
    base_has_dupes = len(base_columns) != len(set(base_columns))
    current_has_dupes = len(current_columns) != len(set(current_columns))
    if base_has_dupes or current_has_dupes:
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.WARNING,
            operations=[DDLOperation("REVIEW REQUIRED (duplicate column names)")],
        )

    # Column diff
    base_set = set(base_columns)
    current_set = set(current_columns)
    added = sorted(current_set - base_set)
    removed = sorted(base_set - current_set)

    if osc == "fail":
        if added or removed:
            return DDLPrediction(
                model_name=model_name,
                materialization=materialization,
                on_schema_change=osc,
                safety=Safety.WARNING,
                operations=[DDLOperation("BUILD FAILURE")],
                columns_added=added,
                columns_removed=removed,
            )
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=Safety.SAFE,
        )

    if osc == "append_new_columns":
        ops = [DDLOperation("ADD COLUMN", col) for col in added]
        # Columns removed from SQL will remain in the physical table as stale data
        safety = Safety.WARNING if removed else Safety.SAFE
        if removed:
            ops.append(DDLOperation("STALE COLUMNS (not populated)"))
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=safety,
            operations=ops,
            columns_added=added,
            columns_removed=removed,
        )

    if osc == "sync_all_columns":
        ops = [DDLOperation("ADD COLUMN", col) for col in added]
        ops += [DDLOperation("DROP COLUMN", col) for col in removed]
        safety = Safety.DESTRUCTIVE if removed else Safety.SAFE

        # Detect column reordering: sync_all_columns drops + re-adds
        # columns to match the new order even when the column set is unchanged
        if not added and not removed and base_columns != current_columns:
            ops = [DDLOperation("COLUMNS REORDERED")]
            safety = Safety.WARNING

        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=osc,
            safety=safety,
            operations=ops,
            columns_added=added,
            columns_removed=removed,
        )

    # Unknown on_schema_change
    return DDLPrediction(
        model_name=model_name,
        materialization=materialization,
        on_schema_change=osc,
        safety=Safety.WARNING,
        operations=[DDLOperation(f"UNKNOWN on_schema_change: {osc}")],
    )


def analyze_cascade_impacts(
    predictions: list[DDLPrediction],
    model_node_ids: dict[str, str],
    model_cols: dict[str, tuple[list[str] | None, list[str] | None]],
    all_downstream: dict[str, list[str]],
    node_index: dict,
    base_node_index: dict,
    compiled_sql_index: dict[str, Path],
) -> tuple[list[DDLPrediction], dict[str, list[str]]]:
    """Analyze cascade impacts of column changes on downstream models.

    Args:
        predictions: DDL predictions for changed models.
        model_node_ids: model_name → node_id mapping.
        model_cols: model_name → (base_cols, current_cols) from extraction.
        all_downstream: node_id → list of downstream node_ids.
        node_index: current manifest model node lookup (name → ModelNode).
        base_node_index: base manifest model node lookup (name → ModelNode).
        compiled_sql_index: model_name → Path to compiled SQL file.

    Returns:
        (updated_predictions, downstream_map)
    """
    updated = list(predictions)
    downstream_map: dict[str, list[str]] = {}

    for i, pred in enumerate(updated):
        node_id = model_node_ids.get(pred.model_name)
        if not node_id:
            continue
        downstream_nids = all_downstream.get(node_id, [])
        if not downstream_nids:
            continue

        downstream_names = [nid.split(".")[-1] for nid in downstream_nids]
        downstream_map[pred.model_name] = downstream_names

        # Skip cascade for incremental+ignore: no physical schema change occurs,
        # so downstream models are not affected by column changes in the SQL
        if (
            pred.materialization == "incremental"
            and (pred.on_schema_change or "ignore") == "ignore"
        ):
            continue

        # Compute SQL-level column diff for cascade analysis
        # (predictor doesn't populate columns_removed for table/view,
        #  so we use the raw column extraction results)
        stored_base, stored_curr = model_cols.get(pred.model_name, (None, None))
        cascade_removed = list(pred.columns_removed)
        cascade_added = list(pred.columns_added)

        # Removed models: ALL base columns are effectively removed
        if stored_curr is None and stored_base is not None and stored_base != ["*"]:
            cascade_removed = list(stored_base)
        elif (
            not cascade_removed
            and not cascade_added
            and stored_base is not None
            and stored_curr is not None
        ):
            if stored_base != ["*"] and stored_curr != ["*"]:
                cascade_removed = sorted(set(stored_base) - set(stored_curr))
                cascade_added = sorted(set(stored_curr) - set(stored_base))

        if not cascade_removed and not cascade_added:
            continue

        # Pre-compile regex patterns for column matching (avoid per-iteration compile)
        col_patterns = (
            {
                col: re.compile(r"\b" + re.escape(col) + r"\b", re.IGNORECASE)
                for col in cascade_removed
            }
            if cascade_removed
            else {}
        )

        impacts: list[DownstreamImpact] = []
        for ds_nid in downstream_nids:
            ds_node = node_index.get(ds_nid.split(".")[-1])
            if not ds_node:
                ds_node = base_node_index.get(ds_nid.split(".")[-1])
            if not ds_node:
                continue

            ds_mat = ds_node.materialization
            ds_osc = ds_node.on_schema_change or "ignore"

            # ephemeral: CTE substitution, no physical table — always safe
            if ds_mat == "ephemeral":
                continue

            # incremental + fail: guaranteed build failure on any schema change
            if ds_mat == "incremental" and ds_osc == "fail" and (cascade_added or cascade_removed):
                impacts.append(
                    DownstreamImpact(
                        model_name=ds_node.name,
                        materialization=ds_mat,
                        on_schema_change=ds_osc,
                        risk="build_failure",
                        reason="upstream schema changed, on_schema_change=fail",
                    )
                )
                # Don't continue — also check for broken column refs below

            # Check for broken column references in downstream SQL
            # Applies to ALL materialization types (table/view/incremental):
            # even though table/view DDL is safe (CREATE OR REPLACE),
            # the SELECT will fail if it references a dropped upstream column
            if col_patterns:
                ds_sql_path = compiled_sql_index.get(ds_node.name)
                ds_sql = None
                if ds_sql_path:
                    try:
                        ds_sql = ds_sql_path.read_text()
                    except (OSError, UnicodeDecodeError):
                        pass  # unreadable file — skip broken_ref check for this model

                if ds_sql:
                    broken_refs = [
                        col for col, pattern in col_patterns.items() if pattern.search(ds_sql)
                    ]
                    if broken_refs:
                        impacts.append(
                            DownstreamImpact(
                                model_name=ds_node.name,
                                materialization=ds_mat,
                                on_schema_change=ds_osc,
                                risk="broken_ref",
                                reason=f"references dropped column(s): {', '.join(broken_refs)}",
                            )
                        )

        if impacts:
            # Escalate safety based on cascade risk
            cascade_safety = pred.safety
            if any(imp.risk == "broken_ref" for imp in impacts):
                cascade_safety = Safety.DESTRUCTIVE
            elif any(imp.risk == "build_failure" for imp in impacts):
                if cascade_safety == Safety.SAFE:
                    cascade_safety = Safety.WARNING

            updated[i] = replace(pred, safety=cascade_safety, downstream_impacts=impacts)

    return updated, downstream_map
