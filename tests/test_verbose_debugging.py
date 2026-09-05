"""Tests for verbose (--verbose / -v) debugging output in dbt-plan check.

Validates that verbose mode emits useful debugging information to stderr
without contaminating stdout, across all output formats.
"""

from __future__ import annotations

import argparse
import json

from dbt_plan.cli import _do_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    tmp_path,
    *,
    models_sql: dict[str, str] | None = None,
    manifest: dict | None = None,
    base_sql: dict[str, str] | None = None,
    base_manifest: dict | None = None,
    config_yml: str | None = None,
):
    """Set up a fake dbt project structure.

    Returns project_dir Path.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # target/compiled/{proj}/models/
    compiled = project_dir / "target" / "compiled" / "my_project" / "models"
    compiled.mkdir(parents=True)
    if models_sql:
        for name, sql in models_sql.items():
            (compiled / f"{name}.sql").write_text(sql)

    # manifest.json
    if manifest is not None:
        (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

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

    if config_yml is not None:
        (project_dir / ".dbt-plan.yml").write_text(config_yml)

    return project_dir


def _make_args(project_dir, *, fmt="text", verbose=False, dialect=None, select=None):
    """Build argparse.Namespace for _do_check."""
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        verbose=verbose,
        dialect=dialect,
        select=select,
    )


def _manifest_with_nodes(nodes: dict, *, child_map: dict | None = None):
    """Build a minimal manifest dict with proper structure."""
    manifest_nodes = {}
    for name, config in nodes.items():
        node_id = f"model.my_project.{name}"
        manifest_nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": config.get("materialization", "table"),
                "on_schema_change": config.get("on_schema_change"),
                "enabled": config.get("enabled", True),
            },
            "columns": config.get("columns", {}),
        }
    result = {
        "nodes": manifest_nodes,
        "child_map": child_map or {},
        "metadata": {"project_name": "my_project"},
    }
    return result


# ---------------------------------------------------------------------------
# Scenario fixtures
# ---------------------------------------------------------------------------


def _destructive_scenario(tmp_path):
    """Create a DESTRUCTIVE scenario: incremental+sync_all_columns with dropped column."""
    manifest = _manifest_with_nodes(
        {
            "int_order_enriched": {
                "materialization": "incremental",
                "on_schema_change": "sync_all_columns",
            },
        }
    )
    project_dir = _make_project(
        tmp_path,
        models_sql={"int_order_enriched": "SELECT user_id, new_col FROM src"},
        manifest=manifest,
        base_sql={"int_order_enriched": "SELECT user_id, old_col FROM src"},
        base_manifest=manifest,
    )
    return project_dir


# ===========================================================================
# Test 1: Verbose output goes to stderr only
# ===========================================================================


class TestVerboseStderrIsolation:
    """Verbose output must go to stderr only -- never contaminate stdout."""

    def test_verbose_json_stdout_is_valid_json(self, tmp_path, capsys):
        """With verbose=True and --format json, stdout must be valid JSON."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="json", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # stdout must be valid JSON
        parsed = json.loads(captured.out)
        assert "summary" in parsed
        assert "models" in parsed

        # stderr must have [verbose] lines
        assert "[verbose]" in captured.err

    def test_verbose_lines_on_stderr(self, tmp_path, capsys):
        """Verbose lines appear on stderr, not stdout."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="text", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # stdout should NOT contain [verbose]
        assert "[verbose]" not in captured.out
        # stderr should contain [verbose]
        assert "[verbose]" in captured.err


# ===========================================================================
# Test 2: Key debugging info is present
# ===========================================================================


class TestKeyDebuggingInfo:
    """Verbose mode shows essential debugging information."""

    def test_config_summary(self, tmp_path, capsys):
        """Verbose names the dialect it settled on and the ignore list."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True, dialect="bigquery")

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Dialect: bigquery" in stderr
        assert "Config: ignore=" in stderr

    def test_per_model_info(self, tmp_path, capsys):
        """Verbose shows 'MODIFIED model_name: materialization, on_schema_change=...'."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert (
            "MODIFIED int_order_enriched: incremental, on_schema_change=sync_all_columns" in stderr
        )

    def test_base_cols_shown(self, tmp_path, capsys):
        """Verbose shows 'base_cols=[...]'."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "base_cols=" in stderr
        # Should contain the actual columns
        assert "user_id" in stderr

    def test_current_cols_shown(self, tmp_path, capsys):
        """Verbose shows 'current_cols=[...]'."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "current_cols=" in stderr

    def test_manifest_indexed_count(self, tmp_path, capsys):
        """Verbose shows 'Manifest: N model(s) indexed'."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Manifest: 1 model(s) indexed" in stderr

    def test_changed_model_count(self, tmp_path, capsys):
        """Verbose shows 'Found N changed model(s)'."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Found 1 changed model(s)" in stderr

    def test_compiled_paths_shown(self, tmp_path, capsys):
        """Verbose shows base and current compiled directory paths."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Base compiled:" in stderr
        assert "Current compiled:" in stderr


