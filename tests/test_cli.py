"""Tests for CLI entry points — snapshot and check subcommands."""

import json
from unittest.mock import patch

import pytest

from dbt_plan.cli import _do_check, _do_snapshot, main


def _make_project(tmp_path, *, models_sql=None, manifest=None, base_sql=None, base_manifest=None):
    """Helper to set up a fake dbt project structure for CLI tests.

    Returns:
        project_dir Path
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # target/compiled/{proj}/models/
    target = project_dir / "target"
    compiled = target / "compiled" / "my_project" / "models"
    compiled.mkdir(parents=True)
    if models_sql:
        for name, sql in models_sql.items():
            (compiled / f"{name}.sql").write_text(sql)

    # manifest.json
    if manifest is not None:
        (target / "manifest.json").write_text(json.dumps(manifest))

    # base snapshot
    if base_sql is not None:
        base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
        base_compiled.mkdir(parents=True)
        for name, sql in base_sql.items():
            (base_compiled / f"{name}.sql").write_text(sql)

    if base_manifest is not None:
        base_dir = project_dir / ".dbt-plan" / "base"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "manifest.json").write_text(json.dumps(base_manifest))

    return project_dir


def _make_check_args(project_dir, fmt="text", no_color=True, manifest=None, select=None):
    """Build an argparse.Namespace for _do_check."""
    import argparse

    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=manifest,
        format=fmt,
        no_color=no_color,
        select=select,
        verbose=False,
        dialect=None,
    )


def _make_snapshot_args(project_dir):
    import argparse

    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
    )


class TestSnapshotPathValidation:
    def test_snapshot_rejects_base_dir_escaping_project(self, tmp_path):
        """Snapshot aborts if base_dir resolves outside project directory."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create compiled SQL so we get past the first check
        compiled = project_dir / "target" / "compiled" / "proj" / "models"
        compiled.mkdir(parents=True)
        (compiled / "m.sql").write_text("SELECT 1")

        # Create a symlink that escapes the project dir
        escape_target = tmp_path / "outside"
        escape_target.mkdir()
        base_dir = project_dir / ".dbt-plan" / "base"
        base_dir.parent.mkdir(parents=True, exist_ok=True)
        base_dir.symlink_to(escape_target)

        args = _make_snapshot_args(project_dir)
        with pytest.raises(SystemExit) as exc_info:
            _do_snapshot(args)
        assert exc_info.value.code == 2

    def test_snapshot_missing_manifest_warns(self, tmp_path, capsys):
        """Snapshot prints warning when manifest.json is missing from target/."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        compiled = project_dir / "target" / "compiled" / "proj" / "models"
        compiled.mkdir(parents=True)
        (compiled / "m.sql").write_text("SELECT 1")
        # No manifest.json created

        args = _make_snapshot_args(project_dir)
        _do_snapshot(args)

        captured = capsys.readouterr()
        assert "Warning: manifest.json not found" in captured.err
        assert "Snapshot saved to" in captured.out

    def test_snapshot_happy_path(self, tmp_path, capsys):
        """Snapshot copies compiled SQL + manifest to .dbt-plan/base/."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        compiled = project_dir / "target" / "compiled" / "proj" / "models"
        compiled.mkdir(parents=True)
        (compiled / "m.sql").write_text("SELECT 1")
        (project_dir / "target" / "manifest.json").write_text('{"nodes":{}}')

        args = _make_snapshot_args(project_dir)
        _do_snapshot(args)

        captured = capsys.readouterr()
        assert "Snapshot saved to" in captured.out
        base = project_dir / ".dbt-plan" / "base"
        assert (base / "compiled" / "m.sql").exists()
        assert (base / "manifest.json").exists()

    def test_snapshot_overwrites_existing(self, tmp_path, capsys):
        """Running snapshot twice overwrites the first snapshot."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        compiled = project_dir / "target" / "compiled" / "proj" / "models"
        compiled.mkdir(parents=True)
        (compiled / "m.sql").write_text("SELECT 1")
        (project_dir / "target" / "manifest.json").write_text('{"nodes":{}}')

        args = _make_snapshot_args(project_dir)
        _do_snapshot(args)
        # Modify and re-snapshot
        (compiled / "m.sql").write_text("SELECT 2")
        _do_snapshot(args)

        base = project_dir / ".dbt-plan" / "base"
        assert (base / "compiled" / "m.sql").read_text() == "SELECT 2"

    def test_snapshot_no_compiled_sql_exits_2(self, tmp_path):
        """Snapshot exits 2 when no compiled SQL exists."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "target").mkdir()

        args = _make_snapshot_args(project_dir)
        with pytest.raises(SystemExit) as exc_info:
            _do_snapshot(args)
        assert exc_info.value.code == 2


