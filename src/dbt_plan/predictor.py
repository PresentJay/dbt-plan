"""DDL risk assessment rules based on materialization and on_schema_change."""

from __future__ import annotations

import re
from collections.abc import Callable
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
    """Predicted impact on a downstream node.

    Usually a model. Unit tests reuse this with `materialization="unit_test"`,
    because what they need reporting -- a name, a risk and a reason -- is the
    same three things, and neither field below is rendered anywhere.
    """

    model_name: str
    materialization: str
    on_schema_change: str | None
    risk: str  # a key of RISK_SAFETY
    reason: str  # human-readable explanation


# What each cascade risk is worth on its own. The formatter colours by this and
# the escalation in analyze_cascade_impacts reads it, so a risk added to only
# one of the two is how a red finding ends up wearing a yellow icon.
RISK_SAFETY: dict[str, Safety] = {
    "broken_ref": Safety.DESTRUCTIVE,
    "build_failure": Safety.WARNING,
    "unit_test_failure": Safety.WARNING,
    "unit_test_unreadable": Safety.WARNING,
    "inherited_drop": Safety.DESTRUCTIVE,
    "inherited_change": Safety.WARNING,
    "data_test_failure": Safety.WARNING,
    "data_test_unreadable": Safety.WARNING,
}

_SAFETY_RANK = {Safety.SAFE: 0, Safety.WARNING: 1, Safety.DESTRUCTIVE: 2}


