"""E2E test: actual dbt compile → dbt-plan snapshot → modify → compile → check.

Requires: pip install dbt-core dbt-duckdb
Skip if dbt is not installed.
"""

import importlib.util
import json
import os
import shlex
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


def _dbt_plan(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run dbt-plan CLI."""
    return subprocess.run(
        _DBT_PLAN_ARGV + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
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
        assert "Snapshot saved" in result.stderr
        assert "Snapshot saved" not in result.stdout

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


def _dbt_build(
    project_dir: Path, select: str, exclude: str | None = None
) -> subprocess.CompletedProcess:
    """Run dbt build, which is where a broken test actually surfaces.

    `exclude` matters because dbt stops at the first failure and skips the rest:
    with both a unit test and a generic test broken by the same change, only one
    of them ever runs.
    """
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
            *(["--exclude", exclude] if exclude else []),
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

        # The partial build rewrites the manifest for only stg_orders. Refresh
        # all model compilation evidence while retaining the generated unit-test
        # artifact whose exclusion this regression actually measures.
        _dbt_compile(dbt_project)
        assert all(path.exists() for path in compiled_unit_tests)
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


class TestDataTestsAreReachedByCascade:
    """tests/dbt_project declares generic tests and one singular test."""

    @pytest.fixture
    def project_without_customer_id(self, dbt_project):
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        (dbt_project / "models" / "staging" / "stg_orders.sql").write_text(
            _STG_ORDERS_WITHOUT_CUSTOMER_ID
        )
        _dbt_compile(dbt_project)
        return dbt_project

    def test_dbt_build_cannot_even_bind_the_generic_test(self, project_without_customer_id):
        """The claim dbt-plan makes below is this, measured."""
        result = _dbt_build(
            project_without_customer_id, "stg_orders", exclude="test_stg_orders_shape"
        )
        assert result.returncode != 0
        assert "not_null_stg_orders_customer_id" in result.stdout
        assert 'Referenced column "customer_id" not found' in result.stdout

    def test_check_names_the_generic_tests_and_the_singular_one(self, project_without_customer_id):
        result = _dbt_plan(
            ["check", "--project-dir", str(project_without_customer_id), "--no-color"]
        )
        assert "DATA_TEST_FAILURE" in result.stdout, result.stdout
        assert "not_null_stg_orders_customer_id: tests dropped column(s): customer_id" in (
            result.stdout
        )
        assert "accepted_values_stg_orders_customer_id__cust_abc" in result.stdout
        # The singular test names no column in the manifest; its SQL is the answer.
        assert (
            "no_order_without_a_customer: its SQL names dropped column(s): customer_id"
            in result.stdout
        )
        # And a test on a column that survives stays out of it.
        assert "not_null_stg_orders_order_id" not in result.stdout
        assert result.returncode == 2, result.stdout

    def test_an_added_column_leaves_every_test_alone(self, dbt_project):
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
        assert "DATA_TEST" not in result.stdout, result.stdout


_CONTRACT_MODEL = """{{{{ config(materialized='table', contract={{'enforced': True}}) }}}}

SELECT
    1 AS order_id{extra}
"""


@pytest.fixture
def contract_project(tmp_path):
    """A project with one contracted model, declared in YAML and produced in SQL."""
    project = tmp_path / "contract_project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: contract_project\nversion: '1.0.0'\nprofile: contract_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "contract_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    (project / "models" / "fct_contract.sql").write_text(
        _CONTRACT_MODEL.format(extra=",\n    'cust_abc' AS customer_id")
    )
    (project / "models" / "schema.yml").write_text(
        """version: 2
models:
  - name: fct_contract
    config:
      contract:
        enforced: true
    columns:
      - name: order_id
        data_type: integer
      - name: customer_id
        data_type: varchar
"""
    )
    return project


class TestAnEnforcedContract:
    def _snapshot_then(self, project, sql):
        _dbt_compile(project)
        _dbt_plan(["snapshot", "--project-dir", str(project)])
        (project / "models" / "fct_contract.sql").write_text(sql)
        _dbt_compile(project)
        return _dbt_plan(["check", "--project-dir", str(project), "--no-color"])

    def test_a_removed_column_is_named_the_way_dbt_names_it(self, contract_project):
        result = self._snapshot_then(contract_project, _CONTRACT_MODEL.format(extra=""))
        assert "CONTRACT VIOLATION: customer_id missing in definition" in result.stdout
        assert result.returncode == 2, result.stdout

    def test_dbt_build_really_refuses_it(self, contract_project):
        """The claim above, measured. A table is CREATE OR REPLACE and otherwise safe."""
        _dbt_compile(contract_project)
        (contract_project / "models" / "fct_contract.sql").write_text(
            _CONTRACT_MODEL.format(extra="")
        )
        result = _dbt_build(contract_project, "fct_contract")
        assert result.returncode != 0
        assert "This model has an enforced contract that failed" in result.stdout
        assert "missing in definition" in result.stdout

    def test_an_added_column_is_a_violation_too(self, contract_project):
        """Everywhere else in dbt-plan an added column is safe. Under a contract it is not."""
        result = self._snapshot_then(
            contract_project,
            _CONTRACT_MODEL.format(extra=",\n    'cust_abc' AS customer_id,\n    'x' AS note"),
        )
        assert "CONTRACT VIOLATION: note missing in contract" in result.stdout
        assert result.returncode == 2, result.stdout

    def test_a_change_that_keeps_the_shape_stays_safe(self, contract_project):
        result = self._snapshot_then(
            contract_project,
            _CONTRACT_MODEL.format(extra=",\n    'cust_xyz' AS customer_id"),
        )
        assert "CONTRACT VIOLATION" not in result.stdout
        assert result.returncode == 0, result.stdout


_VERSIONED_V2 = """{{{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}}}

SELECT 1 AS order_id{extra}
"""


@pytest.fixture
def versioned_project(tmp_path):
    """Two versions of one model, which share a dbt name and differ only by file."""
    project = tmp_path / "versioned_project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: versioned_project\nversion: '1.0.0'\nprofile: versioned_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "versioned_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    (project / "models" / "fct_orders_v1.sql").write_text(
        _VERSIONED_V2.format(extra=",\n    'cust_abc' AS customer_id")
    )
    (project / "models" / "fct_orders_v2.sql").write_text(
        _VERSIONED_V2.format(extra=",\n    'cust_abc' AS customer_id")
    )
    (project / "models" / "schema.yml").write_text(
        """version: 2
models:
  - name: fct_orders
    latest_version: 2
    config:
      materialized: incremental
      on_schema_change: sync_all_columns
    versions:
      - v: 1
      - v: 2
"""
    )
    return project


class TestVersionedModelsAreAnalysed:
    def test_a_drop_in_one_version_is_reported_against_that_version(self, versioned_project):
        _dbt_compile(versioned_project)
        _dbt_plan(["snapshot", "--project-dir", str(versioned_project)])
        (versioned_project / "models" / "fct_orders_v2.sql").write_text(
            _VERSIONED_V2.format(extra="")
        )
        _dbt_compile(versioned_project)

        result = _dbt_plan(["check", "--project-dir", str(versioned_project), "--no-color"])
        assert "DESTRUCTIVE  fct_orders_v2 (incremental, sync_all_columns)" in result.stdout
        assert "DROP COLUMN  customer_id" in result.stdout
        # Neither warning that used to stand in for the analysis.
        assert "not found in manifest" not in result.stdout
        assert "compile is incomplete" not in result.stdout
        # v1 was not touched.
        assert "fct_orders_v1" not in result.stdout
        assert result.returncode == 1, result.stdout

    def test_an_untouched_versioned_project_is_quiet(self, versioned_project):
        _dbt_compile(versioned_project)
        _dbt_plan(["snapshot", "--project-dir", str(versioned_project)])
        _dbt_compile(versioned_project)

        result = _dbt_plan(["check", "--project-dir", str(versioned_project), "--no-color"])
        assert result.returncode == 0, result.stdout
        assert "no model changes detected" in result.stdout


@pytest.fixture
def unruled_project(tmp_path):
    """A materialized view and a `SELECT *` that resolves through `ref()`.

    Both are cases where the resolved manifest looks ordinary and is not: dbt fills
    in `on_schema_change: ignore` for the first, and the second reads as `*` until
    the DAG is followed.
    """
    project = tmp_path / "unruled_project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: unruled_project\nversion: '1.0.0'\nprofile: unruled_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "unruled_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    (project / "models" / "stg_base.sql").write_text(
        "{{ config(materialized='view') }}\nSELECT 1 AS order_id, 10.0 AS amount\n"
    )
    (project / "models" / "mv_thing.sql").write_text(
        "{{ config(materialized='materialized_view') }}\n"
        "SELECT order_id FROM {{ ref('stg_base') }}\n"
    )
    (project / "models" / "int_star.sql").write_text(
        "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
        "SELECT * FROM {{ ref('stg_base') }}\n"
    )
    return project


class TestAMaterializationWithNoRule:
    def test_a_materialized_view_losing_a_column_is_not_reported_safe(self, unruled_project):
        """dbt resolves `on_schema_change: ignore` for it, which asserts nothing."""
        _dbt_compile(unruled_project)
        _dbt_plan(["snapshot", "--project-dir", str(unruled_project)])
        (unruled_project / "models" / "mv_thing.sql").write_text(
            "{{ config(materialized='materialized_view') }}\n"
            "SELECT 1 AS other_col FROM {{ ref('stg_base') }}\n"
        )
        _dbt_compile(unruled_project)

        result = _dbt_plan(
            ["check", "--project-dir", str(unruled_project), "--dialect", "duckdb", "--no-color"]
        )
        assert "NO DDL" not in result.stdout, result.stdout
        assert "REVIEW REQUIRED (materialized_view is driven by" in result.stdout
        assert "DROP COLUMN  order_id" in result.stdout
        assert result.returncode == 2, result.stdout


class TestStatsAgreesWithCheck:
    def test_a_star_check_can_read_is_not_counted_as_unreadable(self, unruled_project):
        """`stats` used to run the extraction without the resolver `check` passes."""
        _dbt_compile(unruled_project)

        stats = _dbt_plan(["stats", "--project-dir", str(unruled_project), "--dialect", "duckdb"])
        assert "SELECT * usage: 1/3 models (33%)" in stats.stdout
        assert "Columns readable: 3/3 compiled model(s)" in stats.stdout
        assert "SELECT * resolved through ref() or a CTE: 1" in stats.stdout
        assert "add column docs to resolve" not in stats.stdout

        # And the count of models with no rule names the materialized view.
        assert "DDL rules: 2/3 model(s)" in stats.stdout
        assert "materialized_view (no on_schema_change)" in stats.stdout

    def test_check_really_does_read_that_star(self, unruled_project):
        """The half stats was contradicting."""
        _dbt_compile(unruled_project)
        _dbt_plan(["snapshot", "--project-dir", str(unruled_project)])
        (unruled_project / "models" / "int_star.sql").write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "-- touched\nSELECT * FROM {{ ref('stg_base') }}\n"
        )
        _dbt_compile(unruled_project)

        result = _dbt_plan(
            ["check", "--project-dir", str(unruled_project), "--dialect", "duckdb", "--no-color"]
        )
        assert "SAFE  int_star" in result.stdout
        assert "REVIEW REQUIRED (SELECT *)" not in result.stdout


@pytest.fixture
def renamed_paths_project(tmp_path):
    """`model-paths` is not `models`, and there are two of them."""
    project = tmp_path / "renamed_paths"
    (project / "transformations").mkdir(parents=True)
    (project / "extras").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: renamed_paths\nversion: '1.0.0'\nprofile: renamed_profile\n"
        'model-paths: ["transformations", "extras"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "renamed_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    (project / "transformations" / "stg_orders.sql").write_text(
        "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
        "SELECT 1 AS order_id, 'cust' AS customer_id\n"
    )
    (project / "extras" / "aux_thing.sql").write_text(
        "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
        "SELECT 1 AS a, 2 AS b\n"
    )
    return project


class TestARenamedModelPath:
    def test_snapshot_finds_the_compile_it_used_to_deny(self, renamed_paths_project):
        _dbt_compile(renamed_paths_project)
        result = _dbt_plan(["snapshot", "--project-dir", str(renamed_paths_project)])
        assert result.returncode == 0, result.stderr
        assert "No compiled SQL found" not in result.stderr
        base = renamed_paths_project / ".dbt-plan" / "base" / "compiled"
        assert (base / "transformations" / "stg_orders.sql").exists()
        assert (base / "extras" / "aux_thing.sql").exists()

    def test_a_drop_in_each_path_is_reported(self, renamed_paths_project):
        """The second path used to be skipped without a word, which is a missed finding."""
        _dbt_compile(renamed_paths_project)
        _dbt_plan(["snapshot", "--project-dir", str(renamed_paths_project)])
        (renamed_paths_project / "transformations" / "stg_orders.sql").write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "SELECT 1 AS order_id\n"
        )
        (renamed_paths_project / "extras" / "aux_thing.sql").write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "SELECT 1 AS a\n"
        )
        _dbt_compile(renamed_paths_project)

        result = _dbt_plan(["check", "--project-dir", str(renamed_paths_project), "--no-color"])
        assert "DROP COLUMN  customer_id" in result.stdout, result.stdout
        assert "DROP COLUMN  b" in result.stdout
        assert result.returncode == 1

    def test_a_snapshot_from_before_this_change_still_compares(self, renamed_paths_project):
        """Pre-0.14 snapshots were copied from inside the model directory.

        Filtering those by the model-path prefix would find nothing and report every
        model as added -- a wall of false findings rather than an honest "re-snapshot".
        """
        import shutil

        _dbt_compile(renamed_paths_project)
        _dbt_plan(["snapshot", "--project-dir", str(renamed_paths_project)])

        base = renamed_paths_project / ".dbt-plan" / "base" / "compiled"
        legacy = renamed_paths_project / ".dbt-plan" / "base" / "legacy"
        legacy.mkdir()
        for path in ("transformations", "extras"):
            for sql in (base / path).glob("*.sql"):
                shutil.copy2(sql, legacy / sql.name)
        shutil.rmtree(base)
        legacy.rename(base)

        result = _dbt_plan(["check", "--project-dir", str(renamed_paths_project), "--no-color"])
        assert "no model changes detected" in result.stdout, result.stdout
        assert result.returncode == 0


class TestAFailedCompileIsNotACleanRun:
    """The #106 case, against real dbt."""

    def test_a_change_that_never_compiled_is_not_reported_as_no_changes(self, dbt_project):
        import subprocess

        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])

        # Exercise staleness beyond the documented filesystem tolerance.
        # Fast Linux runners can edit within the one-second tolerance; relying
        # on subprocess overhead made this test machine-speed dependent.
        import os

        manifest_path = dbt_project / "target" / "manifest.json"
        stamp = manifest_path.stat().st_mtime - 5
        os.utime(manifest_path, (stamp, stamp))

        # Drop a column, and break the parse so nothing recompiles.
        (dbt_project / "models" / "staging" / "stg_orders.sql").write_text(
            _STG_ORDERS_WITHOUT_CUSTOMER_ID
        )
        with (dbt_project / "models" / "schema.yml").open("a", encoding="utf-8") as f:
            f.write("\nmodels:\n  - name: [not valid\n")

        failed = subprocess.run(
            [_DBT, "compile", "--profiles-dir", ".", "--target-path", "target"],
            cwd=dbt_project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert failed.returncode != 0, "the compile was supposed to fail"

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--no-color"])
        assert "no model changes detected" not in result.stdout
        assert "target/ may be out of date" in result.stdout, result.stdout
        assert "stg_orders.sql" in result.stdout
        assert result.returncode == 2, result.stdout

    def test_a_fresh_compile_says_nothing_about_staleness(self, dbt_project):
        """The check has to be quiet in the ordinary case or it is worthless."""
        _dbt_compile(dbt_project)
        _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
        _dbt_compile(dbt_project)

        result = _dbt_plan(["check", "--project-dir", str(dbt_project), "--no-color"])
        assert "out of date" not in result.stdout
        assert result.returncode == 0, result.stdout


@pytest.fixture
def ambiguous_project(tmp_path):
    """A downstream model that mentions a column three times and reads none of them."""
    project = tmp_path / "ambiguous"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: ambiguous\nversion: '1.0.0'\nprofile: ambiguous_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "ambiguous_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    (project / "models" / "stg_orders.sql").write_text(
        "{{ config(materialized='view') }}\n"
        "SELECT 1 AS order_id, 'c' AS customer_id, 'open' AS status\n"
    )
    (project / "models" / "dim_customers.sql").write_text(
        "{{ config(materialized='view') }}\nSELECT 'c' AS customer_id, 'gold' AS tier\n"
    )
    # Mentions customer_id in a comment, a string literal, and as another table's
    # column. Reads none of stg_orders' customer_id.
    (project / "models" / "fct_innocent.sql").write_text(
        "{{ config(materialized='table') }}\n"
        "-- customer_id used to live here\n"
        "SELECT o.order_id, c.customer_id, c.tier\n"
        "FROM {{ ref('stg_orders') }} o\n"
        "JOIN {{ ref('dim_customers') }} c ON o.order_id = 1\n"
        "WHERE 'customer_id' <> ''\n"
    )
    (project / "models" / "fct_guilty.sql").write_text(
        "{{ config(materialized='table') }}\n"
        "SELECT order_id, customer_id FROM {{ ref('stg_orders') }}\n"
    )
    return project


