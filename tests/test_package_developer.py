"""Tests from a dbt package developer's perspective.

Key question: When a user upgrades your package, does dbt-plan correctly
handle the changes?

Covers:
  - Ephemeral package model upgrade with column additions
  - Unchanged package dependency not leaking into output
  - Duplicate model names across root and package
  - Package with more models than root (heuristic vs metadata)
  - Disabled package models
"""

from __future__ import annotations

from dbt_plan.diff import diff_compiled_dirs
from dbt_plan.manifest import ModelNode, build_node_index
from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    nodes: dict, metadata: dict | None = None, child_map: dict | None = None
) -> dict:
    """Build a minimal manifest dict."""
    return {
        "nodes": nodes,
        "metadata": metadata or {},
        "child_map": child_map or {},
    }


def _model_node(
    pkg: str,
    name: str,
    materialization: str = "table",
    on_schema_change: str | None = None,
    enabled: bool = True,
    columns: dict | None = None,
) -> tuple[str, dict]:
    """Return (node_id, node_dict) pair for a manifest model."""
    node_id = f"model.{pkg}.{name}"
    node = {
        "name": name,
        "config": {
            "materialized": materialization,
            "on_schema_change": on_schema_change,
            "enabled": enabled,
        },
    }
    if columns is not None:
        node["columns"] = columns
    return node_id, node


# ===========================================================================
# Scenario 1: Package upgrade adds a column to an ephemeral model
# ===========================================================================


class TestPackageEphemeralUpgrade:
    """Package model `utils_pivot` is ephemeral.
    New version adds a column.
    User's root model references the old columns.
    """

    def _manifest(self, *, include_packages: bool) -> dict:
        """Build manifest with root + package ephemeral model."""
        nid_pivot, node_pivot = _model_node("my_pkg", "utils_pivot", "ephemeral")
        nid_root, node_root = _model_node(
            "myproject", "fct_sales", "incremental", "sync_all_columns"
        )
        return _make_manifest(
            nodes={nid_pivot: node_pivot, nid_root: node_root},
            metadata={"project_name": "myproject"},
        )

    def test_include_packages_detects_ephemeral_change(self, tmp_path):
        """With include_packages=True, package ephemeral model change is detected in diff."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "utils_pivot.sql").write_text("SELECT a, b FROM src")

        current = tmp_path / "current"
        current.mkdir()
        (current / "utils_pivot.sql").write_text("SELECT a, b, new_col FROM src")

        diffs = diff_compiled_dirs(base, current)
        names = [d.model_name for d in diffs]
        assert "utils_pivot" in names
        assert diffs[0].status == "modified"

        # With include_packages=True, the model appears in the index
        manifest = self._manifest(include_packages=True)
        index = build_node_index(manifest, include_packages=True)
        assert "utils_pivot" in index
        assert index["utils_pivot"].materialization == "ephemeral"

    def test_exclude_packages_hides_ephemeral_change(self):
        """With include_packages=False, package ephemeral model is NOT in node_index."""
        manifest = self._manifest(include_packages=False)
        index = build_node_index(manifest, include_packages=False)
        assert "utils_pivot" not in index
        # Root model is still present
        assert "fct_sales" in index

    def test_ephemeral_prediction_always_safe(self):
        """Ephemeral materialization is always SAFE (no physical object)."""
        result = predict_ddl(
            model_name="utils_pivot",
            materialization="ephemeral",
            on_schema_change=None,
            base_columns=["a", "b"],
            current_columns=["a", "b", "new_col"],
        )
        assert result.safety == Safety.SAFE
        assert result.operations == []


# ===========================================================================
# Scenario 2: Package model is a dependency but not changed
# ===========================================================================


class TestUnchangedPackageDependency:
    """Root model `fct_orders` depends on package model `pkg_date_spine`.
    Only root model changes (adds a column).
    Package model should not appear in diff output.
    """

    def test_unchanged_package_model_not_in_diff(self, tmp_path):
        """Package model with identical SQL across base/current is excluded from diff."""
        base = tmp_path / "base"
        base.mkdir()
        # Package model: identical in both
        (base / "pkg_date_spine.sql").write_text("SELECT date_day FROM raw.dates")
        # Root model: changed
        (base / "fct_orders.sql").write_text("SELECT order_id, user_id FROM events")

        current = tmp_path / "current"
        current.mkdir()
        # Package model: unchanged
        (current / "pkg_date_spine.sql").write_text("SELECT date_day FROM raw.dates")
        # Root model: column added
        (current / "fct_orders.sql").write_text("SELECT order_id, user_id, channel FROM events")

        diffs = diff_compiled_dirs(base, current)
        names = [d.model_name for d in diffs]

        # Only fct_orders shows up, not pkg_date_spine
        assert "fct_orders" in names
        assert "pkg_date_spine" not in names
        assert len(diffs) == 1

    def test_unchanged_package_not_in_predictions(self, tmp_path):
        """Even though pkg_date_spine is in child_map, it doesn't appear
        in predictions when its SQL hasn't changed."""
        manifest = _make_manifest(
            nodes={
                "model.myproject.fct_orders": {
                    "name": "fct_orders",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                },
                "model.dbt_date.pkg_date_spine": {
                    "name": "pkg_date_spine",
                    "config": {"materialized": "ephemeral"},
                },
            },
            metadata={"project_name": "myproject"},
            child_map={
                "model.dbt_date.pkg_date_spine": ["model.myproject.fct_orders"],
                "model.myproject.fct_orders": [],
            },
        )

        # Only fct_orders changed — prediction should only be for fct_orders
        pred = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "user_id"],
            current_columns=["order_id", "user_id", "channel"],
        )
        assert pred.model_name == "fct_orders"
        assert pred.columns_added == ["channel"]
        # Verify package model is excluded from node_index when include_packages=False
        index = build_node_index(manifest, include_packages=False)
        assert "pkg_date_spine" not in index
        assert "fct_orders" in index


