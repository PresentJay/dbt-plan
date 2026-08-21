"""Tests for dbt Cloud / dbt CLI directory layout compatibility.

dbt Cloud and different dbt CLI versions produce slightly different compiled
output layouts.  These tests verify that _find_compiled_dir and
diff_compiled_dirs handle all known variants gracefully.
"""

from __future__ import annotations

import pytest

from dbt_plan.cli import _find_compiled_dir
from dbt_plan.diff import diff_compiled_dirs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sql(path, content="SELECT 1"):
    """Create a .sql file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Scenario 1: Standard dbt CLI layout (baseline)
# ---------------------------------------------------------------------------


class TestStandardCliLayout:
    """target/compiled/{project_name}/models/ — the most common layout."""

    def test_find_compiled_dir_returns_models(self, tmp_path):
        target = tmp_path / "target"
        models = target / "compiled" / "my_project" / "models" / "staging"
        _make_sql(models / "model.sql")

        result = _find_compiled_dir(target)
        assert result is not None
        assert result == target / "compiled" / "my_project" / "models"

    def test_find_compiled_dir_with_deep_model(self, tmp_path):
        target = tmp_path / "target"
        models = target / "compiled" / "my_project" / "models" / "staging" / "raw"
        _make_sql(models / "stg_orders.sql")

        result = _find_compiled_dir(target)
        assert result == target / "compiled" / "my_project" / "models"


# ---------------------------------------------------------------------------
# Scenario 2: Flat layout (some dbt versions)
# ---------------------------------------------------------------------------


class TestFlatLayout:
    """target/compiled/models/ — no project subdirectory."""

    def test_find_compiled_dir_flat(self, tmp_path):
        target = tmp_path / "target"
        models = target / "compiled" / "models"
        _make_sql(models / "model.sql")

        result = _find_compiled_dir(target)
        assert result is not None
        assert result == target / "compiled" / "models"

    def test_flat_takes_priority_over_project_subdir(self, tmp_path):
        """When both flat models/ and project/models/ exist, flat wins."""
        target = tmp_path / "target"
        flat_models = target / "compiled" / "models"
        _make_sql(flat_models / "flat_model.sql")

        proj_models = target / "compiled" / "my_project" / "models"
        _make_sql(proj_models / "proj_model.sql")

        result = _find_compiled_dir(target)
        # Flat layout check comes first in the implementation
        assert result == target / "compiled" / "models"


# ---------------------------------------------------------------------------
# Scenario 3: Nested model directories
# ---------------------------------------------------------------------------


class TestNestedModelDirectories:
    """Multiple subdirectories under models/ — all found by rglob."""

    def test_rglob_finds_all_nested_models(self, tmp_path):
        target = tmp_path / "target"
        proj_models = target / "compiled" / "proj" / "models"

        _make_sql(proj_models / "staging" / "raw" / "model_a.sql", "SELECT a FROM t")
        _make_sql(proj_models / "marts" / "core" / "model_b.sql", "SELECT b FROM t")
        _make_sql(proj_models / "intermediate" / "model_c.sql", "SELECT c FROM t")

        compiled_dir = _find_compiled_dir(target)
        assert compiled_dir is not None

        found = {f.stem for f in compiled_dir.rglob("*.sql")}
        assert found == {"model_a", "model_b", "model_c"}

    def test_diff_finds_all_nested_models(self, tmp_path):
        """diff_compiled_dirs finds models in arbitrarily nested subdirectories."""
        base = tmp_path / "base"
        _make_sql(base / "staging" / "model_a.sql", "SELECT old_a FROM t")
        _make_sql(base / "marts" / "model_b.sql", "SELECT old_b FROM t")

        current = tmp_path / "current"
        _make_sql(current / "staging" / "model_a.sql", "SELECT new_a FROM t")
        _make_sql(current / "marts" / "model_b.sql", "SELECT new_b FROM t")
        _make_sql(current / "intermediate" / "model_c.sql", "SELECT c FROM t")

        result = diff_compiled_dirs(base, current)
        names = sorted(d.model_name for d in result)
        assert names == ["model_a", "model_b", "model_c"]
        statuses = {d.model_name: d.status for d in result}
        assert statuses["model_a"] == "modified"
        assert statuses["model_b"] == "modified"
        assert statuses["model_c"] == "added"


# ---------------------------------------------------------------------------
# Scenario 4: Non-SQL files in compiled dir
# ---------------------------------------------------------------------------


class TestNonSqlFilesFiltered:
    """Only .sql files should be picked up; .py, .csv, .yml are ignored."""

    def test_diff_ignores_non_sql_files(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir(parents=True)
        (base / "model_a.sql").write_text("SELECT a FROM t")
        (base / "python_model.py").write_text("def model(dbt, session): pass")
        (base / "seeds.csv").write_text("id,name\n1,foo")
        (base / "schema.yml").write_text("version: 2")

        current = tmp_path / "current"
        current.mkdir(parents=True)
        (current / "model_a.sql").write_text("SELECT a, b FROM t")
        (current / "python_model.py").write_text("def model(dbt, session): return 1")
        (current / "seeds.csv").write_text("id,name\n1,bar")
        (current / "schema.yml").write_text("version: 2\nmodels: []")

        result = diff_compiled_dirs(base, current)
        names = [d.model_name for d in result]
        assert names == ["model_a"]

    def test_rglob_only_sql(self, tmp_path):
        """diff_compiled_dirs uses rglob('*.sql') which only matches .sql files."""
        target = tmp_path / "target"
        models = target / "compiled" / "proj" / "models"
        _make_sql(models / "real_model.sql")
        (models / "python_model.py").write_text("def model(): pass")
        models.mkdir(parents=True, exist_ok=True)
        (models / "schema.yml").write_text("version: 2")

        compiled_dir = _find_compiled_dir(target)
        assert compiled_dir is not None
        found = list(compiled_dir.rglob("*.sql"))
        assert len(found) == 1
        assert found[0].stem == "real_model"


# ---------------------------------------------------------------------------
# Scenario 5: Empty target/compiled/ (no project dirs, no models dir)
# ---------------------------------------------------------------------------


class TestEmptyCompiledDir:
    """Empty or missing compiled/ → _find_compiled_dir returns None."""

    def test_empty_compiled_returns_none(self, tmp_path):
        target = tmp_path / "target"
        compiled = target / "compiled"
        compiled.mkdir(parents=True)

        result = _find_compiled_dir(target)
        assert result is None

    def test_no_compiled_dir_returns_none(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir(parents=True)

        result = _find_compiled_dir(target)
        assert result is None

    def test_no_target_dir_returns_none(self, tmp_path):
        """Completely absent target directory → None."""
        result = _find_compiled_dir(tmp_path / "nonexistent_target")
        assert result is None


# ---------------------------------------------------------------------------
# Scenario 6: compiled/ has directories without models/ subdir
# ---------------------------------------------------------------------------


class TestNonModelDirectories:
    """Packages with only macros/ (no models/) should not be picked up."""

    def test_macros_only_package_not_picked(self, tmp_path):
        target = tmp_path / "target"
        macros = target / "compiled" / "some_package" / "macros"
        _make_sql(macros / "macro.sql", "{% macro foo() %}SELECT 1{% endmacro %}")

        result = _find_compiled_dir(target)
        assert result is None

    def test_macros_package_ignored_when_project_exists(self, tmp_path):
        """A package with only macros/ should not interfere with real project."""
        target = tmp_path / "target"

        # Package with only macros
        macros = target / "compiled" / "dbt_utils" / "macros"
        _make_sql(macros / "generate_surrogate_key.sql")

        # Real project with models
        models = target / "compiled" / "my_project" / "models"
        _make_sql(models / "dim_users.sql")

        result = _find_compiled_dir(target)
        assert result is not None
        assert result == target / "compiled" / "my_project" / "models"

    def test_multiple_projects_with_models_raises(self, tmp_path):
        """Multiple project directories with models/ → ValueError."""
        target = tmp_path / "target"
        _make_sql(target / "compiled" / "project_a" / "models" / "m.sql")
        _make_sql(target / "compiled" / "project_b" / "models" / "m.sql")

        with pytest.raises(ValueError, match="Multiple dbt projects"):
            _find_compiled_dir(target)


# ---------------------------------------------------------------------------
# Scenario 7: Very deep nesting
# ---------------------------------------------------------------------------


class TestVeryDeepNesting:
    """Deeply nested model paths — rglob should find them."""

    def test_deep_nesting_found(self, tmp_path):
        target = tmp_path / "target"
        deep_dir = target / "compiled" / "proj" / "models" / "a" / "b" / "c" / "d" / "e" / "f"
        _make_sql(deep_dir / "deep_model.sql", "SELECT id FROM deep_table")

        compiled_dir = _find_compiled_dir(target)
        assert compiled_dir is not None

        found = list(compiled_dir.rglob("*.sql"))
        assert len(found) == 1
        assert found[0].stem == "deep_model"

    def test_deep_nesting_in_diff(self, tmp_path):
        """diff_compiled_dirs finds deeply nested models."""
        base = tmp_path / "base" / "a" / "b" / "c" / "d" / "e" / "f"
        _make_sql(base / "deep_model.sql", "SELECT old FROM t")

        current = tmp_path / "current" / "a" / "b" / "c" / "d" / "e" / "f"
        _make_sql(current / "deep_model.sql", "SELECT new FROM t")

        result = diff_compiled_dirs(tmp_path / "base", tmp_path / "current")
        assert len(result) == 1
        assert result[0].model_name == "deep_model"
        assert result[0].status == "modified"


# ---------------------------------------------------------------------------
# Scenario 8: Model with special characters in path
# ---------------------------------------------------------------------------


class TestSpecialCharactersInPath:
    """Dashes and other characters in directory/file names."""

    def test_dashes_in_directory_and_filename(self, tmp_path):
        target = tmp_path / "target"
        src_dir = target / "compiled" / "proj" / "models" / "staging" / "my-source"
        _make_sql(src_dir / "model_with_dashes.sql", "SELECT id FROM t")

        compiled_dir = _find_compiled_dir(target)
        assert compiled_dir is not None

        found = list(compiled_dir.rglob("*.sql"))
        assert len(found) == 1
        assert found[0].stem == "model_with_dashes"

    def test_dashes_in_diff(self, tmp_path):
        """Model name extraction (stem) works for files in dashed directories."""
        base = tmp_path / "base" / "staging" / "my-source"
        _make_sql(base / "model_with_dashes.sql", "SELECT old FROM t")

        current = tmp_path / "current" / "staging" / "my-source"
        _make_sql(current / "model_with_dashes.sql", "SELECT new FROM t")

        result = diff_compiled_dirs(tmp_path / "base", tmp_path / "current")
        assert len(result) == 1
        assert result[0].model_name == "model_with_dashes"
        assert result[0].status == "modified"

    def test_underscores_and_numbers(self, tmp_path):
        """Model names with underscores and numbers are correctly extracted."""
        base = tmp_path / "base"
        base.mkdir(parents=True)
        (base / "stg_orders_v2.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir(parents=True)
        (current / "stg_orders_v2.sql").write_text("SELECT b FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].model_name == "stg_orders_v2"
