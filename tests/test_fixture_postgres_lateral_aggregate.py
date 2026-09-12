"""PostgreSQL correlated LATERAL aggregate projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_lateral_aggregate.sql"
EXPECTED = ["customer_id", "last_order_at"]
LATERAL = (
    "LEFT JOIN LATERAL (SELECT MAX(o.ordered_at) AS last_order_at "
    "FROM orders AS o WHERE o.customer_id = c.customer_id) AS recent ON TRUE"
)


def test_only_outer_projections_count():
    """The inner aggregate and correlation predicate add no output columns."""
    sql = f"SELECT c.customer_id, recent.last_order_at FROM customers AS c {LATERAL};"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(fixture, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """The removed projection must not reappear from inner expressions or join predicates."""
    sql = f"SELECT c.customer_id FROM customers AS c {LATERAL};"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_inner_predicate_change_still_projects_same_names():
    """An extra inner filter changes nothing about the outer schema."""
    sql = (
        "SELECT c.customer_id, recent.last_order_at FROM customers AS c "
        "LEFT JOIN LATERAL (SELECT MAX(o.ordered_at) AS last_order_at "
        "FROM orders AS o WHERE o.customer_id = c.customer_id AND o.status = 'paid') "
        "AS recent ON TRUE;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
