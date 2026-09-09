"""Trino map subscript and element_at projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "trino"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trino_map_lookup.sql"
EXPECTED = ["order_id", "sales_channel"]


def test_map_subscript_retains_outer_alias():
    """The map lookup key and input map do not become projections."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected expression keeps the remaining name only."""
    sql = "SELECT order_id FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == ["order_id"]


def test_element_at_spelling_projects_same_names():
    """The element_at spellings retain the same outer alias."""
    sql = "SELECT order_id, element_at(attributes, 'channel') AS sales_channel FROM orders;"
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED
