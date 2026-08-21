"""Tests for JSON output consumed by CI/CD pipeline scripts.

Validates that format_json produces stable, machine-parseable JSON
suitable for jq queries, schema validators, and round-trip reconstruction.
"""

from __future__ import annotations

import json
import subprocess

from dbt_plan.formatter import CheckResult, format_json
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prediction(
    name: str = "model_a",
    materialization: str = "table",
    on_schema_change: str | None = None,
    safety: Safety = Safety.SAFE,
    operations: list[DDLOperation] | None = None,
    columns_added: list[str] | None = None,
    columns_removed: list[str] | None = None,
    downstream_impacts: list[DownstreamImpact] | None = None,
) -> DDLPrediction:
    return DDLPrediction(
        model_name=name,
        materialization=materialization,
        on_schema_change=on_schema_change,
        safety=safety,
        operations=operations or [],
        columns_added=columns_added or [],
        columns_removed=columns_removed or [],
        downstream_impacts=downstream_impacts or [],
    )


def _parse(result: CheckResult) -> dict:
    """Shortcut: format_json + json.loads."""
    return json.loads(format_json(result))


# ===========================================================================
# 1. JSON schema stability
# ===========================================================================


class TestJsonSchemaStability:
    """Top-level keys and per-model keys are always present and typed correctly."""

    def test_top_level_keys_always_present(self):
        """Every JSON output has exactly summary, models, parse_failures, skipped_models."""
        result = CheckResult()
        data = _parse(result)
        assert set(data.keys()) == {"summary", "models", "parse_failures", "skipped_models"}

    def test_summary_always_has_required_keys(self):
        """summary always contains total, safe, warning, destructive."""
        result = CheckResult()
        data = _parse(result)
        for key in ("total", "safe", "warning", "destructive"):
            assert key in data["summary"], f"Missing summary key: {key}"

    def test_summary_cascade_risks_only_when_positive(self):
        """cascade_risks appears in summary only when > 0."""
        # No cascades
        result = CheckResult(predictions=[_make_prediction()])
        data = _parse(result)
        assert "cascade_risks" not in data["summary"]

        # With cascades
        impact = DownstreamImpact(
            model_name="child",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream changed",
        )
        result_cascade = CheckResult(
            predictions=[
                _make_prediction(
                    safety=Safety.WARNING,
                    downstream_impacts=[impact],
                )
            ]
        )
        data_cascade = _parse(result_cascade)
        assert data_cascade["summary"]["cascade_risks"] == 1

    def test_model_always_has_required_keys(self):
        """Each model object has the 7 mandatory keys."""
        required = {
            "model_name",
            "materialization",
            "on_schema_change",
            "safety",
            "operations",
            "columns_added",
            "columns_removed",
        }
        result = CheckResult(predictions=[_make_prediction()])
        data = _parse(result)
        model = data["models"][0]
        assert required.issubset(set(model.keys()))

    def test_operations_is_list_of_dicts_with_operation_and_column(self):
        """operations is always a list of {operation, column}."""
        ops = [
            DDLOperation("ADD COLUMN", "new_col"),
            DDLOperation("CREATE OR REPLACE TABLE"),
        ]
        result = CheckResult(predictions=[_make_prediction(operations=ops)])
        data = _parse(result)
        for op in data["models"][0]["operations"]:
            assert "operation" in op
            assert "column" in op  # column can be null but key must exist

    def test_operations_column_can_be_null(self):
        """operation without column has column: null, not missing."""
        ops = [DDLOperation("CREATE OR REPLACE TABLE")]
        result = CheckResult(predictions=[_make_prediction(operations=ops)])
        data = _parse(result)
        assert data["models"][0]["operations"][0]["column"] is None

    def test_downstream_key_conditional(self):
        """downstream key appears only when the model has downstream models."""
        result_no_ds = CheckResult(predictions=[_make_prediction()])
        data = _parse(result_no_ds)
        assert "downstream" not in data["models"][0]

        result_ds = CheckResult(
            predictions=[_make_prediction()],
            downstream_map={"model_a": ["child_1", "child_2"]},
        )
        data_ds = _parse(result_ds)
        assert data_ds["models"][0]["downstream"] == ["child_1", "child_2"]

    def test_downstream_impacts_key_conditional(self):
        """downstream_impacts key appears only when non-empty."""
        result_no = CheckResult(predictions=[_make_prediction()])
        data = _parse(result_no)
        assert "downstream_impacts" not in data["models"][0]

        impact = DownstreamImpact(
            model_name="child",
            materialization="table",
            on_schema_change=None,
            risk="broken_ref",
            reason="dropped col",
        )
        result_yes = CheckResult(predictions=[_make_prediction(downstream_impacts=[impact])])
        data_yes = _parse(result_yes)
        assert len(data_yes["models"][0]["downstream_impacts"]) == 1

    def test_empty_result_schema(self):
        """Even an empty CheckResult produces a fully-formed JSON."""
        data = _parse(CheckResult())
        assert data["summary"]["total"] == 0
        assert data["models"] == []
        assert data["parse_failures"] == []
        assert data["skipped_models"] == []

    def test_parse_failures_and_skipped_models_present(self):
        """parse_failures and skipped_models are always present."""
        result = CheckResult(
            predictions=[_make_prediction()],
            parse_failures=["broken_sql"],
            skipped_models=["orphan"],
        )
        data = _parse(result)
        assert data["parse_failures"] == ["broken_sql"]
        assert data["skipped_models"] == ["orphan"]


