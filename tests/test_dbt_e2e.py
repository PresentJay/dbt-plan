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
# Invoke dbt-plan as a module rather than via the console script. The script is
# absent whenever the project itself is not installed, which would make these
# tests skip with a message blaming dbt -- a broken environment disguised as an
# intentional skip.
_DBT_PLAN_ARGV = [sys.executable, "-m", "dbt_plan.cli"]


def _missing_requirement() -> str | None:
    """Name the missing piece, so a skip never hides the wrong problem."""
    if importlib.util.find_spec("dbt_plan") is None:
        return "dbt_plan is not importable -- run `uv sync` or `pip install -e .`"
    if not Path(_DBT).exists():
        return "dbt-core is not installed"
    if importlib.util.find_spec("dbt.adapters.duckdb") is None:
        return "dbt-duckdb adapter is not installed"
    return None


pytestmark = pytest.mark.skipif(
    _missing_requirement() is not None, reason=_missing_requirement() or ""
)


def _dbt_compile(project_dir: Path):
    """Run dbt compile in the project directory."""
    result = subprocess.run(
        [_DBT, "compile", "--profiles-dir", ".", "--target-path", "target"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"dbt compile failed: {result.stderr}"


def _dbt_plan(args: list[str]) -> subprocess.CompletedProcess:
    """Run dbt-plan CLI."""
    return subprocess.run(
        _DBT_PLAN_ARGV + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def _dbt_run(project_dir: Path) -> subprocess.CompletedProcess:
    """Run dbt run, which is where an incremental DROP COLUMN actually happens."""
    return subprocess.run(
        [_DBT, "run", "--profiles-dir", ".", "--target-path", "target"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _dbt_build(project_dir: Path, select: str) -> subprocess.CompletedProcess:
    """Run dbt build, which is where a broken unit test actually surfaces."""
    return subprocess.run(
        [
            _DBT,
            "build",
            "--profiles-dir",
            ".",
            "--target-path",
            "target",
            "--select",
            select,
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


_STG_ORDERS_WITHOUT_CUSTOMER_ID = """{{ config(materialized='view') }}

SELECT
    1 AS order_id,
    'store_001' AS store_id,
    '2024-01-01' AS order_date
"""


class TestUnitTestsAreReachedByCascade:
    """The prediction and the build, side by side, on the same change.

    tests/dbt_project declares two unit tests. Dropping `customer_id` from
    stg_orders breaks both -- one through its own `expect`, one through the
    `given` fixture standing in for stg_orders inside dim_books' test. dbt-plan
    has to name both before the build does.
    """

    @pytest.fixture
    def project_without_customer_id(self, dbt_project):
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        stg = dbt_project / "models" / "staging" / "stg_orders.sql"
        stg.write_text(_STG_ORDERS_WITHOUT_CUSTOMER_ID)
        _dbt_compile(dbt_project)
        return dbt_project

    def test_dbt_build_really_fails_on_the_dropped_column(self, project_without_customer_id):
        """The claim dbt-plan makes below is this, measured."""
        result = _dbt_build(project_without_customer_id, "stg_orders")
        assert result.returncode != 0
        assert "test_stg_orders_shape" in result.stdout
        assert "Invalid column name: 'customer_id'" in result.stdout

    def test_check_names_both_unit_tests(self, project_without_customer_id):
        result = _dbt_plan(
            ["check", "--project-dir", str(project_without_customer_id), "--no-color"]
        )
        assert "UNIT_TEST_FAILURE" in result.stdout
        assert "test_stg_orders_shape" in result.stdout
        assert "test_dim_books_groups_by_store" in result.stdout
        # A view is CREATE OR REPLACE and safe on its own; the build is not.
        assert result.returncode == 2, result.stdout

    def test_compiled_unit_test_sql_is_not_reported_as_a_model(self, dbt_project):
        """dbt build writes unit test SQL into target/compiled, next to the models."""
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        _dbt_build(dbt_project, "stg_orders")

        compiled_unit_tests = list(
            (dbt_project / "target" / "compiled").rglob("test_stg_orders_shape.sql")
        )
        assert compiled_unit_tests, "dbt did not compile the unit test; the guard is untested"

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--no-color"])
        assert "test_stg_orders_shape" not in result.stdout
        assert "not found in manifest" not in result.stdout
        assert result.returncode == 0, result.stdout


class TestExposuresAreNamedOnARealProject:
    """tests/dbt_project declares one exposure, on stg_orders."""

    def test_a_change_that_is_not_safe_names_the_dashboard_and_its_owner(self, dbt_project):
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        (dbt_project / "models" / "staging" / "stg_orders.sql").write_text(
            _STG_ORDERS_WITHOUT_CUSTOMER_ID
        )
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--no-color"])
        assert "EXPOSURE  orders_dashboard (dashboard) -- owner: Data Team" in result.stdout

    def test_a_safe_change_says_nothing_about_it(self, dbt_project):
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        (dbt_project / "models" / "staging" / "stg_orders.sql").write_text(
            """{{ config(materialized='view') }}

SELECT
    1 AS order_id,
    'store_001' AS store_id,
    '2024-01-01' AS order_date,
    'cust_abc' AS customer_id,
    'web' AS channel
"""
        )
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--no-color"])
        assert "SAFE" in result.stdout
        assert "orders_dashboard" not in result.stdout


_STAR_STG_ORDERS = """{{{{ config(materialized='view') }}}}

SELECT
    1 AS order_id,
    'open' AS status{extra}
"""

_STAR_FCT_ORDERS = """{{ config(
    materialized='incremental',
    on_schema_change='sync_all_columns'
) }}

SELECT * FROM {{ ref('stg_orders') }}
"""


@pytest.fixture
def star_project(tmp_path):
    """A project where the downstream model's file never changes.

    Deliberately not tests/dbt_project: this one has to survive `dbt run`, so it
    needs a duckdb file on disk and models that actually build.
    """
    project = tmp_path / "star_project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: star_project\nversion: '1.0.0'\nprofile: star_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "star_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: "dev.duckdb"\n'
    )
    (project / "models" / "stg_orders.sql").write_text(
        _STAR_STG_ORDERS.format(extra=",\n    'cust_abc' AS customer_id")
    )
    (project / "models" / "fct_orders.sql").write_text(_STAR_FCT_ORDERS)
    return project


def _fct_orders_columns(project_dir: Path) -> list[str]:
    import duckdb

    con = duckdb.connect(str(project_dir / "dev.duckdb"), read_only=True)
    try:
        return [
            row[0]
            for row in con.execute(
                "select column_name from information_schema.columns "
                "where table_name = 'fct_orders' order by ordinal_position"
            ).fetchall()
        ]
    finally:
        con.close()


class TestADownstreamStarLosesAColumn:
    """The model whose file did not change is the one that loses data."""

    def test_check_names_the_downstream_model_and_fails(self, star_project):
        _dbt_compile(star_project)
        _dbt_plan(["snapshot", "--project-dir", str(star_project)])
        (star_project / "models" / "stg_orders.sql").write_text(_STAR_STG_ORDERS.format(extra=""))
        _dbt_compile(star_project)

        result = _dbt_plan(["check", "--project-dir", str(star_project), "--no-color"])
        assert "INHERITED_DROP" in result.stdout, result.stdout
        assert "fct_orders: file unchanged, loses customer_id from upstream" in result.stdout
        assert "DROP COLUMN customer_id" in result.stdout
        # stg_orders is a view; its own DDL is CREATE OR REPLACE and safe.
        assert result.returncode == 1, result.stdout

    def test_dbt_really_drops_the_column_from_the_downstream_table(self, star_project):
        """The claim above, measured. This is what exit 0 used to be hiding."""
        assert _dbt_run(star_project).returncode == 0
        assert "customer_id" in _fct_orders_columns(star_project)

        (star_project / "models" / "stg_orders.sql").write_text(_STAR_STG_ORDERS.format(extra=""))
        assert _dbt_run(star_project).returncode == 0
        assert _fct_orders_columns(star_project) == ["order_id", "status"]
