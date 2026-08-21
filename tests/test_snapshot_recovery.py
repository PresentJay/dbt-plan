"""Tests for snapshot corruption and recovery scenarios.

Verifies dbt-plan handles broken, incomplete, or unexpected snapshot states
gracefully — no crashes, clear errors, correct fallback behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_manifest(models: dict[str, dict] | None = None, project_name: str = "my_project"):
    """Build a minimal manifest.json dict."""
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


def _setup_target(project_dir: Path, models_sql: dict[str, str], manifest: dict):
    """Create target/compiled/{project}/models/*.sql and manifest.json."""
    models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, sql in models_sql.items():
        (models_dir / f"{name}.sql").write_text(sql)
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))


# ---------------------------------------------------------------------------
# 1. Corrupted manifest.json in snapshot
# ---------------------------------------------------------------------------


class TestCorruptedManifest:
    """Base manifest contains invalid JSON — _do_check should not crash."""

    def test_corrupted_base_manifest_falls_back_gracefully(self, tmp_path, capsys):
        """Corrupted base manifest.json ({broken) should be silently skipped.

        _do_check wraps base manifest loading in try/except (json.JSONDecodeError, OSError).
        The check should still succeed using only the current manifest.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})

        # Current target
        _setup_target(project_dir, {"dim_user": "SELECT id, name FROM users"}, manifest)

        # Base snapshot with valid compiled SQL but corrupted manifest
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text("{broken")

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # Should not crash — falls back to current manifest only
        assert exit_code == 0
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "dim_user"
        assert result["models"][0]["safety"] == "safe"

    def test_corrupted_current_manifest_returns_error(self, tmp_path, capsys):
        """Corrupted current manifest.json should return exit code 2 with error message."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Current target with corrupted manifest
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "dim_user.sql").write_text("SELECT id, name FROM users")
        (project_dir / "target" / "manifest.json").write_text("{broken json")

        # Valid base snapshot
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        base_manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()

        assert exit_code == 2
        assert "Could not parse manifest.json" in captured.err


# ---------------------------------------------------------------------------
# 2. Empty manifest.json in snapshot
# ---------------------------------------------------------------------------


class TestEmptyManifest:
    """Base manifest.json is an empty string — json.JSONDecodeError should be caught."""

    def test_empty_base_manifest_falls_back(self, tmp_path, capsys):
        """Empty base manifest.json should be silently skipped (json.JSONDecodeError)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        _setup_target(project_dir, {"dim_user": "SELECT id, name FROM users"}, manifest)

        # Base snapshot with valid SQL but empty manifest
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text("")

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # Should not crash
        assert exit_code == 0
        assert result["summary"]["total"] == 1

    def test_empty_current_manifest_returns_error(self, tmp_path, capsys):
        """Empty current manifest.json should return exit code 2 when diffs exist."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Use different SQL so a diff is detected, forcing manifest load
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "dim_user.sql").write_text("SELECT id, name FROM users")
        (project_dir / "target" / "manifest.json").write_text("")

        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        base_manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()

        assert exit_code == 2
        assert "Could not parse manifest.json" in captured.err

    def test_empty_current_manifest_no_diff_returns_safe(self, tmp_path, capsys):
        """Empty current manifest.json with no SQL changes returns 0 (no manifest needed)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Same SQL in both base and current -> no diff -> manifest never loaded
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "dim_user.sql").write_text("SELECT id FROM users")
        (project_dir / "target" / "manifest.json").write_text("")

        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(_minimal_manifest({"dim_user": {"materialized": "table"}}))
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # No changes = exit 0, manifest is never parsed
        assert exit_code == 0
        assert result["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# 3. Missing compiled SQL in snapshot but manifest exists
# ---------------------------------------------------------------------------


class TestMissingCompiledInSnapshot:
    """Snapshot has manifest.json but compiled/ is empty — all current models appear as 'added'."""

    def test_empty_base_compiled_shows_all_as_added(self, tmp_path, capsys):
        """Empty base compiled dir means every current model is 'added'."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest(
            {
                "model_a": {"materialized": "table"},
                "model_b": {"materialized": "incremental", "on_schema_change": "fail"},
            }
        )
        _setup_target(
            project_dir,
            {
                "model_a": "SELECT id, name FROM users",
                "model_b": "SELECT order_id, ts FROM events",
            },
            manifest,
        )

        # Base snapshot: manifest exists, compiled dir exists but is empty
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # All models should appear as added (not in base)
        assert result["summary"]["total"] == 2
        model_map = {m["model_name"]: m for m in result["models"]}
        assert model_map["model_a"]["safety"] == "safe"
        assert model_map["model_b"]["safety"] == "safe"
        assert exit_code == 0


# ---------------------------------------------------------------------------
# 4. Snapshot with extra non-SQL files
# ---------------------------------------------------------------------------


class TestExtraNonSQLFiles:
    """Snapshot contains .tmp, .bak files alongside .sql — only .sql should be compared."""

    def test_non_sql_files_ignored_in_diff(self, tmp_path, capsys):
        """Only .sql files in base/current should be compared."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"model_a": {"materialized": "table"}})
        _setup_target(project_dir, {"model_a": "SELECT id, name FROM users"}, manifest)

        # Base snapshot with .sql + extra non-SQL files
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "model_a.sql").write_text("SELECT id FROM users")
        (base_compiled / "model_a.sql.tmp").write_text("temp garbage")
        (base_compiled / "model_a.sql.bak").write_text("backup garbage")
        (base_compiled / "notes.txt").write_text("some notes")
        (base_compiled / ".DS_Store").write_bytes(b"\x00\x00")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # Only model_a should appear as modified (the .sql file changed)
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "model_a"
        assert exit_code == 0

    def test_extra_files_in_current_also_ignored(self, tmp_path, capsys):
        """Non-.sql files in current target should also be ignored."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"model_a": {"materialized": "table"}})

        # Set up target with extra files
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "model_a.sql").write_text("SELECT id FROM users")
        (models_dir / "model_a.sql.bak").write_text("backup")
        (models_dir / "random.tmp").write_text("temp")
        (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

        # Base with same SQL content
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "model_a.sql").write_text("SELECT id FROM users")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # No changes because only .sql files are compared and content is identical
        assert result["summary"]["total"] == 0
        assert exit_code == 0


# ---------------------------------------------------------------------------
# 5. Partially written snapshot (interrupted mid-write)
# ---------------------------------------------------------------------------


class TestPartialSnapshot:
    """Snapshot has only a subset of model files — missing ones appear as 'added'."""

    def test_partial_base_missing_models_appear_as_added(self, tmp_path, capsys):
        """If base only has model_a.sql, but current has model_a + model_b,
        model_b should appear as 'added'."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest(
            {
                "model_a": {"materialized": "table"},
                "model_b": {"materialized": "table"},
                "model_c": {"materialized": "view"},
            }
        )
        _setup_target(
            project_dir,
            {
                "model_a": "SELECT id FROM users",
                "model_b": "SELECT id, name FROM orders",
                "model_c": "SELECT order_id FROM events",
            },
            manifest,
        )

        # Base snapshot: only model_a was written (interrupted mid-write)
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "model_a.sql").write_text("SELECT id FROM users")
        # model_b and model_c are missing from base
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # model_a is unchanged (same SQL) -> not in results
        # model_b and model_c are "added" (not in base)
        model_names = {m["model_name"] for m in result["models"]}
        assert "model_a" not in model_names  # unchanged
        assert "model_b" in model_names  # added
        assert "model_c" in model_names  # added
        assert result["summary"]["total"] == 2
        assert exit_code == 0  # added models are safe


# ---------------------------------------------------------------------------
# 6. Re-snapshot after check (full lifecycle)
# ---------------------------------------------------------------------------


class TestReSnapshotAfterCheck:
    """Snapshot -> modify -> check (changes) -> re-snapshot -> check (no changes)."""

    def test_full_lifecycle(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})

        # Step 1: Initial target
        _setup_target(project_dir, {"dim_user": "SELECT id FROM users"}, manifest)

        # Step 2: Snapshot
        _do_snapshot(_snapshot_args(project_dir))
        capsys.readouterr()  # discard

        # Step 3: Modify model
        models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
        (models_dir / "dim_user.sql").write_text("SELECT id, name, email FROM users")

        # Step 4: Check -> should show changes
        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "dim_user"
        assert exit_code == 0

        # Step 5: Re-snapshot (overwrites old snapshot with current state)
        _do_snapshot(_snapshot_args(project_dir))
        capsys.readouterr()  # discard

        # Step 6: Check again -> should show NO changes
        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["summary"]["total"] == 0
        assert exit_code == 0


# ---------------------------------------------------------------------------
# 7. Snapshot dir is a file, not a directory
# ---------------------------------------------------------------------------


class TestSnapshotDirIsFile:
    """`.dbt-plan/base` is a regular file instead of a directory."""

    def test_snapshot_overwrites_file_with_directory(self, tmp_path, capsys):
        """_do_snapshot should handle the case where base_dir is a file.

        shutil.rmtree raises NotADirectoryError on a file. The snapshot
        command should either handle this gracefully or we document the bug.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        _setup_target(project_dir, {"dim_user": "SELECT id FROM users"}, manifest)

        # Create .dbt-plan/base as a regular FILE (not directory)
        dbt_plan_dir = project_dir / ".dbt-plan"
        dbt_plan_dir.mkdir(parents=True)
        base_file = dbt_plan_dir / "base"
        base_file.write_text("I am a file, not a directory")

        # _do_snapshot calls shutil.rmtree(base_dir) when base_dir.exists()
        # shutil.rmtree on a file raises NotADirectoryError
        # This is a potential bug — let's see if it crashes
        try:
            _do_snapshot(_snapshot_args(project_dir))
            # If it succeeds, verify the snapshot was created properly
            captured = capsys.readouterr()
            assert "Snapshot saved" in captured.out
            assert (project_dir / ".dbt-plan" / "base" / "compiled").is_dir()
            snapshot_succeeded = True
        except (NotADirectoryError, OSError):
            snapshot_succeeded = False

        if not snapshot_succeeded:
            # This is a bug: _do_snapshot should handle file-instead-of-dir
            pytest.fail(
                "BUG: _do_snapshot crashes when .dbt-plan/base is a file. "
                "shutil.rmtree cannot remove a regular file."
            )

    def test_check_when_base_is_file_returns_error(self, tmp_path, capsys):
        """_do_check should return error exit code when base_dir is a file."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        _setup_target(project_dir, {"dim_user": "SELECT id FROM users"}, manifest)

        # Create .dbt-plan/base as a regular file
        dbt_plan_dir = project_dir / ".dbt-plan"
        dbt_plan_dir.mkdir(parents=True)
        (dbt_plan_dir / "base").write_text("I am a file")

        # _do_check looks for base_dir / "compiled" which would fail on a file
        exit_code = _do_check(_check_args(project_dir))
        capsys.readouterr()

        # Should return error exit code, not crash with unhandled exception
        assert exit_code == 2


# ---------------------------------------------------------------------------
# 8. Very old snapshot with completely different models
# ---------------------------------------------------------------------------


class TestCompletelyDifferentModels:
    """Base has A, B, C; current has D, E, F. All old removed, all new added."""

    def test_all_removed_and_all_added(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Base snapshot: models A, B, C
        base_manifest = _minimal_manifest(
            {
                "model_a": {"materialized": "table"},
                "model_b": {"materialized": "view"},
                "model_c": {"materialized": "incremental", "on_schema_change": "ignore"},
            }
        )
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "model_a.sql").write_text("SELECT a1, a2 FROM src_a")
        (base_compiled / "model_b.sql").write_text("SELECT b1, b2 FROM src_b")
        (base_compiled / "model_c.sql").write_text("SELECT c1, c2 FROM src_c")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        # Current: completely different models D, E, F
        current_manifest = _minimal_manifest(
            {
                "model_d": {"materialized": "table"},
                "model_e": {"materialized": "view"},
                "model_f": {"materialized": "incremental", "on_schema_change": "sync_all_columns"},
            }
        )
        _setup_target(
            project_dir,
            {
                "model_d": "SELECT d1, d2 FROM src_d",
                "model_e": "SELECT e1, e2 FROM src_e",
                "model_f": "SELECT f1, f2 FROM src_f",
            },
            current_manifest,
        )

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        model_map = {m["model_name"]: m for m in result["models"]}

        # 6 total changes: 3 removed + 3 added
        assert result["summary"]["total"] == 6

        # Old models are removed -> destructive (except ephemeral)
        assert model_map["model_a"]["safety"] == "destructive"
        assert model_map["model_b"]["safety"] == "destructive"
        # model_c is incremental, removed -> destructive
        assert model_map["model_c"]["safety"] == "destructive"

        # New models are added -> safe
        assert model_map["model_d"]["safety"] == "safe"
        assert model_map["model_e"]["safety"] == "safe"
        assert model_map["model_f"]["safety"] == "safe"

        # Exit code 1 because of destructive removals
        assert exit_code == 1

    def test_removed_models_operations(self, tmp_path, capsys):
        """Removed models should have 'MODEL REMOVED' operation."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        base_manifest = _minimal_manifest(
            {
                "old_model": {"materialized": "table"},
            }
        )
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "old_model.sql").write_text("SELECT 1 AS id")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
            json.dumps(base_manifest)
        )

        current_manifest = _minimal_manifest(
            {
                "new_model": {"materialized": "table"},
            }
        )
        _setup_target(project_dir, {"new_model": "SELECT 2 AS id"}, current_manifest)

        _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        model_map = {m["model_name"]: m for m in result["models"]}

        # Removed model should have MODEL REMOVED operation
        old_ops = [op["operation"] for op in model_map["old_model"]["operations"]]
        assert "MODEL REMOVED" in old_ops

        # Added model (table) should have CREATE OR REPLACE TABLE
        new_ops = [op["operation"] for op in model_map["new_model"]["operations"]]
        assert "CREATE OR REPLACE TABLE" in new_ops


# ---------------------------------------------------------------------------
# 9. Base manifest missing entirely (not corrupted, just absent)
# ---------------------------------------------------------------------------


class TestBaseManifestMissing:
    """Base snapshot has compiled SQL but no manifest.json at all."""

    def test_check_works_without_base_manifest(self, tmp_path, capsys):
        """Check should still work using only the current manifest."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"dim_user": {"materialized": "table"}})
        _setup_target(project_dir, {"dim_user": "SELECT id, name FROM users"}, manifest)

        # Base: compiled SQL but NO manifest.json
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "dim_user.sql").write_text("SELECT id FROM users")
        # Deliberately no manifest.json in base

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        assert exit_code == 0
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "dim_user"


# ---------------------------------------------------------------------------
# 10. Snapshot with binary files mixed in
# ---------------------------------------------------------------------------


class TestBinaryFilesInSnapshot:
    """Snapshot contains binary files (.pyc, .DS_Store) — should be ignored."""

    def test_binary_files_do_not_interfere(self, tmp_path, capsys):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _minimal_manifest({"model_a": {"materialized": "table"}})
        _setup_target(project_dir, {"model_a": "SELECT id, name FROM users"}, manifest)

        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        (base_compiled / "model_a.sql").write_text("SELECT id FROM users")
        # Add binary junk files
        (base_compiled / "__pycache__").mkdir()
        (base_compiled / "__pycache__" / "something.pyc").write_bytes(b"\x00\x01\x02\x03\xff\xfe")
        (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(json.dumps(manifest))

        exit_code = _do_check(_check_args(project_dir))
        captured = capsys.readouterr()
        result = json.loads(captured.out)

        # Only the .sql file should be compared
        assert result["summary"]["total"] == 1
        assert result["models"][0]["model_name"] == "model_a"
        assert exit_code == 0
