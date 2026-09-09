"""PostgreSQL AT TIME ZONE projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "postgres"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postgres_timezone.sql"
EXPECTED = ["order_id", "ordered_utc"]


def test_timezone_literal_does_not_leak_into_schema():
    """The AT TIME ZONE literal and input timestamp stay out of the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected expression keeps the remaining name only."""
    sql = "SELECT order_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_independent_timezone_literal_still_projects_same_names():
    """A different timezone literal changes nothing about the schema."""
    sql = "SELECT order_id, ordered_at AT TIME ZONE 'Asia/Seoul' AS ordered_utc FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
