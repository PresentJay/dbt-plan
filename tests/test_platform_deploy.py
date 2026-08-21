"""Platform deployment tests: multi-project, package interactions, config sharing.

Simulates a platform engineer deploying dbt-plan across multiple dbt projects
with varying package dependencies and configurations.
"""

from __future__ import annotations

import pytest

from dbt_plan.cli import _find_compiled_dir
from dbt_plan.config import Config
from dbt_plan.diff import diff_compiled_dirs
from dbt_plan.manifest import build_node_index, find_downstream_batch
from dbt_plan.predictor import (
    Safety,
    analyze_cascade_impacts,
    predict_ddl,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    *,
    root_project: str,
    root_models: dict[str, dict],
    package_models: dict[str, dict] | None = None,
    child_map: dict[str, list[str]] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Build a minimal manifest dict for testing.

    root_models: {model_name: {"materialized": ..., "on_schema_change": ...}}
    package_models: {"pkg.model_name": {"materialized": ...}}  (pkg.name format)
    """
    nodes = {}
    for name, cfg in root_models.items():
        node_id = f"model.{root_project}.{name}"
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": cfg.get("materialized", "table"),
                "on_schema_change": cfg.get("on_schema_change"),
            },
            "columns": cfg.get("columns", {}),
        }

    if package_models:
        for key, cfg in package_models.items():
            pkg, name = key.split(".", 1)
            node_id = f"model.{pkg}.{name}"
            nodes[node_id] = {
                "name": name,
                "config": {
                    "materialized": cfg.get("materialized", "table"),
                    "on_schema_change": cfg.get("on_schema_change"),
                },
                "columns": cfg.get("columns", {}),
            }

    meta = metadata if metadata is not None else {"project_name": root_project}
    return {
        "nodes": nodes,
        "child_map": child_map or {},
        "metadata": meta,
    }


def _make_node(name, materialization="incremental", on_schema_change="ignore"):
    from dbt_plan.manifest import ModelNode

    return ModelNode(
        node_id=f"model.test.{name}",
        name=name,
        materialization=materialization,
        on_schema_change=on_schema_change,
    )


# ===========================================================================
# Scenario 1: include_packages interaction with cascade
# ===========================================================================


class TestIncludePackagesCascade:
    """Verify that include_packages controls whether package models participate
    in cascade impact analysis."""

    @pytest.fixture()
    def project_manifest(self):
        """Manifest with root project (myproject) + dbt_utils package.

        Dependency graph:
            stg_users -> int_users -> fct_users
            utils_surrogate_key -> int_users
        """
        return _make_manifest(
            root_project="myproject",
            root_models={
                "stg_users": {"materialized": "table"},
                "int_users": {
                    "materialized": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "fct_users": {
                    "materialized": "incremental",
                    "on_schema_change": "fail",
                },
            },
            package_models={
                "dbt_utils.utils_date_spine": {"materialized": "ephemeral"},
                "dbt_utils.utils_surrogate_key": {"materialized": "ephemeral"},
            },
            child_map={
                "model.myproject.stg_users": [
                    "model.myproject.int_users",
                ],
                "model.myproject.int_users": [
                    "model.myproject.fct_users",
                ],
                "model.dbt_utils.utils_surrogate_key": [
                    "model.myproject.int_users",
                ],
                "model.myproject.fct_users": [],
                "model.dbt_utils.utils_date_spine": [],
            },
        )

    # -- Test A: include_packages=False (default) --

    def test_a_exclude_packages_node_index(self, project_manifest):
        """With include_packages=False, only root project models are indexed."""
        index = build_node_index(project_manifest, include_packages=False)
        assert sorted(index.keys()) == ["fct_users", "int_users", "stg_users"]
        assert "utils_surrogate_key" not in index
        assert "utils_date_spine" not in index

    def test_a_exclude_packages_cascade_root_only(self, project_manifest, tmp_path):
        """Cascade from stg_users change checks root downstream only, not packages."""
        index = build_node_index(project_manifest, include_packages=False)

        # stg_users changes columns: user_id, email -> user_id, email, phone
        pred = predict_ddl(
            model_name="stg_users",
            materialization="table",
            on_schema_change=None,
            base_columns=["user_id", "email"],
            current_columns=["user_id", "email", "phone"],
        )

        # Write downstream SQL files
        int_sql = tmp_path / "int_users.sql"
        int_sql.write_text("SELECT user_id, email FROM {{ ref('stg_users') }}")
        fct_sql = tmp_path / "fct_users.sql"
        fct_sql.write_text("SELECT user_id FROM {{ ref('int_users') }}")

        downstream = find_downstream_batch(
            ["model.myproject.stg_users"],
            project_manifest["child_map"],
        )

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_users": "model.myproject.stg_users"},
            model_cols={"stg_users": (["user_id", "email"], ["user_id", "email", "phone"])},
            all_downstream=downstream,
            node_index=index,
            base_node_index={},
            compiled_sql_index={"int_users": int_sql, "fct_users": fct_sql},
        )

        # stg_users is a table -> safe (CREATE OR REPLACE), but has downstream
        assert "stg_users" in downstream_map
        ds_names = downstream_map["stg_users"]
        assert "int_users" in ds_names
        assert "fct_users" in ds_names
        # Package models must NOT appear in downstream_map
        assert "utils_surrogate_key" not in ds_names
        assert "utils_date_spine" not in ds_names

    def test_a_package_model_not_in_predictions(self, project_manifest, tmp_path):
        """Package model utils_surrogate_key should not appear in predictions
        even when its downstream int_users is affected."""
        index = build_node_index(project_manifest, include_packages=False)
        # utils_surrogate_key is not in the node_index
        assert "utils_surrogate_key" not in index

    # -- Test B: include_packages=True --

    def test_b_include_packages_node_index(self, project_manifest):
        """With include_packages=True, package models ARE included in node_index."""
        index = build_node_index(project_manifest, include_packages=True)
        assert "stg_users" in index
        assert "int_users" in index
        assert "fct_users" in index
        assert "utils_surrogate_key" in index
        assert "utils_date_spine" in index

    def test_b_include_packages_indexes_all(self, project_manifest, tmp_path):
        """include_packages=True indexes both root and package models."""
        index = build_node_index(project_manifest, include_packages=True)

        # Root models are indexed
        assert "stg_users" in index
        assert "int_users" in index
        assert "fct_users" in index
        # Package models are also indexed
        assert "utils_surrogate_key" in index
        assert "utils_date_spine" in index

    # -- Test C: Package model changed --

    def test_c_package_changed_excluded(self, project_manifest, tmp_path):
        """With include_packages=False, changes to package model are ignored."""
        index = build_node_index(project_manifest, include_packages=False)

        # utils_surrogate_key is not in the index, so it's skipped
        assert "utils_surrogate_key" not in index

        # Simulate diff: utils_surrogate_key changed
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        curr_dir = tmp_path / "current"
        curr_dir.mkdir()

        (base_dir / "utils_surrogate_key.sql").write_text("SELECT md5(a) AS sk FROM x")
        (curr_dir / "utils_surrogate_key.sql").write_text("SELECT sha256(a) AS sk FROM x")

        diffs = diff_compiled_dirs(base_dir, curr_dir)
        assert len(diffs) == 1
        assert diffs[0].model_name == "utils_surrogate_key"

        # But when we look up the node, it's not in the index
        node = index.get(diffs[0].model_name)
        assert node is None  # should be skipped in check flow

    def test_c_package_changed_included(self, project_manifest, tmp_path):
        """With include_packages=True, changes to package model are detected."""
        index = build_node_index(project_manifest, include_packages=True)

        assert "utils_surrogate_key" in index
        node = index["utils_surrogate_key"]
        assert node.materialization == "ephemeral"

        # Predict DDL for the changed package model
        pred = predict_ddl(
            model_name="utils_surrogate_key",
            materialization=node.materialization,
            on_schema_change=node.on_schema_change,
            base_columns=["sk"],
            current_columns=["sk"],
        )
        # ephemeral -> always safe
        assert pred.safety == Safety.SAFE


# ===========================================================================
# Scenario 2: Config shared across projects
# ===========================================================================


class TestConfigSharedAcrossProjects:
    """Verify the same .dbt-plan.yml works for different project structures."""

    CONFIG_CONTENT = (
        "ignore_models: [scratch_model]\ndialect: snowflake\nwarning_exit_code: 0\nformat: json\n"
    )

    def test_project_a_loads_config(self, tmp_path):
        """Project A with its own models loads shared config correctly."""
        project_a = tmp_path / "project_a"
        project_a.mkdir()
        (project_a / ".dbt-plan.yml").write_text(self.CONFIG_CONTENT)

        config = Config.load(project_a)
        assert config.ignore_models == ["scratch_model"]
        assert config.dialect == "snowflake"
        assert config.warning_exit_code == 0
        assert config.format == "json"

    def test_project_b_same_config(self, tmp_path):
        """Project B with different models but same config loads correctly."""
        project_b = tmp_path / "project_b"
        project_b.mkdir()
        (project_b / ".dbt-plan.yml").write_text(self.CONFIG_CONTENT)

        config = Config.load(project_b)
        assert config.ignore_models == ["scratch_model"]
        assert config.dialect == "snowflake"
        assert config.warning_exit_code == 0
        assert config.format == "json"

    def test_configs_are_independent_instances(self, tmp_path):
        """Config instances for different projects are independent (no shared state)."""
        project_a = tmp_path / "project_a"
        project_a.mkdir()
        (project_a / ".dbt-plan.yml").write_text("ignore_models: [model_a]\ndialect: bigquery\n")

        project_b = tmp_path / "project_b"
        project_b.mkdir()
        (project_b / ".dbt-plan.yml").write_text("ignore_models: [model_b]\ndialect: postgres\n")

        config_a = Config.load(project_a)
        config_b = Config.load(project_b)

        assert config_a.ignore_models == ["model_a"]
        assert config_a.dialect == "bigquery"
        assert config_b.ignore_models == ["model_b"]
        assert config_b.dialect == "postgres"

        # Mutating one doesn't affect the other
        config_a.ignore_models.append("extra")
        assert "extra" not in config_b.ignore_models

    def test_project_without_config_uses_defaults(self, tmp_path):
        """Project without .dbt-plan.yml uses defaults (no crash)."""
        project_c = tmp_path / "project_c"
        project_c.mkdir()

        config = Config.load(project_c)
        assert config.ignore_models == []
        assert config.dialect == "snowflake"
        assert config.warning_exit_code == 2
        assert config.format == "text"
        assert config.include_packages is False


# ===========================================================================
# Scenario 3: Multiple dbt projects in compiled dir error
# ===========================================================================


class TestMultipleProjectsInCompiledDir:
    """When target/compiled/ has multiple project subdirs, user gets a clear error."""

    def test_multiple_projects_raises_error(self, tmp_path):
        """Multiple project dirs in target/compiled/ raises ValueError."""
        target = tmp_path / "target"
        compiled = target / "compiled"

        # Create two project directories with models/
        (compiled / "project_alpha" / "models").mkdir(parents=True)
        (compiled / "project_beta" / "models").mkdir(parents=True)

        with pytest.raises(ValueError) as exc_info:
            _find_compiled_dir(target)

        error_msg = str(exc_info.value)
        # Error message should name both projects
        assert "project_alpha" in error_msg
        assert "project_beta" in error_msg
        # Error message should suggest --project-dir
        assert "--project-dir" in error_msg

    def test_multiple_projects_error_includes_all_names(self, tmp_path):
        """Error message lists ALL conflicting project names."""
        target = tmp_path / "target"
        compiled = target / "compiled"

        (compiled / "proj_x" / "models").mkdir(parents=True)
        (compiled / "proj_y" / "models").mkdir(parents=True)
        (compiled / "proj_z" / "models").mkdir(parents=True)

        with pytest.raises(ValueError) as exc_info:
            _find_compiled_dir(target)

        error_msg = str(exc_info.value)
        assert "proj_x" in error_msg
        assert "proj_y" in error_msg
        assert "proj_z" in error_msg
        assert "--project-dir" in error_msg

    def test_single_project_no_error(self, tmp_path):
        """Single project in compiled/ returns the models dir without error."""
        target = tmp_path / "target"
        compiled = target / "compiled"

        models_dir = compiled / "my_project" / "models"
        models_dir.mkdir(parents=True)

        result = _find_compiled_dir(target)
        assert result == models_dir

    def test_flat_layout_no_error(self, tmp_path):
        """Flat layout (target/compiled/models/) works without error."""
        target = tmp_path / "target"
        models_dir = target / "compiled" / "models"
        models_dir.mkdir(parents=True)

        result = _find_compiled_dir(target)
        assert result == models_dir

    def test_no_compiled_dir_returns_none(self, tmp_path):
        """Missing compiled/ directory returns None."""
        target = tmp_path / "target"
        target.mkdir()

        result = _find_compiled_dir(target)
        assert result is None

    def test_dir_without_models_subdir_ignored(self, tmp_path):
        """Project dirs without models/ subdirectory are not counted."""
        target = tmp_path / "target"
        compiled = target / "compiled"

        # Only one has models/
        (compiled / "real_project" / "models").mkdir(parents=True)
        (compiled / "not_a_project").mkdir(parents=True)  # no models/ inside

        result = _find_compiled_dir(target)
        assert result == compiled / "real_project" / "models"


# ===========================================================================
# Scenario 4: Metadata project_name vs heuristic
# ===========================================================================


class TestMetadataVsHeuristic:
    """Verify metadata.project_name is preferred over the most-common-package heuristic."""

    def test_metadata_project_name_used(self):
        """When metadata.project_name is set, it is used as root project."""
        manifest = _make_manifest(
            root_project="my_tiny_project",
            root_models={
                "my_model": {"materialized": "table"},
            },
            package_models={
                "big_package.a": {"materialized": "table"},
                "big_package.b": {"materialized": "table"},
                "big_package.c": {"materialized": "table"},
                "big_package.d": {"materialized": "table"},
            },
            metadata={"project_name": "my_tiny_project"},
        )

        index = build_node_index(manifest, include_packages=False)
        # Only root project model should be included
        assert "my_model" in index
        assert "a" not in index
        assert "b" not in index

    def test_no_metadata_uses_heuristic(self):
        """Without metadata.project_name, heuristic picks most common package."""
        manifest = {
            "nodes": {
                "model.app.m1": {"name": "m1", "config": {"materialized": "table"}},
                "model.app.m2": {"name": "m2", "config": {"materialized": "table"}},
                "model.app.m3": {"name": "m3", "config": {"materialized": "table"}},
                "model.ext.x1": {"name": "x1", "config": {"materialized": "table"}},
            },
            "child_map": {},
            "metadata": {},  # no project_name
        }

        index = build_node_index(manifest, include_packages=False)
        # Heuristic picks "app" (3 models) as root
        assert sorted(index.keys()) == ["m1", "m2", "m3"]
        assert "x1" not in index

    def test_heuristic_picks_wrong_package_metadata_saves(self):
        """When root project has fewer models than a package, heuristic would fail.
        metadata.project_name saves the day."""
        manifest = _make_manifest(
            root_project="startup",
            root_models={
                "orders": {"materialized": "incremental", "on_schema_change": "sync_all_columns"},
            },
            package_models={
                # This package has MORE models than root project
                "dbt_utils.date_spine": {"materialized": "ephemeral"},
                "dbt_utils.generate_series": {"materialized": "ephemeral"},
                "dbt_utils.surrogate_key": {"materialized": "ephemeral"},
                "dbt_utils.pivot": {"materialized": "ephemeral"},
                "dbt_utils.unpivot": {"materialized": "ephemeral"},
            },
            metadata={"project_name": "startup"},
        )

        index = build_node_index(manifest, include_packages=False)
        # metadata forces "startup" as root, even though dbt_utils has 5 models
        assert "orders" in index
        assert len(index) == 1
        assert "date_spine" not in index

    def test_heuristic_wrong_without_metadata(self):
        """Without metadata, heuristic picks the WRONG root (most models = package)."""
        manifest = {
            "nodes": {
                "model.startup.orders": {
                    "name": "orders",
                    "config": {"materialized": "incremental"},
                },
                "model.dbt_utils.date_spine": {
                    "name": "date_spine",
                    "config": {"materialized": "ephemeral"},
                },
                "model.dbt_utils.generate_series": {
                    "name": "generate_series",
                    "config": {"materialized": "ephemeral"},
                },
                "model.dbt_utils.surrogate_key": {
                    "name": "surrogate_key",
                    "config": {"materialized": "ephemeral"},
                },
            },
            "child_map": {},
            "metadata": {},  # no project_name!
        }

        index = build_node_index(manifest, include_packages=False)
        # Heuristic picks "dbt_utils" (3 models) as root -- WRONG!
        # This is the expected (known) behavior without metadata
        assert "date_spine" in index
        assert "orders" not in index  # root project model gets excluded

    def test_no_metadata_falls_back_to_heuristic(self):
        """Without metadata.project_name, falls back to most-common-package heuristic."""
        manifest = {
            "nodes": {
                "model.myproj.m1": {"name": "m1", "config": {"materialized": "table"}},
                "model.myproj.m2": {"name": "m2", "config": {"materialized": "table"}},
                "model.pkg.p1": {"name": "p1", "config": {"materialized": "table"}},
            },
            "child_map": {},
            "metadata": {},  # no project_name — uses heuristic (myproj has 2, pkg has 1)
        }

        index = build_node_index(manifest, include_packages=False)
        assert "m1" in index
        assert "m2" in index
        assert "p1" not in index

    def test_include_packages_ignores_metadata(self):
        """With include_packages=True, metadata doesn't matter -- all models included."""
        manifest = _make_manifest(
            root_project="small",
            root_models={"x": {"materialized": "table"}},
            package_models={
                "big.a": {"materialized": "table"},
                "big.b": {"materialized": "table"},
            },
            metadata={"project_name": "small"},
        )

        index = build_node_index(manifest, include_packages=True)
        assert sorted(index.keys()) == ["a", "b", "x"]


# ===========================================================================
# Integration: Full cascade flow with package boundaries
# ===========================================================================


class TestCascadeWithPackageBoundaries:
    """End-to-end: column change in root model cascades through the DAG,
    respecting package boundaries."""

    def test_cascade_stops_at_package_boundary(self, tmp_path):
        """When include_packages=False, cascade does not analyze package models
        even if they are in the child_map."""
        manifest = _make_manifest(
            root_project="shop",
            root_models={
                "stg_orders": {"materialized": "view"},
                "int_orders": {
                    "materialized": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "fct_revenue": {
                    "materialized": "incremental",
                    "on_schema_change": "fail",
                },
            },
            package_models={
                "analytics.agg_daily": {"materialized": "table"},
            },
            child_map={
                "model.shop.stg_orders": ["model.shop.int_orders"],
                "model.shop.int_orders": [
                    "model.shop.fct_revenue",
                    "model.analytics.agg_daily",  # cross-package downstream
                ],
                "model.shop.fct_revenue": [],
                "model.analytics.agg_daily": [],
            },
        )

        index = build_node_index(manifest, include_packages=False)
        assert "agg_daily" not in index

        # int_orders drops a column
        pred = predict_ddl(
            model_name="int_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "amount", "currency"],
            current_columns=["order_id", "amount"],
        )
        assert pred.safety == Safety.DESTRUCTIVE

        # Write downstream SQL
        fct_sql = tmp_path / "fct_revenue.sql"
        fct_sql.write_text("SELECT order_id, amount, currency FROM {{ ref('int_orders') }}")

        downstream = find_downstream_batch(
            ["model.shop.int_orders"],
            manifest["child_map"],
        )

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_orders": "model.shop.int_orders"},
            model_cols={
                "int_orders": (["order_id", "amount", "currency"], ["order_id", "amount"])
            },
            all_downstream=downstream,
            node_index=index,
            base_node_index={},
            compiled_sql_index={"fct_revenue": fct_sql},
        )

        # fct_revenue has broken_ref (references dropped "currency")
        assert len(updated[0].downstream_impacts) >= 1
        impact_names = [imp.model_name for imp in updated[0].downstream_impacts]
        assert "fct_revenue" in impact_names
        # agg_daily is NOT in impacts (not in node_index, so skipped)
        assert "agg_daily" not in impact_names

        # downstream_map includes all BFS-reachable nodes (including package ones)
        # but cascade analysis only impacts root models
        ds = downstream_map.get("int_orders", [])
        assert "fct_revenue" in ds

    def test_cascade_includes_package_model_when_enabled(self, tmp_path):
        """When include_packages=True, package model IS analyzed in cascade."""
        manifest = _make_manifest(
            root_project="shop",
            root_models={
                "int_orders": {
                    "materialized": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            },
            package_models={
                "analytics.agg_daily": {
                    "materialized": "incremental",
                    "on_schema_change": "fail",
                },
            },
            child_map={
                "model.shop.int_orders": ["model.analytics.agg_daily"],
                "model.analytics.agg_daily": [],
            },
        )

        index = build_node_index(manifest, include_packages=True)
        assert "agg_daily" in index

        pred = predict_ddl(
            model_name="int_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "amount", "currency"],
            current_columns=["order_id", "amount"],
        )

        agg_sql = tmp_path / "agg_daily.sql"
        agg_sql.write_text("SELECT order_id, currency FROM {{ ref('int_orders') }}")

        downstream = find_downstream_batch(
            ["model.shop.int_orders"],
            manifest["child_map"],
        )

        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"int_orders": "model.shop.int_orders"},
            model_cols={
                "int_orders": (["order_id", "amount", "currency"], ["order_id", "amount"])
            },
            all_downstream=downstream,
            node_index=index,
            base_node_index={},
            compiled_sql_index={"agg_daily": agg_sql},
        )

        impact_names = [imp.model_name for imp in updated[0].downstream_impacts]
        # agg_daily should now be detected (build_failure + broken_ref)
        assert "agg_daily" in impact_names
        risks = [
            imp.risk for imp in updated[0].downstream_impacts if imp.model_name == "agg_daily"
        ]
        assert "build_failure" in risks or "broken_ref" in risks
