"""BigQuery SAFE_OFFSET array projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "bigquery"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bigquery_safe_offset.sql"
EXPECTED = ["order_id", "first_book_id"]


def test_array_index_and_array_name_do_not_become_projections():
    """SAFE_OFFSET naming and lookup stay out of the output schema."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected expression keeps the remaining name only."""
    sql = "SELECT order_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_independent_index_still_projects_same_names():
    """A different array index changes nothing about the schema."""
    sql = "SELECT order_id, book_ids[SAFE_OFFSET(2)] AS first_book_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
