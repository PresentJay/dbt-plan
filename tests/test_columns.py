"""Tests for extract_columns — SQLGlot-based column extraction."""

from pathlib import Path

from dbt_plan.columns import extract_columns


class TestLibraryReExports:
    """Key symbols should be importable from the top-level package."""

    def test_top_level_imports(self):
        from dbt_plan import (  # noqa: F401
            CheckResult,
            Config,
            DDLOperation,
            DDLPrediction,
            ModelDiff,
            ModelNode,
            Safety,
            extract_columns,
            predict_ddl,
        )

        assert Safety.SAFE is not None
        assert callable(extract_columns)
        assert callable(predict_ddl)

    def test_submodule_access(self):
        import dbt_plan

        assert hasattr(dbt_plan, "extract_columns")
        assert hasattr(dbt_plan, "predict_ddl")
        assert hasattr(dbt_plan, "Safety")


class TestExplicitColumns:
    def test_cte_with_explicit_final_select(self, explicit_columns_sql):
        """CTE chain → explicit final SELECT extracts all column names."""
        result = extract_columns(explicit_columns_sql)
        assert result == [
            "store_id",
            "order_date",
            "customer_id",
            "total_sales",
            "order_count",
            "is_active",
            "day_n",
        ]

    def test_simple_select(self):
        """Plain SELECT without CTE extracts column names."""
        sql = "SELECT id, name, email FROM users"
        result = extract_columns(sql)
        assert result == ["id", "name", "email"]

    def test_cte_with_union_all(self):
        """CTE wrapping UNION ALL → extracts from final outer SELECT."""
        sql = """
        WITH combined AS (
            SELECT a, b FROM t1
            UNION ALL
            SELECT a, b FROM t2
        )
        SELECT a, b, 'combined' AS source FROM combined
        """
        result = extract_columns(sql)
        assert result == ["a", "b", "source"]


class TestSelectStar:
    def test_select_star_returns_star_list(self, select_star_sql):
        """SELECT * → returns ["*"]."""
        result = extract_columns(select_star_sql)
        assert result == ["*"]


class TestVariantAccess:
    def test_variant_with_qualify(self, variant_access_sql):
        """VARIANT col:path::TYPE + QUALIFY → extracts aliased names."""
        result = extract_columns(variant_access_sql)
        assert result == [
            "loaded_at",
            "shipping_postal_code",
            "shipping_recipient",
            "shipping_country",
            "store_id",
            "order_date",
        ]


class TestQualifiedStar:
    def test_qualified_star_returns_star_list(self):
        """SELECT t.* → returns ["*"] (same as unqualified star)."""
        sql = "SELECT t.* FROM my_table t"
        result = extract_columns(sql)
        assert result == ["*"]

    def test_qualified_star_mixed_with_columns(self):
        """SELECT t.*, extra_col → returns ["*"] (cannot resolve)."""
        sql = "SELECT t.*, extra_col FROM my_table t JOIN other o ON t.id = o.id"
        result = extract_columns(sql)
        assert result == ["*"]


class TestUnaliasedExpressions:
    def test_unaliased_case_returns_none(self):
        """CASE without alias → None (ambiguity, cannot determine column name)."""
        sql = "SELECT id, CASE WHEN status = 1 THEN 'active' ELSE 'inactive' END FROM users"
        result = extract_columns(sql)
        assert result is None

    def test_all_aliased_returns_columns(self):
        """All expressions aliased → returns column list normally."""
        sql = "SELECT id, CASE WHEN status = 1 THEN 'active' ELSE 'inactive' END AS status_label FROM users"
        result = extract_columns(sql)
        assert result == ["id", "status_label"]


class TestDialects:
    def test_bigquery_dialect(self):
        """BigQuery dialect parses ARRAY_AGG correctly."""
        sql = "SELECT user_id, ARRAY_AGG(item ORDER BY ts) AS items FROM events GROUP BY 1"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["user_id", "items"]

    def test_postgres_dialect(self):
        """Postgres dialect parses JSONB operator correctly."""
        sql = """SELECT id, data->>'email' AS email FROM users"""
        result = extract_columns(sql, dialect="postgres")
        assert result == ["id", "email"]

    def test_default_dialect_is_snowflake(self):
        """Default dialect is snowflake (backward compat)."""
        sql = "SELECT id FROM t"
        assert extract_columns(sql) == extract_columns(sql, dialect="snowflake")


class TestEdgeCases:
    def test_parse_failure_returns_none(self):
        """Invalid SQL → returns None (never raises, never returns 'safe')."""
        result = extract_columns("NOT VALID SQL !!!")
        assert result is None

    def test_lateral_flatten_no_crash(self):
        """LATERAL FLATTEN → returns None or list, never crashes."""
        sql = """
        SELECT
            f.value::STRING AS tag
        FROM events,
            LATERAL FLATTEN(input => events.tags) AS f
        """
        result = extract_columns(sql)
        assert result is None or isinstance(result, list)


