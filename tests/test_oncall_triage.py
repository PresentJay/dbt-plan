"""On-call triage tests -- can a sleep-deprived engineer debug CI failures at 3am?

These tests simulate the 6 most common on-call scenarios and verify that dbt-plan
provides enough information to diagnose, override, or suppress issues without
reading the source code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_user_journeys.py)
# ---------------------------------------------------------------------------


def _build_manifest(nodes: dict, child_map: dict | None = None, project: str = "test") -> dict:
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
        if cfg.get("enabled") is False:
            node["config"]["enabled"] = False
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
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    project_name = manifest.get("metadata", {}).get("project_name", "test")
    compiled_dir = project_dir / "target" / "compiled" / project_name / "models"
    compiled_dir.mkdir(parents=True)
    for name, sql in current_sqls.items():
        (compiled_dir / f"{name}.sql").write_text(sql)

    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

    base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
    base_compiled.mkdir(parents=True)
    for name, sql in base_sqls.items():
        (base_compiled / f"{name}.sql").write_text(sql)

    base_manifest_data = base_manifest if base_manifest is not None else manifest
    (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
        json.dumps(base_manifest_data)
    )

    if config_yml is not None:
        (project_dir / ".dbt-plan.yml").write_text(config_yml)

    return project_dir


def _make_args(project_dir: Path, *, fmt: str = "json", **overrides) -> argparse.Namespace:
    defaults = dict(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        select=None,
        verbose=False,
        dialect=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_check(
    project_dir: Path,
    *,
    fmt: str = "json",
    env: dict[str, str] | None = None,
    **overrides,
) -> tuple[int, str]:
    """Run _do_check and capture stdout. Returns (exit_code, raw_stdout)."""
    from dbt_plan.cli import _do_check

    args = _make_args(project_dir, fmt=fmt, **overrides)
    buf = StringIO()
    err_buf = StringIO()
    env_patch = env or {}
    with (
        patch("sys.stdout", buf),
        patch("sys.stderr", err_buf),
        patch.dict(os.environ, env_patch, clear=False),
    ):
        exit_code = _do_check(args)
    return exit_code, buf.getvalue(), err_buf.getvalue()


# ===========================================================================
# Scenario 1: "CI failed, what broke?"
# ===========================================================================


class TestScenario1CIFailedWhatBroke:
    """On-call gets paged: CI failed with exit code 1.
    Can they quickly determine WHICH model, WHAT columns, WHY destructive?
    """

    def _make_destructive_project(self, tmp_path):
        """incremental + sync_all_columns + column dropped = exit 1."""
        base_sql = "SELECT user_id, customer_tier, revenue, created_at FROM raw_orders"
        current_sql = (
            "SELECT user_id, created_at FROM raw_orders"  # dropped customer_tier, revenue
        )

        manifest = _build_manifest(
            {
                "fct_orders": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        return _setup_project(
            tmp_path,
            base_sqls={"fct_orders": base_sql},
            current_sqls={"fct_orders": current_sql},
            manifest=manifest,
        )

    def test_exit_code_is_1_for_destructive(self, tmp_path):
        """CI exit code 1 = destructive change detected."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 1

    def test_text_output_tells_which_model(self, tmp_path):
        """Text output names the specific model that caused the failure."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert "fct_orders" in stdout

    def test_text_output_tells_what_columns_dropped(self, tmp_path):
        """Text output shows DROP COLUMN with the specific column names."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert "DROP COLUMN" in stdout
        assert "customer_tier" in stdout
        assert "revenue" in stdout

    def test_text_output_tells_why_destructive(self, tmp_path):
        """Text output shows 'DESTRUCTIVE' label so on-call knows the severity."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert "DESTRUCTIVE" in stdout

    def test_text_output_shows_materialization_context(self, tmp_path):
        """Text output shows materialization and on_schema_change for context."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert "incremental" in stdout
        assert "sync_all_columns" in stdout

    def test_summary_line_is_grepable(self, tmp_path):
        """Summary line starts with 'dbt-plan:' for grep in CI logs."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")

        # grep "^dbt-plan:" equivalent
        summary_lines = [line for line in stdout.splitlines() if line.startswith("dbt-plan:")]
        assert len(summary_lines) == 1, f"Expected exactly 1 summary line, got: {summary_lines}"

        summary = summary_lines[0]
        # Summary should contain counts that are machine-parseable
        assert "1 checked" in summary
        assert "1 destructive" in summary

    def test_json_output_has_all_fields(self, tmp_path):
        """JSON output includes all fields needed for programmatic CI decisions."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        model = data["models"][0]
        assert model["model_name"] == "fct_orders"
        assert model["safety"] == "destructive"
        assert model["materialization"] == "incremental"
        assert model["on_schema_change"] == "sync_all_columns"
        assert "customer_tier" in model["columns_removed"]
        assert "revenue" in model["columns_removed"]

    def test_json_summary_counts(self, tmp_path):
        """JSON summary has total/safe/warning/destructive counts."""
        project_dir = self._make_destructive_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        assert data["summary"]["total"] == 1
        assert data["summary"]["destructive"] == 1
        assert data["summary"]["safe"] == 0
        assert data["summary"]["warning"] == 0