# ===========================================================================
# 2. Safety value enum
# ===========================================================================


class TestSafetyValueEnum:
    """safety is always a lowercase string from a fixed set."""

    def test_safe_is_lowercase(self):
        data = _parse(CheckResult(predictions=[_make_prediction(safety=Safety.SAFE)]))
        assert data["models"][0]["safety"] == "safe"

    def test_warning_is_lowercase(self):
        data = _parse(CheckResult(predictions=[_make_prediction(safety=Safety.WARNING)]))
        assert data["models"][0]["safety"] == "warning"

    def test_destructive_is_lowercase(self):
        data = _parse(CheckResult(predictions=[_make_prediction(safety=Safety.DESTRUCTIVE)]))
        assert data["models"][0]["safety"] == "destructive"

    def test_safety_never_uppercase(self):
        """Exhaustively test all Safety enum values are lowercase."""
        for s in Safety:
            data = _parse(CheckResult(predictions=[_make_prediction(safety=s)]))
            value = data["models"][0]["safety"]
            assert value == value.lower(), f"Safety {s} produced uppercase: {value}"
            assert value in ("safe", "warning", "destructive")

    def test_safety_never_null_or_missing(self):
        """safety key is always present and never null."""
        for s in Safety:
            data = _parse(CheckResult(predictions=[_make_prediction(safety=s)]))
            model = data["models"][0]
            assert "safety" in model
            assert model["safety"] is not None


# ===========================================================================
# 3. Types are consistent
# ===========================================================================


class TestTypesConsistent:
    """All fields have the expected Python types after JSON parse."""

    def test_summary_counts_are_integers(self):
        result = CheckResult(
            predictions=[
                _make_prediction(name="a", safety=Safety.SAFE),
                _make_prediction(name="b", safety=Safety.WARNING),
                _make_prediction(name="c", safety=Safety.DESTRUCTIVE),
            ]
        )
        data = _parse(result)
        for key in ("total", "safe", "warning", "destructive"):
            assert isinstance(data["summary"][key], int), f"{key} is not int"

    def test_columns_added_is_list_of_strings(self):
        result = CheckResult(predictions=[_make_prediction(columns_added=["col_a", "col_b"])])
        data = _parse(result)
        cols = data["models"][0]["columns_added"]
        assert isinstance(cols, list)
        assert all(isinstance(c, str) for c in cols)

    def test_columns_removed_is_list_of_strings(self):
        result = CheckResult(predictions=[_make_prediction(columns_removed=["old_col"])])
        data = _parse(result)
        cols = data["models"][0]["columns_removed"]
        assert isinstance(cols, list)
        assert all(isinstance(c, str) for c in cols)

    def test_columns_lists_empty_by_default(self):
        data = _parse(CheckResult(predictions=[_make_prediction()]))
        assert data["models"][0]["columns_added"] == []
        assert data["models"][0]["columns_removed"] == []

    def test_parse_failures_is_list_of_strings(self):
        result = CheckResult(parse_failures=["m1", "m2"])
        data = _parse(result)
        assert isinstance(data["parse_failures"], list)
        assert all(isinstance(s, str) for s in data["parse_failures"])

    def test_skipped_models_is_list_of_strings(self):
        result = CheckResult(skipped_models=["s1"])
        data = _parse(result)
        assert isinstance(data["skipped_models"], list)
        assert all(isinstance(s, str) for s in data["skipped_models"])

    def test_parse_failures_empty_by_default(self):
        data = _parse(CheckResult())
        assert data["parse_failures"] == []

    def test_skipped_models_empty_by_default(self):
        data = _parse(CheckResult())
        assert data["skipped_models"] == []

    def test_on_schema_change_can_be_null(self):
        """on_schema_change is nullable (None for table/view)."""
        result = CheckResult(predictions=[_make_prediction(on_schema_change=None)])
        data = _parse(result)
        assert data["models"][0]["on_schema_change"] is None

    def test_on_schema_change_can_be_string(self):
        result = CheckResult(
            predictions=[
                _make_prediction(
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                )
            ]
        )
        data = _parse(result)
        assert data["models"][0]["on_schema_change"] == "sync_all_columns"