class TestCascadeResolvesRatherThanMatches:
    def test_only_the_model_that_reads_the_column_is_reported(self, ambiguous_project):
        _dbt_compile(ambiguous_project)
        _dbt_plan(["snapshot", "--project-dir", str(ambiguous_project)])
        (ambiguous_project / "models" / "stg_orders.sql").write_text(
            "{{ config(materialized='view') }}\nSELECT 1 AS order_id, 'open' AS status\n"
        )
        _dbt_compile(ambiguous_project)

        result = _dbt_plan(
            ["check", "--project-dir", str(ambiguous_project), "--dialect", "duckdb", "--no-color"]
        )
        assert "BROKEN_REF  fct_guilty: reads dropped column(s): customer_id" in result.stdout
        # It stays in the informational downstream list, where it belongs. What it
        # must not be is a finding.
        findings = [ln for ln in result.stdout.splitlines() if ">>" in ln]
        assert not any("fct_innocent" in ln for ln in findings), result.stdout
        assert "Downstream: fct_guilty, fct_innocent" in result.stdout
        assert result.returncode == 1

    def test_dbt_agrees_about_which_one_breaks(self, ambiguous_project):
        """The claim above, measured. fct_innocent builds; fct_guilty does not."""
        _dbt_compile(ambiguous_project)
        (ambiguous_project / "models" / "stg_orders.sql").write_text(
            "{{ config(materialized='view') }}\nSELECT 1 AS order_id, 'open' AS status\n"
        )
        # `+model` so the upstream views exist: the profile is in-memory, so
        # nothing survives between invocations.
        innocent = _dbt_build(ambiguous_project, "+fct_innocent")
        assert innocent.returncode == 0, innocent.stdout

        guilty = _dbt_build(ambiguous_project, "+fct_guilty")
        assert guilty.returncode != 0
        assert 'column "customer_id" not found' in guilty.stdout.lower()


