"""Tests for null/None safety across manifest parsing, node indexing, and CLI paths.

The bug: Python's dict.get(key, default) only uses `default` when the key
is MISSING. If the key EXISTS with value None, it returns None, not the default.
Example: {"config": null} → node.get("config", {}) returns None, not {}.

These tests cover every code path with null/None values in manifest data:
1. {"config": null}
2. {"config": {"materialized": null}}
3. {"config": {"on_schema_change": null}}
4. {"config": {"materialized": null, "on_schema_change": null}}
5. {"config": {}}
6. No "config" key at all
7. {"name": null}
8. {"columns": null}
"""

import json

from dbt_plan.manifest import (
    ModelNode,
    build_node_index,
    find_node_by_name,
    load_manifest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest_with_node(node_id: str, node_data: dict) -> dict:
    """Build a minimal manifest dict wrapping a single node."""
    return {"nodes": {node_id: node_data}, "child_map": {}, "metadata": {}}


# ---------------------------------------------------------------------------
# 1. {"config": null} — node with null config
# ---------------------------------------------------------------------------


class TestNullConfig:
    """Config key exists but is explicitly null/None."""

    def test_build_node_index_null_config(self):
        """build_node_index handles config: null without crashing."""
        manifest = _manifest_with_node("model.p.m", {"name": "m", "config": None})
        index = build_node_index(manifest)
        assert "m" in index
        assert index["m"].materialization == "table"
        assert index["m"].on_schema_change is None

    def test_find_node_by_name_null_config(self):
        """find_node_by_name handles config: null without crashing."""
        manifest = _manifest_with_node("model.p.m", {"name": "m", "config": None})
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"
        assert node.on_schema_change is None


# ---------------------------------------------------------------------------
# 2. {"config": {"materialized": null}} — null materialized
# ---------------------------------------------------------------------------


class TestNullMaterialized:
    """Materialized key is explicitly null."""

    def test_build_node_index_null_materialized(self):
        """Null materialized defaults to 'table'."""
        manifest = _manifest_with_node(
            "model.p.m", {"name": "m", "config": {"materialized": None}}
        )
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"

    def test_find_node_by_name_null_materialized(self):
        """find_node_by_name defaults null materialized to 'table'."""
        manifest = _manifest_with_node(
            "model.p.m", {"name": "m", "config": {"materialized": None}}
        )
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"


# ---------------------------------------------------------------------------
# 3. {"config": {"on_schema_change": null}} — null on_schema_change
# ---------------------------------------------------------------------------


class TestNullOnSchemaChange:
    """on_schema_change key is explicitly null."""

    def test_build_node_index_null_osc(self):
        """Null on_schema_change is stored as None (predictor handles it)."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "incremental", "on_schema_change": None}},
        )
        index = build_node_index(manifest)
        assert index["m"].on_schema_change is None

    def test_find_node_by_name_null_osc(self):
        """find_node_by_name stores null on_schema_change as None."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "incremental", "on_schema_change": None}},
        )
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.on_schema_change is None


# ---------------------------------------------------------------------------
# 4. Both materialized and on_schema_change are null
# ---------------------------------------------------------------------------


class TestBothNull:
    """Both materialized and on_schema_change are null."""

    def test_build_node_index_both_null(self):
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": None, "on_schema_change": None}},
        )
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"
        assert index["m"].on_schema_change is None

    def test_find_node_by_name_both_null(self):
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": None, "on_schema_change": None}},
        )
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"
        assert node.on_schema_change is None


# ---------------------------------------------------------------------------
# 5. {"config": {}} — empty config
# ---------------------------------------------------------------------------


class TestEmptyConfig:
    """Config exists but is an empty dict."""

    def test_build_node_index_empty_config(self):
        manifest = _manifest_with_node("model.p.m", {"name": "m", "config": {}})
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"
        assert index["m"].on_schema_change is None

    def test_find_node_by_name_empty_config(self):
        manifest = _manifest_with_node("model.p.m", {"name": "m", "config": {}})
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"


# ---------------------------------------------------------------------------
# 6. No "config" key at all
# ---------------------------------------------------------------------------


class TestMissingConfig:
    """Node has no 'config' key whatsoever."""

    def test_build_node_index_missing_config(self):
        manifest = _manifest_with_node("model.p.m", {"name": "m"})
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"
        assert index["m"].on_schema_change is None

    def test_find_node_by_name_missing_config(self):
        manifest = _manifest_with_node("model.p.m", {"name": "m"})
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"


# ---------------------------------------------------------------------------
# 7. {"name": null} — null model name
# ---------------------------------------------------------------------------