class TestFindCompiledDir:
    def test_flat_layout(self, tmp_path):
        """Flat layout: target/compiled/models/ without project subdir."""
        from dbt_plan.cli import _find_compiled_dir

        compiled = tmp_path / "compiled"
        models = compiled / "models"
        models.mkdir(parents=True)
        (models / "m.sql").write_text("SELECT 1")

        result = _find_compiled_dir(tmp_path)
        assert result == models

    def test_standard_layout(self, tmp_path):
        """Standard layout: target/compiled/{project}/models/."""
        from dbt_plan.cli import _find_compiled_dir

        compiled = tmp_path / "compiled"
        models = compiled / "my_project" / "models"
        models.mkdir(parents=True)
        (models / "m.sql").write_text("SELECT 1")

        result = _find_compiled_dir(tmp_path)
        assert result == models

    def test_no_compiled_dir(self, tmp_path):
        """No compiled dir → None."""
        from dbt_plan.cli import _find_compiled_dir

        result = _find_compiled_dir(tmp_path)
        assert result is None

    def test_multiple_projects_raises(self, tmp_path):
        """Multiple project dirs → ValueError."""
        from dbt_plan.cli import _find_compiled_dir

        compiled = tmp_path / "compiled"
        (compiled / "proj_a" / "models").mkdir(parents=True)
        (compiled / "proj_b" / "models").mkdir(parents=True)

        with pytest.raises(ValueError, match="Multiple dbt projects"):
            _find_compiled_dir(tmp_path)


class TestCheckLegacyAndEdgePaths:
    def test_legacy_snapshot_format(self, tmp_path, capsys):
        """check works with old snapshot format (SQL directly in base_dir, no compiled/ subdir)."""
        manifest = {
            "nodes": {
                "model.p.m": {"name": "m", "config": {"materialized": "table"}},
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b FROM t"},
            manifest=manifest,
        )
        # Create legacy format: SQL directly in .dbt-plan/base/ (no compiled/ subdir)
        base_dir = project_dir / ".dbt-plan" / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "m.sql").write_text("SELECT a FROM t")
        (base_dir / "manifest.json").write_text(json.dumps(manifest))

        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "m" in output

    def test_missing_current_compiled_returns_2(self, tmp_path, capsys):
        """check returns 2 when current compiled SQL directory doesn't exist."""
        manifest = {
            "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            manifest=manifest,
            base_sql={"m": "SELECT 1"},
            base_manifest=manifest,
        )
        # Remove compiled dir but keep target/
        import shutil

        compiled = project_dir / "target" / "compiled"
        if compiled.exists():
            shutil.rmtree(compiled)

        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2
        assert "compile" in capsys.readouterr().err.lower()


