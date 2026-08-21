"""Adversarial tests for extract_columns — trying to crash or confuse it.

Every test verifies the contract: extract_columns MUST return either a valid
list[str] or None. It must NEVER raise an exception, regardless of input.
"""

import pytest

from dbt_plan.columns import extract_columns


def assert_safe_result(result):
    """Assert that the result is either None or a list of strings — never an exception."""
    assert result is None or (
        isinstance(result, list) and all(isinstance(c, str) for c in result)
    ), f"Expected None or list[str], got {type(result)}: {result!r}"


class TestExtremelyLongSQL:
    """100KB+ of nested CTEs — does it timeout or OOM?"""

    def test_100kb_nested_ctes(self):
        """Generate enough CTEs chained together to produce 100KB+ SQL."""
        ctes = []
        for i in range(2000):
            if i == 0:
                ctes.append(f"cte_{i} AS (SELECT 1 AS col_{i})")
            else:
                ctes.append(f"cte_{i} AS (SELECT col_{i - 1}, {i} AS col_{i} FROM cte_{i - 1})")
        sql = "WITH " + ",\n".join(ctes) + "\nSELECT * FROM cte_1999"
        assert len(sql) > 100_000, f"SQL is only {len(sql)} bytes"

        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_100kb_repeated_columns(self):
        """Single SELECT with enough columns to exceed 100KB."""
        cols = ", ".join(f"col_{i}" for i in range(12000))
        sql = f"SELECT {cols} FROM t"
        assert len(sql) > 100_000, f"SQL is only {len(sql)} bytes"

        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


class TestDeeplyNestedSubqueries:
    """50+ levels of SELECT (SELECT (SELECT ...))."""

    def test_50_levels_of_nesting(self):
        """Build 50 levels of nested scalar subqueries."""
        sql = "SELECT 1 AS val"
        for _i in range(50):
            sql = f"SELECT ({sql}) AS val"
        sql += " FROM t"

        try:
            extract_safe(sql)
        except RecursionError:
            pytest.fail("extract_columns hit RecursionError on 50 levels of nesting")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_100_levels_of_nesting(self):
        """Push to 100 levels — more extreme."""
        sql = "SELECT 1 AS val"
        for _i in range(100):
            sql = f"SELECT ({sql}) AS val"
        sql += " FROM t"

        try:
            extract_safe(sql)
        except RecursionError:
            pytest.fail("extract_columns hit RecursionError on 100 levels of nesting")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


class TestNullBytes:
    """SQL with null bytes — does it crash?"""

    def test_null_byte_in_column_list(self):
        sql = "SELECT id\x00, name FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_null_byte_at_start(self):
        sql = "\x00SELECT id FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_null_byte_everywhere(self):
        sql = "S\x00E\x00L\x00E\x00C\x00T\x00 i\x00d FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


class TestEmptyString:
    """Empty string should return None."""

    def test_empty_string(self):
        try:
            result = extract_columns("")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for empty string, got {result!r}"


class TestOnlyWhitespace:
    """Only whitespace should return None."""

    def test_spaces_only(self):
        try:
            result = extract_columns("   ")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for whitespace, got {result!r}"

    def test_tabs_and_newlines(self):
        try:
            result = extract_columns("   \n\t  ")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for whitespace, got {result!r}"

    def test_mixed_whitespace(self):
        try:
            result = extract_columns("\r\n\t \r\n")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for whitespace, got {result!r}"


class TestOnlyComments:
    """SQL with only comments — should return None."""

    def test_line_comment_only(self):
        try:
            result = extract_columns("-- just a comment")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for comment-only SQL, got {result!r}"

    def test_block_comment_only(self):
        try:
            result = extract_columns("/* block comment */")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for comment-only SQL, got {result!r}"

    def test_mixed_comments(self):
        try:
            result = extract_columns("-- just a comment\n/* block */")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert result is None, f"Expected None for comment-only SQL, got {result!r}"