class TestAContractTypeMismatch:
    """dbt's own answer, on both sides of the line this check draws."""

    def _with_cast(self, project, cast):
        (project / "models" / "fct_contract.sql").write_text(
            "{{ config(materialized='table', contract={'enforced': True}) }}\n"
            f"SELECT 1 AS order_id, {cast} AS customer_id\n"
        )

    def test_a_different_family_is_reported_and_dbt_refuses_it(self, contract_project):
        self._with_cast(contract_project, "CAST('c' AS VARCHAR)")
        _dbt_compile(contract_project)
        _dbt_plan(["snapshot", "--project-dir", str(contract_project)])

        self._with_cast(contract_project, "CAST(5 AS INTEGER)")
        _dbt_compile(contract_project)

        result = _dbt_plan(
            ["check", "--project-dir", str(contract_project), "--dialect", "duckdb", "--no-color"]
        )
        assert "declared varchar, cast as INT -- data type mismatch" in result.stdout
        assert result.returncode == 2, result.stdout

        build = _dbt_build(contract_project, "fct_contract")
        assert build.returncode != 0
        assert "data type mismatch" in build.stdout

    def test_the_same_family_is_not_reported_and_dbt_builds_it(self, contract_project):
        """`varchar` and `TEXT` are the same type here. A finding would be a false one."""
        self._with_cast(contract_project, "CAST('c' AS VARCHAR)")
        _dbt_compile(contract_project)
        _dbt_plan(["snapshot", "--project-dir", str(contract_project)])

        self._with_cast(contract_project, "CAST('c' AS TEXT)")
        _dbt_compile(contract_project)

        result = _dbt_plan(
            ["check", "--project-dir", str(contract_project), "--dialect", "duckdb", "--no-color"]
        )
        assert "data type mismatch" not in result.stdout, result.stdout

        assert _dbt_build(contract_project, "fct_contract").returncode == 0