class TestCheckErrorPaths:
    def test_missing_base_dir_returns_2(self, tmp_path, capsys):
        """check returns 2 when base dir does not exist."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest={"nodes": {}, "child_map": {}},
        )
        # No base snapshot
        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2
        assert "snapshot" in capsys.readouterr().err.lower()

    def test_missing_manifest_returns_2(self, tmp_path, capsys):
        """check returns 2 when manifest.json is missing."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            base_sql={"m": "SELECT 1"},
        )
        # manifest not created
        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2
        assert "manifest" in capsys.readouterr().err.lower()

    def test_corrupt_manifest_returns_2(self, tmp_path, capsys):
        """check returns 2 when manifest.json is invalid JSON and there are changes."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b FROM t"},
            base_sql={"m": "SELECT a FROM t"},
        )
        (project_dir / "target" / "manifest.json").write_text("{invalid json")
        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2
        assert "manifest" in capsys.readouterr().err.lower()


class TestCheckValueErrorCatch:
    def test_check_catches_duplicate_model_valueerror(self, tmp_path, capsys):
        """check returns 2 when diff_compiled_dirs raises ValueError (duplicate models)."""
        manifest = {
            "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest=manifest,
            base_sql={"m": "SELECT 1"},
            base_manifest=manifest,
        )

        # Create duplicate in current compiled dir
        compiled = project_dir / "target" / "compiled" / "my_project" / "models"
        sub = compiled / "subdir"
        sub.mkdir()
        (sub / "m.sql").write_text("SELECT 2")

        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "Duplicate model name" in captured.err


class TestCheckJsonFormatRouting:
    def test_empty_diff_json_format(self, tmp_path, capsys):
        """When no diffs exist, --format=json outputs valid JSON."""
        manifest = {
            "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest=manifest,
            base_sql={"m": "SELECT 1"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 0

    def test_empty_diff_github_format(self, tmp_path, capsys):
        """When no diffs exist, --format=github outputs markdown header."""
        manifest = {
            "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest=manifest,
            base_sql={"m": "SELECT 1"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="github")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no model changes detected" in captured.out

    def test_empty_diff_text_no_color(self, tmp_path, capsys):
        """When no diffs exist, --format=text --no-color outputs plain text."""
        manifest = {
            "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest=manifest,
            base_sql={"m": "SELECT 1"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="text", no_color=True)
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no model changes detected" in captured.out
        assert "\033[" not in captured.out


class TestCheckSkippedModels:
    def test_model_not_in_manifest_is_skipped(self, tmp_path, capsys):
        """Model in diff but not in any manifest → appears in skipped_models."""
        manifest = {
            "nodes": {},
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"orphan": "SELECT 1"},
            manifest=manifest,
            base_sql={},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        _do_check(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "orphan" in data["skipped_models"]


class TestCheckJsonFormatWithPredictions:
    def test_check_json_with_changes(self, tmp_path, capsys):
        """check --format=json with actual model changes produces valid JSON."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b, c FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT a, b FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        assert data["models"][0]["model_name"] == "m"
        assert data["models"][0]["safety"] == "safe"


class TestExitCodes:
    def test_warning_prediction_returns_warning_exit_code(self, tmp_path, capsys):
        """WARNING prediction (e.g., BUILD FAILURE) returns warning_exit_code, not 0."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "fail",
                    },
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b, c FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT a, b FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 2  # default warning_exit_code

    def test_safe_prediction_returns_zero(self, tmp_path, capsys):
        """SAFE prediction returns exit code 0."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT a FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        assert exit_code == 0