class TestMultipleStatements:
    """Multiple statements — what does parse_one do?"""

    def test_two_selects_semicolon(self):
        """parse_one should either parse the first or error — never raise unhandled."""
        sql = "SELECT a FROM t; SELECT b FROM t2"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_select_then_drop(self):
        """SELECT followed by DROP TABLE — should not crash."""
        sql = "SELECT a FROM t; DROP TABLE t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_three_statements(self):
        sql = "SELECT a FROM t; SELECT b FROM t2; SELECT c FROM t3"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


class TestIncompleteSQL:
    """Incomplete SQL fragments — should return None, never raise."""

    def test_select_from_nothing(self):
        try:
            result = extract_columns("SELECT a, b FROM")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_select_keyword_only(self):
        try:
            result = extract_columns("SELECT")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_select_trailing_comma(self):
        try:
            result = extract_columns("SELECT a,")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_with_no_body(self):
        try:
            result = extract_columns("WITH cte AS (")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_select_where_no_predicate(self):
        try:
            result = extract_columns("SELECT a FROM t WHERE")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_unclosed_parenthesis(self):
        try:
            result = extract_columns("SELECT (a + b FROM t")
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestSQLWithEmoji:
    """SQL containing emoji characters — should not crash."""

    def test_emoji_in_string_literal(self):
        sql = "SELECT '🔥' AS emoji_col FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_emoji_in_column_alias(self):
        """Most SQL engines don't allow this, but sqlglot should handle gracefully."""
        sql = 'SELECT 1 AS "🔥fire🔥" FROM t'
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_emoji_in_table_name(self):
        sql = 'SELECT id FROM "🔥table🔥"'
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestBinaryGarbageData:
    """Binary/garbage data — should return None, never crash."""

    def test_all_byte_values_latin1(self):
        """All 256 byte values decoded as latin-1."""
        sql = bytes(range(256)).decode("latin-1")
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_random_binary_prefix(self):
        """Random binary prefix before valid SQL."""
        sql = bytes(range(128)).decode("latin-1") + "SELECT id FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_control_characters(self):
        """ASCII control characters mixed in."""
        sql = "\x01\x02\x03SELECT\x04 id\x05 FROM\x06 t\x07"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestVeryLongColumnName:
    """Column name with 1000+ characters."""

    def test_1000_char_column_name(self):
        col = "a" * 1000
        sql = f"SELECT {col} FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)
        if result is not None:
            assert result == [col.lower()]

    def test_1000_char_alias(self):
        alias = "b" * 1000
        sql = f"SELECT 1 AS {alias} FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)
        if result is not None:
            assert result == [alias.lower()]


class TestManyColumns:
    """1000 columns in a single SELECT."""

    def test_1000_columns(self):
        cols = ", ".join(f"col_{i}" for i in range(1000))
        sql = f"SELECT {cols} FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)
        if result is not None:
            assert len(result) == 1000
            assert result[0] == "col_0"
            assert result[999] == "col_999"


