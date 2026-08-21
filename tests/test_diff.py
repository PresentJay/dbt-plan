"""Tests for diff_compiled_dirs — compiled SQL directory comparison."""

import pytest

from dbt_plan.diff import diff_compiled_dirs


class TestDiffCompiledDirs:
    def test_modified_model(self, tmp_path):
        """Same filename, different content → status='modified'."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "my_model.sql").write_text("SELECT a, b FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "my_model.sql").write_text("SELECT a, c FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].model_name == "my_model"
        assert result[0].status == "modified"
        assert result[0].base_path is not None
        assert result[0].current_path is not None

    def test_added_model(self, tmp_path):
        """File in current only → status='added'."""
        base = tmp_path / "base"
        base.mkdir()

        current = tmp_path / "current"
        current.mkdir()
        (current / "new_model.sql").write_text("SELECT id FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].model_name == "new_model"
        assert result[0].status == "added"
        assert result[0].base_path is None
        assert result[0].current_path is not None

    def test_removed_model(self, tmp_path):
        """File in base only → status='removed'."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "old_model.sql").write_text("SELECT id FROM t")

        current = tmp_path / "current"
        current.mkdir()

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].model_name == "old_model"
        assert result[0].status == "removed"
        assert result[0].base_path is not None
        assert result[0].current_path is None

    def test_unchanged_model_excluded(self, tmp_path):
        """Same filename, same content → not in result."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "stable.sql").write_text("SELECT id FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "stable.sql").write_text("SELECT id FROM t")

        result = diff_compiled_dirs(base, current)
        assert result == []


class TestDuplicateModelDetection:
    def test_duplicate_in_base_raises_valueerror(self, tmp_path):
        """Duplicate model name in base directory → ValueError."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "my_model.sql").write_text("SELECT 1")
        sub = base / "subdir"
        sub.mkdir()
        (sub / "my_model.sql").write_text("SELECT 2")

        current = tmp_path / "current"
        current.mkdir()

        with pytest.raises(ValueError, match="Duplicate model name 'my_model'"):
            diff_compiled_dirs(base, current)

    def test_duplicate_in_current_raises_valueerror(self, tmp_path):
        """Duplicate model name in current directory → ValueError."""
        base = tmp_path / "base"
        base.mkdir()

        current = tmp_path / "current"
        current.mkdir()
        (current / "my_model.sql").write_text("SELECT 1")
        sub = current / "subdir"
        sub.mkdir()
        (sub / "my_model.sql").write_text("SELECT 2")

        with pytest.raises(ValueError, match="Duplicate model name 'my_model'"):
            diff_compiled_dirs(base, current)


class TestFileSizeFastPath:
    def test_same_size_different_content_detected(self, tmp_path):
        """Same file size but different content → still detected as modified."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_text("SELECT aaa FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT bbb FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"

    def test_different_size_detected_as_modified(self, tmp_path):
        """Different file sizes → detected as modified via fast path."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT a, b, c FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"


class TestCRLFAndBOMNormalization:
    """Cross-platform line ending and BOM differences should not cause false diffs."""

    def test_crlf_vs_lf_not_flagged(self, tmp_path):
        """Same SQL with CRLF vs LF line endings → no diff."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_bytes(b"SELECT a,\r\nb FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_bytes(b"SELECT a,\nb FROM t")

        result = diff_compiled_dirs(base, current)
        assert result == []

    def test_bom_vs_no_bom_not_flagged(self, tmp_path):
        """Same SQL with vs without BOM → no diff."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_bytes(b"\xef\xbb\xbfSELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_bytes(b"SELECT a FROM t")

        result = diff_compiled_dirs(base, current)
        assert result == []

    def test_real_content_change_with_crlf_still_detected(self, tmp_path):
        """Actual content change is still detected even with CRLF."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_bytes(b"SELECT a\r\nFROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_bytes(b"SELECT b\nFROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].status == "modified"


class TestSymlinkSkipping:
    """Symlinked SQL files should be skipped to prevent reading outside the project."""

    def test_symlink_sql_file_skipped(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        (base / "real.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "real.sql").write_text("SELECT b FROM t")

        # Create a symlink to an external file
        external = tmp_path / "external.txt"
        external.write_text("not sql")
        (current / "sneaky.sql").symlink_to(external)

        result = diff_compiled_dirs(base, current)
        # sneaky.sql should be skipped, only real.sql should appear
        names = [d.model_name for d in result]
        assert "sneaky" not in names
        assert "real" in names


class TestNonexistentDirectory:
    """Nonexistent directories should raise FileNotFoundError, not silently return empty."""

    def test_nonexistent_base_raises(self, tmp_path):
        current = tmp_path / "current"
        current.mkdir()
        with pytest.raises(FileNotFoundError, match="Base directory"):
            diff_compiled_dirs(tmp_path / "nonexistent", current)

    def test_nonexistent_current_raises(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(FileNotFoundError, match="Current directory"):
            diff_compiled_dirs(base, tmp_path / "nonexistent")


class TestSQLCaching:
    def test_modified_model_caches_sql_content(self, tmp_path):
        """Modified model has base_sql and current_sql populated."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT b FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].base_sql == "SELECT a FROM t"
        assert result[0].current_sql == "SELECT b FROM t"

    def test_added_model_no_cached_sql(self, tmp_path):
        """Added model has no cached SQL (base_sql=None, current_sql=None)."""
        base = tmp_path / "base"
        base.mkdir()

        current = tmp_path / "current"
        current.mkdir()
        (current / "m.sql").write_text("SELECT a FROM t")

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].base_sql is None
        assert result[0].current_sql is None

    def test_removed_model_no_cached_sql(self, tmp_path):
        """Removed model has no cached SQL."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "m.sql").write_text("SELECT a FROM t")

        current = tmp_path / "current"
        current.mkdir()

        result = diff_compiled_dirs(base, current)
        assert len(result) == 1
        assert result[0].base_sql is None
        assert result[0].current_sql is None
