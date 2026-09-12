"""PostgreSQL GROUPING SETS and GROUPING projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_grouping_sets.sql"
EXPECTED = ["customer_id", "store_id", "total_amount", "grouping_mask"]


def test_grouping_sets_projects_exact_names():
    """Empty grouping sets do not invent extra output columns."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the GROUPING mask keeps the remaining names only."""
    sql = (
        "SELECT customer_id, store_id, SUM(amount) AS total_amount"
        " FROM orders"
        " GROUP BY GROUPING SETS ((customer_id, store_id), (customer_id), ());"
    )
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id", "store_id", "total_amount"]


def test_grouping_alias_rename_requires_updated_output_name():
    """Renaming only the GROUPING output alias updates that name."""
    sql = (
        "SELECT customer_id, store_id, SUM(amount) AS total_amount,"
        " GROUPING(customer_id, store_id) AS grouping_level"
        " FROM orders"
        " GROUP BY GROUPING SETS ((customer_id, store_id), (customer_id), ());"
    )
    assert extract_columns(sql, dialect=DIALECT) == [
        "customer_id",
        "store_id",
        "total_amount",
        "grouping_level",
    ]