@pytest.fixture
def two_hop_project(tmp_path):
    """Three ways a downstream model can read a column without naming its source.

    stg_orders writes to `orders_clean` (an alias), `mid` passes it through with a
    star, and the three fct_ models read customer_id directly, two hops away, and
    through the style-guide import-CTE pattern. dbt build fails all three.
    """
    project = tmp_path / "two_hop"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: two_hop\nversion: '1.0.0'\nprofile: two_hop_profile\n"
        'model-paths: ["models"]\ntarget-path: "target"\n'
    )
    (project / "profiles.yml").write_text(
        "two_hop_profile:\n  target: dev\n  outputs:\n    dev:\n"
        '      type: duckdb\n      path: ":memory:"\n'
    )
    m = project / "models"
    (m / "stg_orders.sql").write_text(
        "{{ config(materialized='view', alias='orders_clean') }}\n"
        "SELECT 1 AS order_id, 'c' AS customer_id, 'open' AS status\n"
    )
    (m / "mid.sql").write_text(
        "{{ config(materialized='view') }}\nSELECT * FROM {{ ref('stg_orders') }}\n"
    )
    (m / "fct_alias.sql").write_text(
        "{{ config(materialized='table') }}\nSELECT customer_id FROM {{ ref('stg_orders') }}\n"
    )
    (m / "fct_twohop.sql").write_text(
        "{{ config(materialized='table') }}\nSELECT customer_id FROM {{ ref('mid') }}\n"
    )
    (m / "fct_cte.sql").write_text(
        "{{ config(materialized='table') }}\n"
        "WITH orders AS (SELECT * FROM {{ ref('stg_orders') }})\n"
        "SELECT customer_id FROM orders\n"
    )
    return project


class TestReadsThatNeverNameTheChangedModel:
    def _drop_customer_id(self, project):
        (project / "models" / "stg_orders.sql").write_text(
            "{{ config(materialized='view', alias='orders_clean') }}\n"
            "SELECT 1 AS order_id, 'open' AS status\n"
        )

    def test_all_three_readers_are_reported(self, two_hop_project):
        _dbt_compile(two_hop_project)
        _dbt_plan(["snapshot", "--project-dir", str(two_hop_project)])
        self._drop_customer_id(two_hop_project)
        _dbt_compile(two_hop_project)

        result = _dbt_plan(["check", "--project-dir", str(two_hop_project), "--no-color"])
        findings = [ln for ln in result.stdout.splitlines() if "BROKEN_REF" in ln]
        assert len(findings) == 3, result.stdout
        for name in ("fct_alias", "fct_twohop", "fct_cte"):
            assert any(name in ln and "customer_id" in ln for ln in findings), result.stdout
        # The passthrough loses the column without failing; it is not a broken ref.
        assert not any("mid:" in ln for ln in findings)
        assert result.returncode == 1

    def test_dbt_agrees_all_three_fail_and_the_passthrough_does_not(self, two_hop_project):
        _dbt_compile(two_hop_project)
        self._drop_customer_id(two_hop_project)
        build = _dbt_build(two_hop_project, "+fct_alias +fct_twohop +fct_cte")
        assert build.returncode != 0
        for name in ("fct_alias", "fct_twohop", "fct_cte"):
            assert f"ERROR creating sql table model main.{name}" in build.stdout, build.stdout
        assert "OK created sql view model main.mid" in build.stdout


class TestADeletedModelAgainstRealDbt:
    """dbt compile leaves the deleted model's compiled SQL behind. The manifest does not."""

    @pytest.fixture
    def leaf_project(self, tmp_path):
        project = tmp_path / "leaf"
        (project / "models").mkdir(parents=True)
        (project / "dbt_project.yml").write_text(
            "name: leaf\nversion: '1.0.0'\nprofile: leaf_profile\n"
            'model-paths: ["models"]\ntarget-path: "target"\n'
        )
        (project / "profiles.yml").write_text(
            "leaf_profile:\n  target: dev\n  outputs:\n    dev:\n"
            '      type: duckdb\n      path: ":memory:"\n'
        )
        (project / "models" / "keep.sql").write_text(
            "{{ config(materialized='table') }}\nSELECT 1 AS a\n"
        )
        (project / "models" / "doomed.sql").write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "SELECT 1 AS a, 2 AS b\n"
        )
        return project

    def test_the_orphan_is_still_in_target_and_the_model_is_still_reported(self, leaf_project):
        _dbt_compile(leaf_project)
        _dbt_plan(["snapshot", "--project-dir", str(leaf_project)])
        (leaf_project / "models" / "doomed.sql").unlink()
        _dbt_compile(leaf_project)

        # The measured cause: dbt did not clean up after itself.
        orphan = leaf_project / "target" / "compiled" / "leaf" / "models" / "doomed.sql"
        assert orphan.exists(), "dbt started cleaning target/; this test's premise is gone"

        result = _dbt_plan(["check", "--project-dir", str(leaf_project), "--no-color"])
        assert "DESTRUCTIVE  doomed (incremental, sync_all_columns)" in result.stdout
        assert "MODEL REMOVED" in result.stdout
        assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("change", ["remove", "add"])
def test_ignore_schema_changes_warn_before_real_incremental_failure(tmp_path, change):
    """Build the old target first: a first build would hide the ignore failure."""
    project = tmp_path / "ignore_project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: ignore_project\nversion: "1.0"\nconfig-version: 2\nprofile: ignore_project\n'
    )
    (project / "profiles.yml").write_text(
        "ignore_project:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: warehouse.duckdb\n      threads: 1\n"
    )
    old = "select 1 as id, 2 as tax" if change == "remove" else "select 1 as id"
    new = "select 1 as id" if change == "remove" else "select 1 as id, 2 as tax"
    model = project / "models/orders.sql"
    config = "{{ config(materialized='incremental', on_schema_change='ignore') }}\n"
    model.write_text(config + old)
    build = subprocess.run(
        [_DBT, "run", "--profiles-dir", "."],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert _dbt_plan(["snapshot", "--project-dir", str(project)]).returncode == 0
    model.write_text(config + new)
    if change == "add":
        (project / "models/reader.sql").write_text("select tax from {{ ref('orders') }}")
    _dbt_compile(project)
    check = _dbt_plan(["check", "--project-dir", str(project), "--format", "json"])
    assert check.returncode != 0, check.stdout
    assert "on_schema_change=ignore" in check.stdout
    build = subprocess.run(
        [_DBT, "run", "--profiles-dir", "."],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode != 0, build.stdout
    assert "tax" in build.stdout + build.stderr


def _run_git(project, *args):
    result = subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


_RUN_BASE_SQL = (
    "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
    "select 1 as order_id, 2 as amount\n"
)
_RUN_INVALID_SQL = "{{ exceptions.raise_compiler_error('intentional run test failure') }}\n"


@pytest.fixture
def run_project(tmp_path):
    project = tmp_path / "run project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: run_project\nversion: '1.0.0'\nprofile: test_profile\n", encoding="utf-8"
    )
    shutil.copy2(DBT_PROJECT / "profiles.yml", project / "profiles.yml")
    # --profiles-dir . makes dbt write its own .user.yml beside the test profile.
    (project / ".gitignore").write_text("target/\nlogs/\n.user.yml\n", encoding="utf-8")
    (project / "models" / "orders.sql").write_text(_RUN_BASE_SQL, encoding="utf-8")
    _run_git(project, "init", "-q", "-b", "main")
    _run_git(project, "config", "user.email", "test@example.com")
    _run_git(project, "config", "user.name", "Run test")
    _run_git(project, "config", "core.autocrlf", "false")
    _run_git(project, "add", "-A")
    _run_git(project, "commit", "-qm", "initial project")
    model = project / "models" / "orders.sql"
    model.write_text(_RUN_BASE_SQL + "-- earlier work\n", encoding="utf-8")
    _run_git(project, "stash", "push", "-qm", "existing user stash")
    initialized = _dbt_plan(["init", "--project-dir", str(project)])
    assert initialized.returncode == 0, initialized.stderr
    return project


def _run_project_state(project):
    return (
        _run_git(project, "rev-parse", "HEAD"),
        _run_git(project, "symbolic-ref", "--short", "HEAD"),
        _run_git(project, "status", "--porcelain", "--untracked-files=all"),
        _run_git(project, "diff", "--binary"),
        _run_git(project, "diff", "--cached", "--binary"),
        _run_git(project, "stash", "list", "--format=%H"),
        (project / "models" / "orders.sql").read_bytes(),
        (project / "notes.txt").read_bytes() if (project / "notes.txt").exists() else None,
    )


def _real_run(project):
    command = shlex.join([_DBT, "compile", "--profiles-dir", ".", "--target-path", "target"])
    return _dbt_plan(
        ["run", "--project-dir", str(project), "--compile-command", command, "--format", "json"],
        timeout=150,  # run invokes dbt twice, unlike the artifact-only commands
    )


class TestRunWithRealCompile:
    def test_init_and_repeated_runs_preserve_work_and_report_column_drop(self, run_project):
        before = _run_project_state(run_project)
        first = _real_run(run_project)
        assert first.returncode == 0, first.stderr
        json.loads(first.stdout)
        assert (run_project / ".dbt-plan" / "base").exists()
        assert _run_project_state(run_project) == before

        model = run_project / "models" / "orders.sql"
        model.write_text(_RUN_BASE_SQL + "-- staged review note\n", encoding="utf-8")
        _run_git(run_project, "add", "models/orders.sql")
        model.write_text(_RUN_BASE_SQL.replace(", 2 as amount", ""), encoding="utf-8")
        (run_project / "notes.txt").write_bytes(b"untracked review notes\n")
        before = _run_project_state(run_project)
        for _ in range(2):
            result = _real_run(run_project)
            assert result.returncode == 1, result.stderr
            report = json.loads(result.stdout)
            assert any(
                model["model_name"] == "orders" and model["safety"] == "destructive"
                for model in report["models"]
            )
            assert "amount" in result.stdout
            assert _run_project_state(run_project) == before

    @pytest.mark.parametrize("phase", ["baseline", "current"])
    def test_failed_compile_preserves_work_index_and_existing_stash(self, run_project, phase):
        model = run_project / "models" / "orders.sql"
        if phase == "baseline":
            model.write_text(_RUN_INVALID_SQL, encoding="utf-8")
            _run_git(run_project, "add", "models/orders.sql")
            _run_git(run_project, "commit", "-qm", "baseline that cannot compile")
        model.write_text(_RUN_BASE_SQL + "-- staged review note\n", encoding="utf-8")
        _run_git(run_project, "add", "models/orders.sql")
        current = _RUN_BASE_SQL if phase == "baseline" else _RUN_INVALID_SQL
        model.write_text(current, encoding="utf-8")
        (run_project / "notes.txt").write_bytes(b"untracked review notes\n")
        (run_project / ".dbt-plan").mkdir()
        before = _run_project_state(run_project)
        result = _real_run(run_project)
        assert result.returncode == 3, result.stderr
        assert f"compile failed for {phase}" in result.stderr
        assert _run_project_state(run_project) == before


@pytest.mark.parametrize("finding", ["safe", "warning", "destructive"])
def test_generated_workflow_compiles_reports_and_gates_real_changes(
    run_project, tmp_path, finding
):
    from tests.test_generated_ci_execution import execute, outputs

    _run_git(run_project, "add", "-A")
    _run_git(run_project, "commit", "-qm", "initialized project")
    base = _run_git(run_project, "rev-parse", "HEAD").strip()
    changed = _RUN_BASE_SQL + "-- current revision\n"
    if finding != "safe":
        changed = changed.replace(", 2 as amount", "")
    if finding == "warning":
        changed = changed.replace("sync_all_columns", "fail")
    (run_project / "models/orders.sql").write_text(changed, encoding="utf-8")
    _run_git(run_project, "add", "models/orders.sql")
    _run_git(run_project, "commit", "-qm", "current model change")
    head = _run_git(run_project, "rev-parse", "HEAD").strip()
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {
        **os.environ,
        "PATH": str(_VENV_BIN) + os.pathsep + os.environ.get("PATH", ""),
        "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(runner / "outputs"),
        "GITHUB_STEP_SUMMARY": str(runner / "summary"),
        "DBT_PROFILES_DIR": str(run_project),
        "BASE_REF": base,
        "HEAD_REF": head,
    }
    # Use this checkout's module; an editable console script can point elsewhere.
    prefix = f'dbt-plan() {{ {shlex.join(_DBT_PLAN_ARGV)} "$@"; }}\n'
    snapshot = execute("Snapshot base", run_project, env, prefix)
    assert snapshot.returncode == 0, snapshot.stdout + snapshot.stderr
    checked = execute("Check current", run_project, env, prefix)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    code = outputs(env)["exit-code"]
    assert code == {"safe": "0", "warning": "2", "destructive": "1"}[finding]
    report = json.loads((runner / "dbt-plan-report.json").read_text())
    assert report["parse_failures"] == []
    if finding != "safe":
        assert report["models"][0]["safety"] == finding
    env.update(CODE=code, FAIL_ON="destructive")
    rendered = execute("Report", run_project, env, prefix)
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert (runner / "summary").read_text().strip()
    assert execute("Gate", run_project, env).returncode == (1 if finding == "destructive" else 0)
    assert _run_git(run_project, "rev-parse", "HEAD").strip() == head


class TestAudit24RulesAgainstRealDbt:
    """Issue #151: compare the static plan with an already-built DuckDB target."""

    @pytest.fixture
    def rule_project(self, tmp_path):
        project = tmp_path / "audit24_rules"
        (project / "models").mkdir(parents=True)
        (project / "macros").mkdir()
        (project / "dbt_project.yml").write_text(
            "name: audit24_rules\nversion: '1.0'\nprofile: audit24_rules\n",
            encoding="utf-8",
        )
        (project / "profiles.yml").write_text(
            "audit24_rules:\n  target: dev\n  outputs:\n    dev:\n"
            "      type: duckdb\n      path: dev.duckdb\n      threads: 1\n",
            encoding="utf-8",
        )
        return project

    def _snapshot_built(self, project):
        built = _dbt_run(project)
        assert built.returncode == 0, built.stdout + built.stderr
        snapshot = _dbt_plan(["snapshot", "--project-dir", str(project)])
        assert snapshot.returncode == 0, snapshot.stdout + snapshot.stderr

    def _check(self, project):
        _dbt_compile(project)
        result = _dbt_plan(
            ["check", "--project-dir", str(project), "--dialect", "duckdb", "--format", "json"]
        )
        assert result.returncode in (0, 1, 2), result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["stale_sources"] == [], result.stdout
        assert report["uncompiled_models"] == [], result.stdout
        return result, report

    @pytest.mark.parametrize("osc", ["ignore", "fail", "append_new_columns", "sync_all_columns"])
    @pytest.mark.parametrize("change", ["add", "remove"])
    def test_incremental_schema_change_on_existing_target(self, rule_project, osc, change):
        config = "{{ config(materialized='incremental', on_schema_change='" + osc + "') }}\n"
        narrow = "select 1 as order_id, 'open' as status\n"
        wide = "select 1 as order_id, 'open' as status, 2 as amount\n"
        old, new = (narrow, wide) if change == "add" else (wide, narrow)
        model = rule_project / "models/fct_orders.sql"
        model.write_text(config + old, encoding="utf-8")
        self._snapshot_built(rule_project)
        assert _fct_orders_columns(rule_project) == (
            ["order_id", "status"] if change == "add" else ["order_id", "status", "amount"]
        )
        model.write_text(config + new, encoding="utf-8")
        check, report = self._check(rule_project)
        expected_code = (
            2
            if osc in {"ignore", "fail"} or (osc == "append_new_columns" and change == "remove")
            else (1 if osc == "sync_all_columns" and change == "remove" else 0)
        )
        assert check.returncode == expected_code, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert any(item["model_name"] == "fct_orders" for item in report["models"])
        if osc == "sync_all_columns" and change == "remove":
            assert "DROP COLUMN" in check.stdout and "amount" in check.stdout
        built = _dbt_run(rule_project)
        should_fail = osc == "fail" or (osc == "ignore" and change == "remove")
        assert (built.returncode != 0) == should_fail, built.stdout + built.stderr
        expected = ["order_id", "status"]
        if (change == "remove" and osc != "sync_all_columns") or (
            change == "add" and osc in {"append_new_columns", "sync_all_columns"}
        ):
            expected.append("amount")
        assert _fct_orders_columns(rule_project) == expected
        import duckdb

        with duckdb.connect(str(rule_project / "dev.duckdb"), read_only=True) as connection:
            assert connection.execute("select count(*) from fct_orders").fetchone() == (
                1 if should_fail else 2,
            )
            if osc == "append_new_columns":
                # Schema retention is not backfill: an added/retired column has
                # one original value and one NULL across the two successful runs.
                assert connection.execute(
                    "select amount from fct_orders order by amount nulls last"
                ).fetchall() == [(2,), (None,)]

    def test_ephemeral_star_uses_real_inlined_cte(self, rule_project):
        stage = rule_project / "models/stg_orders.sql"
        config = "{{ config(materialized='ephemeral') }}\n"
        stage.write_text(config + "select 1 as order_id, 2 as amount\n", encoding="utf-8")
        downstream = rule_project / "models/fct_orders.sql"
        downstream.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "select * from {{ ref('stg_orders') }}\n",
            encoding="utf-8",
        )
        self._snapshot_built(rule_project)
        original_model = downstream.read_bytes()
        compiled = rule_project / "target/compiled/audit24_rules/models/fct_orders.sql"
        assert "__dbt__cte__stg_orders" in compiled.read_text(encoding="utf-8")
        stage.write_text(config + "select 1 as order_id\n", encoding="utf-8")
        check, report = self._check(rule_project)
        assert downstream.read_bytes() == original_model
        assert "__dbt__cte__stg_orders" in compiled.read_text(encoding="utf-8")
        assert report["parse_failures"] == []
        assert check.returncode == 1, check.stdout + check.stderr
        assert "DROP COLUMN" in check.stdout and "amount" in check.stdout
        built = _dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert _fct_orders_columns(rule_project) == ["order_id"]

    def test_materialization_change_is_reported_and_executed(self, rule_project):
        model = rule_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='view') }}\nselect 1 as order_id\n", encoding="utf-8"
        )
        self._snapshot_built(rule_project)
        model.write_text(
            "{{ config(materialized='table') }}\nselect 1 as order_id\n", encoding="utf-8"
        )
        check, report = self._check(rule_project)
        assert "materialization" in check.stdout.lower(), check.stdout
        assert "view" in check.stdout and "table" in check.stdout
        assert report["parse_failures"] == []
        built = _dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        import duckdb

        with duckdb.connect(str(rule_project / "dev.duckdb"), read_only=True) as connection:
            assert connection.execute(
                "select table_type from information_schema.tables where table_name='fct_orders'"
            ).fetchone() == ("BASE TABLE",)

    def test_macro_only_edit_changes_compiled_schema(self, rule_project):
        macro = rule_project / "macros/order_columns.sql"
        macro.write_text(
            "{% macro order_columns() %}1 as order_id, 2 as amount{% endmacro %}\n",
            encoding="utf-8",
        )
        model = rule_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "select {{ order_columns() }}\n",
            encoding="utf-8",
        )
        self._snapshot_built(rule_project)
        original = model.read_bytes()
        macro.write_text(
            "{% macro order_columns() %}1 as order_id{% endmacro %}\n", encoding="utf-8"
        )
        check, report = self._check(rule_project)
        assert model.read_bytes() == original
        assert check.returncode == 1, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert "DROP COLUMN" in check.stdout and "amount" in check.stdout
        built = _dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert _fct_orders_columns(rule_project) == ["order_id"]