class TestConfigChangeDetection:
    def test_materialization_change_detected(self, tmp_path, capsys):
        """Model changing from table to incremental shows MATERIALIZATION CHANGED."""
        base_manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        current_manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "incremental", "on_schema_change": "ignore"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b FROM t"},
            manifest=current_manifest,
            base_sql={"m": "SELECT a FROM t"},
            base_manifest=base_manifest,
        )
        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        captured = capsys.readouterr()
        assert "MATERIALIZATION CHANGED" in captured.out
        assert "table -> incremental" in captured.out
        assert exit_code == 2  # WARNING

    def test_on_schema_change_policy_change_detected(self, tmp_path, capsys):
        """on_schema_change changing from ignore to sync_all_columns shows warning."""
        base_manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "incremental", "on_schema_change": "ignore"},
                },
            },
            "child_map": {},
        }
        current_manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b, c FROM t"},
            manifest=current_manifest,
            base_sql={"m": "SELECT a, b FROM t"},
            base_manifest=base_manifest,
        )
        args = _make_check_args(project_dir)
        exit_code = _do_check(args)
        captured = capsys.readouterr()
        assert "on_schema_change CHANGED" in captured.out
        assert "ignore -> sync_all_columns" in captured.out
        assert exit_code == 2  # WARNING (osc change + SAFE from sync = WARNING wins)

    def test_no_change_no_extra_ops(self, tmp_path, capsys):
        """Same materialization and osc → no config change ops."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT a, b FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT a FROM t"},
            base_manifest=manifest,
        )
        args = _make_check_args(project_dir)
        _do_check(args)
        captured = capsys.readouterr()
        assert "MATERIALIZATION CHANGED" not in captured.out
        assert "on_schema_change CHANGED" not in captured.out


class TestSelectFilter:
    def test_select_single_model(self, tmp_path, capsys):
        """--select filters results to only the specified model."""
        manifest = {
            "nodes": {
                "model.p.m1": {
                    "name": "m1",
                    "config": {"materialized": "table"},
                },
                "model.p.m2": {
                    "name": "m2",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a, b FROM t", "m2": "SELECT x, y FROM t"},
            manifest=manifest,
            base_sql={"m1": "SELECT a FROM t", "m2": "SELECT x FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json", select="m1")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        assert data["models"][0]["model_name"] == "m1"

    def test_select_multiple_models(self, tmp_path, capsys):
        """--select with comma-separated models includes all specified."""
        manifest = {
            "nodes": {
                "model.p.m1": {
                    "name": "m1",
                    "config": {"materialized": "table"},
                },
                "model.p.m2": {
                    "name": "m2",
                    "config": {"materialized": "table"},
                },
                "model.p.m3": {
                    "name": "m3",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={
                "m1": "SELECT a, b FROM t",
                "m2": "SELECT x, y FROM t",
                "m3": "SELECT p, q FROM t",
            },
            manifest=manifest,
            base_sql={
                "m1": "SELECT a FROM t",
                "m2": "SELECT x FROM t",
                "m3": "SELECT p FROM t",
            },
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json", select="m1,m3")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        model_names = sorted(m["model_name"] for m in data["models"])
        assert model_names == ["m1", "m3"]

    def test_select_no_match_returns_empty(self, tmp_path, capsys):
        """--select with non-existent model name results in empty output."""
        manifest = {
            "nodes": {
                "model.p.m1": {
                    "name": "m1",
                    "config": {"materialized": "table"},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a, b FROM t"},
            manifest=manifest,
            base_sql={"m1": "SELECT a FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json", select="nonexistent")
        exit_code = _do_check(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 0


class TestIgnoreModels:
    def test_ignore_models_excludes_from_check(self, tmp_path, capsys):
        """Models in ignore_models config are excluded from check."""
        manifest = {
            "nodes": {
                "model.p.m1": {"name": "m1", "config": {"materialized": "table"}},
                "model.p.m2": {"name": "m2", "config": {"materialized": "table"}},
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a, b FROM t", "m2": "SELECT x, y FROM t"},
            manifest=manifest,
            base_sql={"m1": "SELECT a FROM t", "m2": "SELECT x FROM t"},
            base_manifest=manifest,
        )
        # Create config with ignore_models
        (project_dir / ".dbt-plan.yml").write_text("ignore_models: [m2]\n")

        args = _make_check_args(project_dir, fmt="json")
        _do_check(args)
        data = json.loads(capsys.readouterr().out)
        model_names = [m["model_name"] for m in data["models"]]
        assert "m1" in model_names
        assert "m2" not in model_names


class TestParseFailures:
    def test_parse_failure_incremental_produces_warning(self, tmp_path, capsys):
        """Unparseable SQL on incremental model → parse failure warning."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "THIS IS NOT VALID SQL AT ALL"},
            manifest=manifest,
            base_sql={"m": "SELECT a, b FROM t"},
            base_manifest=manifest,
        )
        args = _make_check_args(project_dir, fmt="json")
        exit_code = _do_check(args)
        data = json.loads(capsys.readouterr().out)
        assert "m" in data["parse_failures"]
        assert exit_code == 2  # warning


