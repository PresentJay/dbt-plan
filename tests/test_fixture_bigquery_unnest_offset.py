"""BigQuery UNNEST WITH OFFSET projections regression fixture."""

from pathlib import Path

from dbt_plan.columns import extract_columns

DIALECT = "bigquery"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bigquery_unnest_offset.sql"
EXPECTED = ["order_id", "item_sku", "item_offset"]


def test_unnest_with_offset_projects_exact_names():
    """UNNEST WITH OFFSET yields the array element and offset names in order."""
    sql = FIXTURE.read_text(encoding="utf-8")
    assert extract_columns(sql, dialect=DIALECT) == EXPECTED


def test_projection_removal_control():
    """Removing the projected element keeps the remaining names only."""
    sql = (
        "SELECT o.order_id, item_offset"
        " FROM orders AS o"
        " CROSS JOIN UNNEST(o.items) AS item WITH OFFSET AS item_offset;"
    )
    assert extract_columns(sql, dialect=DIALECT) == ["order_id", "item_offset"]


def test_offset_alias_rename_requires_updated_output_name():
    """Renaming the WITH OFFSET alias in projection and alias updates the name."""
    sql = (
        "SELECT o.order_id, item.sku AS item_sku, item_position"
        " FROM orders AS o"
        " CROSS JOIN UNNEST(o.items) AS item WITH OFFSET AS item_position;"
    )
    assert extract_columns(sql, dialect=DIALECT) == ["order_id", "item_sku", "item_position"]