def test_select_graph_operators_on_real_compilation(dbt_project):
    _dbt_compile(dbt_project)
    snapshot = _dbt_plan(["snapshot", "--project-dir", str(dbt_project)])
    assert snapshot.returncode == 0, snapshot.stderr
    for relative in ["staging/stg_orders.sql", "marts/fct_orders.sql"]:
        path = dbt_project / "models" / relative
        path.write_text(path.read_text() + "\n-- changed for selection regression\n")
    _dbt_compile(dbt_project)
    cases = {
        "stg_orders": {"stg_orders"},
        "stg_orders+": {"stg_orders", "fct_orders"},
        "+dim_books": {"stg_orders"},
        "+fct_orders": {"stg_orders", "fct_orders"},
        "+stg_orders+": {"stg_orders", "fct_orders"},
        "stg_orders,fct_orders": {"stg_orders", "fct_orders"},
        "dim_books": set(),
    }
    for term, expected in cases.items():
        result = _dbt_plan(
            ["check", "--project-dir", str(dbt_project), "--format", "json", "--select", term]
        )
        assert result.returncode == 0, (term, result.stdout, result.stderr)
        report = json.loads(result.stdout)
        assert {m["model_name"] for m in report["models"]} == expected, term
    for term in [
        "tag:nightly",
        "stg_orders+2",
        "2+dim_books",
        "@stg_orders",
        "stg_orders,typo",
        "",
        "fct_order",
    ]:
        result = _dbt_plan(
            ["check", "--project-dir", str(dbt_project), "--format", "json", "--select", term]
        )
        assert result.returncode == 3, (term, result.stdout, result.stderr)
        assert result.stdout == ""
        assert "Error: --select" in result.stderr


@pytest.mark.parametrize("renamed", [False, True])
def test_select_versioned_models_and_defined_in_on_real_compilation(versioned_project, renamed):
    project = versioned_project
    stem = "orders_current" if renamed else "fct_orders_v2"
    if renamed:
        (project / "models/fct_orders_v2.sql").rename(project / f"models/{stem}.sql")
        schema = project / "models/schema.yml"
        schema.write_text(
            schema.read_text().replace(
                "      - v: 2", "      - v: 2\n        defined_in: orders_current"
            )
        )
    reader = project / "models/reader.sql"
    reader.write_text("select order_id from {{ ref('fct_orders', v=2) }}\n")
    _dbt_compile(project)
    snapshot = _dbt_plan(["snapshot", "--project-dir", str(project)])
    assert snapshot.returncode == 0, snapshot.stderr
    (project / f"models/{stem}.sql").write_text(_VERSIONED_V2.format(extra=""))
    reader.write_text(reader.read_text() + "-- changed reader\n")
    _dbt_compile(project)
    cases = {
        "fct_orders_v2": {stem},
        stem: {stem},
        "fct_orders_v2+": {stem, "reader"},
        "+reader": {stem, "reader"},
        "fct_orders_v1": set(),
        "fct_orders_v1+": set(),
        "fct_orders_v1,fct_orders_v2": {stem},
    }
    for term, expected in cases.items():
        result = _dbt_plan(
            ["check", "--project-dir", str(project), "--format", "json", "--select", term]
        )
        assert result.returncode == (1 if stem in expected else 0), (
            term,
            result.stdout,
            result.stderr,
        )
        assert {m["model_name"] for m in json.loads(result.stdout)["models"]} == expected
    result = _dbt_plan(
        ["check", "--project-dir", str(project), "--format", "json", "--select", "fct_orders"]
    )
    assert result.returncode == 3
    assert result.stdout == ""
    assert "fct_orders" in result.stderr and "version" in result.stderr


