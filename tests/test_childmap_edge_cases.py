"""Tests for child_map edge cases in manifest processing and cascade analysis."""

from __future__ import annotations

from dbt_plan.manifest import (
    ModelNode,
    find_downstream,
    find_downstream_batch,
)
from dbt_plan.predictor import (
    DDLPrediction,
    Safety,
    analyze_cascade_impacts,
)


# ---------------------------------------------------------------------------
# 1. child_map with null values
# ---------------------------------------------------------------------------
class TestChildMapNullValues:
    def test_find_downstream_null_children(self):
        """child_map value is None instead of a list — should not crash."""
        child_map = {"model.p.a": None}
        # child_map.get("model.p.a", []) returns None (not the default [])
        # iterating over None raises TypeError
        result = find_downstream("model.p.a", child_map)
        assert result == []

    def test_find_downstream_batch_null_children(self):
        """find_downstream_batch with null children value — should not crash."""
        child_map = {"model.p.a": None}
        result = find_downstream_batch(["model.p.a"], child_map)
        assert result["model.p.a"] == []

    def test_find_downstream_null_in_transitive_path(self):
        """Transitive child has null value — should not crash mid-BFS."""
        child_map = {
            "model.p.a": ["model.p.b"],
            "model.p.b": None,
        }
        result = find_downstream("model.p.a", child_map)
        assert result == ["model.p.b"]

    def test_find_downstream_batch_null_in_transitive_path(self):
        """Batch version: transitive child has null value — should not crash."""
        child_map = {
            "model.p.a": ["model.p.b"],
            "model.p.b": None,
        }
        result = find_downstream_batch(["model.p.a"], child_map)
        assert result["model.p.a"] == ["model.p.b"]


# ---------------------------------------------------------------------------
# 2. child_map with empty lists
# ---------------------------------------------------------------------------
class TestChildMapEmptyLists:
    def test_find_downstream_empty_children(self):
        """Model with no children returns empty downstream."""
        child_map = {"model.p.a": []}
        result = find_downstream("model.p.a", child_map)
        assert result == []

    def test_find_downstream_batch_empty_children(self):
        """Batch: model with no children returns empty downstream."""
        child_map = {"model.p.a": []}
        result = find_downstream_batch(["model.p.a"], child_map)
        assert result["model.p.a"] == []


# ---------------------------------------------------------------------------
# 3. child_map with test nodes
# ---------------------------------------------------------------------------
class TestChildMapTestNodes:
    def test_models_only_true_skips_tests(self):
        """find_downstream(models_only=True) skips test nodes."""
        child_map = {
            "model.p.a": ["test.p.test_a_is_unique", "model.p.b"],
            "model.p.b": [],
        }
        result = find_downstream("model.p.a", child_map, models_only=True)
        assert result == ["model.p.b"]
        assert "test.p.test_a_is_unique" not in result

    def test_models_only_false_includes_tests(self):
        """find_downstream(models_only=False) includes test nodes."""
        child_map = {
            "model.p.a": ["test.p.test_a_is_unique", "model.p.b"],
            "model.p.b": [],
        }
        result = find_downstream("model.p.a", child_map, models_only=False)
        assert "test.p.test_a_is_unique" in result
        assert "model.p.b" in result

    def test_batch_models_only_true_skips_tests(self):
        """Batch version also filters test nodes when models_only=True."""
        child_map = {
            "model.p.a": ["test.p.t1", "model.p.b"],
            "model.p.b": ["test.p.t2"],
        }
        result = find_downstream_batch(["model.p.a"], child_map, models_only=True)
        assert result["model.p.a"] == ["model.p.b"]

    def test_batch_models_only_false_includes_tests(self):
        """Batch version includes test nodes when models_only=False."""
        child_map = {
            "model.p.a": ["test.p.t1", "model.p.b"],
            "model.p.b": ["test.p.t2"],
        }
        result = find_downstream_batch(["model.p.a"], child_map, models_only=False)
        assert "test.p.t1" in result["model.p.a"]
        assert "test.p.t2" in result["model.p.a"]
        assert "model.p.b" in result["model.p.a"]

    def test_test_only_children(self):
        """All children are test nodes — models_only=True returns empty."""
        child_map = {
            "model.p.a": ["test.p.t1", "test.p.t2"],
        }
        result = find_downstream("model.p.a", child_map, models_only=True)
        assert result == []