# ===========================================================================
# Scenario 3: Duplicate model names across packages
# ===========================================================================


class TestDuplicateModelNamesAcrossPackages:
    """Root project has `dim_date`, package also has `dim_date`.
    The code uses `if name and name not in index` — first one wins.
    """

    def _manifest_root_first(self):
        """Manifest where root's dim_date appears before package's."""
        return _make_manifest(
            nodes={
                "model.myproject.dim_date": {
                    "name": "dim_date",
                    "config": {"materialized": "table"},
                },
                "model.dbt_date.dim_date": {
                    "name": "dim_date",
                    "config": {"materialized": "ephemeral"},
                },
            },
            metadata={"project_name": "myproject"},
        )

    def _manifest_package_first(self):
        """Manifest where package's dim_date appears before root's."""
        return _make_manifest(
            nodes={
                "model.dbt_date.dim_date": {
                    "name": "dim_date",
                    "config": {"materialized": "ephemeral"},
                },
                "model.myproject.dim_date": {
                    "name": "dim_date",
                    "config": {"materialized": "table"},
                },
            },
            metadata={"project_name": "myproject"},
        )

    def test_exclude_packages_only_root_dim_date(self):
        """With include_packages=False, only root's dim_date is indexed."""
        index = build_node_index(self._manifest_root_first(), include_packages=False)
        assert "dim_date" in index
        assert index["dim_date"].node_id == "model.myproject.dim_date"
        assert index["dim_date"].materialization == "table"

    def test_exclude_packages_ignores_package_dim_date(self):
        """With include_packages=False, package's dim_date is filtered out."""
        # Even if package node comes first in iteration order
        index = build_node_index(self._manifest_package_first(), include_packages=False)
        assert "dim_date" in index
        assert index["dim_date"].node_id == "model.myproject.dim_date"

    def test_include_packages_first_one_wins(self):
        """With include_packages=True, first-encountered dim_date wins (dict order)."""
        # Root first → root wins
        index_rf = build_node_index(self._manifest_root_first(), include_packages=True)
        assert index_rf["dim_date"].node_id == "model.myproject.dim_date"

        # Package first → package wins
        index_pf = build_node_index(self._manifest_package_first(), include_packages=True)
        assert index_pf["dim_date"].node_id == "model.dbt_date.dim_date"

    def test_include_packages_determinism_concern(self):
        """With include_packages=True and duplicate names, winner depends on dict insertion order.

        This is a known behavior: the code has `if name and name not in index`
        which silently drops the second model. There is no warning or error.
        """
        # Build index twice with same manifest — should be deterministic within same dict
        manifest = self._manifest_root_first()
        index1 = build_node_index(manifest, include_packages=True)
        index2 = build_node_index(manifest, include_packages=True)
        assert index1["dim_date"].node_id == index2["dim_date"].node_id

    def test_only_one_dim_date_in_index(self):
        """Regardless of settings, only ONE dim_date entry exists in the index."""
        manifest = self._manifest_root_first()

        index_inc = build_node_index(manifest, include_packages=True)
        assert len([k for k in index_inc if k == "dim_date"]) == 1

        index_exc = build_node_index(manifest, include_packages=False)
        assert len([k for k in index_exc if k == "dim_date"]) == 1