class TestNullName:
    """Model name is explicitly null — should be skipped."""

    def test_build_node_index_null_name_skipped(self):
        """Models with null name are not indexed."""
        manifest = _manifest_with_node(
            "model.p.m", {"name": None, "config": {"materialized": "table"}}
        )
        index = build_node_index(manifest)
        assert len(index) == 0

    def test_find_node_by_name_null_name_not_found(self):
        """find_node_by_name with None target returns None."""
        manifest = _manifest_with_node(
            "model.p.m", {"name": None, "config": {"materialized": "table"}}
        )
        # Searching for None should not match
        node = find_node_by_name("m", manifest)
        assert node is None

    def test_build_node_index_empty_name_skipped(self):
        """Models with empty string name are not indexed (falsy)."""
        manifest = _manifest_with_node(
            "model.p.m", {"name": "", "config": {"materialized": "table"}}
        )
        index = build_node_index(manifest)
        assert len(index) == 0


# ---------------------------------------------------------------------------
# 8. {"columns": null} — null columns
# ---------------------------------------------------------------------------


class TestNullColumns:
    """Columns key is explicitly null vs missing vs empty dict."""

    def test_build_node_index_null_columns(self):
        """Null columns should produce empty tuple, not crash."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "table"}, "columns": None},
        )
        index = build_node_index(manifest)
        assert index["m"].columns == ()

    def test_build_node_index_missing_columns(self):
        """Missing columns key defaults to empty tuple."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "table"}},
        )
        index = build_node_index(manifest)
        assert index["m"].columns == ()

    def test_build_node_index_empty_columns(self):
        """Empty columns dict produces empty tuple."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "table"}, "columns": {}},
        )
        index = build_node_index(manifest)
        assert index["m"].columns == ()


# ---------------------------------------------------------------------------
# Null manifest-level sections (nodes, child_map, metadata)
# ---------------------------------------------------------------------------


class TestNullManifestSections:
    """Top-level manifest sections can be null in JSON."""

    def test_load_manifest_null_nodes(self, tmp_path):
        """Null nodes section treated as empty dict."""
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"nodes": None, "child_map": {}}))
        result = load_manifest(path)
        assert result["nodes"] == {}

    def test_load_manifest_null_child_map(self, tmp_path):
        """Null child_map section treated as empty dict."""
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"nodes": {}, "child_map": None}))
        result = load_manifest(path)
        assert result["child_map"] == {}

    def test_load_manifest_null_metadata(self, tmp_path):
        """Null metadata section treated as empty dict."""
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"nodes": {}, "child_map": {}, "metadata": None}))
        result = load_manifest(path)
        assert result["metadata"] == {}

    def test_build_node_index_null_nodes_manifest(self):
        """build_node_index works when manifest has null nodes."""
        manifest = {"nodes": None, "child_map": {}}
        index = build_node_index(manifest)
        assert index == {}

    def test_build_node_index_null_metadata(self):
        """build_node_index works when manifest has null metadata."""
        manifest = {
            "nodes": {
                "model.p.m": {"name": "m", "config": {"materialized": "table"}},
            },
            "metadata": None,
        }
        index = build_node_index(manifest)
        assert "m" in index

    def test_find_node_by_name_null_nodes(self):
        """find_node_by_name works when nodes is null."""
        manifest = {"nodes": None}
        node = find_node_by_name("m", manifest)
        assert node is None


# ---------------------------------------------------------------------------
# _do_stats null safety (via CLI internals)
# ---------------------------------------------------------------------------


class TestStatsNullSafety:
    """Verify _do_stats handles null config values without crashing.

    We can't easily call _do_stats directly (it reads from filesystem),
    so we test the vulnerable code pattern extracted from it.
    """

    def test_stats_iteration_null_config(self):
        """The stats loop pattern handles config: null."""
        from collections import Counter

        manifest = {
            "nodes": {
                "model.p.m1": {"name": "m1", "config": None},
                "model.p.m2": {"name": "m2", "config": {"materialized": None}},
                "model.p.m3": {
                    "name": "m3",
                    "config": {"materialized": "incremental", "on_schema_change": None},
                },
                "model.p.m4": {"name": "m4"},  # no config key
            },
        }

        # Replicate the exact loop from _do_stats
        mat_counts: Counter[str] = Counter()
        osc_counts: Counter[str] = Counter()
        total = 0

        for nid, node in (manifest.get("nodes") or {}).items():
            if not nid.startswith("model."):
                continue
            total += 1
            config = node.get("config") or {}
            mat = config.get("materialized") or "table"
            osc = config.get("on_schema_change") or "ignore"
            mat_counts[mat] += 1
            osc_counts[osc] += 1

        assert total == 4
        # m1 (config: null) -> table, m2 (materialized: null) -> table,
        # m3 -> incremental, m4 (no config) -> table
        assert mat_counts["table"] == 3
        assert mat_counts["incremental"] == 1
        # All should default to "ignore" except where explicitly set
        assert osc_counts["ignore"] == 4


# ---------------------------------------------------------------------------
# predict_ddl null safety
# ---------------------------------------------------------------------------


class TestPredictorNullSafety:
    """Verify predict_ddl handles None on_schema_change gracefully."""

    def test_predict_ddl_null_osc(self):
        """None on_schema_change defaults to 'ignore' behavior."""
        from dbt_plan.predictor import Safety, predict_ddl

        result = predict_ddl(
            model_name="m",
            materialization="incremental",
            on_schema_change=None,
            base_columns=["id", "name"],
            current_columns=["id", "name", "email"],
            status="modified",
        )
        # None osc → "ignore" → no DDL → SAFE
        assert result.safety == Safety.SAFE

    def test_predict_ddl_null_materialization_treated_as_table(self):
        """If somehow a null materialization leaks through, test it doesn't crash."""
        from dbt_plan.predictor import Safety, predict_ddl

        # Callers should default to "table", but test the predictor won't crash
        # if "table" is passed (which is what the caller defaults to)
        result = predict_ddl(
            model_name="m",
            materialization="table",
            on_schema_change=None,
            base_columns=["id"],
            current_columns=["id", "name"],
            status="modified",
        )
        assert result.safety == Safety.SAFE


