"""DuckDB list-comprehension projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "duckdb"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "duckdb_list_comprehension.sql"
EXPECTED = ["order_id", "doubled_quantities"]


def test_comprehension_binding_stays_out_of_schema():
    """The list-comprehension variable and source list do not leak into the schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the comprehension keeps the remaining name only."""
    sql = "SELECT order_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_comprehension_variable_rename_keeps_names():
    """Renaming the variable consistently leaves output names unchanged."""
    sql = (
        "SELECT order_id,"
        " [quantity * 2 FOR quantity IN quantities IF quantity > 0] AS doubled_quantities"
        " FROM orders;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