# ===========================================================================
# Scenario 4: Package with more models than root
# ===========================================================================


class TestPackageMoreModelsThanRoot:
    """Root has 1 model, package has 5 models.
    Without metadata: heuristic picks package as root (BUG).
    With metadata.project_name: correctly picks root.
    """

    def _nodes(self):
        """1 root model + 5 package models."""
        return {
            # Root project: 1 model
            "model.tiny_app.fct_orders": {
                "name": "fct_orders",
                "config": {"materialized": "incremental", "on_schema_change": "fail"},
            },
            # Package: 5 models — heuristic would pick this as root
            "model.big_package.stg_users": {
                "name": "stg_users",
                "config": {"materialized": "view"},
            },
            "model.big_package.stg_orders": {
                "name": "stg_orders",
                "config": {"materialized": "view"},
            },
            "model.big_package.stg_products": {
                "name": "stg_products",
                "config": {"materialized": "view"},
            },
            "model.big_package.int_order_items": {
                "name": "int_order_items",
                "config": {"materialized": "ephemeral"},
            },
            "model.big_package.dim_products": {
                "name": "dim_products",
                "config": {"materialized": "table"},
            },
        }

    def test_without_metadata_heuristic_picks_package_as_root(self):
        """BUG SCENARIO: Without metadata.project_name, the heuristic picks
        the package with the most models as 'root'. This means the actual
        root model is excluded!

        This test documents the known bug that metadata.project_name fixes.
        """
        manifest = _make_manifest(nodes=self._nodes())
        # No metadata — heuristic kicks in
        index = build_node_index(manifest, include_packages=False)

        # Heuristic picks big_package (5 models) as root
        # Actual root model (tiny_app.fct_orders) is EXCLUDED
        assert "fct_orders" not in index
        # Package models are treated as "root"
        assert "stg_users" in index
        assert len(index) == 5

    def test_with_metadata_correctly_picks_root(self):
        """FIX: With metadata.project_name, root is correctly identified."""
        manifest = _make_manifest(
            nodes=self._nodes(),
            metadata={"project_name": "tiny_app"},
        )
        index = build_node_index(manifest, include_packages=False)

        # Root model correctly included
        assert "fct_orders" in index
        assert len(index) == 1

        # Package models correctly excluded
        assert "stg_users" not in index
        assert "dim_products" not in index

    def test_with_metadata_include_packages_gets_all(self):
        """include_packages=True gets all models regardless of metadata."""
        manifest = _make_manifest(
            nodes=self._nodes(),
            metadata={"project_name": "tiny_app"},
        )
        index = build_node_index(manifest, include_packages=True)
        assert len(index) == 6
        assert "fct_orders" in index
        assert "stg_users" in index

    def test_no_metadata_uses_heuristic(self):
        """Without metadata.project_name, falls back to most-common-package heuristic."""
        manifest = _make_manifest(
            nodes=self._nodes(),
            metadata={},  # no project_name — heuristic picks the package with most models
        )
        index = build_node_index(manifest, include_packages=False)
        # large_package has 5 models vs tiny_app's 1 → heuristic picks large_package
        # This is the known limitation when metadata.project_name is absent
        assert len(index) == 5  # large_package models win


# ===========================================================================
# Scenario 5: Disabled package model
# ===========================================================================


