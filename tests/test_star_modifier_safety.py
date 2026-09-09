"""A star carrying a modifier must never be resolved as a plain star.

`SELECT * EXCEPT(...)`, `EXCLUDE`, `RENAME` and `REPLACE` all change what the
star expands to. Resolving the star while ignoring the modifier produces the
*unmodified* column list -- and since the same thing happens on both sides of
the diff, adding an `EXCEPT(secret)` compares equal and reports SAFE while dbt
drops the column.

This was introduced in 0.8.0 by CTE star resolution and widened in 0.9.0 by
`ref()` resolution. Before those, every one of these returned ["*"] and the
verdict was "review required" -- the safe direction. The guard added with the
feature only checked `except_` on the outer expression, but for a qualified
`a.*` the modifier hangs off the inner Star node, so it was never seen.

Known EXCEPT/EXCLUDE modifiers resolve only when the source column list is
known. RENAME, REPLACE and unknown modifiers refuse with None rather than
falling back to a plain star that manifest documentation could make look safe.
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import extract_columns
from dbt_plan.predictor import predict_ddl


class TestQualifiedStarModifiers:
    @pytest.mark.parametrize(
        "dialect,sql,expected",
        [
            (
                "bigquery",
                "WITH a AS (SELECT p, q, s FROM t) SELECT a.* EXCEPT(s) FROM a",
                ["p", "q"],
            ),
            (
                "snowflake",
                "WITH a AS (SELECT p, q, s FROM t) SELECT a.* EXCLUDE (s) FROM a",
                ["p", "q"],
            ),
            (
                "snowflake",
                "WITH a AS (SELECT p, q FROM t) SELECT a.* RENAME (p AS r) FROM a",
                None,
            ),
            (
                "bigquery",
                "WITH a AS (SELECT p, q FROM t) SELECT a.* REPLACE(q + 1 AS q) FROM a",
                None,
            ),
        ],
    )
    def test_resolves_known_exclusions_and_refuses_other_modifiers(self, dialect, sql, expected):
        assert extract_columns(sql, dialect=dialect) == expected


class TestUnqualifiedStarModifiers:
    @pytest.mark.parametrize(
        "dialect,sql",
        [
            ("snowflake", "WITH a AS (SELECT p, q FROM t) SELECT * RENAME (p AS r) FROM a"),
            ("bigquery", "WITH a AS (SELECT p, q FROM t) SELECT * REPLACE(q + 1 AS q) FROM a"),
        ],
    )
    def test_refuses(self, dialect, sql):
        assert extract_columns(sql, dialect=dialect) is None

    def test_except_removes_known_cte_column(self):
        """Known CTE columns allow exact EXCEPT expansion."""
        sql = "WITH a AS (SELECT p, q, s FROM t) SELECT * EXCEPT(s) FROM a"
        assert extract_columns(sql, dialect="bigquery") == ["p", "q"]


class TestTheFalseSafeItself:
    def test_adding_except_to_a_qualified_star_is_not_safe(self):
        """The scenario, end to end through the predictor.

        `s` is being dropped. Both sides resolved to the same three columns, so
        the diff was empty and the verdict SAFE.
        """
        base = "WITH a AS (SELECT p, q, s FROM t) SELECT a.* FROM a"
        current = "WITH a AS (SELECT p, q, s FROM t) SELECT a.* EXCEPT(s) FROM a"

        b = extract_columns(base, dialect="bigquery")
        c = extract_columns(current, dialect="bigquery")
        verdict = predict_ddl("m", "incremental", "sync_all_columns", b, c, status="modified")

        assert verdict.safety.name == "DESTRUCTIVE"
        assert verdict.columns_removed == ["s"]

    def test_a_rename_is_not_safe_either(self):
        base = "WITH a AS (SELECT p, q FROM t) SELECT a.* FROM a"
        current = "WITH a AS (SELECT p, q FROM t) SELECT a.* RENAME (p AS r) FROM a"

        b = extract_columns(base, dialect="snowflake")
        c = extract_columns(current, dialect="snowflake")
        verdict = predict_ddl("m", "incremental", "sync_all_columns", b, c, status="modified")

        assert verdict.safety.name == "WARNING"


class TestPlainStarsStillResolve:
    """The guard must not undo the feature it protects."""

    def test_a_plain_qualified_star(self):
        sql = "WITH a AS (SELECT p, q FROM t) SELECT a.* FROM a"
        assert extract_columns(sql, dialect="snowflake") == ["p", "q"]

    def test_a_plain_unqualified_star(self):
        sql = "WITH a AS (SELECT p, q FROM t) SELECT * FROM a"
        assert extract_columns(sql, dialect="snowflake") == ["p", "q"]

    def test_a_ref_resolution_is_unaffected(self):
        got = extract_columns(
            'SELECT * FROM "j"."main"."stg"',
            dialect="duckdb",
            table_columns=lambda k: {"j.main.stg": ["a", "b"]}.get(k),
        )
        assert got == ["a", "b"]