def worst_safety(safeties: list[Safety]) -> Safety:
    """The most severe of several verdicts. Cascade escalates, never downgrades."""
    return max(safeties, key=_SAFETY_RANK.__getitem__)


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
    # Not a finding of its own -- see attach_downstream_exposures.
    downstream_exposures: list = field(default_factory=list)


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
        materialization: "table", "view", "incremental", "ephemeral",
            "snapshot". Anything else is reported as unknown rather than
            being treated as incremental.
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

    if materialization != "incremental" and on_schema_change is None:
        # Everything dbt ships is handled above except incremental, so this is a
        # materialized view, an adapter-specific object like a Snowflake dynamic
        # table, or a custom materialization -- and nobody has said how it treats
        # a schema change.
        #
        # An explicit on_schema_change is an assertion by the author about how
        # their materialization behaves, and honouring it is more useful than
        # refusing: a custom materialization set to sync_all_columns still earns
        # a DESTRUCTIVE verdict below. Its *absence* asserts nothing, and
        # defaulting it to "ignore" is what produced the false all-clear -- a
        # materialized view dropping a column reported SAFE / NO DDL, from a
        # setting that does not govern materialized views at all.
        if materialization == "materialized_view":
            reason = (
                "REVIEW REQUIRED (materialized_view is driven by "
                "on_configuration_change, which dbt-plan does not model)"
            )
        else:
            reason = f"UNKNOWN materialization: {materialization}"

        base_set = set(base_columns or [])
        current_set = set(current_columns or [])
        unknown_added = sorted(current_set - base_set)
        unknown_removed = sorted(base_set - current_set)
        return DDLPrediction(
            model_name=model_name,
            materialization=materialization,
            on_schema_change=on_schema_change,
            safety=Safety.WARNING,
            operations=[
                DDLOperation(reason),
                # Carry the diff anyway: "unknown" with nothing attached tells a
                # reviewer nothing about what to go and look at.
                *[DDLOperation("ADD COLUMN", col) for col in unknown_added],
                *[DDLOperation("DROP COLUMN", col) for col in unknown_removed],
            ],
            columns_added=unknown_added,
            columns_removed=unknown_removed,
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


def _column_patterns(lost_by_model: dict[str, list[str]]) -> dict[str, re.Pattern]:
    """Word-boundary patterns for every column being removed, compiled once."""
    return {
        col: re.compile(r"\b" + re.escape(col) + r"\b", re.IGNORECASE)
        for columns in lost_by_model.values()
        for col in columns
    }


def _unit_test_impacts(
    lost_by_model: dict[str, list[str]],
    owner_node_ids: list[str],
    child_map: dict[str, list[str]],
    unit_test_index: dict,
) -> list[DownstreamImpact]:
    """Report unit tests whose hand-written fixtures name a column being removed.

    dbt validates every fixture's column names against the real relation it stands
    for, so a dropped column fails the test at `dbt build` -- as an `expect` block
    on the model losing the column, and as a `given` input anywhere downstream that
    supplies that model's rows.

    `lost_by_model` maps a model name to the columns it is losing, whether from its
    own diff or inherited through a `SELECT *`. Only fixtures standing in for a
    model in that map are compared: those are the only columns known to move.

    A unit test hangs off the model it tests as a direct child, so the changed model
    plus its downstream set covers every one of them with no second walk.
    """
    impacts: list[DownstreamImpact] = []
    seen: set[str] = set()
    for owner_nid in owner_node_ids:
        for child in child_map.get(owner_nid) or []:
            if child in seen:
                continue
            unit_test = unit_test_index.get(child)
            if unit_test is None:
                continue
            seen.add(child)
            for fixture in unit_test.fixtures:
                removed = lost_by_model.get(fixture.model)
                if not removed:
                    continue
                if fixture.columns is None:
                    impacts.append(
                        DownstreamImpact(
                            model_name=unit_test.name,
                            materialization="unit_test",
                            on_schema_change=None,
                            risk="unit_test_unreadable",
                            reason=(
                                f"{fixture.label} {fixture.unreadable_reason}, so dbt-plan "
                                f"cannot tell whether it names the dropped column(s)"
                            ),
                        )
                    )
                    continue
                named = sorted(col for col in removed if col.lower() in fixture.columns)
                if named:
                    impacts.append(
                        DownstreamImpact(
                            model_name=unit_test.name,
                            materialization="unit_test",
                            on_schema_change=None,
                            risk="unit_test_failure",
                            reason=f"{fixture.label} names dropped column(s): {', '.join(named)}",
                        )
                    )
    return impacts


def _inherited_impact(
    ds_node,
    base_columns_of: Callable[[str], list[str] | None],
    current_columns_of: Callable[[str], list[str] | None],
    changed_models: dict[str, str],
) -> tuple[DownstreamImpact | None, list[str]]:
    """Predict what happens to a downstream model whose own file did not change.

    `select * from {{ ref(x) }}` compiles to the same bytes before and after x
    loses a column, so the diff has no entry for it and dbt-plan used to say
    nothing -- while dbt, on `incremental` + `sync_all_columns`, issues a
    DROP COLUMN against a table that has data in it.

    Both sides are resolved through `ref()` from the project's own compiled SQL
    and run back through predict_ddl, so the verdict follows the same
    materialization rules as any other model rather than a second set invented
    here. A model whose columns cannot be read on both sides is still put through
    predict_ddl, which answers REVIEW REQUIRED where the configuration allows a
    drop and SAFE where it cannot -- silence would be the wrong answer, and so
    would a guess.

    Returns (impact, lost). The impact is None when there is nothing to report --
    the model has its own entry in the diff already, or its own verdict comes back
    safe -- but `lost` is filled in either way, because a downstream model can lose
    a column safely and still break the data tests that read it.
    """
    if ds_node.name in changed_models:
        return None, []  # it changed on its own; it has a verdict of its own

    ds_base = base_columns_of(ds_node.name)
    ds_current = current_columns_of(ds_node.name)
    lost = sorted(set(ds_base) - set(ds_current)) if ds_base and ds_current else []
    if ds_base is not None and ds_current is not None and not lost:
        return None, []  # resolved on both sides and losing nothing

    ds_pred = predict_ddl(
        model_name=ds_node.name,
        materialization=ds_node.materialization,
        on_schema_change=ds_node.on_schema_change,
        base_columns=ds_base,
        current_columns=ds_current,
        status="modified",
    )
    if ds_pred.safety == Safety.SAFE:
        return None, lost

    detail = (
        f"loses {', '.join(lost)} from upstream"
        if lost
        else "its columns cannot be resolved on both sides"
    )
    operations = ", ".join(
        f"{op.operation} {op.column or ''}".strip() for op in ds_pred.operations
    )
    return (
        DownstreamImpact(
            model_name=ds_node.name,
            materialization=ds_node.materialization,
            on_schema_change=ds_node.on_schema_change,
            risk="inherited_drop" if ds_pred.safety == Safety.DESTRUCTIVE else "inherited_change",
            reason=f"file unchanged, {detail} -- {operations or ds_pred.safety.value}",
        ),
        lost,
    )


def _data_test_impacts(
    lost_by_model: dict[str, list[str]],
    owner_node_ids: list[str],
    child_map: dict[str, list[str]],
    data_test_index: dict,
    test_sql_index: dict[str, Path],
) -> list[DownstreamImpact]:
    """Report data tests that read a column being removed.

    A `not_null` on a dropped column is not a failing assertion, it is a broken
    query -- dbt cannot even bind it:

        Binder Error: Referenced column "customer_id" not found in FROM clause!

    A generic test names its column in the manifest, so that answers it outright
    and no file is read. A singular test names nothing there, so its compiled SQL
    is searched for the dropped names -- the same check `broken_ref` runs against
    a downstream model, and it reads only the current side for the same reason:
    the question is what the SQL names, not whether the file changed.

    Data tests hang off every model they read as a direct child, so the changed
    model plus its downstream set covers them with no second walk.
    """
    impacts: list[DownstreamImpact] = []
    seen: set[str] = set()
    patterns = _column_patterns(lost_by_model)
    for owner_nid in owner_node_ids:
        for child in child_map.get(owner_nid) or []:
            if child in seen:
                continue
            data_test = data_test_index.get(child)
            if data_test is None:
                continue
            seen.add(child)

            named = sorted(
                col
                for model, declared in data_test.columns_by_model.items()
                for col in lost_by_model.get(model) or []
                if col.lower() in declared
            )
            if named:
                impacts.append(
                    DownstreamImpact(
                        model_name=data_test.name,
                        materialization="data_test",
                        on_schema_change=None,
                        risk="data_test_failure",
                        reason=f"tests dropped column(s): {', '.join(named)}",
                    )
                )
                continue

            # Models this test reads that the manifest says nothing about, column
            # wise: a singular test, or a generic one with no column_name.
            undescribed = [
                model
                for model in data_test.depends_on_models
                if lost_by_model.get(model) and model not in data_test.columns_by_model
            ]
            if not undescribed:
                continue

            sql = None
            sql_path = test_sql_index.get(data_test.name)
            if sql_path is not None:
                try:
                    sql = sql_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    sql = None
            if sql is None:
                impacts.append(
                    DownstreamImpact(
                        model_name=data_test.name,
                        materialization="data_test",
                        on_schema_change=None,
                        risk="data_test_unreadable",
                        reason=(
                            "names no column in the manifest and its compiled SQL was not "
                            "found, so dbt-plan cannot tell whether it reads the dropped "
                            "column(s)"
                        ),
                    )
                )
                continue
            in_sql = sorted(
                {
                    col
                    for model in undescribed
                    for col in lost_by_model[model]
                    if patterns[col].search(sql)
                }
            )
            if in_sql:
                impacts.append(
                    DownstreamImpact(
                        model_name=data_test.name,
                        materialization="data_test",
                        on_schema_change=None,
                        risk="data_test_failure",
                        reason=f"its SQL names dropped column(s): {', '.join(in_sql)}",
                    )
                )
    return impacts


def analyze_cascade_impacts(
    predictions: list[DDLPrediction],
    model_node_ids: dict[str, str],
    model_cols: dict[str, tuple[list[str] | None, list[str] | None]],
    all_downstream: dict[str, list[str]],
    node_index: dict,
    base_node_index: dict,
    compiled_sql_index: dict[str, Path],
    child_map: dict[str, list[str]] | None = None,
    unit_test_index: dict | None = None,
    data_test_index: dict | None = None,
    test_sql_index: dict[str, Path] | None = None,
    base_columns_of: Callable[[str], list[str] | None] | None = None,
    current_columns_of: Callable[[str], list[str] | None] | None = None,
) -> tuple[list[DDLPrediction], dict[str, list[str]]]:
    """Analyze cascade impacts of column changes on downstream nodes.

    Args:
        predictions: DDL predictions for changed models.
        model_node_ids: model_name → node_id mapping.
        model_cols: model_name → (base_cols, current_cols) from extraction.
        all_downstream: node_id → list of downstream node_ids.
        node_index: current manifest model node lookup (name → ModelNode).
        base_node_index: base manifest model node lookup (name → ModelNode).
        compiled_sql_index: model_name → Path to compiled SQL file.
        child_map: manifest child_map, needed to reach non-model children.
        unit_test_index: node_id → UnitTestNode. Omitted, unit tests are skipped.
        data_test_index: node_id → DataTestNode. Omitted, data tests are skipped.
        test_sql_index: test name → compiled SQL, for tests the manifest does not
            describe by column.
        base_columns_of, current_columns_of: resolve a model name to its columns
            on each side, following `ref()` so a `SELECT *` expands. Omitted, the
            unchanged-file check below is skipped.

    Returns:
        (updated_predictions, downstream_map)
    """
    child_map = child_map or {}
    unit_test_index = unit_test_index or {}
    data_test_index = data_test_index or {}
    test_sql_index = test_sql_index or {}
    updated = list(predictions)
    downstream_map: dict[str, list[str]] = {}

    for i, pred in enumerate(updated):
        node_id = model_node_ids.get(pred.model_name)
        if not node_id:
            continue
        downstream_nids = all_downstream.get(node_id, [])
        # A model with nothing downstream still carries its own unit tests, so
        # this does not return early -- only the report line is skipped.
        if downstream_nids:
            downstream_map[pred.model_name] = [nid.split(".")[-1] for nid in downstream_nids]

        # incremental+ignore alters nothing physical, so nothing downstream of
        # it moves. Its own unit tests still do: they run the model's SELECT
        # against fixtures, not the merge, so they see the dropped column.
        ignore_incremental = (
            pred.materialization == "incremental"
            and (pred.on_schema_change or "ignore") == "ignore"
        )

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

        # Every model this change removes a column from, keyed by name: the changed
        # model, plus any downstream one that inherits the loss through a SELECT *.
        # The tests are checked against the whole map in one pass at the end, so a
        # test is only ever compared with the columns of the model it actually reads.
        lost_by_model: dict[str, list[str]] = (
            {pred.model_name: cascade_removed} if cascade_removed else {}
        )
        col_patterns = _column_patterns(lost_by_model)

        downstream_to_check = [] if ignore_incremental else downstream_nids
        impacts: list[DownstreamImpact] = []
        for ds_nid in downstream_to_check:
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
                        ds_sql = ds_sql_path.read_text(encoding="utf-8")
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

            # A model whose own file did not change can still change shape, when it
            # selects `*` from one that did. Nothing above catches it: the diff has
            # no entry for an identical file, and broken_ref looks for the column by
            # name in SQL that never names it.
            if cascade_removed and base_columns_of and current_columns_of:
                inherited, ds_lost = _inherited_impact(
                    ds_node, base_columns_of, current_columns_of, changed_models=model_node_ids
                )
                if inherited is not None:
                    impacts.append(inherited)
                if ds_lost:
                    # It may lose the column harmlessly -- a table is rebuilt -- and
                    # still break every test that reads it.
                    lost_by_model[ds_node.name] = ds_lost

        test_owners = [node_id, *downstream_to_check]
        impacts += _unit_test_impacts(lost_by_model, test_owners, child_map, unit_test_index)
        impacts += _data_test_impacts(
            lost_by_model, test_owners, child_map, data_test_index, test_sql_index
        )

        if impacts:
            # Escalate to the worst risk found; cascade never downgrades a verdict.
            cascade_safety = worst_safety(
                [pred.safety, *(RISK_SAFETY.get(imp.risk, Safety.WARNING) for imp in impacts)]
            )
            updated[i] = replace(pred, safety=cascade_safety, downstream_impacts=impacts)

    return updated, downstream_map


def attach_downstream_exposures(
    predictions: list[DDLPrediction],
    model_node_ids: dict[str, str],
    all_downstream: dict[str, list[str]],
    child_map: dict[str, list[str]],
    exposure_index: dict,
) -> list[DDLPrediction]:
    """Name the exposures reading a model whose verdict is not safe.

    An exposure records that something outside the project -- a dashboard, a
    notebook, a reverse-ETL sync -- reads a model, and it declares that at model
    granularity. There are no columns in one, so this can never claim a dashboard
    breaks: only that its owner is downstream of a change that is not safe.

    Deliberately not a risk and deliberately not an escalation. An exposure
    existing does not make a change more dangerous, and inflating the verdict for
    it would train people to ignore the line. For the same reason it is left off
    safe verdicts: a list of dashboards under a green check is noise.

    Exposures hang off every model they depend on as a direct child, so the
    changed model plus its downstream set covers them with no extra walk.
    """
    if not exposure_index:
        return predictions

    updated = list(predictions)
    for i, pred in enumerate(updated):
        if pred.safety == Safety.SAFE:
            continue
        node_id = model_node_ids.get(pred.model_name)
        if not node_id:
            continue

        found: dict[str, object] = {}
        for owner_nid in (node_id, *all_downstream.get(node_id, [])):
            for child in child_map.get(owner_nid) or []:
                exposure = exposure_index.get(child)
                if exposure is not None:
                    found[child] = exposure
        if found:
            updated[i] = replace(pred, downstream_exposures=[found[k] for k in sorted(found)])
    return updated