# ---------------------------------------------------------------------------
# 4. child_map with source nodes
# ---------------------------------------------------------------------------
class TestChildMapSourceNodes:
    def test_source_as_child_no_crash(self):
        """Sources as children is unusual but should not crash."""
        child_map = {
            "model.p.a": ["source.p.raw_orders"],
        }
        # models_only=True should filter out source nodes
        result = find_downstream("model.p.a", child_map, models_only=True)
        assert result == []

    def test_source_as_child_included_when_models_only_false(self):
        """Sources included when models_only=False."""
        child_map = {
            "model.p.a": ["source.p.raw_orders"],
        }
        result = find_downstream("model.p.a", child_map, models_only=False)
        assert result == ["source.p.raw_orders"]

    def test_batch_source_as_child(self):
        """Batch version handles source children correctly."""
        child_map = {
            "model.p.a": ["source.p.raw_orders", "model.p.b"],
            "model.p.b": [],
        }
        result = find_downstream_batch(["model.p.a"], child_map, models_only=True)
        assert result["model.p.a"] == ["model.p.b"]


# ---------------------------------------------------------------------------
# 5. child_map with duplicate children
# ---------------------------------------------------------------------------
class TestChildMapDuplicateChildren:
    def test_duplicates_in_children_list(self):
        """Duplicate entries in children list — BFS visited set deduplicates."""
        child_map = {
            "model.p.a": ["model.p.b", "model.p.b"],
            "model.p.b": [],
        }
        result = find_downstream("model.p.a", child_map)
        assert result == ["model.p.b"]

    def test_batch_duplicates_in_children_list(self):
        """Batch version also deduplicates."""
        child_map = {
            "model.p.a": ["model.p.b", "model.p.b"],
            "model.p.b": [],
        }
        result = find_downstream_batch(["model.p.a"], child_map)
        assert result["model.p.a"] == ["model.p.b"]

    def test_diamond_dependency_deduplication(self):
        """Diamond pattern: A→B→D, A→C→D — D appears once."""
        child_map = {
            "model.p.a": ["model.p.b", "model.p.c"],
            "model.p.b": ["model.p.d"],
            "model.p.c": ["model.p.d"],
            "model.p.d": [],
        }
        result = find_downstream("model.p.a", child_map)
        assert result.count("model.p.d") == 1
        assert sorted(result) == ["model.p.b", "model.p.c", "model.p.d"]


# ---------------------------------------------------------------------------
# 6. child_map with very long lists (100 direct children)
# ---------------------------------------------------------------------------
class TestChildMapLongLists:
    def test_100_direct_children(self):
        """One model with 100 direct children — verify correctness."""
        children = [f"model.p.child_{i:03d}" for i in range(100)]
        child_map = {"model.p.parent": children}
        for c in children:
            child_map[c] = []

        result = find_downstream("model.p.parent", child_map)
        assert len(result) == 100
        assert result == sorted(children)

    def test_batch_100_direct_children(self):
        """Batch version with 100 direct children — consistent with individual."""
        children = [f"model.p.child_{i:03d}" for i in range(100)]
        child_map = {"model.p.parent": children}
        for c in children:
            child_map[c] = []

        batch = find_downstream_batch(["model.p.parent"], child_map)
        individual = find_downstream("model.p.parent", child_map)
        assert batch["model.p.parent"] == individual

    def test_wide_and_deep_combined(self):
        """50 direct children each with 2 grandchildren — 150 total downstream."""
        child_map = {"model.p.root": []}
        for i in range(50):
            parent = f"model.p.mid_{i:03d}"
            child_map["model.p.root"].append(parent)
            gc1 = f"model.p.leaf_{i:03d}_a"
            gc2 = f"model.p.leaf_{i:03d}_b"
            child_map[parent] = [gc1, gc2]
            child_map[gc1] = []
            child_map[gc2] = []

        result = find_downstream("model.p.root", child_map)
        assert len(result) == 150  # 50 mid + 100 leaf


