"""PostgreSQL TABLESAMPLE and REPEATABLE regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_tablesample.sql"
EXPECTED = ["order_id", "customer_id"]


def test_sampling_and_seed_do_not_become_projections():
    """TABLESAMPLE percentage and REPEATABLE seed stay out of the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing a projection keeps the remaining name; the sampling clause adds none."""
    sql = "SELECT order_id FROM orders TABLESAMPLE SYSTEM (10) REPEATABLE (7);"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_independent_sampling_parameters_still_project_same_names():
    """Different sampling percentage and seed change nothing about the schema."""
    sql = "SELECT order_id, customer_id FROM orders TABLESAMPLE SYSTEM (20) REPEATABLE (9);"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