class TestDisabledPackageModel:
    """Package model with `enabled: false` in config.
    Verify it's excluded from node_index regardless of include_packages setting.
    """

    def _manifest(self):
        return _make_manifest(
            nodes={
                "model.myproject.fct_orders": {
                    "name": "fct_orders",
                    "config": {"materialized": "table"},
                },
                "model.my_pkg.disabled_staging": {
                    "name": "disabled_staging",
                    "config": {"materialized": "view", "enabled": False},
                },
                "model.my_pkg.active_util": {
                    "name": "active_util",
                    "config": {"materialized": "ephemeral"},
                },
            },
            metadata={"project_name": "myproject"},
        )

    def test_disabled_excluded_with_include_packages_true(self):
        """Disabled package model excluded even when include_packages=True."""
        index = build_node_index(self._manifest(), include_packages=True)
        assert "disabled_staging" not in index
        assert "active_util" in index
        assert "fct_orders" in index

    def test_disabled_excluded_with_include_packages_false(self):
        """Disabled package model excluded with include_packages=False."""
        index = build_node_index(self._manifest(), include_packages=False)
        assert "disabled_staging" not in index
        # Package models also excluded (not root)
        assert "active_util" not in index
        # Root model present
        assert "fct_orders" in index

    def test_disabled_root_model_also_excluded(self):
        """Disabled model in root project is also excluded."""
        manifest = _make_manifest(
            nodes={
                "model.myproject.active": {
                    "name": "active",
                    "config": {"materialized": "table"},
                },
                "model.myproject.disabled_root": {
                    "name": "disabled_root",
                    "config": {"materialized": "table", "enabled": False},
                },
            },
            metadata={"project_name": "myproject"},
        )
        index = build_node_index(manifest, include_packages=False)
        assert "active" in index
        assert "disabled_root" not in index


# ===========================================================================
# Integration: Cascade through package model
# ===========================================================================


class TestCascadeThroughPackageModel:
    """When a root model drops a column, cascade analysis should detect
    broken refs in downstream models — but only if those downstream models
    are in the node_index (which depends on include_packages).
    """

    def test_cascade_reaches_package_downstream_when_included(self, tmp_path):
        """With include_packages=True, cascade detects broken refs in package models."""
        ds_sql = tmp_path / "pkg_report.sql"
        ds_sql.write_text("SELECT user_id, dropped_col FROM {{ ref('fct_orders') }}")

        pred = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "dropped_col"],
            current_columns=["user_id"],
        )

        node_index = {
            "pkg_report": ModelNode(
                node_id="model.my_pkg.pkg_report",
                name="pkg_report",
                materialization="table",
                on_schema_change=None,
            ),
        }

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"fct_orders": "model.myproject.fct_orders"},
            model_cols={"fct_orders": (["user_id", "dropped_col"], ["user_id"])},
            all_downstream={"model.myproject.fct_orders": ["model.my_pkg.pkg_report"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"pkg_report": ds_sql},
        )

        assert len(updated[0].downstream_impacts) == 1
        assert updated[0].downstream_impacts[0].risk == "broken_ref"
        assert updated[0].downstream_impacts[0].model_name == "pkg_report"

    def test_cascade_misses_package_downstream_when_excluded(self, tmp_path):
        """With include_packages=False, package model not in node_index,
        so cascade cannot look up its materialization — it's silently skipped.
        """
        ds_sql = tmp_path / "pkg_report.sql"
        ds_sql.write_text("SELECT user_id, dropped_col FROM {{ ref('fct_orders') }}")

        pred = predict_ddl(
            model_name="fct_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["user_id", "dropped_col"],
            current_columns=["user_id"],
        )

        # Empty node_index — package model not included
        node_index: dict[str, ModelNode] = {}

        updated, downstream_map = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"fct_orders": "model.myproject.fct_orders"},
            model_cols={"fct_orders": (["user_id", "dropped_col"], ["user_id"])},
            all_downstream={"model.myproject.fct_orders": ["model.my_pkg.pkg_report"]},
            node_index=node_index,
            base_node_index={},
            compiled_sql_index={"pkg_report": ds_sql},
        )

        # pkg_report appears in downstream_map but has no impact
        # because the model couldn't be resolved in node_index
        assert "fct_orders" in downstream_map
        assert "pkg_report" in downstream_map["fct_orders"]
        # No impacts detected — the downstream model was silently skipped
        assert updated[0].downstream_impacts == []
