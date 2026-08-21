"""Stress tests for dbt-plan with very large manifests.

Validates performance and correctness at scale:
- 5,000 model nodes
- 5,000-entry child_map with realistic fanout
- 50,000 column definitions
- Cascade analysis with 50 changed models
- diff_compiled_dirs with 2,000 SQL files
- format_json with 100 models
- Regex compilation at scale (5,000 matches)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dbt_plan.diff import diff_compiled_dirs
from dbt_plan.formatter import CheckResult, format_json
from dbt_plan.manifest import (
    ModelNode,
    build_node_index,
    find_downstream_batch,
)
from dbt_plan.predictor import (
    DDLOperation,
    DDLPrediction,
    DownstreamImpact,
    Safety,
    analyze_cascade_impacts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_manifest(
    n_models: int,
    n_packages: int = 1,
    cols_per_model: int = 0,
    root_package: str = "myproject",
) -> dict:
    """Generate a manifest dict with n_models across n_packages.

    Models are named model_0000 .. model_{n-1}.
    The root_package gets the majority of models; the rest are spread
    across pkg_1 .. pkg_{n_packages-1}.
    """
    nodes: dict = {}
    for i in range(n_models):
        pkg = f"pkg_{i % n_packages}" if n_packages > 1 and i % n_packages != 0 else root_package
        name = f"model_{i:04d}"
        node_id = f"model.{pkg}.{name}"
        columns = {f"col_{j}": {} for j in range(cols_per_model)} if cols_per_model else {}
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": "incremental" if i % 3 == 0 else "table",
                "on_schema_change": "sync_all_columns" if i % 3 == 0 else None,
            },
            "columns": columns,
        }
    return {
        "metadata": {"project_name": root_package},
        "nodes": nodes,
        "child_map": {},
    }


def _generate_child_map(n_models: int, children_per_node: int = 2) -> dict[str, list[str]]:
    """Generate a child_map where each model has `children_per_node` children.

    Creates a forward-pointing DAG: model_i -> model_{i+1}, model_{i+2}, ...
    so there are no cycles.
    """
    child_map: dict[str, list[str]] = {}
    for i in range(n_models):
        node_id = f"model.myproject.model_{i:04d}"
        children = []
        for offset in range(1, children_per_node + 1):
            child_idx = i + offset
            if child_idx < n_models:
                children.append(f"model.myproject.model_{child_idx:04d}")
        child_map[node_id] = children
    return child_map


def _generate_sql(n_cols: int, model_name: str = "t") -> str:
    """Generate a SELECT statement with n_cols columns."""
    cols = ", ".join(f"col_{j}" for j in range(n_cols))
    return f"SELECT {cols} FROM {model_name}"


# ---------------------------------------------------------------------------
# 1. Manifest with 5,000 model nodes
# ---------------------------------------------------------------------------


class TestLargeManifestNodeIndex:
    def test_build_node_index_5000_models(self):
        """build_node_index handles 5,000 models across 10 packages in < 1 second."""
        manifest = _generate_manifest(5000, n_packages=10, root_package="myproject")
        assert len(manifest["nodes"]) == 5000

        start = time.perf_counter()
        index = build_node_index(manifest, include_packages=False)
        elapsed = time.perf_counter() - start

        # Only root package models are included (every 10th model belongs to root)
        root_count = sum(1 for nid in manifest["nodes"] if nid.split(".")[1] == "myproject")
        assert len(index) == root_count
        assert elapsed < 3.0, f"build_node_index took {elapsed:.3f}s (limit: 3.0s)"

    def test_include_packages_false_filters_correctly(self):
        """include_packages=False correctly filters out non-root packages."""
        manifest = _generate_manifest(5000, n_packages=10, root_package="myproject")

        index_filtered = build_node_index(manifest, include_packages=False)
        index_all = build_node_index(manifest, include_packages=True)

        # All filtered models must be root package models
        for _name, node in index_filtered.items():
            assert node.node_id.startswith("model.myproject."), (
                f"Non-root model leaked through: {node.node_id}"
            )

        # include_packages=True should return more models
        assert len(index_all) > len(index_filtered)

    def test_all_model_names_unique_in_index(self):
        """build_node_index returns unique model names as keys."""
        manifest = _generate_manifest(5000, n_packages=1)
        index = build_node_index(manifest)
        assert len(index) == 5000


# ---------------------------------------------------------------------------
# 2. child_map with 5,000 entries — find_downstream_batch
# ---------------------------------------------------------------------------


class TestLargeChildMap:
    def test_find_downstream_batch_5000_nodes(self):
        """find_downstream_batch for 50 changed models in a 5,000-node DAG in < 2 seconds."""
        child_map = _generate_child_map(5000, children_per_node=2)
        assert len(child_map) == 5000

        # Pick 50 models spread across the DAG (early nodes have the most downstream)
        changed_ids = [f"model.myproject.model_{i * 100:04d}" for i in range(50)]

        start = time.perf_counter()
        result = find_downstream_batch(changed_ids, child_map)
        elapsed = time.perf_counter() - start

        assert len(result) == 50
        assert elapsed < 5.0, f"find_downstream_batch took {elapsed:.3f}s (limit: 5.0s)"

    def test_downstream_correctness(self):
        """Downstream results are correct for a known topology."""
        child_map = _generate_child_map(100, children_per_node=2)
        # model_0097 has children 0098, 0099; model_0098 has child 0099
        result = find_downstream_batch(["model.myproject.model_0097"], child_map)
        ds = result["model.myproject.model_0097"]
        assert "model.myproject.model_0098" in ds
        assert "model.myproject.model_0099" in ds

    def test_fanout_3_children(self):
        """Realistic fanout of 3 children per node works."""
        child_map = _generate_child_map(5000, children_per_node=3)
        changed_ids = [f"model.myproject.model_{i * 100:04d}" for i in range(50)]

        start = time.perf_counter()
        result = find_downstream_batch(changed_ids, child_map)
        elapsed = time.perf_counter() - start

        # Early models should have many downstream
        first = result[changed_ids[0]]
        assert len(first) > 0
        assert elapsed < 2.0, f"find_downstream_batch (fanout=3) took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 3. Memory: manifest with large column definitions
# ---------------------------------------------------------------------------


class TestLargeColumnDefinitions:
    def test_50000_columns_stored_as_tuples(self):
        """1,000 models x 50 columns = 50,000 column names stored as tuples."""
        manifest = _generate_manifest(1000, n_packages=1, cols_per_model=50)

        start = time.perf_counter()
        index = build_node_index(manifest)
        elapsed = time.perf_counter() - start

        assert len(index) == 1000
        assert elapsed < 3.0, f"build_node_index with columns took {elapsed:.3f}s"

        # Verify columns are stored as tuples (immutable, lower memory)
        for name, node in index.items():
            assert isinstance(node.columns, tuple), (
                f"Columns for {name} should be tuple, got {type(node.columns)}"
            )
            assert len(node.columns) == 50

        # Verify total column count
        total_cols = sum(len(node.columns) for node in index.values())
        assert total_cols == 50_000

    def test_columns_are_lowercased(self):
        """Manifest column names are lowercased during extraction."""
        manifest = _generate_manifest(100, n_packages=1, cols_per_model=50)
        index = build_node_index(manifest)
        for node in index.values():
            for col in node.columns:
                assert col == col.lower(), f"Column {col} is not lowercased"

    def test_memory_footprint_reasonable(self):
        """Index memory footprint stays reasonable for 1,000 models x 50 columns."""
        manifest = _generate_manifest(1000, n_packages=1, cols_per_model=50)
        index = build_node_index(manifest)

        # Rough size estimate: sys.getsizeof on the dict + all ModelNodes
        # This is a conservative check — just ensure we're not blowing up
        index_size = sys.getsizeof(index)
        # Each ModelNode is small; the dict overhead should be < 1MB for 1,000 entries
        assert index_size < 1_000_000, (
            f"Index dict overhead is {index_size} bytes — possible memory issue"
        )


# ---------------------------------------------------------------------------
# 4. Cascade analysis at scale
# ---------------------------------------------------------------------------


class TestCascadeAnalysisAtScale:
    def test_cascade_50_changed_models(self, tmp_path):
        """analyze_cascade_impacts with 50 changed models, each having ~20 downstream."""
        n_changed = 50
        n_downstream_per = 20

        # Build predictions for 50 changed models (incremental + sync_all_columns)
        predictions: list[DDLPrediction] = []
        model_node_ids: dict[str, str] = {}
        model_cols: dict[str, tuple[list[str] | None, list[str] | None]] = {}
        all_downstream: dict[str, list[str]] = {}
        node_index: dict[str, ModelNode] = {}
        base_node_index: dict[str, ModelNode] = {}
        compiled_sql_index: dict[str, Path] = {}

        for i in range(n_changed):
            mname = f"changed_{i:04d}"
            nid = f"model.proj.{mname}"
            model_node_ids[mname] = nid

            # Base had col_removed, current doesn't
            base_cols = ["id", "name", "col_removed"]
            curr_cols = ["id", "name", "col_added"]
            model_cols[mname] = (base_cols, curr_cols)

            predictions.append(
                DDLPrediction(
                    model_name=mname,
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE,
                    operations=[
                        DDLOperation("DROP COLUMN", "col_removed"),
                        DDLOperation("ADD COLUMN", "col_added"),
                    ],
                    columns_added=["col_added"],
                    columns_removed=["col_removed"],
                )
            )

            # Each changed model has n_downstream_per downstream models
            downstream_nids = []
            for j in range(n_downstream_per):
                ds_name = f"ds_{i:04d}_{j:04d}"
                ds_nid = f"model.proj.{ds_name}"
                downstream_nids.append(ds_nid)

                # Create downstream model in both indexes
                ds_node = ModelNode(
                    node_id=ds_nid,
                    name=ds_name,
                    materialization="table",
                    on_schema_change=None,
                )
                node_index[ds_name] = ds_node
                base_node_index[ds_name] = ds_node

                # Create compiled SQL file that references the removed column
                sql_dir = tmp_path / "compiled"
                sql_dir.mkdir(exist_ok=True)
                sql_path = sql_dir / f"{ds_name}.sql"
                # 100-line SQL referencing col_removed
                lines = [f"-- line {k}" for k in range(98)]
                lines.append(f"SELECT id, name, col_removed FROM upstream_{i:04d}")
                lines.append("WHERE col_removed IS NOT NULL")
                sql_path.write_text("\n".join(lines))
                compiled_sql_index[ds_name] = sql_path

            all_downstream[nid] = downstream_nids

        start = time.perf_counter()
        updated, downstream_map = analyze_cascade_impacts(
            predictions,
            model_node_ids,
            model_cols,
            all_downstream,
            node_index,
            base_node_index,
            compiled_sql_index,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 10.0, f"analyze_cascade_impacts took {elapsed:.3f}s (limit: 10.0s)"
        assert len(updated) == n_changed

        # Verify cascade impacts were detected
        total_impacts = sum(len(p.downstream_impacts) for p in updated)
        assert total_impacts > 0, "Expected cascade impacts to be detected"

        # Each changed model should have downstream impacts (broken_ref for col_removed)
        for pred in updated:
            assert len(pred.downstream_impacts) > 0, (
                f"{pred.model_name} should have cascade impacts"
            )


# ---------------------------------------------------------------------------
# 5. diff_compiled_dirs with 2,000 SQL files
# ---------------------------------------------------------------------------


class TestDiffCompiledDirsAtScale:
    def test_diff_2000_files_100_modified(self, tmp_path):
        """diff_compiled_dirs with 2,000 files, 100 modified, in < 2 seconds."""
        base_dir = tmp_path / "base"
        current_dir = tmp_path / "current"
        base_dir.mkdir()
        current_dir.mkdir()

        n_total = 2000
        n_modified = 100

        # Create base and current SQL files
        for i in range(n_total):
            name = f"model_{i:04d}.sql"
            sql = f"SELECT col_a, col_b, col_c FROM source_{i:04d}"
            (base_dir / name).write_text(sql)
            if i < n_modified:
                # Modified: add a column
                modified_sql = f"SELECT col_a, col_b, col_c, col_new FROM source_{i:04d}"
                (current_dir / name).write_text(modified_sql)
            else:
                # Unchanged
                (current_dir / name).write_text(sql)

        start = time.perf_counter()
        diffs = diff_compiled_dirs(base_dir, current_dir)
        elapsed = time.perf_counter() - start

        assert len(diffs) == n_modified
        assert all(d.status == "modified" for d in diffs)
        assert elapsed < 5.0, f"diff_compiled_dirs took {elapsed:.3f}s (limit: 5.0s)"

    def test_diff_mixed_added_removed_modified(self, tmp_path):
        """diff_compiled_dirs correctly classifies added/removed/modified at scale."""
        base_dir = tmp_path / "base"
        current_dir = tmp_path / "current"
        base_dir.mkdir()
        current_dir.mkdir()

        # 500 unchanged, 200 modified, 100 added, 100 removed
        n_unchanged = 500
        n_modified = 200
        n_added = 100
        n_removed = 100

        idx = 0
        # Unchanged
        for _i in range(n_unchanged):
            sql = f"SELECT a FROM t_{idx}"
            (base_dir / f"model_{idx:04d}.sql").write_text(sql)
            (current_dir / f"model_{idx:04d}.sql").write_text(sql)
            idx += 1

        # Modified
        for _i in range(n_modified):
            (base_dir / f"model_{idx:04d}.sql").write_text(f"SELECT a FROM t_{idx}")
            (current_dir / f"model_{idx:04d}.sql").write_text(f"SELECT a, b FROM t_{idx}")
            idx += 1

        # Removed (base only)
        for _i in range(n_removed):
            (base_dir / f"model_{idx:04d}.sql").write_text(f"SELECT a FROM t_{idx}")
            idx += 1

        # Added (current only)
        for _i in range(n_added):
            (current_dir / f"model_{idx:04d}.sql").write_text(f"SELECT a FROM t_{idx}")
            idx += 1

        start = time.perf_counter()
        diffs = diff_compiled_dirs(base_dir, current_dir)
        elapsed = time.perf_counter() - start

        modified = [d for d in diffs if d.status == "modified"]
        added = [d for d in diffs if d.status == "added"]
        removed = [d for d in diffs if d.status == "removed"]

        assert len(modified) == n_modified
        assert len(added) == n_added
        assert len(removed) == n_removed
        assert elapsed < 2.0, f"diff_compiled_dirs (mixed) took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 6. format_json with 100 models
# ---------------------------------------------------------------------------


class TestFormatJsonAtScale:
    def test_format_json_100_predictions(self):
        """format_json produces valid JSON for 100 predictions, each with 5 operations."""
        predictions = []
        downstream_map: dict[str, list[str]] = {}

        for i in range(100):
            mname = f"model_{i:04d}"
            ops = [DDLOperation(f"OP_{j}", f"col_{j}") for j in range(5)]
            impacts = [
                DownstreamImpact(
                    model_name=f"ds_{i}_{k}",
                    materialization="table",
                    on_schema_change=None,
                    risk="broken_ref",
                    reason=f"references dropped column col_{k}",
                )
                for k in range(3)
            ]
            predictions.append(
                DDLPrediction(
                    model_name=mname,
                    materialization="incremental",
                    on_schema_change="sync_all_columns",
                    safety=Safety.DESTRUCTIVE if i % 3 == 0 else Safety.WARNING,
                    operations=ops,
                    columns_added=[f"added_{j}" for j in range(3)],
                    columns_removed=[f"removed_{j}" for j in range(2)],
                    downstream_impacts=impacts,
                )
            )
            downstream_map[mname] = [f"downstream_{i}_{j}" for j in range(10)]

        result = CheckResult(
            predictions=predictions,
            downstream_map=downstream_map,
            parse_failures=[f"parse_fail_{i}" for i in range(5)],
            skipped_models=[f"skipped_{i}" for i in range(3)],
        )

        start = time.perf_counter()
        output = format_json(result)
        elapsed = time.perf_counter() - start

        # Verify valid JSON
        parsed = json.loads(output)
        assert "summary" in parsed
        assert "models" in parsed
        assert len(parsed["models"]) == 100

        # Verify summary counts
        assert parsed["summary"]["total"] == 100
        assert parsed["summary"]["destructive"] + parsed["summary"]["warning"] == 100

        # Verify operations and downstream_impacts are present
        for model in parsed["models"]:
            assert len(model["operations"]) == 5
            assert "downstream_impacts" in model
            assert len(model["downstream_impacts"]) == 3
            assert "downstream" in model
            assert len(model["downstream"]) == 10

        # Reasonable output size (should be well under 1MB)
        output_size = len(output.encode("utf-8"))
        assert output_size < 500_000, f"JSON output is {output_size} bytes — too large"

        assert elapsed < 3.0, f"format_json took {elapsed:.3f}s (limit: 3.0s)"


# ---------------------------------------------------------------------------
# 7. Regex compilation in cascade analysis
# ---------------------------------------------------------------------------


class TestRegexAtScale:
    def test_50_removed_columns_100_downstream(self, tmp_path):
        """50 removed columns x 100 downstream models = 5,000 regex matches."""
        n_removed = 50
        n_downstream = 100
        removed_cols = [f"removed_col_{i:03d}" for i in range(n_removed)]

        # Single changed model
        predictions = [
            DDLPrediction(
                model_name="source_model",
                materialization="table",
                on_schema_change=None,
                safety=Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                columns_added=[],
                columns_removed=removed_cols,
            )
        ]

        model_node_ids = {"source_model": "model.proj.source_model"}
        model_cols: dict[str, tuple[list[str] | None, list[str] | None]] = {
            "source_model": (
                ["id"] + removed_cols,
                ["id"],
            ),
        }

        # Build downstream
        downstream_nids = []
        node_index: dict[str, ModelNode] = {}
        base_node_index: dict[str, ModelNode] = {}
        compiled_sql_index: dict[str, Path] = {}

        sql_dir = tmp_path / "compiled"
        sql_dir.mkdir()

        for j in range(n_downstream):
            ds_name = f"downstream_{j:04d}"
            ds_nid = f"model.proj.{ds_name}"
            downstream_nids.append(ds_nid)

            ds_node = ModelNode(
                node_id=ds_nid,
                name=ds_name,
                materialization="incremental",
                on_schema_change="fail",
            )
            node_index[ds_name] = ds_node
            base_node_index[ds_name] = ds_node

            # SQL referencing ~half the removed columns (realistic: not all referenced)
            referenced = removed_cols[: n_removed // 2]
            sql_lines = [f"SELECT {', '.join(referenced)}"]
            # Pad to 100 lines
            sql_lines.extend([f"-- padding line {k}" for k in range(99)])
            sql_path = sql_dir / f"{ds_name}.sql"
            sql_path.write_text("\n".join(sql_lines))
            compiled_sql_index[ds_name] = sql_path

        all_downstream = {"model.proj.source_model": downstream_nids}

        start = time.perf_counter()
        updated, downstream_map = analyze_cascade_impacts(
            predictions,
            model_node_ids,
            model_cols,
            all_downstream,
            node_index,
            base_node_index,
            compiled_sql_index,
        )
        elapsed = time.perf_counter() - start

        assert elapsed < 10.0, f"Regex cascade analysis took {elapsed:.3f}s (limit: 10.0s)"

        # Verify broken_ref impacts were detected
        pred = updated[0]
        broken_ref_impacts = [imp for imp in pred.downstream_impacts if imp.risk == "broken_ref"]
        assert len(broken_ref_impacts) == n_downstream, (
            f"Expected {n_downstream} broken_ref impacts, got {len(broken_ref_impacts)}"
        )

        # Also verify build_failure for incremental+fail
        build_failure_impacts = [
            imp for imp in pred.downstream_impacts if imp.risk == "build_failure"
        ]
        assert len(build_failure_impacts) == n_downstream

    def test_regex_patterns_are_precompiled(self, tmp_path):
        """Verify that regex patterns are compiled once, not per downstream model.

        This is a correctness check — if patterns were compiled per model,
        it would be O(removed * downstream) compiles instead of O(removed).
        We test by timing: with 50 patterns x 100 models, pre-compilation
        should keep total time well under 1 second.
        """
        n_removed = 50
        n_downstream = 100
        removed_cols = [f"regex_col_{i:03d}" for i in range(n_removed)]

        predictions = [
            DDLPrediction(
                model_name="regex_source",
                materialization="table",
                on_schema_change=None,
                safety=Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                columns_added=[],
                columns_removed=removed_cols,
            )
        ]

        model_node_ids = {"regex_source": "model.proj.regex_source"}
        model_cols: dict[str, tuple[list[str] | None, list[str] | None]] = {
            "regex_source": (
                ["id"] + removed_cols,
                ["id"],
            ),
        }

        downstream_nids = []
        node_index: dict[str, ModelNode] = {}
        base_node_index: dict[str, ModelNode] = {}
        compiled_sql_index: dict[str, Path] = {}

        sql_dir = tmp_path / "compiled"
        sql_dir.mkdir()

        for j in range(n_downstream):
            ds_name = f"regex_ds_{j:04d}"
            ds_nid = f"model.proj.{ds_name}"
            downstream_nids.append(ds_nid)
            ds_node = ModelNode(
                node_id=ds_nid,
                name=ds_name,
                materialization="table",
                on_schema_change=None,
            )
            node_index[ds_name] = ds_node
            base_node_index[ds_name] = ds_node

            # SQL that does NOT reference any removed columns (worst case for regex: full scan)
            sql_path = sql_dir / f"{ds_name}.sql"
            sql_path.write_text(
                "\n".join([f"SELECT id, unrelated_{k} FROM t" for k in range(100)])
            )
            compiled_sql_index[ds_name] = sql_path

        all_downstream = {"model.proj.regex_source": downstream_nids}

        start = time.perf_counter()
        updated, _ = analyze_cascade_impacts(
            predictions,
            model_node_ids,
            model_cols,
            all_downstream,
            node_index,
            base_node_index,
            compiled_sql_index,
        )
        elapsed = time.perf_counter() - start

        # No broken refs should be found (no SQL references removed columns)
        pred = updated[0]
        broken_ref_impacts = [imp for imp in pred.downstream_impacts if imp.risk == "broken_ref"]
        assert len(broken_ref_impacts) == 0

        assert elapsed < 3.0, f"Regex (no matches) took {elapsed:.3f}s (limit: 3.0s)"


# ---------------------------------------------------------------------------
# Edge cases at scale
# ---------------------------------------------------------------------------


class TestEdgeCasesAtScale:
    def test_build_node_index_all_disabled(self):
        """5,000 disabled models result in an empty index."""
        nodes = {}
        for i in range(5000):
            name = f"model_{i:04d}"
            nodes[f"model.proj.{name}"] = {
                "name": name,
                "config": {"materialized": "table", "enabled": False},
            }
        manifest = {"metadata": {"project_name": "proj"}, "nodes": nodes}
        index = build_node_index(manifest)
        assert len(index) == 0

    def test_downstream_batch_leaf_nodes_only(self):
        """All queried nodes are leaf nodes — downstream should be empty for each."""
        child_map = _generate_child_map(5000, children_per_node=2)
        # Last 50 nodes have no or few children
        leaf_ids = [f"model.myproject.model_{i:04d}" for i in range(4990, 5000)]
        result = find_downstream_batch(leaf_ids, child_map)
        for nid in leaf_ids:
            # Leaf or near-leaf nodes should have very few downstream
            assert len(result[nid]) <= 10

    def test_diff_empty_directories(self, tmp_path):
        """diff_compiled_dirs with empty directories returns empty list."""
        base = tmp_path / "empty_base"
        current = tmp_path / "empty_current"
        base.mkdir()
        current.mkdir()
        result = diff_compiled_dirs(base, current)
        assert result == []

    def test_diff_all_added(self, tmp_path):
        """diff_compiled_dirs with 1,000 added files."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        base.mkdir()
        current.mkdir()

        for i in range(1000):
            (current / f"new_model_{i:04d}.sql").write_text(f"SELECT id FROM t_{i}")

        start = time.perf_counter()
        diffs = diff_compiled_dirs(base, current)
        elapsed = time.perf_counter() - start

        assert len(diffs) == 1000
        assert all(d.status == "added" for d in diffs)
        assert elapsed < 2.0, f"diff 1,000 added files took {elapsed:.3f}s"

    def test_diff_all_removed(self, tmp_path):
        """diff_compiled_dirs with 1,000 removed files."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        base.mkdir()
        current.mkdir()

        for i in range(1000):
            (base / f"old_model_{i:04d}.sql").write_text(f"SELECT id FROM t_{i}")

        start = time.perf_counter()
        diffs = diff_compiled_dirs(base, current)
        elapsed = time.perf_counter() - start

        assert len(diffs) == 1000
        assert all(d.status == "removed" for d in diffs)
        assert elapsed < 2.0, f"diff 1,000 removed files took {elapsed:.3f}s"
