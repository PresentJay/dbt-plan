"""Resolving `SELECT *` through the CTEs of the same statement.

The canonical dbt staging model, the one dbt's own style guide teaches, ends in
`select * from renamed` where `renamed` is a CTE with an explicit column list.
Every column is right there in the file. dbt-plan used to answer `["*"]` to all
of them, which on a real project meant giving up on most models -- and once a
model falls back to the manifest's documented columns, a `schema.yml` that lists
only the tested columns makes both sides of the diff identical and the verdict a
confident, wrong "safe".

Resolution is only worth doing if it cannot invent an answer. Half the tests here
are the refusals: an unqualified star over a join, a set operation, a recursive
CTE, a source that is not a CTE at all. Each must fall back to `["*"]`, because a
column list that is merely *plausible* is worse than admitting ignorance -- it
would be compared against another plausible list and produce a silent "safe".
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import extract_columns

DIALECT = "duckdb"


def cols(sql: str) -> list[str] | None:
    return extract_columns(sql, dialect=DIALECT)


class TestTheCanonicalStagingModel:
    """What dbt's style guide produces, and what jaffle_shop actually compiles to."""

    def test_resolves_select_star_from_a_cte_with_explicit_columns(self):
        sql = """
        with source as (select * from "db"."main"."raw_orders"),
        renamed as (
            select id as order_id, user_id as customer_id, order_date, status
            from source
        )
        select * from renamed
        """
        assert cols(sql) == ["order_id", "customer_id", "order_date", "status"]

    def test_resolves_through_a_chain_of_ctes(self):
        sql = """
        with a as (select * from raw),
             b as (select order_id, customer_id from a),
             c as (select b.*, 1 as flag from b)
        select * from c
        """
        assert cols(sql) == ["order_id", "customer_id", "flag"]

    def test_resolves_a_qualified_star(self):
        sql = "with b as (select x, y from t) select b.* from b"
        assert cols(sql) == ["x", "y"]

    def test_qualified_stars_over_a_join_are_unambiguous(self):
        """Each star names its own source, so a join is fine here."""
        sql = """
        with a as (select x1 from t), b as (select y1 from u)
        select a.*, b.* from a join b on 1 = 1
        """
        assert cols(sql) == ["x1", "y1"]

    def test_star_mixed_with_explicit_columns_keeps_order(self):
        sql = "with a as (select x, y from t) select a.*, 1 as extra from a"
        assert cols(sql) == ["x", "y", "extra"]


class TestRefusals:
    """Every one of these would be a false safe if answered."""

    def test_unqualified_star_over_a_join_refuses(self):
        """The dangerous one.

        `select * from a join b` means a's columns *and* b's. Resolving only the
        FROM would silently drop b's, so removing the join would compare equal and
        report safe.
        """
        sql = """
        with a as (select x1, x2 from t), b as (select y1 from u)
        select * from a join b on a.x1 = b.y1
        """
        assert cols(sql) == ["*"]

    def test_unqualified_star_over_a_comma_join_refuses(self):
        sql = "with a as (select x1 from t), b as (select y1 from u) select * from a, b"
        assert cols(sql) == ["*"]

    def test_set_operation_inside_the_cte_refuses(self):
        sql = "with a as (select p from t union all select q from u) select * from a"
        assert cols(sql) == ["*"]

    def test_recursive_cte_refuses(self):
        sql = (
            "with recursive r as (select 1 as n union all select n + 1 from r where n < 5) "
            "select * from r"
        )
        assert cols(sql) == ["*"]

    def test_star_over_a_real_table_refuses(self):
        """No CTE to resolve against, and dbt-plan does not query the warehouse."""
        assert cols("select * from some_physical_table") == ["*"]

    def test_star_over_a_subquery_refuses(self):
        """Resolvable in principle; deliberately out of scope until it has its own tests."""
        assert cols("select * from (select a, b from t) s") == ["*"]

    def test_star_over_a_cte_that_itself_selects_star_from_a_table_refuses(self):
        """The chain has to bottom out in explicit columns, not in another unknown."""
        sql = "with a as (select * from raw_table) select * from a"
        assert cols(sql) == ["*"]

    def test_circular_cte_reference_terminates_and_refuses(self):
        sql = "with a as (select * from b), b as (select * from a) select * from a"
        assert cols(sql) == ["*"]

    def test_a_cte_resolving_to_nothing_refuses(self):
        """An empty column list would compare equal to any other empty list."""
        sql = "with a as (select from t) select * from a"
        assert cols(sql) in (["*"], None)


class TestExistingBehaviourIsUnchanged:
    def test_explicit_columns_still_work(self):
        assert cols("select a, b as c from t") == ["a", "c"]

    def test_plain_select_star_still_returns_star(self):
        assert cols("select * from t") == ["*"]

    def test_bigquery_except_is_untouched(self):
        got = extract_columns("select * except(b) from t", dialect="bigquery")
        assert got == ["* except(b)"]

    def test_except_over_a_cte_is_not_resolved(self):
        """EXCEPT plus resolution is two features; keep the existing marker."""
        sql = "with a as (select x, y from t) select * except(y) from a"
        assert extract_columns(sql, dialect="bigquery") == ["* except(y)"]

    def test_unparseable_sql_still_returns_none(self):
        assert cols("this is not sql at all !!!") is None


class TestDoesNotBlowUp:
    @pytest.mark.parametrize(
        "sql",
        [
            "with a as (select x from t) select * from a where x > 1",
            "with a as (select x from t) select * from a order by x limit 10",
            "with a as (select x from t), b as (select * from a) select * from b",
            "with a as (select x from t) select * from a group by x having count(*) > 1",
        ],
    )
    def test_common_clauses_do_not_break_resolution(self, sql):
        assert cols(sql) == ["x"]
