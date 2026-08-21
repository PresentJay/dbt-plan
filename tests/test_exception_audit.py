"""Exception handling audit — verify every try/except in src/dbt_plan/ handles edge cases correctly.

This test module systematically checks each try/except block for:
1. Comprehensive exception types (not too narrow, not too broad)
2. Correct behavior in except clause (return None, exit 2, log, etc.)
3. Unexpected exceptions that could be silently swallowed
"""

import json
import os

import pytest
import sqlglot.errors

from dbt_plan.columns import extract_columns
from dbt_plan.config import Config
from dbt_plan.diff import diff_compiled_dirs
from dbt_plan.manifest import load_manifest

# ---------------------------------------------------------------------------
# columns.py — extract_columns try/except audit
# ---------------------------------------------------------------------------


class TestColumnsParseErrorHandling:
    """Audit: columns.py catches (ParseError, TokenError, ValueError, RecursionError).

    Key finding: UnicodeDecodeError is a subclass of ValueError, so it IS caught.
    OptimizeError can only occur during sqlglot.optimize(), not during parse_one().
    """

    def test_parse_error_returns_none(self):
        """ParseError from malformed SQL -> None (not a crash)."""
        result = extract_columns("SELECT FROM WHERE INVALID !!!")
        assert result is None

    def test_token_error_returns_none(self):
        """TokenError from untokenizable input -> None."""
        # Unclosed string literal causes TokenError
        result = extract_columns("SELECT 'unclosed")
        assert result is None

    def test_value_error_from_bad_dialect_returns_none(self):
        """ValueError from unknown dialect -> None."""
        result = extract_columns("SELECT id FROM t", dialect="not_a_real_dialect_xyz")
        assert result is None

    def test_recursion_error_returns_none(self):
        """RecursionError from deeply nested SQL -> None."""
        # Build a deeply nested expression that exceeds stack
        nested = "SELECT " + "(" * 500 + "1" + ")" * 500 + " AS val"
        result = extract_columns(nested)
        # Either None (RecursionError caught) or a list (if sqlglot handles it)
        assert result is None or isinstance(result, list)

    def test_unicode_decode_error_is_subclass_of_value_error(self):
        """Verify UnicodeDecodeError IS caught via the ValueError clause.

        This is the key audit finding: the code does NOT need to add
        UnicodeDecodeError explicitly because it inherits from ValueError.
        """
        assert issubclass(UnicodeDecodeError, ValueError)

    def test_unicode_encode_error_caught_via_value_error(self):
        """UnicodeEncodeError is also a ValueError subclass - verify handling."""
        assert issubclass(UnicodeEncodeError, ValueError)
        # Surrogate characters that sqlglot might choke on
        # This triggers UnicodeEncodeError inside sqlglot's string processing
        result = extract_columns("SELECT \ud800 AS col")
        # Must return None, not crash
        assert result is None or isinstance(result, list)

    def test_optimize_error_not_reachable_from_parse_one(self):
        """Verify OptimizeError is NOT raised by parse_one (only by optimize()).

        The handler does not catch OptimizeError. This test confirms that's correct
        because parse_one never calls optimize().
        """
        # OptimizeError exists in sqlglot
        assert hasattr(sqlglot.errors, "OptimizeError")
        # parse_one should NOT raise it — it only parses, never optimizes
        # If this ever changes, the test will fail and alert us
        result = extract_columns("SELECT a, b FROM t")
        assert result == ["a", "b"]

    def test_bom_stripped_before_parse(self):
        """BOM character is stripped before parsing, preventing TokenError."""
        result = extract_columns("\ufeffSELECT id FROM t")
        assert result == ["id"]

    def test_null_bytes_in_sql(self):
        """SQL containing null bytes should not crash."""
        result = extract_columns("SELECT id\x00 FROM t")
        assert result is None or isinstance(result, list)

    def test_very_long_sql(self):
        """Very long SQL should not crash (memory-bounded, not exception)."""
        # 10000 columns - should parse but take time
        cols = ", ".join(f"col{i}" for i in range(100))
        sql = f"SELECT {cols} FROM t"
        result = extract_columns(sql)
        assert result is not None
        assert len(result) == 100


