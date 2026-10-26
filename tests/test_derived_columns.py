"""Explicit derived-table projections used by empty-table macros."""

import pytest

from dbt_plan.columns import extract_cast_types, extract_columns
from dbt_plan.predictor import Safety, predict_ddl


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake"])
def test_empty_table_macro_columns(dialect):
    sql = """
    SELECT * FROM (
        SELECT CAST(1 AS BIGINT) AS book_id,
               CAST('sample' AS VARCHAR) AS description
    ) AS empty_table WHERE 1 = 0
    """
    assert extract_columns(sql, dialect=dialect) == ["book_id", "description"]


def test_casts_cross_the_derived_table_boundary():
    sql = "SELECT * FROM (SELECT CAST(1 AS BIGINT) AS book_id) AS books"
    assert extract_cast_types(sql, dialect="duckdb") == {"book_id": "BIGINT"}


def test_explicit_projection_over_physical_source():
    sql = "SELECT * FROM (SELECT book_id, description FROM books) b"
    assert extract_columns(sql) == ["book_id", "description"]


def test_excluded_column_is_not_reintroduced():
    sql = """
    SELECT * EXCLUDE (description) FROM (
        SELECT 1 AS book_id, 'sample' AS description
    ) AS books
    """
    assert extract_columns(sql, dialect="duckdb") == ["book_id"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 AS book_id) b JOIN publishers p ON true",
        "SELECT * FROM (SELECT 1 AS book_id) b(renamed_id)",
        "SELECT * FROM (SELECT * FROM books) b",
        "SELECT b.* FROM (SELECT 1 AS book_id) b",
        "SELECT * FROM (WITH books AS (SELECT 1 AS book_id) SELECT book_id FROM books) b",
        "SELECT * FROM (SELECT 1 AS book_id UNION ALL SELECT 2) b",
        "SELECT * FROM (SELECT 1 AS book_id, 1 + 2) b",
        "SELECT * FROM (SELECT 'book') b",
        "SELECT * FROM (SELECT 1) b",
        "SELECT * REPLACE (2 AS book_id) FROM (SELECT 1 AS book_id) b",
        "SELECT * FROM (SELECT 1 AS book_id, 'novel' AS genre) b "
        "PIVOT (count(*) FOR genre IN ('novel'))",
    ],
)
def test_unsupported_sources_still_require_review(sql):
    columns = extract_columns(sql, dialect="duckdb")
    assert columns is None or columns == ["*"]
    assert (
        predict_ddl("books", "incremental", "sync_all_columns", ["book_id"], columns).safety
        == Safety.WARNING
    )


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("bigquery", "SELECT * FROM (SELECT AS STRUCT 1 AS book_id) b"),
        ("bigquery", "SELECT AS STRUCT * FROM (SELECT 1 AS book_id) b"),
        ("duckdb", "SELECT * FROM (SELECT COLUMNS('.*') AS renamed FROM books) b"),
        ("duckdb", "SELECT * FROM (SELECT UNNEST(record) AS expanded FROM books) b"),
    ],
)
def test_column_expanding_projections_still_require_review(dialect, sql):
    assert extract_columns(sql, dialect=dialect) in (None, ["*"])
