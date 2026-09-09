"""PostgreSQL ordered-set aggregate projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_ordered_set.sql"
EXPECTED = ["customer_id", "median_amount"]


def test_ordered_set_inputs_stay_out_of_schema():
    """WITHIN GROUP ordering input does not leak into the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the ordered-set aggregate keeps the group key name only."""
    sql = "SELECT customer_id FROM orders GROUP BY customer_id;"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_order_direction_change_keeps_names():
    """Reversing only the WITHIN GROUP ordering leaves output names unchanged."""
    sql = (
        "SELECT customer_id,"
        " PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount DESC) AS median_amount"
        " FROM orders GROUP BY customer_id;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
