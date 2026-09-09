"""DuckDB GROUP BY ALL projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "duckdb"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "duckdb_group_by_all.sql"
EXPECTED = ["customer_id", "store_id", "total_amount"]


def test_group_by_all_projects_concrete_names():
    """GROUP BY ALL resolves to concrete group keys, never a wildcard column."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the aggregate keeps the group key names only."""
    sql = "SELECT customer_id, store_id FROM orders GROUP BY ALL;"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id", "store_id"]


def test_alias_rename_requires_updated_output_name():
    """Renaming the aggregate alias changes the corresponding output name."""
    sql = "SELECT customer_id, store_id, SUM(amount) AS gross_amount FROM orders GROUP BY ALL;"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id", "store_id", "gross_amount"]