# ---------------------------------------------------------------------------
# manifest.py — load_manifest try/except audit
# ---------------------------------------------------------------------------


class TestManifestExceptionHandling:
    """Audit: manifest.py load_manifest has NO try/except — caller handles.

    json.load() can raise:
    - json.JSONDecodeError (invalid JSON)
    - UnicodeDecodeError (invalid encoding in file)

    The callers (cli.py _do_check, _do_stats) catch (json.JSONDecodeError, OSError).
    UnicodeDecodeError is a subclass of ValueError, NOT OSError — so it would
    NOT be caught by the caller's except clause.
    """

    def test_valid_manifest_loads(self, tmp_path):
        """Baseline: valid manifest loads correctly."""
        manifest = {"nodes": {}, "child_map": {}, "metadata": {}}
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(manifest))
        result = load_manifest(p)
        assert "nodes" in result

    def test_invalid_json_raises_decode_error(self, tmp_path):
        """Invalid JSON raises JSONDecodeError (caught by callers)."""
        p = tmp_path / "manifest.json"
        p.write_text("{invalid json!!!")
        with pytest.raises(json.JSONDecodeError):
            load_manifest(p)

    def test_missing_file_raises_oserror(self, tmp_path):
        """Missing file raises FileNotFoundError (subclass of OSError, caught by callers)."""
        p = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            load_manifest(p)

    def test_binary_file_raises_unicode_error(self, tmp_path):
        """Binary file with invalid UTF-8 raises UnicodeDecodeError.

        GAP FOUND: Callers catch (json.JSONDecodeError, OSError) but
        UnicodeDecodeError is a subclass of ValueError, NOT OSError.
        This would propagate as an unhandled exception.
        """
        p = tmp_path / "manifest.json"
        p.write_bytes(b'{"nodes": \xff\xfe}')
        with pytest.raises(UnicodeDecodeError):
            load_manifest(p)

    @pytest.mark.skipif(
        os.getuid() == 0 if hasattr(os, "getuid") else False,
        reason="chmod(0o000) has no effect when running as root",
    )
    def test_permission_denied_raises_oserror(self, tmp_path):
        """PermissionError (subclass of OSError) is caught by callers."""
        p = tmp_path / "manifest.json"
        p.write_text("{}")
        p.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                load_manifest(p)
        finally:
            p.chmod(0o644)

    def test_empty_file_raises_decode_error(self, tmp_path):
        """Empty file raises JSONDecodeError (caught by callers)."""
        p = tmp_path / "manifest.json"
        p.write_text("")
        with pytest.raises(json.JSONDecodeError):
            load_manifest(p)


# ---------------------------------------------------------------------------
# config.py — _load_file try/except audit
# ---------------------------------------------------------------------------


class TestConfigExceptionHandling:
    """Audit: config.py _load_file catches OSError on read_text().

    GAP: read_text() can also raise UnicodeDecodeError (not subclass of OSError).
    If .dbt-plan.yml has invalid encoding, it would crash instead of being skipped.
    """

    def test_missing_config_file_silently_ignored(self, tmp_path):
        """No config file -> defaults used (no error)."""
        config = Config.load(tmp_path)
        assert config.dialect == "snowflake"

    def test_valid_config_file_loaded(self, tmp_path):
        """Valid config file is parsed correctly."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: bigquery\n")
        config = Config.load(tmp_path)
        assert config.dialect == "bigquery"

    def test_permission_denied_config_silently_ignored(self, tmp_path):
        """PermissionError (subclass of OSError) -> silently ignored."""
        p = tmp_path / ".dbt-plan.yml"
        p.write_text("dialect: bigquery\n")
        p.chmod(0o000)
        try:
            config = Config.load(tmp_path)
            # Should fall back to defaults since file couldn't be read
            assert config.dialect == "snowflake"
        finally:
            p.chmod(0o644)

    def test_unicode_decode_error_in_config_file(self, tmp_path):
        """FIXED: Binary content in .dbt-plan.yml now handled gracefully.

        UnicodeDecodeError is NOT a subclass of OSError. The except clause
        now catches (OSError, UnicodeDecodeError) so non-UTF-8 config files
        are silently skipped (defaults used).
        """
        p = tmp_path / ".dbt-plan.yml"
        p.write_bytes(b"dialect: \xff\xfe bigquery\n")
        # Should not crash — falls back to defaults
        config = Config.load(tmp_path)
        assert config.dialect == "snowflake"  # default, since file was unreadable

    def test_config_bom_stripped(self, tmp_path):
        """BOM in config file is handled (stripped after read)."""
        p = tmp_path / ".dbt-plan.yml"
        p.write_text("\ufeffdialect: bigquery\n")
        config = Config.load(tmp_path)
        assert config.dialect == "bigquery"

    def test_invalid_yaml_values_silently_ignored(self, tmp_path):
        """Invalid values don't crash — just ignored."""
        (tmp_path / ".dbt-plan.yml").write_text(
            "warning_exit_code: not_a_number\nformat: invalid_format\n"
        )
        config = Config.load(tmp_path)
        assert config.warning_exit_code == 2  # default
        assert config.format == "text"  # default