# ===========================================================================
# 4. Round-trip: reconstruct CheckResult from JSON
# ===========================================================================


class TestRoundTrip:
    """Verify JSON output is complete enough to reconstruct CheckResult data."""

    def _roundtrip(self, result: CheckResult) -> dict:
        """Parse JSON and verify all data is recoverable."""
        return _parse(result)

    def test_model_names_preserved(self):
        result = CheckResult(
            predictions=[
                _make_prediction(name="stg_orders"),
                _make_prediction(name="int_sessions"),
            ]
        )
        data = self._roundtrip(result)
        names = [m["model_name"] for m in data["models"]]
        assert names == ["stg_orders", "int_sessions"]

    def test_safety_preserved(self):
        result = CheckResult(
            predictions=[
                _make_prediction(name="a", safety=Safety.SAFE),
                _make_prediction(name="b", safety=Safety.WARNING),
                _make_prediction(name="c", safety=Safety.DESTRUCTIVE),
            ]
        )
        data = self._roundtrip(result)
        safeties = [m["safety"] for m in data["models"]]
        assert safeties == ["safe", "warning", "destructive"]

    def test_operations_preserved(self):
        ops = [
            DDLOperation("DROP COLUMN", "old_col"),
            DDLOperation("ADD COLUMN", "new_col"),
        ]
        result = CheckResult(predictions=[_make_prediction(operations=ops)])
        data = self._roundtrip(result)
        json_ops = data["models"][0]["operations"]
        assert len(json_ops) == 2
        assert json_ops[0] == {"operation": "DROP COLUMN", "column": "old_col"}
        assert json_ops[1] == {"operation": "ADD COLUMN", "column": "new_col"}

    def test_columns_preserved(self):
        result = CheckResult(
            predictions=[
                _make_prediction(
                    columns_added=["new_a", "new_b"],
                    columns_removed=["old_x"],
                )
            ]
        )
        data = self._roundtrip(result)
        assert data["models"][0]["columns_added"] == ["new_a", "new_b"]
        assert data["models"][0]["columns_removed"] == ["old_x"]

    def test_downstream_preserved(self):
        result = CheckResult(
            predictions=[_make_prediction(name="parent")],
            downstream_map={"parent": ["child_a", "child_b"]},
        )
        data = self._roundtrip(result)
        assert data["models"][0]["downstream"] == ["child_a", "child_b"]

    def test_downstream_impacts_preserved(self):
        impact = DownstreamImpact(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream schema changed, on_schema_change=fail",
        )
        result = CheckResult(
            predictions=[
                _make_prediction(
                    name="stg_orders",
                    safety=Safety.WARNING,
                    downstream_impacts=[impact],
                )
            ]
        )
        data = self._roundtrip(result)
        impacts = data["models"][0]["downstream_impacts"]
        assert len(impacts) == 1
        assert impacts[0]["model_name"] == "fct_orders"
        assert impacts[0]["risk"] == "build_failure"
        assert impacts[0]["reason"] == "upstream schema changed, on_schema_change=fail"

    def test_summary_counts_match_predictions(self):
        result = CheckResult(
            predictions=[
                _make_prediction(name="a", safety=Safety.SAFE),
                _make_prediction(name="b", safety=Safety.SAFE),
                _make_prediction(name="c", safety=Safety.WARNING),
                _make_prediction(name="d", safety=Safety.DESTRUCTIVE),
            ]
        )
        data = self._roundtrip(result)
        assert data["summary"]["total"] == 4
        assert data["summary"]["safe"] == 2
        assert data["summary"]["warning"] == 1
        assert data["summary"]["destructive"] == 1

    def test_parse_failures_preserved(self):
        result = CheckResult(parse_failures=["broken_a", "broken_b"])
        data = self._roundtrip(result)
        assert data["parse_failures"] == ["broken_a", "broken_b"]

    def test_skipped_models_preserved(self):
        result = CheckResult(skipped_models=["orphan_1"])
        data = self._roundtrip(result)
        assert data["skipped_models"] == ["orphan_1"]

    def test_materialization_preserved(self):
        for mat in ("table", "view", "incremental", "ephemeral", "snapshot"):
            result = CheckResult(predictions=[_make_prediction(materialization=mat)])
            data = self._roundtrip(result)
            assert data["models"][0]["materialization"] == mat