class TestManifestColumnFallback:
    def test_current_cols_fallback_from_manifest(self, tmp_path, capsys):
        """When extract_columns returns ['*'], use node.columns from manifest."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "columns": {"id": {}, "name": {}, "email": {}},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            # Current SQL uses SELECT * which triggers fallback
            models_sql={"m": "SELECT * FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT id, name FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        _do_check(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        pred = data["models"][0]
        assert pred["model_name"] == "m"
        # With manifest fallback, columns are resolved so we get a real prediction
        # (ADD COLUMN email) instead of "REVIEW REQUIRED (SELECT *)"
        op_texts = [op["operation"] for op in pred["operations"]]
        assert all("REVIEW REQUIRED" not in op for op in op_texts)
        assert "email" in pred["columns_added"]

    def test_base_cols_fallback_from_base_manifest(self, tmp_path, capsys):
        """When base extract_columns returns ['*'], use base_node.columns from base manifest."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "columns": {"id": {}, "name": {}, "email": {}},
                },
            },
            "child_map": {},
        }
        base_manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "columns": {"id": {}, "name": {}},
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT id, name, email FROM t"},
            manifest=manifest,
            # Base SQL uses SELECT * which triggers fallback
            base_sql={"m": "SELECT * FROM t"},
            base_manifest=base_manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        _do_check(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        pred = data["models"][0]
        assert pred["model_name"] == "m"
        # With base manifest fallback, we get a real column diff (ADD COLUMN email)
        op_texts = [op["operation"] for op in pred["operations"]]
        assert all("REVIEW REQUIRED" not in op for op in op_texts)
        assert "email" in pred["columns_added"]

    def test_no_fallback_without_manifest_columns(self, tmp_path, capsys):
        """When manifest has no columns and SQL is SELECT *, no fallback occurs."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    # No columns defined in manifest
                },
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT * FROM t"},
            manifest=manifest,
            base_sql={"m": "SELECT id, name FROM t"},
            base_manifest=manifest,
        )

        args = _make_check_args(project_dir, fmt="json")
        _do_check(args)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        pred = data["models"][0]
        # Without fallback columns, the prediction should indicate review needed
        op_texts = [op["operation"] for op in pred["operations"]]
        assert any("REVIEW REQUIRED" in op for op in op_texts)


class TestInit:
    def test_creates_config_file(self, tmp_path):
        """init creates .dbt-plan.yml with sample config."""
        import argparse

        args = argparse.Namespace(project_dir=str(tmp_path))
        from dbt_plan.cli import _do_init

        _do_init(args)
        config_path = tmp_path / ".dbt-plan.yml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "ignore_models" in content
        assert "warning_exit_code" in content

    def test_creates_gitignore_if_missing(self, tmp_path):
        """init creates .gitignore with .dbt-plan/ if absent."""
        import argparse

        args = argparse.Namespace(project_dir=str(tmp_path))
        from dbt_plan.cli import _do_init

        _do_init(args)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".dbt-plan/" in gitignore.read_text()

    def test_appends_to_existing_gitignore(self, tmp_path):
        """init appends .dbt-plan/ to existing .gitignore."""
        import argparse

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n")
        args = argparse.Namespace(project_dir=str(tmp_path))
        from dbt_plan.cli import _do_init

        _do_init(args)
        content = gitignore.read_text()
        assert "node_modules/" in content
        assert ".dbt-plan/" in content

    def test_skips_gitignore_if_entry_exists(self, tmp_path, capsys):
        """init does not duplicate .dbt-plan/ in .gitignore."""
        import argparse

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(".dbt-plan/\n")
        args = argparse.Namespace(project_dir=str(tmp_path))
        from dbt_plan.cli import _do_init

        _do_init(args)
        output = capsys.readouterr().out
        assert "Added" not in output  # should not add again

    def test_rejects_existing_config(self, tmp_path):
        """init exits 2 when config already exists."""
        import argparse

        (tmp_path / ".dbt-plan.yml").write_text("# existing\n")
        args = argparse.Namespace(project_dir=str(tmp_path))
        from dbt_plan.cli import _do_init

        with pytest.raises(SystemExit) as exc_info:
            _do_init(args)
        assert exc_info.value.code == 2


class TestStats:
    def _make_stats_args(self, project_dir, manifest=None, dialect=None):
        import argparse

        return argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=manifest,
            dialect=dialect,
        )

    def test_stats_counts_materializations(self, tmp_path, capsys):
        """stats shows materialization counts."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a FROM t"},
            manifest={
                "nodes": {
                    "model.p.m1": {
                        "name": "m1",
                        "config": {"materialized": "table"},
                    },
                    "model.p.m2": {
                        "name": "m2",
                        "config": {"materialized": "incremental", "on_schema_change": "fail"},
                    },
                },
                "child_map": {},
            },
        )
        from dbt_plan.cli import _do_stats

        _do_stats(self._make_stats_args(project_dir))
        output = capsys.readouterr().out
        assert "table" in output
        assert "incremental" in output
        assert "2 model(s) in manifest" in output

    def test_stats_cascade_risk(self, tmp_path, capsys):
        """stats shows cascade risk count for incremental+fail models."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a FROM t"},
            manifest={
                "nodes": {
                    "model.p.m1": {
                        "name": "m1",
                        "config": {"materialized": "incremental", "on_schema_change": "fail"},
                    },
                },
                "child_map": {},
            },
        )
        from dbt_plan.cli import _do_stats

        _do_stats(self._make_stats_args(project_dir))
        output = capsys.readouterr().out
        assert "Cascade risk" in output
        assert "1 incremental model(s)" in output

    def test_stats_missing_manifest_exits_2(self, tmp_path):
        """stats exits 2 when manifest.json is missing."""
        project_dir = _make_project(tmp_path, models_sql={"m": "SELECT 1"})
        # No manifest
        from dbt_plan.cli import _do_stats

        with pytest.raises(SystemExit) as exc_info:
            _do_stats(self._make_stats_args(project_dir))
        assert exc_info.value.code == 2

    def test_stats_no_compiled_sql_still_works(self, tmp_path, capsys):
        """stats works when there's no compiled SQL directory (manifest only)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        target = project_dir / "target"
        target.mkdir()
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "nodes": {
                        "model.p.m": {"name": "m", "config": {"materialized": "view"}},
                    },
                    "child_map": {},
                }
            )
        )
        from dbt_plan.cli import _do_stats

        _do_stats(self._make_stats_args(project_dir))
        output = capsys.readouterr().out
        assert "1 model(s) in manifest" in output
        # No division by zero — SELECT * section should be skipped
        assert "SELECT *" not in output


