"""PostgreSQL named WINDOW projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_named_window.sql"
EXPECTED = ["customer_id", "order_id", "order_rank", "running_amount"]


def test_window_name_inputs_stay_out_of_schema():
    """The window name and its ORDER BY input do not leak into the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the windowed projections keeps the remaining names only."""
    sql = (
        "SELECT customer_id, order_id FROM orders"
        " WINDOW customer_orders AS (PARTITION BY customer_id ORDER BY ordered_at);"
    )
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id", "order_id"]


def test_window_rename_keeps_output_names():
    """Renaming the window spec and both OVER references leaves names identical."""
    sql = (
        "SELECT customer_id, order_id,"
        " ROW_NUMBER() OVER per_customer AS order_rank,"
        " SUM(amount) OVER per_customer AS running_amount"
        " FROM orders WINDOW per_customer AS (PARTITION BY customer_id ORDER BY ordered_at);"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
