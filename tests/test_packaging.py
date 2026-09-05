"""Verify dbt-plan package structure, metadata, and build artifacts."""

from __future__ import annotations

import os
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
_has_sdist = bool(sorted(DIST.glob("dbt_plan-*.tar.gz"))) if DIST.is_dir() else False
requires_sdist = pytest.mark.skipif(
    not _has_sdist, reason="No sdist in dist/ — run `uv build` first"
)


def _sdist_filenames() -> list[str]:
    """Paths inside the sdist, with the leading `dbt_plan-<version>/` stripped."""
    import tarfile

    sdist = sorted(DIST.glob("dbt_plan-*.tar.gz"))[-1]
    with tarfile.open(sdist) as tf:
        return [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


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


class TestSdistSurface:
    """An sdist is published permanently and mirrored worldwide.

    Releases 0.2.0-0.3.5 bundled the whole working tree, which put internal
    design documents on PyPI where deleting the release did not fully retract
    them. The sdist contents are a deliberate list, not a default.
    """

    @requires_sdist
    def test_sdist_ships_both_packages(self) -> None:
        """Checks the built artifact, not the declaration.

        Adding "/src/dbt_plan_mcp" to the include list is not enough on its own:
        hatchling selects files through the VCS, so a package that exists on disk
        but is untracked is silently left out. That combination -- declaration
        present, files absent -- ships an extra whose dependencies install and
        whose module then cannot be imported.
        """
        names = _sdist_filenames()
        assert "src/dbt_plan/cli.py" in names
        assert "src/dbt_plan_mcp/server.py" in names, (
            "the mcp extra would install its dependencies and then import nothing"
        )

    def test_sdist_include_list_is_declared(self) -> None:
        pyproject = _read_pyproject()
        assert "[tool.hatch.build.targets.sdist]" in pyproject, (
            "sdist contents must be declared explicitly, not left to hatchling's "
            "default of packaging the entire working tree"
        )

    @requires_sdist
    def test_no_docs_or_tests_in_sdist(self) -> None:
        leaked = [
            f
            for f in _sdist_filenames()
            if f.startswith(("docs/", "tests/", "examples/", ".github/"))
        ]
        assert leaked == [], f"Non-shipping files leaked into sdist: {leaked}"

    @requires_sdist
    def test_sdist_can_still_build_the_package(self) -> None:
        """Trimming must not break `pip install <sdist>`."""
        names = _sdist_filenames()
        for required in ("pyproject.toml", "README.md", "LICENSE"):
            assert required in names, f"sdist cannot build without {required}"
        assert any(f.startswith("src/dbt_plan/") for f in names), "sdist has no source"
        assert "src/dbt_plan/py.typed" in names, "PEP 561 marker missing from sdist"


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

    def test_classifiers_cover_310_to_314(self) -> None:
        pyproject = _read_pyproject()
        for minor in ("3.10", "3.11", "3.12", "3.13", "3.14"):
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
            encoding="utf-8",
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
            encoding="utf-8",
            cwd=str(ROOT),
        )
        assert result.returncode == 0, f"--help exited with {result.returncode}"
        assert "dbt-plan" in result.stdout.lower() or "dbt_plan" in result.stdout.lower()

    def test_cli_help_flag_uses_utf8_when_stdout_defaults_to_cp1252(self) -> None:
        """Windows' legacy console encoding must not crash help text containing arrows."""
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run(
            [sys.executable, "-m", "dbt_plan.cli", "--help"],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert "dbt-plan" in result.stdout.decode("utf-8").lower()

    def test_github_format_output_uses_utf8_when_stdout_defaults_to_cp1252(self) -> None:
        """Preserve intentional GitHub Markdown icons rather than crashing or flattening them."""
        code = """
from dbt_plan.cli import _configure_output_streams
from dbt_plan.formatter import CheckResult, format_github
from dbt_plan.predictor import DDLPrediction, Safety
_configure_output_streams()
print(format_github(CheckResult([DDLPrediction('orders', 'table', None, Safety.DESTRUCTIVE)])))
"""
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert "🔴" in result.stdout.decode("utf-8")


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
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        version = _pyproject_version()
        assert version in changelog, f"CHANGELOG.md does not mention current version {version}"

    def test_changelog_has_section_for_current_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
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
        content = (ROOT / "LICENSE").read_text(encoding="utf-8")
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
        """Both packages ship. dbt_plan_mcp is separate on purpose.

        The analysis core promises to be offline and synchronous, and
        tests/test_invariants.py enforces that by failing on an asyncio or network
        import anywhere under src/dbt_plan/. An MCP server is both, so it lives in
        its own package -- which is what keeps the guarantee provable instead of
        depending on nobody adding the wrong import.
        """
        pyproject = _read_pyproject()
        assert 'packages = ["src/dbt_plan", "src/dbt_plan_mcp"]' in pyproject


class TestMcpRegistryManifest:
    """server.json carries a version by hand, which is exactly how things drift here.

    A stale version publishes a registry entry pointing at a release that is not the
    current one, so an agent installs an older dbt-plan than the manifest advertises.
    """

    def test_server_json_version_matches_the_package(self) -> None:
        import json

        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        version = _pyproject_version()

        assert manifest["version"] == version
        assert manifest["packages"][0]["version"] == version

    def test_readme_carries_the_ownership_line(self) -> None:
        """The registry reads the *PyPI* README to prove the publisher owns the package.

        Without it, publishing fails with:

            PyPI package 'dbt-plan' ownership validation failed. The server name
            'io.github.PresentJay/dbt-plan' must appear as
            'mcp-name: io.github.PresentJay/dbt-plan' in the package README

        And because it is checked against PyPI rather than GitHub, removing this line
        breaks nothing until the next release -- by which point the cause is several
        commits back.
        """
        import json

        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        assert f"mcp-name: {manifest['name']}" in readme

    def test_namespace_matches_the_github_login_exactly(self) -> None:
        """The registry namespace is case-sensitive and is not lowercased.

        Reverse-DNS convention suggests lowercase, so the first version of this
        manifest said `io.github.presentjay/dbt-plan` and publishing returned 403:
        "You have permission to publish: io.github.PresentJay/*". The registry
        derives the namespace from the GitHub login as spelled.

        The Pages URL in `websiteUrl` is genuinely lowercase -- github.io hosts are
        -- so the two differing is correct, not a typo waiting to be tidied.
        """
        import json

        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

        assert manifest["name"] == "io.github.PresentJay/dbt-plan"

    def test_description_fits_the_registry_limit(self) -> None:
        """The registry rejects a description over 100 characters with a 422.

        Found by running `mcp-publisher validate` before publishing rather than
        during it -- the first version of this manifest was 254 characters and
        would have failed at the point of release.
        """
        import json

        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

        assert len(manifest["description"]) <= 100, (
            f"description is {len(manifest['description'])} chars; "
            "the MCP registry rejects anything over 100"
        )

    def test_it_points_at_the_pypi_package_this_repo_publishes(self) -> None:
        import json

        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        package = manifest["packages"][0]

        assert package["registryType"] == "pypi"
        assert package["identifier"] == "dbt-plan"
        assert package["transport"]["type"] == "stdio"
