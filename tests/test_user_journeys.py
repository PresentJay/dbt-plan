"""User journey tests — end-to-end simulation of real-world dbt-plan workflows.

Each test builds a full dbt project structure (compiled SQL + manifest + snapshot),
calls _do_check directly with --format json, and parses the output to verify the
full pipeline: diff -> manifest -> columns -> predictor -> formatter.
"""

from __future__ import annotations

import argparse
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_manifest(nodes: dict, child_map: dict | None = None, project: str = "test") -> dict:
    """Build a minimal manifest.json dict.

    Args:
        nodes: mapping of model_name -> dict with keys:
            materialization (required), on_schema_change (optional), columns (optional).
        child_map: optional explicit child_map; auto-generated if None.
        project: dbt project name used in node IDs.
    """
    manifest_nodes = {}
    auto_child_map = {}
    for name, cfg in nodes.items():
        node_id = f"model.{project}.{name}"
        node = {
            "name": name,
            "config": {
                "materialized": cfg.get("materialization", "table"),
            },
            "columns": cfg.get("columns", {}),
        }
        osc = cfg.get("on_schema_change")
        if osc is not None:
            node["config"]["on_schema_change"] = osc
        manifest_nodes[node_id] = node
        auto_child_map[node_id] = cfg.get("children", [])

    return {
        "nodes": manifest_nodes,
        "child_map": child_map if child_map is not None else auto_child_map,
        "metadata": {"project_name": project},
    }