# ===========================================================================
# Test 3: Verbose shows ignored models
# ===========================================================================


class TestVerboseIgnoredModels:
    """Verbose mode reports which models were ignored."""

    def test_ignored_models_reported(self, tmp_path, capsys):
        """When a model is in ignore_models, verbose shows it was ignored."""
        manifest = _manifest_with_nodes(
            {
                "int_order_enriched": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "scratch_temp": {
                    "materialization": "table",
                },
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={
                "int_order_enriched": "SELECT user_id, new_col FROM src",
                "scratch_temp": "SELECT id, val FROM raw",
            },
            manifest=manifest,
            base_sql={
                "int_order_enriched": "SELECT user_id, old_col FROM src",
                "scratch_temp": "SELECT id FROM raw",
            },
            base_manifest=manifest,
            config_yml="ignore_models: [scratch_temp]",
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Ignored 1 model(s) per config: ['scratch_temp']" in stderr


# ===========================================================================
# Test 4: Verbose shows skipped models
# ===========================================================================


class TestVerboseSkippedModels:
    """Verbose mode reports models not found in any manifest."""

    def test_skip_model_not_in_manifest(self, tmp_path, capsys):
        """Model changed on disk but not in manifest -> verbose shows SKIP."""
        manifest = _manifest_with_nodes(
            {
                "known_model": {
                    "materialization": "table",
                },
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={
                "known_model": "SELECT id FROM src",
                "orphan_model": "SELECT id FROM other",
            },
            manifest=manifest,
            base_sql={
                "known_model": "SELECT id, old FROM src",
                "orphan_model": "SELECT id, old FROM other",
            },
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "SKIP orphan_model: not found in any manifest" in stderr


# ===========================================================================
# Test 5: Verbose shows SELECT * detection
# ===========================================================================


class TestVerboseSelectStarFallback:
    """Verbose reports manifest column fallback for SELECT *."""

    def test_base_cols_fallback_from_manifest(self, tmp_path, capsys):
        """SELECT * in base with manifest columns -> verbose shows fallback."""
        manifest = _manifest_with_nodes(
            {
                "star_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                    "columns": {"user_id": {}, "order_status": {}, "created_at": {}},
                },
            }
        )
        base_manifest = _manifest_with_nodes(
            {
                "star_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                    "columns": {"user_id": {}, "order_status": {}},
                },
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={"star_model": "SELECT user_id, order_status, created_at FROM src"},
            manifest=manifest,
            base_sql={"star_model": "SELECT * FROM src"},
            base_manifest=base_manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "base_cols fallback from manifest: 2 columns" in stderr

    def test_current_cols_fallback_from_manifest(self, tmp_path, capsys):
        """SELECT * in current with manifest columns -> verbose shows fallback."""
        manifest = _manifest_with_nodes(
            {
                "star_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                    "columns": {"user_id": {}, "order_status": {}, "created_at": {}},
                },
            }
        )
        base_manifest = _manifest_with_nodes(
            {
                "star_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                    "columns": {"user_id": {}, "order_status": {}},
                },
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={"star_model": "SELECT * FROM src"},
            manifest=manifest,
            base_sql={"star_model": "SELECT user_id, order_status FROM src"},
            base_manifest=base_manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "current_cols fallback from manifest: 3 columns" in stderr


# ===========================================================================
# Test 6: Verbose shows cascade impacts
# ===========================================================================


class TestVerboseCascadeImpacts:
    """Verbose mode reports cascade impact counts."""

    def test_cascade_impact_count_shown(self, tmp_path, capsys):
        """Model with downstream impacts -> verbose shows count."""
        manifest = _manifest_with_nodes(
            {
                "parent_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "child_model": {
                    "materialization": "incremental",
                    "on_schema_change": "fail",
                },
            },
            child_map={
                "model.my_project.parent_model": ["model.my_project.child_model"],
            },
        )
        # child_model SQL references the parent -- create compiled SQL for it
        project_dir = _make_project(
            tmp_path,
            models_sql={
                "parent_model": "SELECT user_id, new_col FROM src",
                "child_model": "SELECT user_id, old_col FROM {{ ref('parent_model') }}",
            },
            manifest=manifest,
            base_sql={
                "parent_model": "SELECT user_id, old_col FROM src",
            },
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Cascade impacts for parent_model:" in stderr


# ===========================================================================
# Test 7: Non-verbose mode is silent on stderr
# ===========================================================================


class TestNonVerboseSilent:
    """Without verbose, stderr should have no [verbose] lines."""

    def test_no_verbose_lines_when_disabled(self, tmp_path, capsys):
        """verbose=False -> no [verbose] output on stderr."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=False)

        _do_check(args)

        captured = capsys.readouterr()
        assert "[verbose]" not in captured.err

    def test_no_verbose_leaks_on_stdout(self, tmp_path, capsys):
        """verbose=False -> definitely no [verbose] on stdout either."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=False, fmt="json")

        _do_check(args)

        captured = capsys.readouterr()
        assert "[verbose]" not in captured.out
        assert "[verbose]" not in captured.err


# ===========================================================================
# Test 8: Verbose with different formats
# ===========================================================================


class TestVerboseWithFormats:
    """Verbose output coexists correctly with all output formats."""

    def test_verbose_json_format(self, tmp_path, capsys):
        """Verbose + JSON: stderr has verbose, stdout has valid JSON."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="json", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # stdout: valid JSON
        parsed = json.loads(captured.out)
        assert parsed["summary"]["destructive"] >= 1
        # stderr: verbose lines
        assert "[verbose]" in captured.err
        assert "Config:" in captured.err

    def test_verbose_github_format(self, tmp_path, capsys):
        """Verbose + github: stderr has verbose, stdout has markdown."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="github", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # stdout: markdown
        assert "### dbt-plan" in captured.out
        assert "DESTRUCTIVE" in captured.out
        # stderr: verbose lines
        assert "[verbose]" in captured.err
        assert "Config:" in captured.err

    def test_verbose_text_format(self, tmp_path, capsys):
        """Verbose + text: stderr has verbose, stdout has text output."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="text", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # stdout: text output
        assert "dbt-plan" in captured.out
        assert "int_order_enriched" in captured.out
        # stderr: verbose lines
        assert "[verbose]" in captured.err
        assert "MODIFIED int_order_enriched" in captured.err

    def test_verbose_does_not_double_print_results(self, tmp_path, capsys):
        """Verbose mode should not duplicate the check result on stderr."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, fmt="json", verbose=True)

        _do_check(args)

        captured = capsys.readouterr()
        # The JSON result should only appear in stdout, not stderr
        assert '"summary"' not in captured.err


# ===========================================================================
# Regression / edge case tests
# ===========================================================================


class TestVerboseEdgeCases:
    """Edge cases for verbose mode."""

    def test_verbose_with_no_changes(self, tmp_path, capsys):
        """Verbose with no model changes still shows config info."""
        manifest = _manifest_with_nodes(
            {
                "stable_model": {"materialization": "table"},
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={"stable_model": "SELECT id FROM src"},
            manifest=manifest,
            base_sql={"stable_model": "SELECT id FROM src"},
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True)

        exit_code = _do_check(args)

        captured = capsys.readouterr()
        assert exit_code == 0
        # Config line should still be logged
        assert "Dialect: " in captured.err
        # Should show 0 changes
        assert "Found 0 changed model(s)" in captured.err

    def test_verbose_with_added_model(self, tmp_path, capsys):
        """Verbose shows ADDED for new models."""
        manifest = _manifest_with_nodes(
            {
                "new_model": {"materialization": "table"},
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={"new_model": "SELECT id, name FROM src"},
            manifest=manifest,
            base_sql={},
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "ADDED new_model: table" in stderr

    def test_verbose_with_removed_model(self, tmp_path, capsys):
        """Verbose shows REMOVED for deleted models."""
        manifest = _manifest_with_nodes(
            {
                "old_model": {"materialization": "table"},
            }
        )
        base_manifest = _manifest_with_nodes(
            {
                "old_model": {"materialization": "table"},
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={},
            manifest=manifest,
            base_sql={"old_model": "SELECT id FROM src"},
            base_manifest=base_manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "REMOVED old_model: table" in stderr

    def test_verbose_base_manifest_indexed(self, tmp_path, capsys):
        """When base manifest exists, verbose shows its indexed count."""
        manifest = _manifest_with_nodes(
            {
                "int_order_enriched": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={"int_order_enriched": "SELECT user_id, new_col FROM src"},
            manifest=manifest,
            base_sql={"int_order_enriched": "SELECT user_id, old_col FROM src"},
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Base manifest: 1 model(s) indexed" in stderr

    def test_verbose_dialect_falls_back_when_the_manifest_names_no_adapter(self, tmp_path, capsys):
        """These fixtures have no `metadata.adapter_type`, so the default stands."""
        project_dir = _destructive_scenario(tmp_path)
        args = _make_args(project_dir, verbose=True, dialect=None)

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Dialect: snowflake" in stderr

    def test_verbose_select_filter(self, tmp_path, capsys):
        """Verbose shows selected model count when --select is used."""
        manifest = _manifest_with_nodes(
            {
                "model_a": {"materialization": "table"},
                "model_b": {"materialization": "table"},
            }
        )
        project_dir = _make_project(
            tmp_path,
            models_sql={
                "model_a": "SELECT id, new FROM src",
                "model_b": "SELECT id, new FROM src",
            },
            manifest=manifest,
            base_sql={
                "model_a": "SELECT id FROM src",
                "model_b": "SELECT id FROM src",
            },
            base_manifest=manifest,
        )
        args = _make_args(project_dir, verbose=True, select="model_a")

        _do_check(args)

        stderr = capsys.readouterr().err
        assert "Selected 1 of 2 changed model(s)" in stderr
        assert "model_a" in stderr
