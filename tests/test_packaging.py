"""Verify dbt-plan package structure, metadata, and build artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SRC = ROOT / "src" / "dbt_plan"

# Wheel tests require `uv build` to have been run; skip in CI where dist/ doesn't exist
_has_wheel = bool(sorted(DIST.glob("dbt_plan-*.whl"))) if DIST.is_dir() else False
requires_wheel = pytest.mark.skipif(
    not _has_wheel, reason="No wheel in dist/ — run `uv build` first"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text()


def _pyproject_version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', _read_pyproject(), re.MULTILINE)
    assert match, "Could not find version in pyproject.toml"
    return match.group(1)


def _init_version() -> str:
    from dbt_plan import __version__

    return __version__


def _find_wheel() -> Path:
    """Find wheel matching current project version to avoid stale artifacts."""
    version = _pyproject_version()
    wheels = sorted(DIST.glob(f"dbt_plan-{version}-*.whl"))
    if not wheels:
        # Fall back to any wheel if version-matched not found
        wheels = sorted(DIST.glob("dbt_plan-*.whl"))
    assert wheels, f"No wheel found in {DIST}. Run `uv build` first."
    return wheels[-1]


def _wheel_filenames() -> list[str]:
    whl = _find_wheel()
    with zipfile.ZipFile(whl) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# 1. Version consistency
# ---------------------------------------------------------------------------


class TestVersionConsistency:
    def test_pyproject_version_exists(self) -> None:
        ver = _pyproject_version()
        assert re.match(r"^\d+\.\d+\.\d+", ver), f"Invalid version format: {ver}"

    def test_init_version_exists(self) -> None:
        ver = _init_version()
        assert re.match(r"^\d+\.\d+\.\d+", ver), f"Invalid version format: {ver}"

    def test_versions_match(self) -> None:
        assert _pyproject_version() == _init_version(), (
            f"pyproject.toml ({_pyproject_version()}) != __init__.py ({_init_version()})"
        )


# ---------------------------------------------------------------------------
# 2. Source files included in package
# ---------------------------------------------------------------------------


@requires_wheel
class TestSourceInclusion:
    """All .py source files under src/dbt_plan must appear in the wheel."""

    EXPECTED_MODULES = [
        "__init__.py",
        "cli.py",
        "columns.py",
        "config.py",
        "diff.py",
        "formatter.py",
        "manifest.py",
        "predictor.py",
    ]

    def test_all_source_modules_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        for mod in self.EXPECTED_MODULES:
            expected = f"dbt_plan/{mod}"
            assert expected in filenames, f"Missing from wheel: {expected}"

    def test_source_files_on_disk(self) -> None:
        for mod in self.EXPECTED_MODULES:
            assert (SRC / mod).exists(), f"Missing source file: {SRC / mod}"


# ---------------------------------------------------------------------------
# 3. PEP 561 py.typed marker
# ---------------------------------------------------------------------------


@requires_wheel
class TestPEP561:
    def test_py_typed_on_disk(self) -> None:
        assert (SRC / "py.typed").exists(), "py.typed marker missing on disk"

    def test_py_typed_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        assert "dbt_plan/py.typed" in filenames, "py.typed missing from wheel"


# ---------------------------------------------------------------------------
# 4. Test files excluded from package
# ---------------------------------------------------------------------------


@requires_wheel
class TestTestExclusion:
    def test_no_test_files_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        test_files = [f for f in filenames if "test_" in f or "tests/" in f]
        assert test_files == [], f"Test files leaked into wheel: {test_files}"

    def test_no_fixture_files_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        fixture_files = [f for f in filenames if "fixture" in f.lower()]
        assert fixture_files == [], f"Fixture files leaked into wheel: {fixture_files}"

    def test_no_conftest_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        conftest_files = [f for f in filenames if "conftest" in f]
        assert conftest_files == [], f"conftest files leaked into wheel: {conftest_files}"


# ---------------------------------------------------------------------------
# 5. Dependencies
# ---------------------------------------------------------------------------


class TestDependencies:
    def test_only_sqlglot_runtime_dependency(self) -> None:
        pyproject = _read_pyproject()
        # Extract the dependencies list
        match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
        assert match, "Could not find dependencies in pyproject.toml"
        deps_block = match.group(1)
        # Parse individual dependency names (ignore version specifiers)
        dep_names = re.findall(r'"([a-zA-Z0-9_-]+)', deps_block)
        assert dep_names == ["sqlglot"], (
            f"Expected only sqlglot as runtime dependency, got: {dep_names}"
        )

    def test_test_deps_are_optional(self) -> None:
        pyproject = _read_pyproject()
        assert "[project.optional-dependencies]" in pyproject
        # pytest should be in test extras, not in main deps
        match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
        assert match
        assert "pytest" not in match.group(1), "pytest should not be a runtime dependency"


# ---------------------------------------------------------------------------
# 6. Python version support
# ---------------------------------------------------------------------------


class TestPythonVersion:
    def test_requires_python(self) -> None:
        pyproject = _read_pyproject()
        assert 'requires-python = ">=3.10"' in pyproject

    def test_classifiers_cover_310_to_313(self) -> None:
        pyproject = _read_pyproject()
        for minor in ("3.10", "3.11", "3.12", "3.13"):
            classifier = f"Programming Language :: Python :: {minor}"
            assert classifier in pyproject, f"Missing classifier: {classifier}"


# ---------------------------------------------------------------------------
# 7. CLI entry point
# ---------------------------------------------------------------------------


class TestCLIEntryPoint:
    def test_entry_point_declared(self) -> None:
        pyproject = _read_pyproject()
        assert 'dbt-plan = "dbt_plan.cli:main"' in pyproject

    @requires_wheel
    def test_entry_point_in_wheel(self) -> None:
        whl = _find_wheel()
        with zipfile.ZipFile(whl) as zf:
            ep_files = [f for f in zf.namelist() if f.endswith("entry_points.txt")]
            assert ep_files, "No entry_points.txt in wheel"
            content = zf.read(ep_files[0]).decode()
            assert "dbt-plan = dbt_plan.cli:main" in content

    def test_main_function_importable(self) -> None:
        from dbt_plan.cli import main

        assert callable(main)

    def test_cli_version_flag(self) -> None:
        """CLI --version outputs the current version (tests dev install, not wheel)."""
        result = subprocess.run(
            [sys.executable, "-m", "dbt_plan.cli", "--version"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        # Some CLIs use argparse which may write to stdout
        output = result.stdout.strip() or result.stderr.strip()
        expected_version = _pyproject_version()
        assert expected_version in output, (
            f"--version output does not contain {expected_version!r}: {output!r}"
        )

    def test_cli_help_flag(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "dbt_plan.cli", "--help"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, f"--help exited with {result.returncode}"
        assert "dbt-plan" in result.stdout.lower() or "dbt_plan" in result.stdout.lower()


# ---------------------------------------------------------------------------
# 8. Wheel cleanliness (no unexpected files)
# ---------------------------------------------------------------------------


@requires_wheel
class TestWheelCleanliness:
    def test_no_pyc_files(self) -> None:
        filenames = _wheel_filenames()
        pyc_files = [f for f in filenames if f.endswith(".pyc")]
        assert pyc_files == [], f".pyc files found in wheel: {pyc_files}"

    def test_no_pycache_dirs(self) -> None:
        filenames = _wheel_filenames()
        pycache = [f for f in filenames if "__pycache__" in f]
        assert pycache == [], f"__pycache__ entries in wheel: {pycache}"

    def test_no_dot_files(self) -> None:
        filenames = _wheel_filenames()
        # Exclude dist-info which is expected
        code_files = [f for f in filenames if not f.startswith("dbt_plan-")]
        dot_files = [f for f in code_files if "/." in f or f.startswith(".")]
        assert dot_files == [], f"Dot files found in wheel: {dot_files}"

    def test_no_docs_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        doc_files = [f for f in filenames if f.endswith((".md", ".rst")) and "METADATA" not in f]
        assert doc_files == [], f"Documentation files leaked into wheel: {doc_files}"


# ---------------------------------------------------------------------------
# 9. CHANGELOG mentions current version
# ---------------------------------------------------------------------------


class TestChangelog:
    def test_changelog_exists(self) -> None:
        assert (ROOT / "CHANGELOG.md").exists(), "CHANGELOG.md missing"

    def test_changelog_mentions_current_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text()
        version = _pyproject_version()
        assert version in changelog, f"CHANGELOG.md does not mention current version {version}"

    def test_changelog_has_section_for_current_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text()
        version = _pyproject_version()
        # Expect a header like ## [0.3.5]
        pattern = rf"## \[{re.escape(version)}\]"
        assert re.search(pattern, changelog), (
            f"CHANGELOG.md missing section header for [{version}]"
        )


# ---------------------------------------------------------------------------
# 10. LICENSE file
# ---------------------------------------------------------------------------


@requires_wheel
class TestLicense:
    def test_license_file_exists(self) -> None:
        assert (ROOT / "LICENSE").exists(), "LICENSE file missing"

    def test_license_is_apache2(self) -> None:
        content = (ROOT / "LICENSE").read_text()
        assert "Apache License" in content
        assert "Version 2.0" in content

    def test_pyproject_declares_apache2(self) -> None:
        pyproject = _read_pyproject()
        assert 'license = "Apache-2.0"' in pyproject

    def test_license_in_wheel(self) -> None:
        filenames = _wheel_filenames()
        license_files = [f for f in filenames if "LICENSE" in f.upper()]
        assert license_files, "No LICENSE file in wheel"


# ---------------------------------------------------------------------------
# 11. Build system
# ---------------------------------------------------------------------------


class TestBuildSystem:
    def test_hatchling_backend(self) -> None:
        pyproject = _read_pyproject()
        assert 'build-backend = "hatchling.build"' in pyproject

    def test_wheel_packages_config(self) -> None:
        pyproject = _read_pyproject()
        assert 'packages = ["src/dbt_plan"]' in pyproject