def _setup_project(
    tmp_path: Path,
    *,
    base_sqls: dict[str, str],
    current_sqls: dict[str, str],
    manifest: dict,
    base_manifest: dict | None = None,
    config_yml: str | None = None,
) -> Path:
    """Create a full project directory for _do_check.

    Layout:
        project/
          target/compiled/{project}/models/*.sql
          target/manifest.json
          .dbt-plan/base/compiled/*.sql
          .dbt-plan/base/manifest.json  (if base_manifest provided)
          .dbt-plan.yml  (if config_yml provided)
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Current compiled SQL: target/compiled/{project}/models/
    project_name = manifest.get("metadata", {}).get("project_name", "test")
    compiled_dir = project_dir / "target" / "compiled" / project_name / "models"
    compiled_dir.mkdir(parents=True)
    for name, sql in current_sqls.items():
        (compiled_dir / f"{name}.sql").write_text(sql)

    # manifest.json
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

    # Base snapshot: .dbt-plan/base/compiled/*.sql
    base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
    base_compiled.mkdir(parents=True)
    for name, sql in base_sqls.items():
        (base_compiled / f"{name}.sql").write_text(sql)

    # Base manifest
    base_manifest_data = base_manifest if base_manifest is not None else manifest
    (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
        json.dumps(base_manifest_data)
    )

    # Config file
    if config_yml is not None:
        (project_dir / ".dbt-plan.yml").write_text(config_yml)

    return project_dir


def _make_args(
    project_dir: Path, *, fmt: str = "json", select: str | None = None
) -> argparse.Namespace:
    """Build argparse.Namespace for _do_check."""
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        select=select,
        verbose=False,
        dialect=None,
    )


def _run_check(
    project_dir: Path, *, fmt: str = "json", select: str | None = None
) -> tuple[int, dict | str]:
    """Run _do_check and capture stdout.

    Returns:
        (exit_code, parsed_json) when fmt="json"
        (exit_code, raw_text) otherwise
    """
    from dbt_plan.cli import _do_check

    args = _make_args(project_dir, fmt=fmt, select=select)
    buf = StringIO()
    with patch("sys.stdout", buf):
        exit_code = _do_check(args)
    output = buf.getvalue()
    if fmt == "json":
        return exit_code, json.loads(output)
    return exit_code, output


# ---------------------------------------------------------------------------
# Journey 1: Add a column to an incremental model
# ---------------------------------------------------------------------------


class TestJourney1AddColumn:
    """Simulate: developer adds a new column to an incremental model with sync_all_columns."""

    def test_add_column_safe(self, tmp_path):
        base_sql = "SELECT order_id, store_id, order_date FROM events"
        current_sql = "SELECT order_id, store_id, order_date, new_metric FROM events"

        manifest = _build_manifest(
            {
                "stg_orders": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"stg_orders": base_sql},
            current_sqls={"stg_orders": current_sql},
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # Find the model in output
        assert len(result["models"]) == 1
        model = result["models"][0]

        # Assert: model shows ADD COLUMN, safety=SAFE
        assert model["model_name"] == "stg_orders"
        assert model["safety"] == "safe"
        assert "new_metric" in model["columns_added"]
        assert len(model["columns_removed"]) == 0

        # Assert: operations include ADD COLUMN
        ops = [op["operation"] for op in model["operations"]]
        assert any("ADD COLUMN" in op for op in ops)

        # Assert: exit code is 0
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Journey 2: Drop a column from sync_all_columns — destructive
# ---------------------------------------------------------------------------


class TestJourney2DropColumnDestructive:
    """Simulate: developer removes a column from sync_all_columns incremental model."""

    def test_drop_column_destructive_with_cascade(self, tmp_path):
        base_sql = "SELECT col_a, col_b, col_c FROM source_table"
        current_sql = "SELECT col_a, col_c FROM source_table"

        # Downstream model references col_b
        downstream_sql = "SELECT col_a, col_b FROM {{ ref('stg_orders') }}"

        manifest = _build_manifest(
            nodes={
                "stg_orders": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                    "children": ["model.test.dim_report"],
                },
                "dim_report": {
                    "materialization": "table",
                },
            },
            child_map={
                "model.test.stg_orders": ["model.test.dim_report"],
                "model.test.dim_report": [],
            },
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"stg_orders": base_sql, "dim_report": downstream_sql},
            current_sqls={"stg_orders": current_sql, "dim_report": downstream_sql},
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # Find stg_orders in output
        stg = next(m for m in result["models"] if m["model_name"] == "stg_orders")

        # Assert: safety=DESTRUCTIVE, col_b dropped
        assert stg["safety"] == "destructive"
        assert "col_b" in stg["columns_removed"]

        # Assert: operations include DROP COLUMN
        ops = [op["operation"] for op in stg["operations"]]
        assert any("DROP COLUMN" in op for op in ops)
        drop_col_ops = [op for op in stg["operations"] if op["operation"] == "DROP COLUMN"]
        assert any(op["column"] == "col_b" for op in drop_col_ops)

        # Assert: exit code is 1 (destructive)
        assert exit_code == 1

        # Assert: cascade analysis flags downstream model referencing col_b
        assert "downstream_impacts" in stg
        impacts = stg["downstream_impacts"]
        assert len(impacts) >= 1
        broken = [i for i in impacts if i["risk"] == "broken_ref"]
        assert len(broken) >= 1
        assert any("col_b" in i["reason"] for i in broken)


# ---------------------------------------------------------------------------
# Journey 3: Rename a model file
# ---------------------------------------------------------------------------


class TestJourney3RenameModel:
    """Simulate: developer renames dim_users.sql to dim_user.sql (singular rename)."""

    def test_rename_shows_removed_and_added(self, tmp_path):
        model_sql = "SELECT user_id, name, email FROM users"

        # Base manifest has dim_users, current has dim_user
        base_manifest = _build_manifest(
            {
                "dim_users": {"materialization": "table"},
            }
        )

        current_manifest = _build_manifest(
            {
                "dim_user": {"materialization": "table"},
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"dim_users": model_sql},
            current_sqls={"dim_user": model_sql},
            manifest=current_manifest,
            base_manifest=base_manifest,
        )

        exit_code, result = _run_check(project_dir)

        model_names = [m["model_name"] for m in result["models"]]

        # Assert: dim_users shows as removed (destructive)
        assert "dim_users" in model_names
        removed = next(m for m in result["models"] if m["model_name"] == "dim_users")
        assert removed["safety"] == "destructive"
        removed_ops = [op["operation"] for op in removed["operations"]]
        assert any("MODEL REMOVED" in op for op in removed_ops)

        # Assert: dim_user shows as added (safe)
        assert "dim_user" in model_names
        added = next(m for m in result["models"] if m["model_name"] == "dim_user")
        assert added["safety"] == "safe"

        # Assert: exit code=1 (destructive wins)
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Journey 4: Add a brand new model
# ---------------------------------------------------------------------------


class TestJourney4AddNewModel:
    """Simulate: developer adds a new incremental model with on_schema_change=fail."""

    def test_new_model_safe_unchanged_hidden(self, tmp_path):
        existing_sql = "SELECT id, name FROM products"
        existing_sql_2 = "SELECT order_id, product_id FROM orders"
        new_model_sql = "SELECT user_id, order_status, ts FROM raw_orders"

        manifest = _build_manifest(
            {
                "dim_products": {"materialization": "table"},
                "fct_orders": {"materialization": "table"},
                "stg_raw_events": {
                    "materialization": "incremental",
                    "on_schema_change": "fail",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={
                "dim_products": existing_sql,
                "fct_orders": existing_sql_2,
            },
            current_sqls={
                "dim_products": existing_sql,
                "fct_orders": existing_sql_2,
                "stg_raw_events": new_model_sql,
            },
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # Assert: new model shows as added, safety=SAFE
        model_names = [m["model_name"] for m in result["models"]]
        assert "stg_raw_events" in model_names

        new_model = next(m for m in result["models"] if m["model_name"] == "stg_raw_events")
        assert new_model["safety"] == "safe"

        # Assert: existing unchanged models don't appear in output
        assert "dim_products" not in model_names
        assert "fct_orders" not in model_names

        # Assert: only 1 model in output (the new one)
        assert len(result["models"]) == 1

        # Assert: exit code=0
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Journey 5: Change materialization from view to incremental
# ---------------------------------------------------------------------------


class TestJourney5ChangeMaterialization:
    """Simulate: developer changes a model from view to incremental with sync_all_columns."""

    def test_materialization_change_warning(self, tmp_path):
        model_sql_v1 = "SELECT user_id, name, email FROM users"
        # Same columns, minor SQL tweak to trigger "modified" diff
        model_sql_v2 = "SELECT user_id, name, email FROM users WHERE active = true"

        base_manifest = _build_manifest(
            {
                "dim_users": {"materialization": "view"},
            }
        )

        current_manifest = _build_manifest(
            {
                "dim_users": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"dim_users": model_sql_v1},
            current_sqls={"dim_users": model_sql_v2},
            manifest=current_manifest,
            base_manifest=base_manifest,
        )

        exit_code, result = _run_check(project_dir)

        assert len(result["models"]) == 1
        model = result["models"][0]

        # Assert: config change detection shows materialization changed
        ops_text = [op["operation"] for op in model["operations"]]
        assert any("MATERIALIZATION CHANGED" in op for op in ops_text)
        assert any("view" in op and "incremental" in op for op in ops_text)

        # Assert: safety at least WARNING
        assert model["safety"] in ("warning", "destructive")

        # Assert: exit code reflects at least warning (2 by default)
        assert exit_code >= 1  # either 1 (destructive) or 2 (warning)


# ---------------------------------------------------------------------------
# Journey 6: Use --select to focus on one model
# ---------------------------------------------------------------------------


class TestJourney6Select:
    """Simulate: developer uses --select to check only one model."""

    def test_select_filters_output(self, tmp_path):
        safe_sql_base = "SELECT id, name FROM products"
        safe_sql_current = "SELECT id, name, category FROM products"

        destructive_sql_base = "SELECT col_a, col_b FROM events"
        destructive_sql_current = "SELECT col_a FROM events"

        manifest = _build_manifest(
            {
                "dim_products": {"materialization": "table"},
                "stg_orders": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={
                "dim_products": safe_sql_base,
                "stg_orders": destructive_sql_base,
            },
            current_sqls={
                "dim_products": safe_sql_current,
                "stg_orders": destructive_sql_current,
            },
            manifest=manifest,
        )

        # Select only the safe model
        exit_code, result = _run_check(project_dir, select="dim_products")

        # Assert: exit code=0 (only selected model checked, which is safe)
        assert exit_code == 0

        # Assert: output mentions only the selected model
        model_names = [m["model_name"] for m in result["models"]]
        assert "dim_products" in model_names
        assert "stg_orders" not in model_names

        assert len(result["models"]) == 1


# ---------------------------------------------------------------------------
# Journey 7: ignore_models filters out known-noisy model
# ---------------------------------------------------------------------------


class TestJourney7IgnoreModels:
    """Simulate: ignore_models config filters out a known-noisy model."""

    def test_ignored_model_excluded(self, tmp_path):
        noisy_sql_base = "SELECT id, val FROM scratch_temp"
        noisy_sql_current = "SELECT id, val, extra FROM scratch_temp"

        real_sql_base = "SELECT user_id, name FROM users"
        real_sql_current = "SELECT user_id, name, email FROM users"

        manifest = _build_manifest(
            {
                "scratch_temp": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "dim_users": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={
                "scratch_temp": noisy_sql_base,
                "dim_users": real_sql_base,
            },
            current_sqls={
                "scratch_temp": noisy_sql_current,
                "dim_users": real_sql_current,
            },
            manifest=manifest,
            config_yml="ignore_models: [scratch_temp]\n",
        )

        exit_code, result = _run_check(project_dir)

        # Assert: ignored model doesn't appear in output
        model_names = [m["model_name"] for m in result["models"]]
        assert "scratch_temp" not in model_names

        # Assert: only the non-ignored model is checked
        assert "dim_users" in model_names
        assert len(result["models"]) == 1

        # Assert: the non-ignored model shows expected changes
        dim_users = result["models"][0]
        assert dim_users["model_name"] == "dim_users"
        assert "email" in dim_users["columns_added"]
