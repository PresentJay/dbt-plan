"""DuckDB SEMI and ANTI JOIN projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "duckdb"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "duckdb_semi_anti.sql"
EXPECTED = ["customer_id", "customer_name"]
JOIN = "FROM customers AS c SEMI JOIN orders AS o ON c.customer_id = o.customer_id"


def test_semi_join_retains_only_left_projected_columns():
    """Right-side predicate columns are not outputs of a SEMI JOIN."""
    sql = f"SELECT c.customer_id, c.customer_name {JOIN};"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(fixture, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """The removed projection must not reappear from the join predicate."""
    sql = f"SELECT c.customer_id {JOIN};"
    assert extract_columns(sql, dialect=DIALECT) == ["customer_id"]


def test_anti_join_projects_same_left_names():
    """ANTI JOIN semantics differ, but the projected schema is unchanged."""
    sql = (
        "SELECT c.customer_id, c.customer_name FROM customers AS c "
        "ANTI JOIN orders AS o ON c.customer_id = o.customer_id;"
    )
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