# ===========================================================================
# Scenario 2: "Is this safe to override?"
# ===========================================================================


class TestScenario2SafeToOverride:
    """On-call wants to programmatically decide if a destructive change is on a
    known-safe model (e.g., scratch tables, temp staging).
    """

    def test_json_has_enough_fields_for_auto_approval(self, tmp_path):
        """JSON output includes model_name, materialization, on_schema_change,
        columns_removed, columns_added -- enough for a CI override script.
        """
        base_sql = "SELECT a, b, c FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "scratch_temp": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"scratch_temp": base_sql},
            current_sqls={"scratch_temp": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        model = data["models"][0]

        # All fields a CI script needs for auto-approval
        required_fields = {
            "model_name",
            "materialization",
            "on_schema_change",
            "columns_removed",
            "columns_added",
            "safety",
            "operations",
        }
        assert required_fields.issubset(set(model.keys()))

    def test_ci_script_can_auto_approve_known_safe_models(self, tmp_path):
        """Simulate a CI script that auto-approves if ALL destructive models
        are in a known-safe list.
        """
        base_sql = "SELECT a, b, c FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "scratch_temp_1": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "scratch_temp_2": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"scratch_temp_1": base_sql, "scratch_temp_2": base_sql},
            current_sqls={"scratch_temp_1": current_sql, "scratch_temp_2": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        assert exit_code == 1  # destructive

        data = json.loads(stdout)
        known_safe = {"scratch_temp_1", "scratch_temp_2"}

        # Simulate CI auto-approval logic
        destructive_models = [
            m["model_name"] for m in data["models"] if m["safety"] == "destructive"
        ]
        all_safe = all(m in known_safe for m in destructive_models)
        assert all_safe, (
            "CI script should be able to determine all destructive models are known-safe"
        )

    def test_ci_script_blocks_unknown_destructive(self, tmp_path):
        """If a destructive model is NOT in the safe list, CI script blocks."""
        base_sql = "SELECT a, b FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "scratch_temp": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "fct_revenue": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"scratch_temp": base_sql, "fct_revenue": base_sql},
            current_sqls={"scratch_temp": current_sql, "fct_revenue": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)
        known_safe = {"scratch_temp"}

        destructive_models = [
            m["model_name"] for m in data["models"] if m["safety"] == "destructive"
        ]
        all_safe = all(m in known_safe for m in destructive_models)
        assert not all_safe, "fct_revenue is not in safe list, CI should block"

    def test_json_operations_show_specific_columns_for_review(self, tmp_path):
        """Operations array gives per-column detail for human override review."""
        base_sql = "SELECT user_id, email, phone FROM users"
        current_sql = "SELECT user_id FROM users"

        manifest = _build_manifest(
            {
                "dim_users": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"dim_users": base_sql},
            current_sqls={"dim_users": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        model = data["models"][0]
        drop_ops = [op for op in model["operations"] if op["operation"] == "DROP COLUMN"]
        dropped_cols = {op["column"] for op in drop_ops}

        assert dropped_cols == {"email", "phone"}
        assert set(model["columns_removed"]) == {"email", "phone"}


# ===========================================================================
# Scenario 3: "I want to suppress this known warning"
# ===========================================================================


class TestScenario3SuppressKnownWarning:
    """On-call adds a model to ignore_models in .dbt-plan.yml to suppress
    a known-noisy destructive warning.
    """

    def test_full_ignore_models_workflow(self, tmp_path):
        """End-to-end: first run fails, add ignore, second run passes."""
        base_sql = "SELECT a, b, c FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "noisy_scratch": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        # Step 1: First run WITHOUT ignore_models -> exit 1
        project_dir = _setup_project(
            tmp_path,
            base_sqls={"noisy_scratch": base_sql},
            current_sqls={"noisy_scratch": current_sql},
            manifest=manifest,
        )

        exit_code_1, stdout_1, _ = _run_check(project_dir, fmt="json")
        assert exit_code_1 == 1, "First run should be destructive (exit 1)"

        data_1 = json.loads(stdout_1)
        assert any(m["model_name"] == "noisy_scratch" for m in data_1["models"])

        # Step 2: Add noisy_scratch to ignore_models
        config_path = project_dir / ".dbt-plan.yml"
        config_path.write_text("ignore_models: [noisy_scratch]\n")

        # Step 3: Second run WITH ignore_models -> exit 0
        exit_code_2, stdout_2, _ = _run_check(project_dir, fmt="json")
        assert exit_code_2 == 0, "Second run should be safe (exit 0) after ignoring"

        data_2 = json.loads(stdout_2)
        model_names_2 = [m["model_name"] for m in data_2["models"]]
        assert "noisy_scratch" not in model_names_2, "Ignored model should not appear in output"

    def test_ignore_models_mixed_project(self, tmp_path):
        """Ignoring one model doesn't affect other models in the same run."""
        base_sql_noisy = "SELECT a, b FROM src"
        current_sql_noisy = "SELECT a FROM src"
        base_sql_real = "SELECT x, y FROM other"
        current_sql_real = "SELECT x, y, z FROM other"

        manifest = _build_manifest(
            {
                "noisy_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "real_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"noisy_model": base_sql_noisy, "real_model": base_sql_real},
            current_sqls={"noisy_model": current_sql_noisy, "real_model": current_sql_real},
            manifest=manifest,
            config_yml="ignore_models: [noisy_model]\n",
        )

        exit_code, stdout, _ = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        model_names = [m["model_name"] for m in data["models"]]
        assert "noisy_model" not in model_names, "Ignored model filtered out"
        assert "real_model" in model_names, "Non-ignored model still checked"

        # real_model added a column (safe), so exit code should be 0
        assert exit_code == 0

    def test_text_output_truly_hides_ignored_model(self, tmp_path):
        """Verify ignored model name doesn't appear anywhere in text output."""
        base_sql = "SELECT a, b FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "secret_scratch_v2": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"secret_scratch_v2": base_sql},
            current_sqls={"secret_scratch_v2": current_sql},
            manifest=manifest,
            config_yml="ignore_models: [secret_scratch_v2]\n",
        )

        exit_code, stdout, _ = _run_check(project_dir, fmt="text")
        assert "secret_scratch_v2" not in stdout, (
            "Ignored model name should not appear in any part of the output"
        )


# ===========================================================================
# Scenario 4: "What models are at risk in my project?"
# ===========================================================================


class TestScenario4StatsCommand:
    """On-call runs `dbt-plan stats` to understand the risk profile of the project."""

    def _setup_stats_project(self, tmp_path):
        """Create a project with mixed materializations for stats."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = _build_manifest(
            {
                "dim_users": {"materialization": "table"},
                "dim_products": {"materialization": "table"},
                "vw_report": {"materialization": "view"},
                "stg_orders": {
                    "materialization": "incremental",
                    "on_schema_change": "fail",
                },
                "fct_revenue": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "fct_daily": {
                    "materialization": "incremental",
                    "on_schema_change": "ignore",
                },
                "snap_orders": {"materialization": "snapshot"},
                "eph_helper": {"materialization": "ephemeral"},
            }
        )

        # Write manifest
        target_dir = project_dir / "target"
        target_dir.mkdir()
        (target_dir / "manifest.json").write_text(json.dumps(manifest))

        # Write compiled SQL (some with SELECT *)
        compiled = target_dir / "compiled" / "test" / "models"
        compiled.mkdir(parents=True)
        (compiled / "dim_users.sql").write_text("SELECT id, name, email FROM users")
        (compiled / "dim_products.sql").write_text("SELECT * FROM raw_products")
        (compiled / "vw_report.sql").write_text("SELECT * FROM summary")
        (compiled / "stg_orders.sql").write_text("SELECT order_id, ts FROM events")
        (compiled / "fct_revenue.sql").write_text("SELECT rev_id, amount FROM revenue")
        (compiled / "fct_daily.sql").write_text("SELECT day, total FROM agg")
        (compiled / "snap_orders.sql").write_text("SELECT order_id, status FROM orders")
        (compiled / "eph_helper.sql").write_text("SELECT 1 AS one")

        return project_dir

    def test_stats_shows_materialization_breakdown(self, tmp_path):
        """Stats output shows count per materialization type."""
        from dbt_plan.cli import _do_stats

        project_dir = self._setup_stats_project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=None,
            dialect="snowflake",
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            _do_stats(args)
        output = buf.getvalue()

        assert "Materializations:" in output
        assert "incremental" in output
        assert "table" in output
        assert "view" in output
        assert "snapshot" in output
        assert "ephemeral" in output

    def test_stats_shows_incremental_fail_count(self, tmp_path):
        """Stats highlights incremental+fail as a cascade risk."""
        from dbt_plan.cli import _do_stats

        project_dir = self._setup_stats_project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=None,
            dialect="snowflake",
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            _do_stats(args)
        output = buf.getvalue()

        assert "on_schema_change (incremental only):" in output
        assert "fail" in output
        # Should note cascade risk
        assert "Cascade risk:" in output or "dbt-plan monitors this" in output

    def test_stats_shows_select_star_count(self, tmp_path):
        """Stats shows SELECT * usage percentage."""
        from dbt_plan.cli import _do_stats

        project_dir = self._setup_stats_project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=None,
            dialect="snowflake",
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            _do_stats(args)
        output = buf.getvalue()

        assert "SELECT * usage:" in output
        # We have 2 SELECT * models out of 8 total
        assert re.search(r"2/8 models", output)

    def test_stats_shows_model_count(self, tmp_path):
        """Stats header shows total model count."""
        from dbt_plan.cli import _do_stats

        project_dir = self._setup_stats_project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=None,
            dialect="snowflake",
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            _do_stats(args)
        output = buf.getvalue()

        assert "8 model(s) in manifest" in output

    def test_stats_output_is_human_readable(self, tmp_path):
        """Stats output has clear sections -- an on-call engineer can scan it quickly."""
        from dbt_plan.cli import _do_stats

        project_dir = self._setup_stats_project(tmp_path)
        args = argparse.Namespace(
            project_dir=str(project_dir),
            target_dir="target",
            manifest=None,
            dialect="snowflake",
        )

        buf = StringIO()
        with patch("sys.stdout", buf):
            _do_stats(args)
        output = buf.getvalue()

        # Verify section headers exist for quick scanning
        assert "dbt-plan stats" in output
        assert "Materializations:" in output
        assert "on_schema_change" in output
        assert "Coverage:" in output

        # Verify no crash, no empty output
        assert len(output) > 100, "Stats output seems too short to be useful"


# ===========================================================================
# Scenario 5: "Exit code 2 -- is this a warning or an error?"
# ===========================================================================


class TestScenario5ExitCode2Disambiguation:
    """Exit code 2 can come from parse failure OR WARNING prediction.
    On-call needs to distinguish them.
    """

    def test_parse_failure_reports_on_stderr(self, tmp_path):
        """Parse failure for a non-table/view model shows in text output
        with 'Could not extract columns' message.
        """
        # Use unparseable SQL to trigger parse failure
        base_sql = "THIS IS NOT VALID SQL AT ALL ;;;"
        current_sql = "ALSO NOT VALID SQL %%% !!!"

        manifest = _build_manifest(
            {
                "broken_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"broken_model": base_sql},
            current_sqls={"broken_model": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 2, "Parse failure should produce exit code 2"

        # The text output should say "Could not extract columns"
        assert "Could not extract columns" in stdout, (
            "On-call needs to see which models had parse failures"
        )
        assert "broken_model" in stdout

    def test_warning_prediction_shows_in_stdout(self, tmp_path):
        """WARNING prediction (e.g., snapshot) shows the WARNING model in stdout."""
        base_sql = "SELECT id, status, ts FROM orders"
        current_sql = "SELECT id, status, ts, new_col FROM orders"

        manifest = _build_manifest(
            {
                "snap_orders": {"materialization": "snapshot"},
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"snap_orders": base_sql},
            current_sqls={"snap_orders": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 2, "Snapshot warning should produce exit code 2"

        assert "WARNING" in stdout
        assert "snap_orders" in stdout
        assert "snapshot" in stdout

    def test_parse_failure_vs_warning_distinguishable_in_json(self, tmp_path):
        """JSON output separates parse_failures from warning-level models
        so a CI script can distinguish them.
        """
        # One model with parse failure, one with snapshot warning
        unparseable_sql = "NOT SQL {{{{ broken jinja }}}}"
        snapshot_base = "SELECT id, ts FROM src"
        snapshot_current = "SELECT id, ts, extra FROM src"

        manifest = _build_manifest(
            {
                "broken_inc": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "snap_model": {"materialization": "snapshot"},
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"broken_inc": unparseable_sql, "snap_model": snapshot_base},
            current_sqls={
                "broken_inc": unparseable_sql + " --modified",
                "snap_model": snapshot_current,
            },
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="json")
        data = json.loads(stdout)

        # parse_failures field tells you which models failed to parse
        assert "broken_inc" in data["parse_failures"]

        # The snapshot model is in the models list as a WARNING
        snap = next(m for m in data["models"] if m["model_name"] == "snap_model")
        assert snap["safety"] == "warning"

        # These are structurally distinct -- a script can differentiate
        assert data["parse_failures"] != [], "parse_failures should list unparseable models"

    def test_incremental_fail_column_change_is_exit_2(self, tmp_path):
        """incremental+fail with changed columns = WARNING = exit 2."""
        base_sql = "SELECT a, b FROM src"
        current_sql = "SELECT a, c FROM src"

        manifest = _build_manifest(
            {
                "strict_model": {
                    "materialization": "incremental",
                    "on_schema_change": "fail",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"strict_model": base_sql},
            current_sqls={"strict_model": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 2, "incremental+fail should produce WARNING (exit 2)"
        assert "BUILD FAILURE" in stdout
        assert "strict_model" in stdout


# ===========================================================================
# Scenario 6: "warning_exit_code=0 to unblock CI"
# ===========================================================================


class TestScenario6WarningExitCodeOverride:
    """Emergency workaround: set DBT_PLAN_WARNING_EXIT_CODE=0 to turn
    warnings into exit 0, unblocking CI without config file changes.
    """

    def _make_warning_project(self, tmp_path):
        """A snapshot model change that produces exit 2 (warning)."""
        base_sql = "SELECT id, status FROM orders"
        current_sql = "SELECT id, status, updated_at FROM orders"

        manifest = _build_manifest(
            {
                "snap_orders": {"materialization": "snapshot"},
            }
        )

        return _setup_project(
            tmp_path,
            base_sqls={"snap_orders": base_sql},
            current_sqls={"snap_orders": current_sql},
            manifest=manifest,
        )

    def test_default_warning_exit_code_is_2(self, tmp_path):
        """Without override, warnings produce exit code 2."""
        project_dir = self._make_warning_project(tmp_path)
        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 2

    def test_env_var_overrides_warning_exit_code_to_0(self, tmp_path):
        """Setting DBT_PLAN_WARNING_EXIT_CODE=0 via env var makes warnings exit 0."""
        project_dir = self._make_warning_project(tmp_path)
        exit_code, stdout, stderr = _run_check(
            project_dir,
            fmt="text",
            env={"DBT_PLAN_WARNING_EXIT_CODE": "0"},
        )
        assert exit_code == 0, "With WARNING_EXIT_CODE=0, warnings should not fail CI"

    def test_warnings_still_appear_in_output_with_exit_0(self, tmp_path):
        """Even with exit 0, warnings are still visible in stdout."""
        project_dir = self._make_warning_project(tmp_path)
        exit_code, stdout, stderr = _run_check(
            project_dir,
            fmt="text",
            env={"DBT_PLAN_WARNING_EXIT_CODE": "0"},
        )
        assert exit_code == 0
        assert "WARNING" in stdout, "Warning text must still be visible"
        assert "snap_orders" in stdout, "Model name must still be visible"

    def test_warnings_still_in_json_with_exit_0(self, tmp_path):
        """JSON output still shows the warning model even when exit is 0."""
        project_dir = self._make_warning_project(tmp_path)
        exit_code, stdout, stderr = _run_check(
            project_dir,
            fmt="json",
            env={"DBT_PLAN_WARNING_EXIT_CODE": "0"},
        )
        assert exit_code == 0

        data = json.loads(stdout)
        assert data["summary"]["warning"] == 1
        model = data["models"][0]
        assert model["safety"] == "warning"
        assert model["model_name"] == "snap_orders"

    def test_destructive_not_affected_by_warning_exit_code(self, tmp_path):
        """DBT_PLAN_WARNING_EXIT_CODE=0 does NOT override destructive (exit 1).
        This prevents accidentally merging truly dangerous changes.
        """
        base_sql = "SELECT a, b FROM src"
        current_sql = "SELECT a FROM src"

        manifest = _build_manifest(
            {
                "danger_model": {
                    "materialization": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"danger_model": base_sql},
            current_sqls={"danger_model": current_sql},
            manifest=manifest,
        )

        exit_code, stdout, stderr = _run_check(
            project_dir,
            fmt="text",
            env={"DBT_PLAN_WARNING_EXIT_CODE": "0"},
        )
        assert exit_code == 1, (
            "Destructive changes must ALWAYS be exit 1, regardless of warning_exit_code"
        )

    def test_config_file_override_also_works(self, tmp_path):
        """warning_exit_code can also be set in .dbt-plan.yml."""
        base_sql = "SELECT id, status FROM orders"
        current_sql = "SELECT id, status, updated_at FROM orders"

        manifest = _build_manifest(
            {
                "snap_orders": {"materialization": "snapshot"},
            }
        )

        project_dir = _setup_project(
            tmp_path,
            base_sqls={"snap_orders": base_sql},
            current_sqls={"snap_orders": current_sql},
            manifest=manifest,
            config_yml="warning_exit_code: 0\n",
        )

        exit_code, stdout, stderr = _run_check(project_dir, fmt="text")
        assert exit_code == 0
        assert "WARNING" in stdout  # Still shows warning text