# ---------------------------------------------------------------------------
# 7. Base manifest child_map merging in _do_check
# ---------------------------------------------------------------------------
class TestChildMapMerging:
    def test_base_entry_preserved_for_removed_model(self):
        """Merge preserves base child_map entry when key missing from current."""
        # Simulates the _do_check merge logic:
        # for k, v in base_child_map.items():
        #     if k not in child_map:
        #         child_map[k] = v
        current_child_map = {
            "model.p.existing": ["model.p.a"],
        }
        base_child_map = {
            "model.p.removed_model": ["model.p.a"],
            "model.p.existing": ["model.p.b"],  # different value — should NOT override
        }

        # Apply same merge logic as _do_check
        merged = dict(current_child_map)
        for k, v in base_child_map.items():
            if k not in merged:
                merged[k] = v

        # Removed model's children should be accessible
        assert "model.p.removed_model" in merged
        assert merged["model.p.removed_model"] == ["model.p.a"]

        # Current takes precedence — should NOT be overwritten by base
        assert merged["model.p.existing"] == ["model.p.a"]

    def test_downstream_of_removed_model_via_merged_childmap(self):
        """After merge, find_downstream works for removed model node_ids."""
        current_child_map = {
            "model.p.surviving": ["model.p.leaf"],
            "model.p.leaf": [],
        }
        base_child_map = {
            "model.p.removed": ["model.p.surviving"],
        }

        merged = dict(current_child_map)
        for k, v in base_child_map.items():
            if k not in merged:
                merged[k] = v

        result = find_downstream("model.p.removed", merged)
        assert "model.p.surviving" in result
        assert "model.p.leaf" in result


# ---------------------------------------------------------------------------
# 8. child_map references non-existent nodes
# ---------------------------------------------------------------------------
class TestChildMapNonExistentNodes:
    def test_find_downstream_nonexistent_child(self):
        """Children referencing non-existent nodes are returned as IDs."""
        child_map = {
            "model.p.a": ["model.p.nonexistent"],
        }
        result = find_downstream("model.p.a", child_map)
        # The ID is still returned; it's find_downstream's job to return IDs,
        # not to validate existence in node_index
        assert result == ["model.p.nonexistent"]

    def test_cascade_analysis_skips_nonexistent_downstream(self):
        """analyze_cascade_impacts gracefully skips downstream not in node_index."""
        pred = DDLPrediction(
            model_name="parent",
            materialization="table",
            on_schema_change=None,
            safety=Safety.SAFE,
            columns_removed=["dropped_col"],
        )
        model_node_ids = {"parent": "model.p.parent"}
        model_cols = {"parent": (["id", "dropped_col"], ["id"])}
        all_downstream = {"model.p.parent": ["model.p.nonexistent"]}
        # Neither node_index nor base_node_index has the nonexistent model
        node_index = {}
        base_node_index = {}
        compiled_sql_index = {}

        updated, ds_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids=model_node_ids,
            model_cols=model_cols,
            all_downstream=all_downstream,
            node_index=node_index,
            base_node_index=base_node_index,
            compiled_sql_index=compiled_sql_index,
        )
        # Should not crash; downstream_map still records the name
        assert "parent" in ds_map
        assert ds_map["parent"] == ["nonexistent"]
        # No impacts because the model wasn't found in either index
        assert updated[0].downstream_impacts == []

    def test_cascade_analysis_found_in_base_index(self):
        """analyze_cascade_impacts falls back to base_node_index for removed models."""
        pred = DDLPrediction(
            model_name="parent",
            materialization="table",
            on_schema_change=None,
            safety=Safety.SAFE,
            columns_removed=["dropped_col"],
        )
        model_node_ids = {"parent": "model.p.parent"}
        model_cols = {"parent": (["id", "dropped_col"], ["id"])}
        all_downstream = {"model.p.parent": ["model.p.child"]}
        node_index = {}  # child not in current
        base_node_index = {
            "child": ModelNode(
                node_id="model.p.child",
                name="child",
                materialization="incremental",
                on_schema_change="fail",
            )
        }
        compiled_sql_index = {}

        updated, ds_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids=model_node_ids,
            model_cols=model_cols,
            all_downstream=all_downstream,
            node_index=node_index,
            base_node_index=base_node_index,
            compiled_sql_index=compiled_sql_index,
        )
        # Should find child in base_node_index and detect build_failure
        assert len(updated[0].downstream_impacts) > 0
        assert any(imp.risk == "build_failure" for imp in updated[0].downstream_impacts)