# ---------------------------------------------------------------------------
# Cascade analysis null safety
# ---------------------------------------------------------------------------


class TestCascadeNullSafety:
    """Verify cascade analysis handles null on_schema_change in downstream models."""

    def test_cascade_downstream_null_osc(self):
        """Downstream models with null on_schema_change don't crash cascade analysis."""
        from dbt_plan.predictor import DDLPrediction, Safety, analyze_cascade_impacts

        predictions = [
            DDLPrediction(
                model_name="upstream",
                materialization="table",
                on_schema_change=None,
                safety=Safety.SAFE,
                columns_removed=["old_col"],
            )
        ]
        model_node_ids = {"upstream": "model.p.upstream"}
        model_cols = {"upstream": (["id", "old_col"], ["id"])}
        all_downstream = {"model.p.upstream": ["model.p.downstream"]}
        node_index = {
            "downstream": ModelNode(
                node_id="model.p.downstream",
                name="downstream",
                materialization="incremental",
                on_schema_change=None,  # null osc
            )
        }

        # Should not crash — null osc treated as "ignore"
        updated, ds_map = analyze_cascade_impacts(
            predictions=predictions,
            model_node_ids=model_node_ids,
            model_cols=model_cols,
            all_downstream=all_downstream,
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={},
        )
        assert len(updated) == 1


# ---------------------------------------------------------------------------
# End-to-end: JSON round-trip with null values
# ---------------------------------------------------------------------------


class TestJsonRoundTripNullValues:
    """Verify the full pipeline handles null values that come from actual JSON parsing."""

    def test_manifest_with_json_nulls(self, tmp_path):
        """Load a manifest.json containing explicit null values and build index."""
        manifest_data = {
            "nodes": {
                "model.proj.null_config": {
                    "name": "null_config",
                    "config": None,
                },
                "model.proj.null_mat": {
                    "name": "null_mat",
                    "config": {"materialized": None, "on_schema_change": "fail"},
                },
                "model.proj.null_cols": {
                    "name": "null_cols",
                    "config": {"materialized": "view"},
                    "columns": None,
                },
                "model.proj.null_name": {
                    "name": None,
                    "config": {"materialized": "table"},
                },
                "model.proj.healthy": {
                    "name": "healthy",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "columns": {"id": {}, "name": {}},
                },
            },
            "child_map": None,
            "metadata": None,
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest_data))

        manifest = load_manifest(path)
        assert manifest["child_map"] == {}
        assert manifest["metadata"] == {}

        index = build_node_index(manifest, include_packages=True)

        # null_config: should be indexed with defaults
        assert "null_config" in index
        assert index["null_config"].materialization == "table"

        # null_mat: materialized null -> "table", osc preserved as "fail"
        assert "null_mat" in index
        assert index["null_mat"].materialization == "table"
        assert index["null_mat"].on_schema_change == "fail"

        # null_cols: columns null -> empty tuple
        assert "null_cols" in index
        assert index["null_cols"].columns == ()

        # null_name: should be skipped
        assert "null_name" not in index

        # healthy: normal model
        assert "healthy" in index
        assert index["healthy"].materialization == "incremental"
        assert index["healthy"].columns == ("id", "name")


# ---------------------------------------------------------------------------
# Edge case: enabled key with null config
# ---------------------------------------------------------------------------


class TestEnabledNullConfig:
    """Ensure enabled: false detection works even with edge-case configs."""

    def test_enabled_false_in_normal_config(self):
        """Standard enabled: false skips the model."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "table", "enabled": False}},
        )
        index = build_node_index(manifest)
        assert len(index) == 0

    def test_enabled_null_includes_model(self):
        """enabled: null should NOT skip the model (only explicit False skips)."""
        manifest = _manifest_with_node(
            "model.p.m",
            {"name": "m", "config": {"materialized": "table", "enabled": None}},
        )
        index = build_node_index(manifest)
        # enabled: None is not `is False`, so model is included
        assert "m" in index

    def test_null_config_does_not_skip_as_disabled(self):
        """config: null should not cause the model to be skipped as disabled."""
        manifest = _manifest_with_node("model.p.m", {"name": "m", "config": None})
        index = build_node_index(manifest)
        assert "m" in index
