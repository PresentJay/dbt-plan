"""E2E test: actual dbt compile → dbt-plan snapshot → modify → compile → check.

Requires: pip install dbt-core dbt-duckdb
Skip if dbt is not installed.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DBT_PROJECT = Path(__file__).parent / "dbt_project"

# Find dbt and dbt-plan executables in the same venv as pytest
_VENV_BIN = Path(sys.executable).parent
_DBT = str(_VENV_BIN / "dbt")
_DBT_PLAN = str(_VENV_BIN / "dbt-plan")


def _dbt_available():
    """Check dbt CLI, dbt-plan CLI, and dbt-duckdb adapter are all installed."""
    if not Path(_DBT).exists():
        return False
    if not Path(_DBT_PLAN).exists():
        return False
    if importlib.util.find_spec("dbt.adapters.duckdb") is None:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _dbt_available(), reason="dbt-core, dbt-duckdb, or dbt-plan not installed"
)


def _dbt_compile(project_dir: Path):
    """Run dbt compile in the project directory."""
    result = subprocess.run(
        [_DBT, "compile", "--profiles-dir", ".", "--target-path", "target"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"dbt compile failed: {result.stderr}"


def _dbt_plan(args: list[str]) -> subprocess.CompletedProcess:
    """Run dbt-plan CLI."""
    return subprocess.run(
        [_DBT_PLAN] + args,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def dbt_project(tmp_path):
    """Copy dbt project to tmp_path so each test has a clean copy."""
    project = tmp_path / "dbt_project"
    shutil.copytree(DBT_PROJECT, project)
    # Clean any leftover artifacts
    for d in ["target", ".dbt-plan", "logs"]:
        p = project / d
        if p.exists():
            shutil.rmtree(p)
    return project


class TestDbtE2E:
    def test_compile_snapshot_check_no_changes(self, dbt_project):
        """compile → snapshot → compile again (no changes) → check → exit 0."""
        _dbt_compile(dbt_project)

        # Snapshot
        result = _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        assert result.returncode == 0
        assert "Snapshot saved" in result.stdout

        # Check (no changes)
        result = _dbt_plan(["check", "--project-dir", str(dbt_project)])
        assert result.returncode == 0
        assert "no model changes detected" in result.stdout

    def test_destructive_change_detected(self, dbt_project):
        """Modify sync_all_columns model → DROP COLUMN detected → exit 1."""
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])

        # Modify fct_orders: remove customer_uuid
        fct_orders = dbt_project / "models" / "marts" / "fct_orders.sql"
        fct_orders.write_text("""{{ config(
    materialized='incremental',
    on_schema_change='sync_all_columns'
) }}

SELECT
    order_id,
    store_id,
    order_date,
    'unknown' AS source
FROM {{ ref('stg_orders') }}
""")
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project)])
        assert result.returncode == 1, (
            f"Expected exit 1, got {result.returncode}. Output: {result.stdout}"
        )
        assert "DESTRUCTIVE" in result.stdout
        assert "DROP COLUMN" in result.stdout
        assert "customer_uuid" in result.stdout

    def test_safe_table_change(self, dbt_project):
        """Modify table model → CREATE OR REPLACE → SAFE."""
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])

        # Modify dim_books: add a column
        dim_books = dbt_project / "models" / "marts" / "dim_books.sql"
        dim_books.write_text("""{{ config(materialized='table') }}

SELECT
    store_id,
    'App Name' AS title,
    'active' AS status
FROM {{ ref('stg_orders') }}
GROUP BY 1
""")
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project)])
        assert result.returncode == 0
        assert "SAFE" in result.stdout
        assert "dim_books" in result.stdout

    def test_github_format(self, dbt_project):
        """--format github produces markdown."""
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])

        # Make a change
        dim_books = dbt_project / "models" / "marts" / "dim_books.sql"
        dim_books.write_text("""{{ config(materialized='table') }}

SELECT
    store_id,
    'App Name' AS title,
    'v2' AS version
FROM {{ ref('stg_orders') }}
GROUP BY 1
""")
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--format", "github"])
        assert "###" in result.stdout
        assert "**SAFE**" in result.stdout