# ---------------------------------------------------------------------------
# 9. Deeply nested child_map (100 levels)
# ---------------------------------------------------------------------------
class TestChildMapDeeplyNested:
    def test_100_levels_deep_no_recursion_error(self):
        """100 levels deep chain — BFS is iterative, no RecursionError."""
        child_map = {}
        for i in range(100):
            parent = f"model.p.level_{i:03d}"
            child = f"model.p.level_{i + 1:03d}"
            child_map[parent] = [child]
        child_map["model.p.level_100"] = []

        result = find_downstream("model.p.level_000", child_map)
        assert len(result) == 100
        assert "model.p.level_001" in result
        assert "model.p.level_100" in result

    def test_batch_100_levels_deep(self):
        """Batch version also handles 100-level chains without error."""
        child_map = {}
        for i in range(100):
            parent = f"model.p.level_{i:03d}"
            child = f"model.p.level_{i + 1:03d}"
            child_map[parent] = [child]
        child_map["model.p.level_100"] = []

        result = find_downstream_batch(["model.p.level_000"], child_map)
        assert len(result["model.p.level_000"]) == 100

    def test_1000_levels_deep(self):
        """1000 levels deep — stress test BFS iterative approach."""
        child_map = {}
        for i in range(1000):
            parent = f"model.p.n{i}"
            child = f"model.p.n{i + 1}"
            child_map[parent] = [child]
        child_map["model.p.n1000"] = []

        result = find_downstream("model.p.n0", child_map)
        assert len(result) == 1000


# ---------------------------------------------------------------------------
# 10. child_map is completely empty or missing
# ---------------------------------------------------------------------------
class TestChildMapEmptyOrMissing:
    def test_empty_child_map(self):
        """Empty child_map produces empty downstream for any node."""
        result = find_downstream("model.p.a", {})
        assert result == []

    def test_batch_empty_child_map(self):
        """Batch with empty child_map produces empty downstream."""
        result = find_downstream_batch(["model.p.a", "model.p.b"], {})
        assert result == {"model.p.a": [], "model.p.b": []}

    def test_node_not_in_child_map(self):
        """Node not present as key in child_map — returns empty."""
        child_map = {"model.p.other": ["model.p.x"]}
        result = find_downstream("model.p.missing", child_map)
        assert result == []

    def test_batch_node_not_in_child_map(self):
        """Batch: node not present in child_map — returns empty."""
        child_map = {"model.p.other": ["model.p.x"]}
        result = find_downstream_batch(["model.p.missing"], child_map)
        assert result["model.p.missing"] == []

    def test_cascade_analysis_with_empty_downstream(self):
        """analyze_cascade_impacts with empty all_downstream — no impacts."""
        pred = DDLPrediction(
            model_name="parent",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            safety=Safety.DESTRUCTIVE,
            columns_removed=["dropped"],
        )
        updated, ds_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"parent": "model.p.parent"},
            model_cols={"parent": (["id", "dropped"], ["id"])},
            all_downstream={},
            node_index={},
            base_node_index={},
            compiled_sql_index={},
        )
        assert updated[0].downstream_impacts == []
        assert "parent" not in ds_map