# ===========================================================================
# 5. jq-friendly patterns
# ===========================================================================


class TestJqFriendlyPatterns:
    """Common jq queries work on format_json output.

    These tests validate the JSON structure works with real jq expressions
    by using subprocess to call jq when available, falling back to pure
    Python equivalents.
    """

    @staticmethod
    def _jq_available() -> bool:
        try:
            subprocess.run(["jq", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    @staticmethod
    def _jq(json_str: str, query: str) -> str:
        """Run a jq query and return stdout."""
        proc = subprocess.run(
            ["jq", "-r", query],
            input=json_str,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def _make_mixed_result(self) -> CheckResult:
        return CheckResult(
            predictions=[
                _make_prediction(name="safe_model", safety=Safety.SAFE),
                _make_prediction(
                    name="danger_model",
                    safety=Safety.DESTRUCTIVE,
                    columns_removed=["dropped_a", "dropped_b"],
                ),
                _make_prediction(
                    name="warn_model",
                    safety=Safety.WARNING,
                    columns_removed=["dropped_c"],
                ),
            ],
        )

    def test_summary_destructive_count(self):
        """.summary.destructive returns count of destructive models."""
        result = self._make_mixed_result()
        data = _parse(result)
        assert data["summary"]["destructive"] == 1

        if self._jq_available():
            raw = format_json(result)
            assert self._jq(raw, ".summary.destructive") == "1"

    def test_select_destructive_model_names(self):
        """.models[] | select(.safety == "destructive") | .model_name works."""
        result = self._make_mixed_result()
        data = _parse(result)
        names = [m["model_name"] for m in data["models"] if m["safety"] == "destructive"]
        assert names == ["danger_model"]

        if self._jq_available():
            raw = format_json(result)
            out = self._jq(raw, '.models[] | select(.safety == "destructive") | .model_name')
            assert out == "danger_model"

    def test_all_removed_columns_across_models(self):
        """.models[] | .columns_removed[] collects all removed columns."""
        result = self._make_mixed_result()
        data = _parse(result)
        all_removed = []
        for m in data["models"]:
            all_removed.extend(m["columns_removed"])
        assert sorted(all_removed) == ["dropped_a", "dropped_b", "dropped_c"]

        if self._jq_available():
            raw = format_json(result)
            out = self._jq(raw, "[.models[] | .columns_removed[]] | sort | .[]")
            assert out == "dropped_a\ndropped_b\ndropped_c"

    def test_cascade_risks_with_default(self):
        """.summary.cascade_risks // 0 returns 0 when absent."""
        result = CheckResult(predictions=[_make_prediction()])
        data = _parse(result)
        cascade = data["summary"].get("cascade_risks", 0)
        assert cascade == 0

        if self._jq_available():
            raw = format_json(result)
            assert self._jq(raw, ".summary.cascade_risks // 0") == "0"

    def test_cascade_risks_present_value(self):
        """.summary.cascade_risks // 0 returns actual value when present."""
        impact = DownstreamImpact(
            model_name="child",
            materialization="table",
            on_schema_change=None,
            risk="broken_ref",
            reason="dropped col",
        )
        result = CheckResult(predictions=[_make_prediction(downstream_impacts=[impact, impact])])
        data = _parse(result)
        cascade = data["summary"].get("cascade_risks", 0)
        assert cascade == 2

        if self._jq_available():
            raw = format_json(result)
            assert self._jq(raw, ".summary.cascade_risks // 0") == "2"

    def test_filter_warnings(self):
        """Pipeline can filter warning models."""
        result = self._make_mixed_result()
        data = _parse(result)
        warnings = [m["model_name"] for m in data["models"] if m["safety"] == "warning"]
        assert warnings == ["warn_model"]

    def test_count_models(self):
        """.models | length equals summary.total."""
        result = self._make_mixed_result()
        data = _parse(result)
        assert len(data["models"]) == data["summary"]["total"]


# ===========================================================================
# 6. Multiple runs produce identical JSON for same input
# ===========================================================================


class TestIdempotent:
    """Same CheckResult produces byte-identical JSON on multiple calls."""

    def test_simple_idempotent(self):
        result = CheckResult(
            predictions=[
                _make_prediction(name="m1", safety=Safety.SAFE),
                _make_prediction(name="m2", safety=Safety.DESTRUCTIVE),
            ]
        )
        first = format_json(result)
        second = format_json(result)
        assert first == second

    def test_complex_idempotent(self):
        """Complex result with all optional fields is still byte-identical."""
        impact = DownstreamImpact(
            model_name="downstream",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream changed",
        )
        result = CheckResult(
            predictions=[
                _make_prediction(
                    name="parent",
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE,
                    operations=[
                        DDLOperation("DROP COLUMN", "col_a"),
                        DDLOperation("ADD COLUMN", "col_b"),
                    ],
                    columns_added=["col_b"],
                    columns_removed=["col_a"],
                    downstream_impacts=[impact],
                ),
                _make_prediction(name="simple", safety=Safety.SAFE),
            ],
            downstream_map={"parent": ["downstream", "other"]},
            parse_failures=["broken_1"],
            skipped_models=["orphan_1"],
        )
        first = format_json(result)
        second = format_json(result)
        assert first == second

    def test_empty_idempotent(self):
        result = CheckResult()
        first = format_json(result)
        second = format_json(result)
        assert first == second

    def test_bytes_identical(self):
        """Encode to bytes and compare -- catches encoding differences."""
        result = CheckResult(
            predictions=[
                _make_prediction(
                    columns_added=["unicode_col_\u00e9"],
                    columns_removed=["ascii_col"],
                )
            ]
        )
        first_bytes = format_json(result).encode("utf-8")
        second_bytes = format_json(result).encode("utf-8")
        assert first_bytes == second_bytes


# ===========================================================================
# 7. Large output (50 models)
# ===========================================================================


class TestLargeOutput:
    """50-model output is valid JSON with correct summary counts."""

    @staticmethod
    def _make_large_result() -> CheckResult:
        safety_cycle = [Safety.SAFE, Safety.WARNING, Safety.DESTRUCTIVE]
        predictions = []
        for i in range(50):
            safety = safety_cycle[i % 3]
            ops = []
            cols_added: list[str] = []
            cols_removed: list[str] = []
            if safety == Safety.SAFE:
                ops = [DDLOperation("CREATE OR REPLACE TABLE")]
            elif safety == Safety.WARNING:
                ops = [DDLOperation("BUILD FAILURE")]
                cols_added = [f"new_col_{i}"]
            else:
                ops = [DDLOperation("DROP COLUMN", f"removed_col_{i}")]
                cols_removed = [f"removed_col_{i}"]
            predictions.append(
                _make_prediction(
                    name=f"model_{i:03d}",
                    safety=safety,
                    operations=ops,
                    columns_added=cols_added,
                    columns_removed=cols_removed,
                )
            )
        return CheckResult(predictions=predictions)

    def test_50_models_valid_json(self):
        result = self._make_large_result()
        raw = format_json(result)
        data = json.loads(raw)  # must not raise
        assert len(data["models"]) == 50

    def test_50_models_summary_counts_correct(self):
        result = self._make_large_result()
        data = _parse(result)
        assert data["summary"]["total"] == 50
        # 0,3,6,...,48 -> safe: indices 0,3,6,9,...,48 = 17
        # 1,4,7,...,49 -> warning: indices 1,4,7,10,...,49 = 17
        # 2,5,8,...,47 -> destructive: indices 2,5,8,...,47 = 16
        assert data["summary"]["safe"] == 17
        assert data["summary"]["warning"] == 17
        assert data["summary"]["destructive"] == 16

    def test_50_models_summary_adds_up(self):
        result = self._make_large_result()
        data = _parse(result)
        s = data["summary"]
        assert s["safe"] + s["warning"] + s["destructive"] == s["total"]

    def test_50_models_all_model_names_unique(self):
        result = self._make_large_result()
        data = _parse(result)
        names = [m["model_name"] for m in data["models"]]
        assert len(names) == len(set(names))

    def test_50_models_schema_per_model(self):
        """Every model in the large output has all required keys."""
        required = {
            "model_name",
            "materialization",
            "on_schema_change",
            "safety",
            "operations",
            "columns_added",
            "columns_removed",
        }
        result = self._make_large_result()
        data = _parse(result)
        for i, model in enumerate(data["models"]):
            missing = required - set(model.keys())
            assert not missing, f"Model {i} missing keys: {missing}"


# ===========================================================================
# Edge cases for CI script robustness
# ===========================================================================


class TestEdgeCases:
    """Additional edge cases a CI pipeline might encounter."""

    def test_model_with_no_operations(self):
        """A model with empty operations still produces operations: []."""
        result = CheckResult(predictions=[_make_prediction(operations=[])])
        data = _parse(result)
        assert data["models"][0]["operations"] == []

    def test_special_characters_in_model_name(self):
        """Model names with dots/underscores are preserved."""
        result = CheckResult(predictions=[_make_prediction(name="schema.stg_orders__v2")])
        data = _parse(result)
        assert data["models"][0]["model_name"] == "schema.stg_orders__v2"

    def test_all_safety_levels_in_single_output(self):
        """Output with all three safety levels has correct counts."""
        result = CheckResult(
            predictions=[
                _make_prediction(name="s", safety=Safety.SAFE),
                _make_prediction(name="w", safety=Safety.WARNING),
                _make_prediction(name="d", safety=Safety.DESTRUCTIVE),
            ]
        )
        data = _parse(result)
        assert data["summary"]["safe"] == 1
        assert data["summary"]["warning"] == 1
        assert data["summary"]["destructive"] == 1
        assert data["summary"]["total"] == 3

    def test_multiple_downstream_impacts_counted(self):
        """cascade_risks sums all impacts across all models."""
        imp1 = DownstreamImpact("c1", "table", None, "broken_ref", "reason")
        imp2 = DownstreamImpact("c2", "incremental", "fail", "build_failure", "reason")
        imp3 = DownstreamImpact("c3", "view", None, "broken_ref", "reason")
        result = CheckResult(
            predictions=[
                _make_prediction(name="p1", downstream_impacts=[imp1, imp2]),
                _make_prediction(name="p2", downstream_impacts=[imp3]),
            ]
        )
        data = _parse(result)
        assert data["summary"]["cascade_risks"] == 3

    def test_json_is_pretty_printed(self):
        """Output uses indent=2 for readability in CI logs."""
        result = CheckResult(predictions=[_make_prediction()])
        raw = format_json(result)
        # Pretty-printed JSON has newlines and indentation
        assert "\n" in raw
        assert "  " in raw

    def test_output_is_valid_utf8(self):
        """Output can be encoded to UTF-8 without errors."""
        result = CheckResult(predictions=[_make_prediction(columns_added=["col_\u00e9\u00e8"])])
        raw = format_json(result)
        raw.encode("utf-8")  # must not raise

    def test_downstream_impacts_structure(self):
        """Each downstream_impact has model_name, risk, reason."""
        impact = DownstreamImpact(
            model_name="fct_daily",
            materialization="incremental",
            on_schema_change="fail",
            risk="build_failure",
            reason="upstream schema changed",
        )
        result = CheckResult(predictions=[_make_prediction(downstream_impacts=[impact])])
        data = _parse(result)
        di = data["models"][0]["downstream_impacts"][0]
        assert set(di.keys()) == {"model_name", "risk", "reason"}
        assert isinstance(di["model_name"], str)
        assert isinstance(di["risk"], str)
        assert isinstance(di["reason"], str)
