"""PostgreSQL aggregate FILTER projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_aggregate_filter.sql"
EXPECTED = ["customer_id", "paid_orders", "net_amount"]


def test_filter_predicates_stay_out_of_schema():
    """FILTER predicate-only columns do not leak into the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the aggregated projections keeps the group key name only."""
    sql = "SELECT customer_id FROM orders GROUP BY customer_id;"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_independent_predicate_change_keeps_names():
    """Changing only the FILTER predicate leaves the output names identical."""
    sql = (
        "SELECT customer_id,"
        " COUNT(*) FILTER (WHERE status = 'shipped') AS paid_orders,"
        " SUM(amount) FILTER (WHERE refunded = false) AS net_amount"
        " FROM orders GROUP BY customer_id;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
