"""PostgreSQL DISTINCT ON projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_distinct_on.sql"
EXPECTED = ["customer_id", "latest_order_id"]


def test_distinct_on_inputs_stay_out_of_schema():
    """DISTINCT ON and ORDER BY inputs do not leak into the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected expression keeps the remaining name only."""
    sql = "SELECT customer_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_alias_rename_maps_to_new_output_name():
    """Renaming only the output alias changes the corresponding output name."""
    sql = (
        "SELECT DISTINCT ON (customer_id) customer_id,"
        " order_id AS final_order_id FROM orders"
        " ORDER BY customer_id, ordered_at DESC;"
    )
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id", "final_order_id"]
