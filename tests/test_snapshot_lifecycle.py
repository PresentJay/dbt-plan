"""Tests for the snapshot/check workflow lifecycle — edge cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dbt_plan.cli import _do_check, _do_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_manifest(models: dict[str, dict] | None = None, project_name: str = "my_project"):
    """Build a minimal manifest.json dict.

    Args:
        models: dict of model_name -> overrides (materialized, on_schema_change, etc.)
    """
    nodes = {}
    child_map = {}
    for name, overrides in (models or {}).items():
        node_id = f"model.{project_name}.{name}"
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": overrides.get("materialized", "table"),
                "on_schema_change": overrides.get("on_schema_change"),
                "enabled": overrides.get("enabled", True),
            },
            "columns": overrides.get("columns", {}),
        }
        child_map[node_id] = overrides.get("children", [])
    return {
        "nodes": nodes,
        "child_map": child_map,
        "metadata": {"project_name": project_name},
    }


def _snapshot_args(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
    )


def _check_args(
    project_dir: Path, *, fmt: str = "json", no_color: bool = True
) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=no_color,
        select=None,
        verbose=False,
        dialect=None,
    )


def _setup_target(
    project_dir: Path, models_sql: dict[str, str], manifest: dict, *, subdir: str = ""
):
    """Create target/compiled/{project}/models/[subdir]/*.sql and manifest.json."""
    models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
    if subdir:
        models_dir = models_dir / subdir
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, sql in models_sql.items():
        (models_dir / f"{name}.sql").write_text(sql)
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))


# ---------------------------------------------------------------------------
# 1. Legacy snapshot format
# ---------------------------------------------------------------------------


class TestLegacySnapshotFormat:
    """_do_check backward-compat: old snapshots stored SQL directly in base_dir (no compiled/ subdir)."""

    def test_new_format_works(self, tmp_path, capsys):
        """New format: .dbt-plan/base/compiled/*.sql + .dbt-plan/base/manifest.json."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})

        # Current target
        _setup_target(project_dir, {"dim_user": "SELECT id, name FROM users"}, manifest)

        # Base: new format (.dbt-plan/base/compiled/*.sql)
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert exit_code == 0
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "dim_user"
        assert result["models"][0]["safety"] == "safe"

    def test_old_format_works(self, tmp_path, capsys):
        """Old format: .dbt-plan/base/*.sql directly (no compiled/ subdir)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})

        # Current target
        _setup_target(project_dir, {"dim_user": "SELECT id, name FROM users"}, manifest)

        # Base: old format (SQL files directly in base_dir, no compiled/ subdir)
        base_dir = project_dir / ".dbt-plan" / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "dim_user.sql").write_text("SELECT id FROM users")
        (base_dir / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # Old format should still detect the change
        assert exit_code == 0
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "dim_user"


# ---------------------------------------------------------------------------
# 2. Snapshot overwrites previous snapshot
# ---------------------------------------------------------------------------


class TestSnapshotOverwrite:
    """Second snapshot should cleanly replace the first; stale files must not persist."""

    def test_stale_files_removed(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"model_a": {"materialized": "table"}})

        # First snapshot: model_a
        _setup_target(project_dir, {"model_a": "SELECT 1 AS a"}, manifest)
        _do_snapshot(_snapshot_args(project_dir))

        base_dir = project_dir / ".dbt-plan" / "base"
        assert (base_dir / "compiled" / "model_a.sql").exists()

        # Now change target to only have model_b
        # Remove old compiled files and recreate
        import shutil

        shutil.rmtree(project_dir / "target" / "compiled")
        manifest2 = _minimal_manifest({"model_b": {"materialized": "table"}})
        _setup_target(project_dir, {"model_b": "SELECT 2 AS b"}, manifest2)

        # Second snapshot should replace the first
        _do_snapshot(_snapshot_args(project_dir))

        # model_a should be gone, model_b should exist
        assert not (base_dir / "compiled" / "model_a.sql").exists()
        assert (base_dir / "compiled" / "model_b.sql").exists()

    def test_manifest_replaced(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest1 = _minimal_manifest({"old_model": {"materialized": "view"}})
        _setup_target(project_dir, {"old_model": "SELECT 1"}, manifest1)
        _do_snapshot(_snapshot_args(project_dir))

        base_dir = project_dir / ".dbt-plan" / "base"
        m1 = json.loads((base_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "model.my_project.old_model" in m1["nodes"]

        # Second snapshot with different manifest
        import shutil

        shutil.rmtree(project_dir / "target" / "compiled")
        manifest2 = _minimal_manifest({"new_model": {"materialized": "table"}})
        _setup_target(project_dir, {"new_model": "SELECT 2"}, manifest2)
        _do_snapshot(_snapshot_args(project_dir))

        m2 = json.loads((base_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "model.my_project.old_model" not in m2["nodes"]
        assert "model.my_project.new_model" in m2["nodes"]


# ---------------------------------------------------------------------------
# 3. Empty compiled directory
# ---------------------------------------------------------------------------


class TestEmptyCompiledDir:
    """target/compiled/project/models/ exists but has no .sql files."""

    def test_snapshot_succeeds_with_empty_compiled(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create models dir but put no SQL files in it
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (project_dir / "target" / "manifest.json").write_text(json.dumps(_minimal_manifest()))

        _do_snapshot(_snapshot_args(project_dir))

        captured = capsys.readouterr()
        assert "Snapshot saved" in captured.out

        # Verify the snapshot dir was created
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        assert base_compiled.exists()

    def test_check_no_changes_with_empty(self, tmp_path, capsys):
        """Both base and current are empty -> no changes detected."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest()

        # Current target: empty models dir
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

        # Base: empty compiled dir
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert exit_code == 0
        assert result["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# 4. Subdirectory models
# ---------------------------------------------------------------------------


class TestSubdirectoryModels:
    """Models nested in subdirs: target/compiled/project/models/staging/raw/model.sql."""

    def test_snapshot_finds_subdirectory_models(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"deep_model": {"materialized": "table"}})

        # Place model in a deep subdirectory
        deep_dir = (
            project_dir / "target" / "compiled" / "my_project" / "models" / "staging" / "raw"
        )
        deep_dir.mkdir(parents=True)
        (deep_dir / "deep_model.sql").write_text("SELECT id FROM source")
        (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

        _do_snapshot(_snapshot_args(project_dir))

        # Verify snapshot preserves subdirectory structure
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        found = list(base_compiled.rglob("*.sql"))
        assert len(found) == 1
        assert found[0].name == "deep_model.sql"

    def test_check_finds_subdirectory_models(self, tmp_path, capsys):
        """Check detects changes in models nested in subdirectories."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"deep_model": {"materialized": "table"}})

        # Current: model in subdirectory with updated SQL
        deep_dir = (
            project_dir / "target" / "compiled" / "my_project" / "models" / "staging" / "raw"
        )
        deep_dir.mkdir(parents=True)
        (deep_dir / "deep_model.sql").write_text("SELECT id, name FROM source")
        (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

        # Base: same subdirectory structure, different SQL
        base_deep = project_dir / ".dbt-plan" / "base" / "compiled" / "staging" / "raw"
        base_deep.mkdir(parents=True)
        (base_deep / "deep_model.sql").write_text("SELECT id FROM source")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert exit_code == 0
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "deep_model"


# ---------------------------------------------------------------------------
# 5. Manifest missing during snapshot
# ---------------------------------------------------------------------------


class TestManifestMissingDuringSnapshot:
    """Snapshot warns on stderr but still saves compiled SQL when manifest.json is absent."""

    def test_warning_on_stderr_and_sql_saved(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create compiled SQL but no manifest.json
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "my_model.sql").write_text("SELECT 1")
        # Deliberately do NOT create manifest.json

        _do_snapshot(_snapshot_args(project_dir))

        captured = capsys.readouterr()
        # Warning should appear on stderr
        assert "Warning" in captured.err
        assert "manifest.json" in captured.err

        # But the compiled SQL should still be saved
        assert "Snapshot saved" in captured.out
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        assert (base_compiled / "my_model.sql").exists()
        assert (base_compiled / "my_model.sql").read_text(encoding="utf-8") == "SELECT 1"

        # And manifest.json should NOT exist in base
        assert not (project_dir / ".dbt-plan" / "base" / "manifest.json").exists()


# ---------------------------------------------------------------------------
# 6. Check with stale snapshot
# ---------------------------------------------------------------------------


class TestStaleSnapshot:
    """Snapshot from old version: completely different models in current.

    All old models should appear as "removed", all new as "added".
    """

    def test_old_removed_new_added(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Base snapshot has old_model_a and old_model_b
        base_manifest = _minimal_manifest(
            {
                "old_model_a": {"materialized": "table"},
                "old_model_b": {"materialized": "view"},
            }
        )
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "old_model_a.sql").write_text("SELECT 1")
        (base_compiled / "old_model_b.sql").write_text("SELECT 2")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        # Current has completely different models
        current_manifest = _minimal_manifest(
            {
                "new_model_x": {"materialized": "table"},
                "new_model_y": {"materialized": "incremental", "on_schema_change": "fail"},
            }
        )
        _setup_target(
            project_dir,
            {
                "new_model_x": "SELECT 10",
                "new_model_y": "SELECT 20",
            },
            current_manifest,
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        model_names = {m["model_name"] for m in result["models"]}
        assert "old_model_a" in model_names
        assert "old_model_b" in model_names
        assert "new_model_x" in model_names
        assert "new_model_y" in model_names

        # Check statuses: old models are removed (destructive), new are added (safe)
        model_map = {m["model_name"]: m for m in result["models"]}

        # old_model_a (table, removed) -> destructive
        assert model_map["old_model_a"]["safety"] == "destructive"
        # old_model_b (view, removed) -> destructive
        assert model_map["old_model_b"]["safety"] == "destructive"
        # new_model_x (table, added) -> safe
        assert model_map["new_model_x"]["safety"] == "safe"
        # new_model_y (incremental, added) -> safe
        assert model_map["new_model_y"]["safety"] == "safe"

        # Exit code 1 because there are destructive changes
        assert exit_code == 1


# ---------------------------------------------------------------------------
# 7. Check with identical files (no changes)
# ---------------------------------------------------------------------------


class TestIdenticalFiles:
    """After snapshot, no changes made. Check should return exit 0, 'no changes'."""

    def test_no_changes_exit_zero(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest(
            {
                "dim_user": {"materialized": "table"},
                "fct_orders": {
                    "materialized": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )
        sql_files = {
            "dim_user": "SELECT id, name FROM users",
            "fct_orders": "SELECT order_id, user_id, ts FROM events",
        }

        # Set up target
        _setup_target(project_dir, sql_files, manifest)

        # Take snapshot
        _do_snapshot(_snapshot_args(project_dir))
        capsys.readouterr()  # discard snapshot output

        # Run check — files are identical to snapshot
        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert exit_code == 0
        assert result["summary"]["total"] == 0

    def test_no_changes_text_format(self, tmp_path, capsys):
        """Text format shows 'no model changes detected'."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        sql_files = {"dim_user": "SELECT id FROM users"}

        _setup_target(project_dir, sql_files, manifest)
        _do_snapshot(_snapshot_args(project_dir))
        capsys.readouterr()  # discard snapshot output

        exit_code = _do_check(_check_args(project_dir, fmt="text"))
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "no model changes detected" in captured.out


# ---------------------------------------------------------------------------
# 8. Model renamed
# ---------------------------------------------------------------------------


class TestModelRenamed:
    """dim_customers.sql -> dim_buyers.sql: should show one 'removed' and one 'added'."""

    def test_rename_shows_removed_and_added(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Base snapshot has dim_customers
        base_manifest = _minimal_manifest({"dim_customers": {"materialized": "table"}})
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_customers.sql").write_text("SELECT id, name FROM buyers")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        # Current has dim_buyers (renamed)
        current_manifest = _minimal_manifest({"dim_buyers": {"materialized": "table"}})
        _setup_target(
            project_dir,
            {
                "dim_buyers": "SELECT id, name FROM buyers",
            },
            current_manifest,
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        model_map = {m["model_name"]: m for m in result["models"]}

        # dim_customers is removed -> destructive (physical object orphaned)
        assert "dim_customers" in model_map
        assert model_map["dim_customers"]["safety"] == "destructive"

        # dim_buyers is added -> safe (new table/view created)
        assert "dim_buyers" in model_map
        assert model_map["dim_buyers"]["safety"] == "safe"

        # Exit code 1 due to destructive removal
        assert exit_code == 1

    def test_rename_count(self, tmp_path, capsys):
        """Exactly two model changes for a rename."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        base_manifest = _minimal_manifest({"dim_customers": {"materialized": "view"}})
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_customers.sql").write_text("SELECT 1")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        current_manifest = _minimal_manifest({"dim_buyers": {"materialized": "view"}})
        _setup_target(project_dir, {"dim_buyers": "SELECT 1"}, current_manifest)

        _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert result["summary"]["total"] == 2
