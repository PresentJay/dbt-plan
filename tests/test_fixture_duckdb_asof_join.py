"""DuckDB ASOF JOIN projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "duckdb"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "duckdb_asof_join.sql"
EXPECTED = ["order_id", "unit_price"]
JOIN = (
    "FROM orders AS o ASOF LEFT JOIN book_prices AS p "
    "ON o.book_id = p.book_id AND o.ordered_at >= p.valid_from"
)


def test_asof_join_keys_stay_out_of_projections():
    """The temporal join keys and right-side table do not become outputs."""
    sql = f"SELECT o.order_id, p.price AS unit_price {JOIN};"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(fixture, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected expression keeps the remaining name only."""
    sql = f"SELECT o.order_id {JOIN};"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_join_operator_variation_still_projects_same_names():
    """Changing >= to > changes nothing about the schema."""
    sql = (
        "SELECT o.order_id, p.price AS unit_price FROM orders AS o "
        "ASOF LEFT JOIN book_prices AS p "
        "ON o.book_id = p.book_id AND o.ordered_at > p.valid_from;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