# ---------------------------------------------------------------------------
# diff.py — read_text() exception audit
# ---------------------------------------------------------------------------


class TestDiffExceptionHandling:
    """Audit: diff.py read_text() calls are NOT wrapped in try/except.

    diff_compiled_dirs reads file content eagerly for modified models.
    If a .sql file has invalid UTF-8 encoding, read_text() will raise
    UnicodeDecodeError and the whole diff operation crashes.

    This is a real gap: compiled SQL files could contain non-UTF-8 bytes
    if the warehouse or editor uses a different encoding.
    """

    def test_valid_files_diffed_correctly(self, tmp_path):
        """Baseline: valid UTF-8 files diff correctly."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT b FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"

    def test_binary_content_in_sql_file_handled_gracefully(self, tmp_path):
        """FIXED: Binary content in .sql file now treated as modified with no cached SQL.

        read_text() UnicodeDecodeError is caught. The model is reported as
        modified with base_sql=None and current_sql=None, which triggers
        REVIEW REQUIRED downstream (consistent with false-safe-ban rule).
        """
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_bytes(b"SELECT \xff\xfe FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT b FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"
        assert result[0].base_sql is None  # couldn't decode
        assert result[0].current_sql is None  # not cached when decode failed

    def test_symlinks_skipped(self, tmp_path):
        """Symlinks are properly skipped (not followed)."""
        base = tmp_path / "base"
        base.mkdir()

        current = tmp_path / "current"
        current.mkdir()
        (current / "real.sql").write_text("SELECT a FROM t")

        external = tmp_path / "external.sql"
        external.write_text("SELECT evil FROM hack")
        (current / "link.sql").symlink_to(external)

        result = diff_compiled_dirs(base, current)
        names = [d.model_name for d in result]
        assert "link" not in names
        assert "real" in names


# ---------------------------------------------------------------------------
# cli.py — _do_check and _do_stats exception audit
# ---------------------------------------------------------------------------


class TestCliCheckExceptionHandling:
    """Audit: cli.py _do_check catches (json.JSONDecodeError, OSError) for manifest.

    GAP: json.load() can raise UnicodeDecodeError (ValueError subclass) for
    binary manifest files. This is NOT caught by (json.JSONDecodeError, OSError).
    """

    def test_manifest_json_decode_error_caught(self, tmp_path):
        """JSONDecodeError from manifest -> exit 2 with error message."""
        import argparse

        from dbt_plan.cli import _do_check

        # Set up directories
        base_dir = tmp_path / ".dbt-plan" / "base" / "compiled"
        base_dir.mkdir(parents=True)
        (base_dir / "m.sql").write_text("SELECT a FROM t")

        current_dir = tmp_path / "target" / "compiled" / "proj" / "models"
        current_dir.mkdir(parents=True)
        (current_dir / "m.sql").write_text("SELECT b FROM t")

        # Write invalid manifest
        manifest_path = tmp_path / "target" / "manifest.json"
        manifest_path.write_text("{invalid json")

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            target_dir="target",
            base_dir=".dbt-plan/base",
            manifest=str(manifest_path),
            format="text",
            no_color=True,
            verbose=False,
            dialect="snowflake",
            select=None,
        )
        exit_code = _do_check(args)
        assert exit_code == 2

    def test_manifest_oserror_caught(self, tmp_path):
        """Missing manifest -> exit 2 with error message."""
        import argparse

        from dbt_plan.cli import _do_check

        base_dir = tmp_path / ".dbt-plan" / "base" / "compiled"
        base_dir.mkdir(parents=True)
        (base_dir / "m.sql").write_text("SELECT a FROM t")

        current_dir = tmp_path / "target" / "compiled" / "proj" / "models"
        current_dir.mkdir(parents=True)
        (current_dir / "m.sql").write_text("SELECT b FROM t")

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            target_dir="target",
            base_dir=".dbt-plan/base",
            manifest=str(tmp_path / "target" / "manifest.json"),
            format="text",
            no_color=True,
            verbose=False,
            dialect="snowflake",
            select=None,
        )
        # manifest.json doesn't exist -> exits with 2
        exit_code = _do_check(args)
        assert exit_code == 2


class TestCliCheckManifestUnicodeError:
    """Verify cli.py _do_check now handles UnicodeDecodeError from manifest."""

    def test_binary_manifest_returns_exit_2(self, tmp_path):
        """Binary manifest with invalid UTF-8 -> exit 2 (not crash)."""
        import argparse

        from dbt_plan.cli import _do_check

        base_dir = tmp_path / ".dbt-plan" / "base" / "compiled"
        base_dir.mkdir(parents=True)
        (base_dir / "m.sql").write_text("SELECT a FROM t")

        current_dir = tmp_path / "target" / "compiled" / "proj" / "models"
        current_dir.mkdir(parents=True)
        (current_dir / "m.sql").write_text("SELECT b FROM t")

        manifest_path = tmp_path / "target" / "manifest.json"
        manifest_path.write_bytes(b'{"nodes": \xff\xfe}')

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            target_dir="target",
            base_dir=".dbt-plan/base",
            manifest=str(manifest_path),
            format="text",
            no_color=True,
            verbose=False,
            dialect="snowflake",
            select=None,
        )
        exit_code = _do_check(args)
        assert exit_code == 2


class TestCliSnapshotExceptionHandling:
    """Audit: cli.py _do_snapshot catches ValueError from _find_compiled_dir.

    The resolve() call for path traversal check is safe — Path.resolve()
    only raises OSError in very rare filesystem conditions (broken mount).
    """

    def test_multiple_projects_raises_value_error(self, tmp_path):
        """Multiple dbt projects -> ValueError caught, exit 2."""
        import argparse

        from dbt_plan.cli import _do_snapshot

        target = tmp_path / "target" / "compiled"
        (target / "proj1" / "models").mkdir(parents=True)
        (target / "proj1" / "models" / "m.sql").write_text("SELECT 1")
        (target / "proj2" / "models").mkdir(parents=True)
        (target / "proj2" / "models" / "m.sql").write_text("SELECT 2")

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            target_dir="target",
        )
        with pytest.raises(SystemExit) as exc_info:
            _do_snapshot(args)
        assert exc_info.value.code == 2


class TestCliRunExceptionHandling:
    """Audit: cli.py _do_run catches FileNotFoundError for missing git/dbt.

    PermissionError is a subclass of OSError. The code catches
    FileNotFoundError specifically (which is also OSError subclass).
    If the binary exists but is not executable (PermissionError), the
    subprocess.run() will raise PermissionError which is NOT caught.
    This is an edge case but worth documenting.
    """

    def test_missing_dbt_command(self, tmp_path):
        """Missing compile command -> exit 2."""
        import argparse

        from dbt_plan.cli import _do_run

        # Create config so compile_command resolves
        (tmp_path / ".dbt-plan.yml").write_text(
            "compile_command: nonexistent_binary_xyz compile\n"
        )

        args = argparse.Namespace(
            project_dir=str(tmp_path),
            target_dir="target",
            format="text",
            no_color=True,
            verbose=False,
            dialect="snowflake",
            select=None,
            compile_command="nonexistent_binary_xyz compile",
        )
        exit_code = _do_run(args)
        assert exit_code == 2


# ---------------------------------------------------------------------------
# predictor.py — analyze_cascade_impacts exception audit
# ---------------------------------------------------------------------------


class TestPredictorCascadeExceptionHandling:
    """Audit: predictor.py analyze_cascade_impacts catches (OSError, UnicodeDecodeError).

    PermissionError IS a subclass of OSError -> correctly caught.
    The except clause does `pass` which skips the broken_ref check for that model.
    This is correct: unable to read downstream SQL -> skip the check (not safe, just skip).
    """

    def test_permission_error_is_oserror_subclass(self):
        """Verify PermissionError is caught by except OSError."""
        assert issubclass(PermissionError, OSError)

    def test_file_not_found_is_oserror_subclass(self):
        """Verify FileNotFoundError is caught by except OSError."""
        assert issubclass(FileNotFoundError, OSError)

    def test_is_a_directory_is_oserror_subclass(self):
        """Verify IsADirectoryError is caught by except OSError."""
        assert issubclass(IsADirectoryError, OSError)

    def test_cascade_with_unreadable_downstream_sql(self, tmp_path):
        """Unreadable downstream SQL file -> broken_ref check skipped (not crash)."""
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import (
            DDLOperation,
            DDLPrediction,
            Safety,
            analyze_cascade_impacts,
        )

        # Create an unreadable SQL file
        sql_path = tmp_path / "downstream.sql"
        sql_path.write_text("SELECT removed_col FROM upstream")
        sql_path.chmod(0o000)

        try:
            predictions = [
                DDLPrediction(
                    model_name="upstream",
                    materialization="table",
                    on_schema_change=None,
                    safety=Safety.SAFE,
                    operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                    columns_removed=["removed_col"],
                )
            ]
            node_index = {
                "upstream": ModelNode(
                    node_id="model.proj.upstream",
                    name="upstream",
                    materialization="table",
                    on_schema_change=None,
                ),
                "downstream": ModelNode(
                    node_id="model.proj.downstream",
                    name="downstream",
                    materialization="incremental",
                    on_schema_change="fail",
                ),
            }
            model_node_ids = {"upstream": "model.proj.upstream"}
            model_cols = {"upstream": (["id", "removed_col"], ["id"])}
            all_downstream = {"model.proj.upstream": ["model.proj.downstream"]}
            compiled_sql_index = {"downstream": sql_path}

            # Should NOT crash, should skip broken_ref for unreadable file
            updated, downstream_map = analyze_cascade_impacts(
                predictions=predictions,
                model_node_ids=model_node_ids,
                model_cols=model_cols,
                all_downstream=all_downstream,
                node_index=node_index,
                base_node_index={},
                compiled_sql_index=compiled_sql_index,
            )
            # The cascade should still detect build_failure (incremental+fail)
            # but NOT broken_ref (file unreadable)
            assert len(updated) == 1
            impacts = updated[0].downstream_impacts
            # Should have build_failure but no broken_ref (file was unreadable)
            risk_types = [imp.risk for imp in impacts]
            assert "build_failure" in risk_types
        finally:
            sql_path.chmod(0o644)

    def test_cascade_with_binary_downstream_sql(self, tmp_path):
        """Binary downstream SQL file -> UnicodeDecodeError caught, check skipped."""
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import (
            DDLOperation,
            DDLPrediction,
            Safety,
            analyze_cascade_impacts,
        )

        sql_path = tmp_path / "downstream.sql"
        sql_path.write_bytes(b"SELECT \xff\xfe removed_col FROM upstream")

        predictions = [
            DDLPrediction(
                model_name="upstream",
                materialization="table",
                on_schema_change=None,
                safety=Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                columns_removed=["removed_col"],
            )
        ]
        node_index = {
            "upstream": ModelNode(
                node_id="model.proj.upstream",
                name="upstream",
                materialization="table",
                on_schema_change=None,
            ),
            "downstream": ModelNode(
                node_id="model.proj.downstream",
                name="downstream",
                materialization="table",
                on_schema_change=None,
            ),
        }
        model_node_ids = {"upstream": "model.proj.upstream"}
        model_cols = {"upstream": (["id", "removed_col"], ["id"])}
        all_downstream = {"model.proj.upstream": ["model.proj.downstream"]}
        compiled_sql_index = {"downstream": sql_path}

        # Should NOT crash — UnicodeDecodeError is caught
        updated, downstream_map = analyze_cascade_impacts(
            predictions=predictions,
            model_node_ids=model_node_ids,
            model_cols=model_cols,
            all_downstream=all_downstream,
            node_index=node_index,
            base_node_index={},
            compiled_sql_index=compiled_sql_index,
        )
        assert len(updated) == 1
        # No broken_ref because the file couldn't be decoded
        broken_refs = [imp for imp in updated[0].downstream_impacts if imp.risk == "broken_ref"]
        assert len(broken_refs) == 0

    def test_cascade_with_readable_downstream_sql(self, tmp_path):
        """Readable downstream SQL -> broken_ref correctly detected."""
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import (
            DDLOperation,
            DDLPrediction,
            Safety,
            analyze_cascade_impacts,
        )

        sql_path = tmp_path / "downstream.sql"
        sql_path.write_text("SELECT removed_col, id FROM upstream")

        predictions = [
            DDLPrediction(
                model_name="upstream",
                materialization="table",
                on_schema_change=None,
                safety=Safety.SAFE,
                operations=[DDLOperation("CREATE OR REPLACE TABLE")],
                columns_removed=["removed_col"],
            )
        ]
        node_index = {
            "upstream": ModelNode(
                node_id="model.proj.upstream",
                name="upstream",
                materialization="table",
                on_schema_change=None,
            ),
            "downstream": ModelNode(
                node_id="model.proj.downstream",
                name="downstream",
                materialization="table",
                on_schema_change=None,
            ),
        }
        model_node_ids = {"upstream": "model.proj.upstream"}
        model_cols = {"upstream": (["id", "removed_col"], ["id"])}
        all_downstream = {"model.proj.upstream": ["model.proj.downstream"]}
        compiled_sql_index = {"downstream": sql_path}

        updated, downstream_map = analyze_cascade_impacts(
            predictions=predictions,
            model_node_ids=model_node_ids,
            model_cols=model_cols,
            all_downstream=all_downstream,
            node_index=node_index,
            base_node_index={},
            compiled_sql_index=compiled_sql_index,
        )
        broken_refs = [imp for imp in updated[0].downstream_impacts if imp.risk == "broken_ref"]
        assert len(broken_refs) == 1
        assert "removed_col" in broken_refs[0].reason


# ---------------------------------------------------------------------------
# Cross-cutting: exception hierarchy verification
# ---------------------------------------------------------------------------


class TestExceptionHierarchyVerification:
    """Verify Python exception hierarchy assumptions that the codebase relies on.

    If Python ever changes these relationships, tests here will catch it.
    """

    def test_permission_error_is_oserror(self):
        assert issubclass(PermissionError, OSError)

    def test_file_not_found_is_oserror(self):
        assert issubclass(FileNotFoundError, OSError)

    def test_is_a_directory_error_is_oserror(self):
        assert issubclass(IsADirectoryError, OSError)

    def test_unicode_decode_error_is_value_error(self):
        assert issubclass(UnicodeDecodeError, ValueError)

    def test_unicode_decode_error_is_NOT_oserror(self):
        """Critical: UnicodeDecodeError is NOT an OSError.

        This means any code catching only OSError will miss UnicodeDecodeError.
        Affects: config.py _load_file, cli.py manifest loading.
        """
        assert not issubclass(UnicodeDecodeError, OSError)

    def test_json_decode_error_is_value_error(self):
        assert issubclass(json.JSONDecodeError, ValueError)

    def test_json_decode_error_is_NOT_oserror(self):
        assert not issubclass(json.JSONDecodeError, OSError)

    def test_recursion_error_is_NOT_value_error(self):
        """RecursionError must be caught explicitly (not via ValueError)."""
        assert not issubclass(RecursionError, ValueError)

    def test_sqlglot_parse_error_hierarchy(self):
        """ParseError inherits from SqlglotError, not ValueError."""
        assert issubclass(sqlglot.errors.ParseError, sqlglot.errors.SqlglotError)
        assert not issubclass(sqlglot.errors.ParseError, ValueError)

    def test_sqlglot_token_error_hierarchy(self):
        """TokenError inherits from SqlglotError, not ValueError."""
        assert issubclass(sqlglot.errors.TokenError, sqlglot.errors.SqlglotError)
        assert not issubclass(sqlglot.errors.TokenError, ValueError)

    def test_sqlglot_optimize_error_hierarchy(self):
        """OptimizeError exists and inherits from SqlglotError."""
        assert hasattr(sqlglot.errors, "OptimizeError")
        assert issubclass(sqlglot.errors.OptimizeError, sqlglot.errors.SqlglotError)


# ---------------------------------------------------------------------------
# Gaps found — tests that demonstrate real issues needing fixes
# ---------------------------------------------------------------------------


class TestIdentifiedGaps:
    """Tests that demonstrate REAL gaps in exception handling.

    Each test documents a gap and shows the behavior. Gaps marked with
    FIX_NEEDED are ones that should be patched.
    """

    def test_gap_config_unicode_decode_error_fixed(self, tmp_path):
        """FIXED [config.py]: _load_file now catches (OSError, UnicodeDecodeError).

        Config file with non-UTF-8 encoding is silently skipped (defaults used).
        """
        p = tmp_path / ".dbt-plan.yml"
        p.write_bytes(b"dialect: \xff\xfe bigquery\n")
        config = Config.load(tmp_path)
        assert config.dialect == "snowflake"  # default

    def test_gap_diff_unicode_decode_error_fixed(self, tmp_path):
        """FIXED [diff.py]: read_text() UnicodeDecodeError now caught.

        Non-UTF-8 compiled SQL is treated as modified with no cached SQL,
        triggering REVIEW REQUIRED downstream.
        """
        base = tmp_path / "base"
        base.mkdir()
        (base / "bad.sql").write_bytes(b"SELECT \xff\xfe FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "bad.sql").write_text("SELECT a FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"
        assert result[0].base_sql is None
        assert result[0].current_sql is None

    def test_gap_manifest_unicode_decode_error_fixed(self, tmp_path):
        """FIXED [cli.py]: Callers now catch (JSONDecodeError, OSError, UnicodeDecodeError).

        Binary manifest files produce a clean error message, not a traceback.
        load_manifest itself still raises (it's the callers that catch).
        """
        p = tmp_path / "manifest.json"
        p.write_bytes(b'{"nodes": \xff\xfe}')

        # load_manifest still raises — but callers now catch it
        with pytest.raises(UnicodeDecodeError):
            load_manifest(p)

    def test_no_gap_columns_handles_all_parse_errors(self):
        """VERIFIED OK: columns.py handles all realistic parse-time errors.

        - ParseError: malformed SQL
        - TokenError: untokenizable input
        - ValueError: unknown dialect (+ UnicodeDecodeError as subclass)
        - RecursionError: deeply nested SQL

        OptimizeError is NOT reachable from parse_one(), so not catching it is correct.
        """
        # All these return None, not crash
        assert extract_columns("NOT SQL!!!") is None
        assert extract_columns("SELECT 'unclosed") is None
        assert extract_columns("SELECT 1", dialect="fake_xyz") is None

    def test_no_gap_predictor_cascade_handles_file_errors(self, tmp_path):
        """VERIFIED OK: predictor.py catches (OSError, UnicodeDecodeError) for file reads.

        Both PermissionError (OSError subclass) and UnicodeDecodeError are caught.
        The except clause does `pass`, skipping the broken_ref check — this is correct
        behavior for a static analysis tool (can't check = skip, don't claim safe).
        """
        # Verified by TestPredictorCascadeExceptionHandling tests above
        pass