class TestCiSetup:
    def test_creates_workflow_file(self, tmp_path, capsys):
        """ci-setup creates .github/workflows/dbt-plan.yml."""
        import argparse

        from dbt_plan.cli import _do_ci_setup

        args = argparse.Namespace(project_dir=str(tmp_path))
        _do_ci_setup(args)

        workflow = tmp_path / ".github" / "workflows" / "dbt-plan.yml"
        assert workflow.exists()
        content = workflow.read_text()
        assert "dbt-plan" in content
        assert "dbt compile" in content
        assert "dbt-plan check" in content
        assert "dbt-plan snapshot" in content

        output = capsys.readouterr().out
        assert "Created" in output

    def test_rejects_existing_workflow(self, tmp_path):
        """ci-setup exits 2 when workflow already exists."""
        import argparse

        from dbt_plan.cli import _do_ci_setup

        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "dbt-plan.yml").write_text("existing")

        args = argparse.Namespace(project_dir=str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            _do_ci_setup(args)
        assert exc_info.value.code == 2


class TestRun:
    def test_run_missing_dbt_returns_2(self, tmp_path, capsys, monkeypatch):
        """run returns 2 when dbt is not available."""
        import argparse

        from dbt_plan.cli import _do_run

        # Make dbt unavailable by overriding PATH
        monkeypatch.setenv("PATH", str(tmp_path))

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            format=None,
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            compile_command=None,
        )
        exit_code = _do_run(args)
        assert exit_code == 2
        assert "not found" in capsys.readouterr().err

    def test_run_custom_compile_command_not_found(self, tmp_path, capsys, monkeypatch):
        """run returns 2 with helpful error when custom compile command not found."""
        import argparse

        from dbt_plan.cli import _do_run

        monkeypatch.setenv("PATH", str(tmp_path))

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            format=None,
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            compile_command="uv run dbt compile",
        )
        exit_code = _do_run(args)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert "compile_command" in err


