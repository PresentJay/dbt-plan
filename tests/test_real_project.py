"""Tests against the real dbt project in tests/dbt_project/.

Validates that dbt-plan correctly handles an actual dbt-generated
manifest.json and compiled SQL (adapter: duckdb, dbt v1.11.7).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dbt_plan.columns import extract_columns
from dbt_plan.diff import diff_compiled_dirs
from dbt_plan.manifest import (
    build_node_index,
    find_downstream,
    load_manifest,
)
from dbt_plan.predictor import Safety, predict_ddl

REAL_PROJECT = Path(__file__).parent / "dbt_project"
MANIFEST_PATH = REAL_PROJECT / "target" / "manifest.json"
COMPILED_DIR = REAL_PROJECT / "target" / "compiled" / "test_project" / "models"


@pytest.fixture
def real_manifest():
    """Load the real manifest.json."""
    assert MANIFEST_PATH.exists(), f"Manifest not found: {MANIFEST_PATH}"
    return load_manifest(MANIFEST_PATH)


@pytest.fixture
def modified_project(tmp_path):
    """Create a modified copy of compiled SQL with a column change in stg_orders.

    Removes 'customer_id' column from stg_orders (simulating a DROP COLUMN upstream).
    This should cascade to fct_orders which references customer_uuid from stg_orders.
    """
    base_dir = tmp_path / "base"
    current_dir = tmp_path / "current"

    # Copy original as base
    shutil.copytree(COMPILED_DIR, base_dir)
    # Copy original as current, then modify
    shutil.copytree(COMPILED_DIR, current_dir)

    # Modify stg_orders: remove customer_id column
    stg_orders = current_dir / "staging" / "stg_orders.sql"
    stg_orders.write_text(
        "\n\n"
        "SELECT\n"
        "    1 AS order_id,\n"
        "    'store_001' AS store_id,\n"
        "    '2024-01-01' AS order_date\n"
    )

    return base_dir, current_dir


class TestRealManifestLoading:
    def test_load_manifest_succeeds(self, real_manifest):
        """Real manifest.json loads without error."""
        assert "nodes" in real_manifest
        assert "child_map" in real_manifest
        assert "metadata" in real_manifest

    def test_metadata_has_project_name(self, real_manifest):
        """Real manifest has metadata.project_name (dbt v1.5+)."""
        metadata = real_manifest["metadata"]
        assert metadata.get("project_name") == "test_project"
        assert metadata.get("dbt_version") == "1.11.7"
        assert metadata.get("adapter_type") == "duckdb"

    def test_three_model_nodes(self, real_manifest):
        """Manifest contains exactly 3 model nodes."""
        model_nodes = {k: v for k, v in real_manifest["nodes"].items() if k.startswith("model.")}
        assert len(model_nodes) == 3
        assert "model.test_project.stg_orders" in model_nodes
        assert "model.test_project.dim_books" in model_nodes
        assert "model.test_project.fct_orders" in model_nodes

    def test_materializations(self, real_manifest):
        """Models have expected materializations."""
        nodes = real_manifest["nodes"]
        assert nodes["model.test_project.stg_orders"]["config"]["materialized"] == "view"
        assert nodes["model.test_project.dim_books"]["config"]["materialized"] == "table"
        assert nodes["model.test_project.fct_orders"]["config"]["materialized"] == "incremental"

    def test_fct_orders_sync_all_columns(self, real_manifest):
        """fct_orders uses on_schema_change=sync_all_columns."""
        fct = real_manifest["nodes"]["model.test_project.fct_orders"]
        assert fct["config"]["on_schema_change"] == "sync_all_columns"

    def test_child_map_structure(self, real_manifest):
        """stg_orders feeds into both dim_books and fct_orders."""
        child_map = real_manifest["child_map"]
        stg_children = child_map.get("model.test_project.stg_orders", [])
        assert "model.test_project.dim_books" in stg_children
        assert "model.test_project.fct_orders" in stg_children
        # A leaf model still has its unit tests hanging off it, and nothing else.
        assert child_map.get("model.test_project.dim_books", []) == [
            "unit_test.test_project.dim_books.test_dim_books_groups_by_store"
        ]
        assert child_map.get("model.test_project.fct_orders", []) == []


class TestRealNodeIndex:
    def test_build_node_index_uses_metadata_project_name(self, real_manifest):
        """build_node_index uses metadata.project_name for root project detection.

        The real manifest has metadata.project_name='test_project',
        so it should NOT fall back to the most-common-package heuristic.
        """
        index = build_node_index(real_manifest, include_packages=False)
        assert len(index) == 3
        assert "stg_orders" in index
        assert "dim_books" in index
        assert "fct_orders" in index

    def test_node_materializations(self, real_manifest):
        """Node index preserves correct materializations."""
        index = build_node_index(real_manifest)
        assert index["stg_orders"].materialization == "view"
        assert index["dim_books"].materialization == "table"
        assert index["fct_orders"].materialization == "incremental"

    def test_node_on_schema_change(self, real_manifest):
        """Node index preserves on_schema_change settings."""
        index = build_node_index(real_manifest)
        assert index["fct_orders"].on_schema_change == "sync_all_columns"
        assert index["stg_orders"].on_schema_change == "ignore"

    def test_node_columns_empty(self, real_manifest):
        """Real manifest has no column definitions (empty columns dict)."""
        index = build_node_index(real_manifest)
        # The manifest has "columns": {} for all models — no column docs
        assert index["stg_orders"].columns == ()
        assert index["fct_orders"].columns == ()

    def test_downstream_from_stg_orders(self, real_manifest):
        """stg_orders downstream includes dim_books and fct_orders."""
        child_map = real_manifest["child_map"]
        downstream = find_downstream("model.test_project.stg_orders", child_map)
        assert sorted(downstream) == [
            "model.test_project.dim_books",
            "model.test_project.fct_orders",
        ]

    def test_leaf_models_have_no_downstream(self, real_manifest):
        """dim_books and fct_orders are leaf models with no downstream."""
        child_map = real_manifest["child_map"]
        assert find_downstream("model.test_project.dim_books", child_map) == []
        assert find_downstream("model.test_project.fct_orders", child_map) == []


class TestRealCompiledSQL:
    def test_compiled_files_exist(self):
        """All 3 compiled SQL files exist."""
        assert (COMPILED_DIR / "staging" / "stg_orders.sql").exists()
        assert (COMPILED_DIR / "marts" / "dim_books.sql").exists()
        assert (COMPILED_DIR / "marts" / "fct_orders.sql").exists()

    def test_stg_orders_columns(self):
        """stg_orders compiled SQL has expected columns."""
        sql = (COMPILED_DIR / "staging" / "stg_orders.sql").read_text(encoding="utf-8")
        # duckdb dialect; try generic parsing
        cols = extract_columns(sql, dialect="duckdb")
        if cols is None:
            # Fallback: duckdb might not be a known sqlglot dialect
            cols = extract_columns(sql, dialect="snowflake")
        assert cols is not None
        assert "order_id" in cols
        assert "store_id" in cols
        assert "order_date" in cols
        assert "customer_id" in cols

    def test_fct_orders_columns(self):
        """fct_orders compiled SQL has expected columns.

        Tries duckdb dialect first (the project's adapter), falls back to snowflake
        if duckdb parse fails. This fallback exists because sqlglot's DuckDB dialect
        may not handle all compiled SQL patterns — if this fallback triggers,
        investigate whether a duckdb-specific SQL pattern regressed.
        """
        sql = (COMPILED_DIR / "marts" / "fct_orders.sql").read_text(encoding="utf-8")
        cols = extract_columns(sql, dialect="duckdb")
        if cols is None:
            cols = extract_columns(sql, dialect="snowflake")
        assert cols is not None
        assert "order_id" in cols
        assert "store_id" in cols
        assert "customer_uuid" in cols
        assert "source" in cols

    def test_dim_books_columns(self):
        """dim_books compiled SQL has expected columns."""
        sql = (COMPILED_DIR / "marts" / "dim_books.sql").read_text(encoding="utf-8")
        cols = extract_columns(sql, dialect="duckdb")
        if cols is None:
            cols = extract_columns(sql, dialect="snowflake")
        assert cols is not None
        assert "store_id" in cols
        assert "title" in cols


class TestRealDiffAndCheck:
    def test_no_diff_when_identical(self, tmp_path):
        """No diff when base and current are identical copies."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        shutil.copytree(COMPILED_DIR, base)
        shutil.copytree(COMPILED_DIR, current)

        diffs = diff_compiled_dirs(base, current)
        assert diffs == []

    def test_diff_detects_modified_model(self, modified_project):
        """Diff detects stg_orders as modified after column removal."""
        base_dir, current_dir = modified_project
        diffs = diff_compiled_dirs(base_dir, current_dir)

        assert len(diffs) == 1
        assert diffs[0].model_name == "stg_orders"
        assert diffs[0].status == "modified"

    def test_diff_detects_added_model(self, tmp_path):
        """Diff detects a new model added to current."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        shutil.copytree(COMPILED_DIR, base)
        shutil.copytree(COMPILED_DIR, current)

        # Add a new model to current
        new_model = current / "marts" / "dim_users.sql"
        new_model.write_text("SELECT 1 AS user_id, 'alice' AS user_name")

        diffs = diff_compiled_dirs(base, current)
        assert len(diffs) == 1
        assert diffs[0].model_name == "dim_users"
        assert diffs[0].status == "added"

    def test_diff_detects_removed_model(self, tmp_path):
        """Diff detects a model removed from current."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        shutil.copytree(COMPILED_DIR, base)
        shutil.copytree(COMPILED_DIR, current)

        # Remove a model from current
        (current / "marts" / "dim_books.sql").unlink()

        diffs = diff_compiled_dirs(base, current)
        assert len(diffs) == 1
        assert diffs[0].model_name == "dim_books"
        assert diffs[0].status == "removed"


class TestFullCheckPipeline:
    """Run the full _do_check pipeline against the real project with simulated changes."""

    def test_stg_orders_column_removal(self, modified_project, real_manifest, capsys):
        """Removing a column from stg_orders (view) should be SAFE for the view itself.

        stg_orders is materialized as 'view' → CREATE OR REPLACE VIEW (always SAFE).
        But it may cascade to downstream models.
        """

        base_dir, current_dir = modified_project

        # We need to set up the directory structure that _do_check expects:
        # base_dir/compiled/ and a target/ with compiled/{project}/models/ and manifest.json

        from dbt_plan.columns import extract_columns
        from dbt_plan.diff import diff_compiled_dirs
        from dbt_plan.manifest import build_node_index, find_downstream_batch
        from dbt_plan.predictor import (
            Safety,
            predict_ddl,
        )

        manifest = real_manifest
        node_index = build_node_index(manifest)
        child_map = manifest["child_map"]

        # Diff
        diffs = diff_compiled_dirs(base_dir, current_dir)
        assert len(diffs) == 1
        assert diffs[0].model_name == "stg_orders"

        diff = diffs[0]
        node = node_index["stg_orders"]

        # Extract columns
        base_cols = extract_columns(diff.base_sql, dialect="duckdb")
        if base_cols is None:
            base_cols = extract_columns(diff.base_sql, dialect="snowflake")
        current_cols = extract_columns(diff.current_sql, dialect="duckdb")
        if current_cols is None:
            current_cols = extract_columns(diff.current_sql, dialect="snowflake")

        assert base_cols is not None
        assert current_cols is not None
        assert "customer_id" in base_cols
        assert "customer_id" not in current_cols

        # Predict DDL for the view
        prediction = predict_ddl(
            model_name="stg_orders",
            materialization=node.materialization,
            on_schema_change=node.on_schema_change,
            base_columns=base_cols,
            current_columns=current_cols,
            status="modified",
        )
        # View → always SAFE (CREATE OR REPLACE VIEW)
        assert prediction.safety == Safety.SAFE
        assert any("CREATE OR REPLACE VIEW" in op.operation for op in prediction.operations)

        # Downstream impact: stg_orders → dim_books, fct_orders
        downstream = find_downstream_batch([node.node_id], child_map)
        assert len(downstream[node.node_id]) == 2

    def test_fct_orders_sync_all_columns_destructive(self, real_manifest, tmp_path):
        """Directly modifying fct_orders (incremental, sync_all_columns) with a column
        removal should produce DESTRUCTIVE prediction."""
        base = tmp_path / "base"
        current = tmp_path / "current"
        shutil.copytree(COMPILED_DIR, base)
        shutil.copytree(COMPILED_DIR, current)

        # Modify fct_orders: remove customer_uuid column
        fct = current / "marts" / "fct_orders.sql"
        fct.write_text(
            "\n\nSELECT\n"
            "    order_id,\n"
            "    store_id,\n"
            "    order_date,\n"
            "    'unknown' AS source\n"
            'FROM "memory"."main"."stg_orders"\n'
        )

        diffs = diff_compiled_dirs(base, current)
        assert len(diffs) == 1
        assert diffs[0].model_name == "fct_orders"

        diff = diffs[0]
        node_index = build_node_index(real_manifest)
        node = node_index["fct_orders"]

        base_cols = extract_columns(diff.base_sql, dialect="duckdb")
        if base_cols is None:
            base_cols = extract_columns(diff.base_sql, dialect="snowflake")
        current_cols = extract_columns(diff.current_sql, dialect="duckdb")
        if current_cols is None:
            current_cols = extract_columns(diff.current_sql, dialect="snowflake")

        assert base_cols is not None
        assert current_cols is not None

        prediction = predict_ddl(
            model_name="fct_orders",
            materialization=node.materialization,
            on_schema_change=node.on_schema_change,
            base_columns=base_cols,
            current_columns=current_cols,
            status="modified",
        )

        # incremental + sync_all_columns + column removed → DESTRUCTIVE
        assert prediction.safety == Safety.DESTRUCTIVE
        assert any("DROP COLUMN" in op.operation for op in prediction.operations)
        assert "customer_uuid" in prediction.columns_removed

    def test_full_pipeline_via_cli(self, real_manifest, tmp_path, monkeypatch, capsys):
        """Full CLI pipeline: modify fct_orders → exit 1 (DESTRUCTIVE)."""
        from dbt_plan.cli import main

        # Set up project directory structure for CLI
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # .dbt-plan/base/compiled/ — the snapshot baseline
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        shutil.copytree(COMPILED_DIR, base_compiled)

        # Also copy manifest to base
        shutil.copy2(MANIFEST_PATH, project_dir / ".dbt-plan" / "base" / "manifest.json")

        # target/compiled/test_project/models/ — the current state
        target_compiled = project_dir / "target" / "compiled" / "test_project" / "models"
        shutil.copytree(COMPILED_DIR, target_compiled)

        # Copy manifest to target
        shutil.copy2(MANIFEST_PATH, project_dir / "target" / "manifest.json")

        # Modify fct_orders in current: remove customer_uuid
        fct = target_compiled / "marts" / "fct_orders.sql"
        fct.write_text(
            "\n\nSELECT\n"
            "    order_id,\n"
            "    store_id,\n"
            "    order_date,\n"
            "    'unknown' AS source\n"
            'FROM "memory"."main"."stg_orders"\n'
        )

        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--project-dir",
                str(project_dir),
                "--format",
                "json",
                "--no-color",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["summary"]["total"] == 1
        assert data["summary"]["destructive"] == 1
        fct_result = data["models"][0]
        assert fct_result["model_name"] == "fct_orders"
        assert fct_result["safety"] == "destructive"
        assert "customer_uuid" in fct_result["columns_removed"]

    def test_full_pipeline_safe_change(self, real_manifest, tmp_path, monkeypatch, capsys):
        """Full CLI pipeline: modify dim_books (table) → exit 0 (SAFE)."""
        from dbt_plan.cli import main

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        shutil.copytree(COMPILED_DIR, base_compiled)
        shutil.copy2(MANIFEST_PATH, project_dir / ".dbt-plan" / "base" / "manifest.json")

        target_compiled = project_dir / "target" / "compiled" / "test_project" / "models"
        shutil.copytree(COMPILED_DIR, target_compiled)
        shutil.copy2(MANIFEST_PATH, project_dir / "target" / "manifest.json")

        # Modify dim_books: add a column (table materialization → always safe)
        dim = target_compiled / "marts" / "dim_books.sql"
        dim.write_text(
            "SELECT\n"
            "    store_id,\n"
            "    'App Name' AS title,\n"
            "    'active' AS status\n"
            'FROM "memory"."main"."stg_orders"\n'
            "GROUP BY 1\n"
        )

        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--project-dir",
                str(project_dir),
                "--format",
                "json",
                "--no-color",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["summary"]["total"] == 1
        assert data["summary"]["safe"] == 1
        dim_result = data["models"][0]
        assert dim_result["model_name"] == "dim_books"
        assert dim_result["safety"] == "safe"


class TestMetadataProjectNameDetection:
    """Verify that the real manifest uses metadata.project_name,
    not the most-common-package heuristic."""

    def test_metadata_project_name_is_used(self, real_manifest):
        """metadata.project_name='test_project' matches the node ID prefix."""
        metadata = real_manifest["metadata"]
        project_name = metadata.get("project_name")
        assert project_name == "test_project"

        # Verify all model node IDs use this project name
        for node_id in real_manifest["nodes"]:
            if node_id.startswith("model."):
                parts = node_id.split(".")
                assert parts[1] == project_name

    def test_heuristic_not_needed(self, real_manifest):
        """Since metadata.project_name exists, the Counter heuristic is NOT used.

        We verify this by checking that build_node_index returns the same
        result with and without metadata — if it relied on heuristic,
        removing metadata would still work (same result), proving the
        heuristic is redundant for this manifest.
        """
        # With metadata
        index_with = build_node_index(real_manifest, include_packages=False)

        # Without metadata — forces heuristic fallback
        manifest_no_meta = dict(real_manifest)
        manifest_no_meta["metadata"] = {}
        index_without = build_node_index(manifest_no_meta, include_packages=False)

        # Both should produce the same index (single-project manifest)
        assert set(index_with.keys()) == set(index_without.keys())

    def test_metadata_project_name_prevents_wrong_heuristic(self):
        """metadata.project_name overrides the heuristic even when
        a hypothetical package has more models.

        This is a synthetic test using the real manifest structure as template.
        """
        manifest = load_manifest(MANIFEST_PATH)
        # Inject a fake package with more models
        manifest["nodes"]["model.fake_pkg.m1"] = {
            "name": "m1",
            "config": {"materialized": "table"},
        }
        manifest["nodes"]["model.fake_pkg.m2"] = {
            "name": "m2",
            "config": {"materialized": "table"},
        }
        manifest["nodes"]["model.fake_pkg.m3"] = {
            "name": "m3",
            "config": {"materialized": "table"},
        }
        manifest["nodes"]["model.fake_pkg.m4"] = {
            "name": "m4",
            "config": {"materialized": "table"},
        }

        # metadata.project_name = "test_project" → should only keep test_project models
        index = build_node_index(manifest, include_packages=False)
        assert "stg_orders" in index
        assert "fct_orders" in index
        assert "dim_books" in index
        assert "m1" not in index
        assert "m2" not in index