def test_run_invalid_selection_restores_work_after_real_compile(run_project):
    before = _run_project_state(run_project)
    result = _dbt_plan(
        [
            "run",
            "--project-dir",
            str(run_project),
            "--format",
            "json",
            "--select",
            "@orders",
            "--compile-command",
            shlex.join([_DBT, "compile", "--profiles-dir", ".", "--target-path", "target"]),
        ],
        timeout=90,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout == ""
    assert "Error: --select" in result.stderr
    assert _run_project_state(run_project) == before


class TestPR200ReviewFixes:
    @staticmethod
    def project(tmp_path):
        (tmp_path / "dbt_project.yml").write_text(
            'name: review\nversion: "1.0"\nprofile: review\n', encoding="utf-8"
        )
        (tmp_path / "profiles.yml").write_text(
            "review:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: review.duckdb\n",
            encoding="utf-8",
        )
        return tmp_path

    @pytest.mark.parametrize("name", ["active_id", "not_null"])
    def test_local_test_and_builtin_override_cannot_hide_dropped_column(self, tmp_path, name):
        p = self.project(tmp_path)
        (p / "models").mkdir()
        (p / "macros").mkdir()
        model = p / "models/orders.sql"
        model.write_text("select 1 as id, 2 as tax", encoding="utf-8")
        (p / "models/schema.yml").write_text(
            f"version: 2\nmodels:\n  - name: orders\n    columns:\n      - name: id\n        data_tests: [{name}]\n",
            encoding="utf-8",
        )
        (p / "macros/custom_test.sql").write_text(
            "{% test " + name + "(model, column_name) %}\n"
            "select {{ column_name }} from {{ model }} where tax < 0\n{% endtest %}",
            encoding="utf-8",
        )
        _dbt_compile(p)
        assert _dbt_plan(["snapshot", "--project-dir", str(p)]).returncode == 0
        model.write_text("select 1 as id", encoding="utf-8")
        _dbt_compile(p)
        result = _dbt_plan(["check", "--project-dir", str(p), "--format", "json"])
        report = json.loads(result.stdout)
        assert result.returncode == 2, report
        assert not report.get("stale_sources")
        assert "data_test_failure" in result.stdout
        build = subprocess.run(
            [_DBT, "build", "--profiles-dir", "."],
            cwd=p,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert build.returncode != 0 and 'column "tax" not found' in build.stdout

    def test_partial_compile_does_not_trust_old_test_file(self, tmp_path):
        p = self.project(tmp_path)
        (p / "models").mkdir()
        model = p / "models/orders.sql"
        schema = p / "models/schema.yml"
        model.write_text("select 1 as id, 2 as tax", encoding="utf-8")
        schema.write_text(
            'version: 2\nmodels:\n  - name: orders\n    columns:\n      - name: id\n        data_tests:\n          - not_null:\n              config:\n                where: "id > 0"\n',
            encoding="utf-8",
        )
        _dbt_compile(p)
        assert _dbt_plan(["snapshot", "--project-dir", str(p)]).returncode == 0
        model.write_text("select 1 as id", encoding="utf-8")
        schema.write_text(
            schema.read_text(encoding="utf-8").replace("id > 0", "tax > 0"), encoding="utf-8"
        )
        compile_result = subprocess.run(
            [
                _DBT,
                "compile",
                "--profiles-dir",
                ".",
                "--select",
                "orders",
                "--indirect-selection",
                "empty",
            ],
            cwd=p,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert compile_result.returncode == 0, compile_result.stdout
        result = _dbt_plan(["check", "--project-dir", str(p), "--format", "json"])
        assert result.returncode == 2, result.stdout
        assert "data_test_unreadable" in result.stdout
        _dbt_compile(p)
        fresh = _dbt_plan(["check", "--project-dir", str(p), "--format", "json"])
        assert fresh.returncode == 2 and "data_test_failure" in fresh.stdout
        assert "data_test_unreadable" not in fresh.stdout

    @pytest.mark.parametrize("mixed", [False, True])
    @pytest.mark.parametrize("with_test", [False, True])
    def test_snapshot_sources_and_test_only_compiled_layout(self, tmp_path, mixed, with_test):
        p = self.project(tmp_path)
        (p / "snapshots").mkdir()
        source = p / "snapshots/history.sql"
        source.write_text(
            "{% snapshot history %}\n"
            "{{ config(target_schema='main', unique_key='id', strategy='check', check_cols=['tax']) }}\n"
            "select 1 as id, 2 as tax\n{% endsnapshot %}\n",
            encoding="utf-8",
        )
        if mixed:
            (p / "models").mkdir()
            (p / "models/orders.sql").write_text("select 1 as id", encoding="utf-8")
        if with_test:
            (p / "snapshots/schema.yml").write_text(
                "version: 2\nsnapshots:\n  - name: history\n    columns:\n      - name: id\n        data_tests: [not_null]\n",
                encoding="utf-8",
            )
        _dbt_compile(p)
        snapshot = _dbt_plan(["snapshot", "--project-dir", str(p)])
        assert snapshot.returncode == 0, snapshot.stderr
        clean = _dbt_plan(["check", "--project-dir", str(p), "--format", "json"])
        assert clean.returncode == 0, clean.stdout + clean.stderr
        source.write_text(
            source.read_text(encoding="utf-8").replace("2 as tax", "3 as tax"), encoding="utf-8"
        )
        _dbt_compile(p)
        changed = _dbt_plan(["check", "--project-dir", str(p), "--format", "json"])
        assert changed.returncode == 2, changed.stdout + changed.stderr
        assert "snapshot changed" in changed.stdout
        assert not json.loads(changed.stdout).get("stale_sources")