class TestColumnExtractionEdgeCases:
    def test_non_select_sql_returns_none(self):
        """CREATE TABLE or INSERT → None (no SELECT to extract from)."""
        assert extract_columns("CREATE TABLE foo (id INT)") is None

    def test_empty_sql_returns_none(self):
        """Empty string → None."""
        assert extract_columns("") is None

    def test_comment_only_returns_none(self):
        """SQL with only comments → None."""
        assert extract_columns("-- just a comment") is None

    def test_bare_union_all_returns_first_branch(self):
        """UNION ALL returns first branch's columns (SQL standard)."""
        sql = "SELECT a, b FROM t1 UNION ALL SELECT x, y FROM t2"
        result = extract_columns(sql)
        assert result == ["a", "b"]


class TestRealWorldFixtures:
    """Tests using real-world dbt SQL patterns from fixtures."""

    FIXTURES = Path(__file__).parent / "fixtures"

    def test_window_functions(self):
        """Window functions with QUALIFY — common in incremental models."""
        sql = (self.FIXTURES / "window_functions.sql").read_text(encoding="utf-8")
        result = extract_columns(sql)
        assert result is not None
        assert "user_id" in result
        assert "order_date" in result
        assert "row_num" in result
        assert "prev_order_date" in result
        assert "lifetime_spend" in result

    def test_cte_chain_with_select_star(self):
        """Multi-CTE chain ending in SELECT * FROM final — resolved through the CTEs.

        This fixture's own comment calls the shape "very common in dbt models", and
        it is: it is what dbt's style guide teaches. It used to return ["*"], so the
        most common model in a dbt project was the one dbt-plan could say least
        about. `final` lists its columns explicitly, so they are readable from the
        file with no warehouse involved.
        """
        sql = (self.FIXTURES / "cte_chain.sql").read_text(encoding="utf-8")
        result = extract_columns(sql)
        assert result == [
            "order_id",
            "ordered_at",
            "user_id",
            "customer_tier",
            "order_date",
        ]

    def test_union_staging_pattern(self):
        """UNION ALL multi-source pattern — returns first branch columns."""
        sql = (self.FIXTURES / "union_staging.sql").read_text(encoding="utf-8")
        result = extract_columns(sql)
        assert result is not None
        assert "book_format" in result
        assert "customer_id" in result
        assert "order_status" in result
        assert "ordered_at" in result

    def test_explicit_columns_fixture(self):
        """Existing fixture: explicit column SELECT."""
        sql = (self.FIXTURES / "explicit_columns.sql").read_text(encoding="utf-8")
        result = extract_columns(sql)
        assert result is not None
        assert len(result) > 0

    def test_variant_access_fixture(self):
        """Existing fixture: Snowflake VARIANT access."""
        sql = (self.FIXTURES / "variant_access.sql").read_text(encoding="utf-8")
        result = extract_columns(sql)
        # VARIANT access may or may not parse depending on dialect
        assert result is None or isinstance(result, list)


class TestInvalidDialect:
    """Invalid dialect should return None, not raise."""

    def test_unknown_dialect_returns_none(self):
        result = extract_columns("SELECT id FROM t", dialect="not_a_real_dialect")
        assert result is None

    def test_empty_dialect_uses_default(self):
        """Empty string dialect falls through to sqlglot default (works fine)."""
        result = extract_columns("SELECT id FROM t", dialect="")
        assert result == ["id"]


class TestStarExcept:
    """BigQuery SELECT * EXCEPT detection."""

    def test_star_except_single_column(self):
        """SELECT * EXCEPT(revenue) → returns sentinel with excluded column."""
        sql = "SELECT * EXCEPT(revenue) FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["* except(revenue)"]

    def test_star_except_multiple_columns(self):
        """SELECT * EXCEPT(revenue, cost) → returns sentinel with sorted columns."""
        sql = "SELECT * EXCEPT(revenue, cost) FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["* except(cost, revenue)"]

    def test_star_replace_returns_star(self):
        """SELECT * REPLACE(expr AS col) → still returns ["*"] (can't enumerate)."""
        sql = "SELECT * REPLACE(revenue * 100 AS revenue) FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["*"]

    def test_star_except_with_replace_returns_except(self):
        """SELECT * EXCEPT(cost) REPLACE(revenue*100 AS revenue) → except sentinel."""
        sql = "SELECT * EXCEPT(cost) REPLACE(revenue * 100 AS revenue) FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["* except(cost)"]

    def test_star_except_lowercased(self):
        """Column names in EXCEPT are lowercased."""
        sql = "SELECT * EXCEPT(Revenue, COST) FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["* except(cost, revenue)"]

    def test_plain_star_bigquery(self):
        """Plain SELECT * on bigquery still returns ["*"]."""
        sql = "SELECT * FROM t"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["*"]


class TestBOMHandling:
    """UTF-8 BOM should not cause parse failures."""

    def test_bom_prefix_stripped(self):
        """BOM at start of SQL file should be ignored."""
        sql = "\ufeffSELECT id, name FROM users"
        result = extract_columns(sql)
        assert result == ["id", "name"]

    def test_bom_with_cte(self):
        """BOM before WITH clause should be ignored."""
        sql = "\ufeffWITH cte AS (SELECT 1 AS a) SELECT a FROM cte"
        result = extract_columns(sql)
        assert result == ["a"]

    def test_no_bom_still_works(self):
        """Regression: normal SQL without BOM still works."""
        sql = "SELECT id FROM users"
        result = extract_columns(sql)
        assert result == ["id"]
