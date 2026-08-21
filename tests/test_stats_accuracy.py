"""Tests for _do_stats accuracy — materialization counts, SELECT * detection, coverage score."""

from __future__ import annotations

import argparse
import json

import pytest

from dbt_plan.cli import _do_stats


def _make_manifest(nodes: dict, metadata: dict | None = None) -> dict:
    """Build a minimal manifest dict."""
    return {
        "nodes": nodes,
        "child_map": {},
        "metadata": metadata or {},
    }


def _model_node(
    name: str,
    project: str = "proj",
    materialization: str = "table",
    on_schema_change: str | None = None,
    columns: dict | None = None,
) -> tuple[str, dict]:
    """Return (node_id, node_dict) for a model node."""
    node_id = f"model.{project}.{name}"
    node = {
        "name": name,
        "config": {
            "materialized": materialization,
            "on_schema_change": on_schema_change,
        },
    }
    if columns is not None:
        node["columns"] = columns
    return node_id, node


def _write_manifest(tmp_path, manifest_data: dict):
    """Write manifest.json to tmp_path and return its path."""
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_data))
    return path


def _setup_compiled_dir(tmp_path, sql_files: dict[str, str]):
    """Create target/compiled/{project}/models/ with SQL files.

    sql_files: mapping of model_name -> SQL content.
    Returns the target dir path.
    """
    target_dir = tmp_path / "target"
    compiled_dir = target_dir / "compiled" / "proj" / "models"
    compiled_dir.mkdir(parents=True)
    for name, sql in sql_files.items():
        (compiled_dir / f"{name}.sql").write_text(sql)
    return target_dir


def _make_args(
    project_dir: str,
    target_dir: str = "target",
    manifest: str | None = None,
    dialect: str | None = None,
) -> argparse.Namespace:
    """Build args namespace for _do_stats."""
    return argparse.Namespace(
        project_dir=project_dir,
        target_dir=target_dir,
        manifest=manifest,
        dialect=dialect,
    )