class TestRunGitHandling:
    def test_run_no_git_returns_2(self, tmp_path, capsys, monkeypatch):
        """run returns 2 with helpful error when git is not available."""
        import argparse

        from dbt_plan.cli import _do_run

        # dbt available but git not
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "dbt").write_text("#!/bin/sh\necho 'dbt'")
        (fake_bin / "dbt").chmod(0o755)
        monkeypatch.setenv("PATH", str(fake_bin))

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            format=None,
            no_color=True,
            verbose=False,
            dialect=None,
            select=None,
            compile_command=None,
        )
        exit_code = _do_run(args)
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "git" in err.lower()


class TestSelectWarning:
    def test_select_no_match_warns(self, tmp_path, capsys):
        """--select with no matching changed models warns on stderr."""
        manifest = {
            "nodes": {
                "model.p.m1": {"name": "m1", "config": {"materialized": "table"}},
            },
            "child_map": {},
        }
        project_dir = _make_project(
            tmp_path,
            models_sql={"m1": "SELECT a, b FROM t"},
            manifest=manifest,
            base_sql={"m1": "SELECT a FROM t"},
            base_manifest=manifest,
        )
        args = _make_check_args(project_dir, select="nonexistent")
        _do_check(args)
        err = capsys.readouterr().err
        assert "matched no changed models" in err


class TestMainDispatch:
    def test_no_subcommand_exits_zero(self):
        """Running dbt-plan with no subcommand exits with code 0."""
        with patch("sys.argv", ["dbt-plan"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_main_init_dispatch(self, tmp_path):
        """main() dispatches init subcommand."""
        with patch("sys.argv", ["dbt-plan", "init", "--project-dir", str(tmp_path)]):
            main()
        assert (tmp_path / ".dbt-plan.yml").exists()

    def test_main_ci_setup_dispatch(self, tmp_path):
        """main() dispatches ci-setup subcommand."""
        with patch("sys.argv", ["dbt-plan", "ci-setup", "--project-dir", str(tmp_path)]):
            main()
        assert (tmp_path / ".github" / "workflows" / "dbt-plan.yml").exists()

    def test_main_stats_dispatch(self, tmp_path, capsys):
        """main() dispatches stats subcommand."""
        project_dir = _make_project(
            tmp_path,
            models_sql={"m": "SELECT 1"},
            manifest={
                "nodes": {"model.p.m": {"name": "m", "config": {"materialized": "table"}}},
                "child_map": {},
            },
        )
        with patch("sys.argv", ["dbt-plan", "stats", "--project-dir", str(project_dir)]):
            main()
        assert "1 model(s)" in capsys.readouterr().out
