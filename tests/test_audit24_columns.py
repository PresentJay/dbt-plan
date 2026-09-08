"""Regressions for audit issues 142, 154 and 160."""

import pytest

from dbt_plan import columns


@pytest.mark.parametrize(
    "dialect, modifier", [("snowflake", "exclude"), ("duckdb", "exclude"), ("bigquery", "except")]
)
@pytest.mark.parametrize("projection", ["*", "s.*"])
def test_exclusions_expand_known_source(dialect, modifier, projection):
    def lookup(name):
        return ["id", "amount", "tax"] if name == "src" else None

    before = columns.extract_columns(
        f"select {projection} {modifier}(tax) from src s", dialect=dialect, table_columns=lookup
    )
    after = columns.extract_columns(
        f"select {projection} {modifier}(tax, amount) from src s",
        dialect=dialect,
        table_columns=lookup,
    )
    assert before == ["id", "amount"]
    assert after == ["id"]


def test_cte_exclusions_expand():
    assert columns.extract_columns(
        "with s as (select 1 as id, 2 as amount) select * exclude(amount) from s"
    ) == ["id"]


@pytest.mark.parametrize(
    "sql",
    [
        "select * exclude(amount) from src",
        "select s.* exclude(amount) from src s",
        "select * replace(1 as amount) from src",
    ],
)
def test_unresolved_modified_star_refuses(sql):
    assert columns.extract_columns(sql) is None


@pytest.mark.parametrize(
    "expression",
    [
        "count(*)",
        "coalesce(amount, 0)",
        "case when id=1 then 2 end",
        "amount + 1",
        "lower(amount)",
        "row_number() over (order by id)",
    ],
)
def test_partial_extraction_retains_known_names(expression):
    result = columns.extract_column_details(f"select id, {expression} from src")
    assert result.columns == ["id"]
    assert result.has_unknown
    assert columns.extract_columns(f"select id, {expression} from src") is None


@pytest.mark.parametrize("dialect", ["snowflake", "postgres", "redshift"])
@pytest.mark.parametrize(
    "sql",
    [
        'select "Id", amount from src',
        'select amount as "Amount" from src',
        'with s as (select 1 as "Id") select * from s',
    ],
)
def test_case_sensitive_quoted_names_refuse(dialect, sql):
    assert columns.extract_columns(sql, dialect=dialect) is None


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_insensitive_names_remain_usable(dialect):
    assert columns.extract_columns("select id as MixedCase", dialect=dialect) == ["mixedcase"]


def test_parse_failure_is_unknown():
    result = columns.extract_column_details("select (")
    assert result.columns == []
    assert result.has_unknown


def test_plain_star_compatibility():
    assert columns.extract_columns("select * from src") == ["*"]


def test_nested_unresolved_modified_star_refuses():
    sql = "with s as (select * exclude(amount) from src) select * from s"
    assert columns.extract_columns(sql) is None


def test_partial_projection_with_plain_star_still_refuses():
    result = columns.extract_column_details("select *, count(*) from src")
    assert result.has_unknown
    assert columns.extract_columns("select *, count(*) from src") is None


def test_nested_partial_projection_is_not_complete_star():
    sql = "with s as (select id, count(*) from src) select * from s"
    assert columns.extract_columns(sql) is None


def test_replace_with_known_source_refuses():
    assert (
        columns.extract_columns(
            "select * replace(1 as amount) from src", table_columns=lambda _: ["id", "amount"]
        )
        is None
    )


def test_unknown_excluded_name_refuses():
    assert (
        columns.extract_columns(
            "select * exclude(missing) from src", table_columns=lambda _: ["id"]
        )
        is None
    )