class TestRecursiveCTE:
    """Recursive CTE that references itself."""

    def test_recursive_cte(self):
        sql = """
        WITH RECURSIVE r AS (
            SELECT 1 AS n
            UNION ALL
            SELECT n + 1 FROM r WHERE n < 10
        )
        SELECT n FROM r
        """
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_recursive_cte_multiple_columns(self):
        sql = """
        WITH RECURSIVE tree AS (
            SELECT id, parent_id, name, 0 AS depth FROM categories WHERE parent_id IS NULL
            UNION ALL
            SELECT c.id, c.parent_id, c.name, t.depth + 1 FROM categories c JOIN tree t ON c.parent_id = t.id
        )
        SELECT id, name, depth FROM tree
        """
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestCreateTableAsSelect:
    """CREATE TABLE AS SELECT — should find the embedded SELECT."""

    def test_ctas_basic(self):
        sql = "CREATE TABLE new_t AS SELECT a, b FROM t"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_ctas_or_replace(self):
        sql = "CREATE OR REPLACE TABLE new_t AS SELECT x, y, z FROM source_table"
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestMergeStatement:
    """MERGE statement — complex DML that may or may not have a SELECT."""

    def test_merge_basic(self):
        sql = """
        MERGE INTO target t
        USING source s ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.name = s.name
        WHEN NOT MATCHED THEN INSERT (id, name) VALUES (s.id, s.name)
        """
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)

    def test_merge_with_cte(self):
        sql = """
        WITH updates AS (SELECT id, name FROM staging)
        MERGE INTO target t
        USING updates u ON t.id = u.id
        WHEN MATCHED THEN UPDATE SET t.name = u.name
        """
        try:
            result = extract_columns(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")
        assert_safe_result(result)


class TestUnionWithDifferentColumnCounts:
    """UNION ALL where branches have different numbers of columns."""

    def test_mismatched_column_counts(self):
        sql = "SELECT a, b, c FROM t1 UNION ALL SELECT x, y FROM t2"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_triple_union_mismatched(self):
        sql = """
        SELECT a FROM t1
        UNION ALL
        SELECT b, c FROM t2
        UNION ALL
        SELECT d, e, f FROM t3
        """
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


# --- Additional edge cases ---


class TestTypeConfusion:
    """Pass non-string types — verify it does not crash."""

    def test_none_input(self):
        """None input — should raise TypeError or be caught."""
        try:
            extract_columns(None)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            pass  # Acceptable: function doesn't guard against wrong types
        except Exception as exc:
            pytest.fail(f"Unexpected exception type {type(exc).__name__}: {exc}")

    def test_integer_input(self):
        """Integer input — should raise TypeError or be caught."""
        try:
            extract_columns(42)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            pass  # Acceptable
        except Exception as exc:
            pytest.fail(f"Unexpected exception type {type(exc).__name__}: {exc}")

    def test_bytes_input(self):
        """Bytes input — should raise TypeError or be caught."""
        try:
            extract_columns(b"SELECT id FROM t")  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            pass  # Acceptable
        except Exception as exc:
            pytest.fail(f"Unexpected exception type {type(exc).__name__}: {exc}")


class TestSQLInjectionPatterns:
    """SQL injection-style payloads — should not crash the parser."""

    def test_single_quote_injection(self):
        sql = "SELECT ' OR 1=1 -- FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_comment_injection(self):
        sql = "SELECT a FROM t WHERE id = 1; DROP TABLE t--"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_unicode_escape(self):
        sql = "SELECT U&'\\0041' AS unicode_col FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


class TestExoticSQLConstructs:
    """Less common SQL constructs that might trip up the parser."""

    def test_values_clause(self):
        sql = "VALUES (1, 'a'), (2, 'b'), (3, 'c')"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_explain_select(self):
        sql = "EXPLAIN SELECT a, b FROM t"
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_set_operations_chain(self):
        sql = """
        SELECT a FROM t1
        UNION
        SELECT a FROM t2
        INTERSECT
        SELECT a FROM t3
        EXCEPT
        SELECT a FROM t4
        """
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_pivot_query(self):
        sql = """
        SELECT *
        FROM monthly_sales
        PIVOT (SUM(amount) FOR month IN ('JAN', 'FEB', 'MAR'))
        """
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")

    def test_unpivot_query(self):
        sql = """
        SELECT *
        FROM quarterly_sales
        UNPIVOT (sales FOR quarter IN (q1, q2, q3, q4))
        """
        try:
            extract_safe(sql)
        except Exception as exc:
            pytest.fail(f"extract_columns raised {type(exc).__name__}: {exc}")


# --- Helper function ---


def extract_safe(sql: str):
    """Call extract_columns and validate the contract (returns None or list[str])."""
    result = extract_columns(sql)
    assert_safe_result(result)
    return result
