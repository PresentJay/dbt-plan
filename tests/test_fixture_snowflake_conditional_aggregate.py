"""Snowflake COUNT_IF and IFF aggregate projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "snowflake"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "snowflake_conditional_aggregate.sql"
EXPECTED = ["customer_id", "paid_orders"]
GROUP = "FROM orders GROUP BY customer_id"


def test_count_if_predicate_input_stays_out_of_schema():
    """COUNT_IF predicate inputs are not projected columns."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the aggregate keeps the grouping column only."""
    sql = f"SELECT customer_id {GROUP};"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_iff_spelling_projects_same_names():
    """SUM(IFF(...)) behaves the same as COUNT_IF for projected names."""
    sql = f"SELECT customer_id, SUM(IFF(status = 'paid', 1, 0)) AS paid_orders {GROUP};"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
