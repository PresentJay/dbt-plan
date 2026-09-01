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

The fix is a whitelist rather than a list of known modifiers: a star with any
argument at all is refused. A modifier this code has never heard of must not
default to being ignored.
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import extract_columns
from dbt_plan.predictor import predict_ddl


class TestQualifiedStarModifiersAreRefused:
    @pytest.mark.parametrize(
        "dialect,sql",
        [
            ("bigquery", "WITH a AS (SELECT p, q, s FROM t) SELECT a.* EXCEPT(s) FROM a"),
            ("snowflake", "WITH a AS (SELECT p, q, s FROM t) SELECT a.* EXCLUDE (s) FROM a"),
            ("snowflake", "WITH a AS (SELECT p, q FROM t) SELECT a.* RENAME (p AS r) FROM a"),
            ("bigquery", "WITH a AS (SELECT p, q FROM t) SELECT a.* REPLACE(q + 1 AS q) FROM a"),
        ],
    )
    def test_refuses(self, dialect, sql):
        assert extract_columns(sql, dialect=dialect) == ["*"]


class TestUnqualifiedStarModifiersAreRefused:
    @pytest.mark.parametrize(
        "dialect,sql",
        [
            ("snowflake", "WITH a AS (SELECT p, q FROM t) SELECT * RENAME (p AS r) FROM a"),
            ("bigquery", "WITH a AS (SELECT p, q FROM t) SELECT * REPLACE(q + 1 AS q) FROM a"),
        ],
    )
    def test_refuses(self, dialect, sql):
        assert extract_columns(sql, dialect=dialect) == ["*"]

    def test_except_keeps_its_existing_marker(self):
        """The unqualified EXCEPT path predates resolution and is unchanged."""
        sql = "WITH a AS (SELECT p, q, s FROM t) SELECT * EXCEPT(s) FROM a"
        assert extract_columns(sql, dialect="bigquery") == ["* except(s)"]


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

        assert verdict.safety.name != "SAFE"

    def test_a_rename_is_not_safe_either(self):
        base = "WITH a AS (SELECT p, q FROM t) SELECT a.* FROM a"
        current = "WITH a AS (SELECT p, q FROM t) SELECT a.* RENAME (p AS r) FROM a"

        b = extract_columns(base, dialect="snowflake")
        c = extract_columns(current, dialect="snowflake")
        verdict = predict_ddl("m", "incremental", "sync_all_columns", b, c, status="modified")

        assert verdict.safety.name != "SAFE"


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