class TestStatsOutputCorrectness:
    """Scenario 1: Stats output correctness with known model counts."""

    def test_materialization_and_osc_counts(self, tmp_path, capsys):
        """5 tables, 3 views, 4 incremental (2 fail, 1 sync_all, 1 ignore), 2 ephemeral, 1 snapshot."""
        nodes = {}

        # 5 tables
        for i in range(5):
            nid, node = _model_node(f"tbl_{i}", materialization="table")
            nodes[nid] = node

        # 3 views
        for i in range(3):
            nid, node = _model_node(f"vw_{i}", materialization="view")
            nodes[nid] = node

        # 4 incremental: 2 fail, 1 sync_all_columns, 1 ignore
        nid, node = _model_node(
            "inc_fail_0", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_fail_1", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_sync", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_ign", materialization="incremental", on_schema_change="ignore"
        )
        nodes[nid] = node

        # 2 ephemeral
        for i in range(2):
            nid, node = _model_node(f"eph_{i}", materialization="ephemeral")
            nodes[nid] = node

        # 1 snapshot
        nid, node = _model_node("snap_0", materialization="snapshot")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        # No compiled dir needed for pure manifest stats
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # Total: 5+3+4+2+1 = 15
        assert "15 model(s) in manifest" in out

        # Materialization counts
        assert "table" in out
        assert "view" in out
        assert "incremental" in out
        assert "ephemeral" in out
        assert "snapshot" in out

        # on_schema_change (incremental only) section
        assert "on_schema_change (incremental only):" in out
        assert "fail" in out
        assert "sync_all_columns" in out
        assert "ignore" in out

    def test_osc_incremental_breakdown(self, tmp_path, capsys):
        """Verify incremental on_schema_change section shows correct counts."""
        nodes = {}
        nid, node = _model_node("inc_f1", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("inc_f2", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node(
            "inc_s", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node("inc_i", materialization="incremental", on_schema_change="ignore")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        lines = out.splitlines()

        # Find the incremental osc section
        osc_section = False
        osc_lines = []
        for line in lines:
            if "on_schema_change (incremental only):" in line:
                osc_section = True
                continue
            if osc_section:
                if line.strip() and line.startswith("  "):
                    osc_lines.append(line)
                elif line.strip() == "":
                    continue
                else:
                    break

        # fail:2, sync_all_columns:1, ignore:1
        fail_line = [ln for ln in osc_lines if "fail" in ln and "sync" not in ln]
        assert len(fail_line) >= 1
        assert "2" in fail_line[0]

        sync_line = [ln for ln in osc_lines if "sync_all_columns" in ln]
        assert len(sync_line) >= 1
        assert "1" in sync_line[0]

        # fail and sync_all_columns should be marked as monitored
        assert "dbt-plan monitors this" in fail_line[0]
        assert "dbt-plan monitors this" in sync_line[0]

    def test_cascade_risk_mentions_fail_models(self, tmp_path, capsys):
        """Cascade risk count mentions fail models."""
        nodes = {}
        nid, node = _model_node("inc_f1", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("inc_f2", materialization="incremental", on_schema_change="fail")
        nodes[nid] = node
        nid, node = _model_node("tbl", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "Cascade risk: 2 incremental model(s) with on_schema_change=fail" in out


class TestSelectStarCounting:
    """Scenario 2: SELECT * counting in compiled SQL."""

    def test_star_count_correct(self, tmp_path, capsys):
        """3 models use SELECT *, 2 use explicit columns -> 3/5 (60%)."""
        nodes = {}
        for name in ("star1", "star2", "star3", "explicit1", "explicit2"):
            nid, node = _model_node(name, materialization="table")
            nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "star1": "SELECT * FROM raw.users",
            "star2": "SELECT * FROM raw.events",
            "star3": "SELECT * FROM raw.orders",
            "explicit1": "SELECT id, name, email FROM raw.users",
            "explicit2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 3/5 models (60%)" in out


class TestManifestColumnFallback:
    """Scenario 3: Manifest column fallback info."""

    def test_fallback_available_count(self, tmp_path, capsys):
        """2 of 3 SELECT * models have manifest columns defined."""
        nodes = {}
        # star1 and star2 have columns in manifest
        nid, node = _model_node("star1", materialization="table", columns={"id": {}, "name": {}})
        nodes[nid] = node
        nid, node = _model_node(
            "star2", materialization="table", columns={"order_id": {}, "total": {}}
        )
        nodes[nid] = node
        # star3 has no columns in manifest
        nid, node = _model_node("star3", materialization="table")
        nodes[nid] = node
        # explicit models
        nid, node = _model_node("explicit1", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("explicit2", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "star1": "SELECT * FROM raw.users",
            "star2": "SELECT * FROM raw.events",
            "star3": "SELECT * FROM raw.orders",
            "explicit1": "SELECT id, name FROM raw.users",
            "explicit2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 3/5 models (60%)" in out
        # manifest_fallback counts ALL models with columns defined (2 out of 5 total)
        assert "Manifest column fallback available: 2/5 models" in out
        # Remaining = star_count - min(star_count, manifest_fallback) = 3 - min(3, 2) = 1
        assert "Remaining without fallback: 1" in out


class TestCoverageScore:
    """Scenario 4: Coverage score math."""

    def test_coverage_calculation(self, tmp_path, capsys):
        """Coverage = tables + views + ephemeral + monitorable incremental."""
        nodes = {}
        # 3 tables
        for i in range(3):
            nid, node = _model_node(f"tbl_{i}", materialization="table")
            nodes[nid] = node
        # 2 views
        for i in range(2):
            nid, node = _model_node(f"vw_{i}", materialization="view")
            nodes[nid] = node
        # 1 ephemeral
        nid, node = _model_node("eph_0", materialization="ephemeral")
        nodes[nid] = node
        # 2 incremental sync_all_columns (monitorable)
        nid, node = _model_node(
            "inc_sync_0", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        nid, node = _model_node(
            "inc_sync_1", materialization="incremental", on_schema_change="sync_all_columns"
        )
        nodes[nid] = node
        # 1 incremental fail (monitorable)
        nid, node = _model_node(
            "inc_fail_0", materialization="incremental", on_schema_change="fail"
        )
        nodes[nid] = node
        # 1 incremental ignore (NOT monitorable)
        nid, node = _model_node(
            "inc_ign_0", materialization="incremental", on_schema_change="ignore"
        )
        nodes[nid] = node
        # 1 snapshot (NOT covered)
        nid, node = _model_node("snap_0", materialization="snapshot")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        total = 11
        # safe = tables(3) + views(2) + ephemeral(1) = 6
        # monitorable = sync_all_columns(2) + fail(1) = 3
        # coverage = 6 + 3 = 9
        assert f"Coverage: 9/{total} models fully analyzed by dbt-plan" in out


class TestEdgeCases:
    """Scenario 5: Edge cases."""

    def test_empty_manifest(self, tmp_path, capsys):
        """Empty manifest (0 models) should handle gracefully."""
        manifest = _make_manifest({})
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "0 model(s) in manifest" in out
        # Coverage should show 0/0
        assert "Coverage: 0/0 models fully analyzed by dbt-plan" in out

    def test_only_test_source_nodes(self, tmp_path, capsys):
        """Manifest with only test/source nodes — 0 models."""
        manifest_data = {
            "nodes": {
                "test.proj.not_null_id": {
                    "name": "not_null_id",
                    "config": {"materialized": "test"},
                },
                "source.proj.raw_users": {
                    "name": "raw_users",
                    "config": {},
                },
                "seed.proj.countries": {
                    "name": "countries",
                    "config": {},
                },
            },
            "child_map": {},
            "metadata": {},
        }
        _write_manifest(tmp_path, manifest_data)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "0 model(s) in manifest" in out

    def test_all_incremental_ignore(self, tmp_path, capsys):
        """All models are incremental+ignore -> cascade risk = 0."""
        nodes = {}
        for i in range(3):
            nid, node = _model_node(
                f"inc_{i}", materialization="incremental", on_schema_change="ignore"
            )
            nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # No "Cascade risk:" line should appear
        assert "Cascade risk:" not in out

    def test_on_schema_change_null_defaults_to_ignore(self, tmp_path, capsys):
        """on_schema_change: null should default to 'ignore'."""
        nodes = {}
        nid, node = _model_node("inc_null", materialization="incremental", on_schema_change=None)
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        # Should show "ignore" in the incremental osc breakdown
        assert "ignore" in out
        # No cascade risk
        assert "Cascade risk:" not in out

    def test_materialized_null_defaults_to_table(self, tmp_path, capsys):
        """materialized: null should default to 'table'."""
        manifest_data = {
            "nodes": {
                "model.proj.m": {
                    "name": "m",
                    "config": {"materialized": None},
                },
            },
            "child_map": {},
            "metadata": {},
        }
        _write_manifest(tmp_path, manifest_data)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "table" in out


class TestStatsWithDialect:
    """Scenario 6: Stats with --dialect flag."""

    def test_bigquery_dialect_select_star_detection(self, tmp_path, capsys):
        """SELECT * detection works with bigquery dialect."""
        nodes = {}
        nid, node = _model_node("bq_star", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("bq_explicit", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "bq_star": "SELECT * FROM `project.dataset.table`",
            "bq_explicit": "SELECT id, name FROM `project.dataset.table`",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(
            str(tmp_path), manifest=str(tmp_path / "manifest.json"), dialect="bigquery"
        )
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/2 models (50%)" in out

    def test_snowflake_dialect_select_star_detection(self, tmp_path, capsys):
        """SELECT * detection works with default snowflake dialect."""
        nodes = {}
        nid, node = _model_node("sf_star", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("sf_explicit", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "sf_star": 'SELECT * FROM "RAW"."SCHEMA"."TABLE"',
            "sf_explicit": 'SELECT ID, NAME FROM "RAW"."SCHEMA"."TABLE"',
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/2 models (50%)" in out

    def test_dialect_none_defaults_to_snowflake(self, tmp_path, capsys):
        """dialect=None should default to snowflake."""
        nodes = {}
        nid, node = _model_node("model_a", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "model_a": "SELECT * FROM raw.users",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"), dialect=None)
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 1/1 models (100%)" in out


class TestStatsWithCorruptManifest:
    """Scenario 7: Stats with corrupt manifest."""

    def test_invalid_json_exits_2(self, tmp_path, capsys):
        """manifest.json with invalid JSON should exit 2 with error message."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{invalid json!!!")

        args = _make_args(str(tmp_path), manifest=str(manifest_path))
        with pytest.raises(SystemExit) as exc:
            _do_stats(args)

        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Could not parse manifest.json" in err

    def test_missing_manifest_exits_2(self, tmp_path, capsys):
        """Non-existent manifest should exit 2."""
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "nonexistent_manifest.json"))
        with pytest.raises(SystemExit) as exc:
            _do_stats(args)

        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "manifest.json not found" in err


class TestStatsWithManifestFlag:
    """Scenario 8: Stats with --manifest flag."""

    def test_custom_manifest_path(self, tmp_path, capsys):
        """Custom manifest path should be used."""
        # Create manifest in a non-default location
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        nodes = {}
        nid, node = _model_node("custom_model", materialization="view")
        nodes[nid] = node
        manifest = _make_manifest(nodes)
        custom_manifest = custom_dir / "my_manifest.json"
        custom_manifest.write_text(json.dumps(manifest))

        args = _make_args(str(tmp_path), manifest=str(custom_manifest))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "view" in out

    def test_default_manifest_path_from_target(self, tmp_path, capsys):
        """Without --manifest, stats reads from target/manifest.json."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        nodes = {}
        nid, node = _model_node("default_model", materialization="table")
        nodes[nid] = node
        manifest = _make_manifest(nodes)
        (target_dir / "manifest.json").write_text(json.dumps(manifest))

        args = _make_args(str(tmp_path))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out


class TestStatsNoCompiledDir:
    """No compiled directory -> SELECT * section should be skipped."""

    def test_no_compiled_dir_skips_star_count(self, tmp_path, capsys):
        """When compiled SQL directory does not exist, SELECT * section is absent."""
        nodes = {}
        nid, node = _model_node("model_a", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        # Don't create target/compiled
        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "1 model(s) in manifest" in out
        assert "SELECT * usage" not in out


class TestStatsSelectStarZero:
    """All models have explicit columns — star count should be 0."""

    def test_zero_star_usage(self, tmp_path, capsys):
        """0 SELECT * models should display 0% usage and no fallback info."""
        nodes = {}
        nid, node = _model_node("m1", materialization="table")
        nodes[nid] = node
        nid, node = _model_node("m2", materialization="table")
        nodes[nid] = node

        manifest = _make_manifest(nodes)
        _write_manifest(tmp_path, manifest)

        sql_files = {
            "m1": "SELECT id, name FROM raw.users",
            "m2": "SELECT order_id, total FROM raw.orders",
        }
        _setup_compiled_dir(tmp_path, sql_files)

        args = _make_args(str(tmp_path), manifest=str(tmp_path / "manifest.json"))
        _do_stats(args)

        out = capsys.readouterr().out
        assert "SELECT * usage: 0/2 models (0%)" in out
        # No fallback info when no SELECT * models
        assert "Manifest column fallback" not in out
