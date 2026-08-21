"""Multi-dialect SQL column extraction tests.

Tests extract_columns across MySQL, DuckDB, Trino/Presto, Spark, and
ClickHouse dialects using each dialect's characteristic syntax.
"""

from dbt_plan.columns import extract_columns


class TestMySQL:
    """MySQL-specific syntax patterns."""

    DIALECT = "mysql"

    def test_sql_calc_found_rows(self):
        """SQL_CALC_FOUND_ROWS hint should not interfere with column extraction."""
        sql = "SELECT SQL_CALC_FOUND_ROWS col1, col2 FROM t LIMIT 10"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "MySQL SQL_CALC_FOUND_ROWS should parse"
        assert "col1" in result
        assert "col2" in result

    def test_group_concat_with_separator(self):
        """GROUP_CONCAT with SEPARATOR keyword — MySQL-specific aggregate."""
        sql = "SELECT col1, GROUP_CONCAT(col2 SEPARATOR ',') AS grouped FROM t GROUP BY col1"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "MySQL GROUP_CONCAT should parse"
        assert "col1" in result
        assert "grouped" in result

    def test_ifnull_function(self):
        """IFNULL — MySQL-specific null coalescing."""
        sql = "SELECT IFNULL(col, 'default') AS col FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "MySQL IFNULL should parse"
        assert "col" in result

    def test_insert_into_select(self):
        """INSERT INTO ... SELECT ... — should extract from the SELECT, not crash."""
        sql = "INSERT INTO target SELECT col1, col2 FROM source"
        result = extract_columns(sql, dialect=self.DIALECT)
        # May return None (no top-level SELECT) or extract from the SELECT
        # Key: must not crash
        assert result is None or isinstance(result, list)


class TestDuckDB:
    """DuckDB-specific syntax patterns."""

    DIALECT = "duckdb"

    def test_read_parquet_star(self):
        """SELECT * FROM read_parquet — external source, star expansion."""
        sql = "SELECT * FROM read_parquet('file.parquet')"
        result = extract_columns(sql, dialect=self.DIALECT)
        # Should return ["*"] since it's SELECT *
        assert result is not None, "DuckDB read_parquet should parse"
        assert result == ["*"]

    def test_direct_file_reference(self):
        """SELECT col1, col2 FROM 'file.csv' — DuckDB direct file read."""
        sql = "SELECT col1, col2 FROM 'file.csv'"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "DuckDB direct file reference should parse"
        assert "col1" in result
        assert "col2" in result

    def test_columns_wildcard(self):
        """COLUMNS('col_*') — DuckDB dynamic column selection."""
        sql = "SELECT COLUMNS('col_*') FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        # This is very DuckDB-specific; may return None or unexpected result
        assert result is None or isinstance(result, list)

    def test_pivot_syntax(self):
        """PIVOT — DuckDB-specific PIVOT syntax."""
        sql = "PIVOT t ON col USING sum(val)"
        result = extract_columns(sql, dialect=self.DIALECT)
        # PIVOT is a non-standard statement; may return None
        assert result is None or isinstance(result, list)

    def test_list_agg(self):
        """list_agg — DuckDB aggregate function."""
        sql = "SELECT list_agg(col) AS agg FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "DuckDB list_agg should parse"
        assert "agg" in result


class TestTrino:
    """Trino/Presto-specific syntax patterns."""

    DIALECT = "trino"

    def test_try_cast(self):
        """TRY_CAST — Trino safe cast that returns NULL on failure."""
        sql = "SELECT TRY_CAST(col AS INTEGER) AS col_int FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Trino TRY_CAST should parse"
        assert "col_int" in result

    def test_transform_lambda(self):
        """TRANSFORM with lambda — Trino array transformation."""
        sql = "SELECT TRANSFORM(arr, x -> x + 1) AS transformed FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Trino TRANSFORM lambda should parse"
        assert "transformed" in result

    def test_cross_join_unnest(self):
        """CROSS JOIN UNNEST — Trino array expansion."""
        sql = "SELECT col FROM t CROSS JOIN UNNEST(arr) AS t(val)"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Trino CROSS JOIN UNNEST should parse"
        assert "col" in result

    def test_approx_distinct(self):
        """APPROX_DISTINCT — Trino approximate count distinct."""
        sql = "SELECT APPROX_DISTINCT(col) AS approx_count FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Trino APPROX_DISTINCT should parse"
        assert "approx_count" in result


class TestPresto:
    """Presto dialect — should behave similarly to Trino."""

    DIALECT = "presto"

    def test_try_cast(self):
        """TRY_CAST via Presto dialect."""
        sql = "SELECT TRY_CAST(col AS INTEGER) AS col_int FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Presto TRY_CAST should parse"
        assert "col_int" in result

    def test_transform_lambda(self):
        """TRANSFORM with lambda via Presto dialect."""
        sql = "SELECT TRANSFORM(arr, x -> x + 1) AS transformed FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Presto TRANSFORM lambda should parse"
        assert "transformed" in result

    def test_cross_join_unnest(self):
        """CROSS JOIN UNNEST via Presto dialect."""
        sql = "SELECT col FROM t CROSS JOIN UNNEST(arr) AS t(val)"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Presto CROSS JOIN UNNEST should parse"
        assert "col" in result

    def test_approx_distinct(self):
        """APPROX_DISTINCT via Presto dialect."""
        sql = "SELECT APPROX_DISTINCT(col) AS approx_count FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Presto APPROX_DISTINCT should parse"
        assert "approx_count" in result


class TestSpark:
    """Spark SQL-specific syntax patterns."""

    DIALECT = "spark"

    def test_explode(self):
        """EXPLODE — Spark array expansion."""
        sql = "SELECT EXPLODE(arr) AS val FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Spark EXPLODE should parse"
        assert "val" in result

    def test_lateral_view_explode(self):
        """LATERAL VIEW EXPLODE — Spark lateral view syntax."""
        sql = "SELECT col1, col2 FROM t LATERAL VIEW EXPLODE(arr) AS val"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Spark LATERAL VIEW EXPLODE should parse"
        assert "col1" in result
        assert "col2" in result

    def test_named_struct(self):
        """named_struct — Spark struct constructor."""
        sql = "SELECT named_struct('a', 1, 'b', 2) AS s"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Spark named_struct should parse"
        assert "s" in result

    def test_transform_lambda(self):
        """TRANSFORM with lambda — Spark higher-order function."""
        sql = "SELECT TRANSFORM(arr, x -> x + 1) AS transformed FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "Spark TRANSFORM lambda should parse"
        assert "transformed" in result


class TestClickHouse:
    """ClickHouse-specific syntax patterns."""

    DIALECT = "clickhouse"

    def test_final_keyword(self):
        """FINAL — ClickHouse deduplication hint on ReplacingMergeTree."""
        sql = "SELECT col1, col2 FROM t FINAL"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "ClickHouse FINAL keyword should parse"
        assert "col1" in result
        assert "col2" in result

    def test_group_array(self):
        """groupArray — ClickHouse aggregate that collects into array."""
        sql = "SELECT groupArray(col) AS arr FROM t"
        result = extract_columns(sql, dialect=self.DIALECT)
        assert result is not None, "ClickHouse groupArray should parse"
        assert "arr" in result

    def test_array_join(self):
        """ARRAY JOIN — ClickHouse array expansion (similar to UNNEST)."""
        sql = "SELECT * FROM t ARRAY JOIN arr AS item"
        result = extract_columns(sql, dialect=self.DIALECT)
        # SELECT * → ["*"] or parse issue with ARRAY JOIN
        assert result is None or isinstance(result, list)


class TestNonexistentDialect:
    """Invalid dialect should return None, not crash (ValueError catch)."""

    def test_nonexistent_dialect_returns_none(self):
        """dialect='nonexistent' triggers ValueError in sqlglot — caught gracefully."""
        result = extract_columns("SELECT id, name FROM users", dialect="nonexistent")
        assert result is None, "Nonexistent dialect must return None, not crash"

    def test_garbage_dialect_returns_none(self):
        """Completely random string as dialect."""
        result = extract_columns("SELECT 1 AS x", dialect="not_a_real_db_engine_12345")
        assert result is None

    def test_none_like_string_dialect(self):
        """String 'None' as dialect."""
        result = extract_columns("SELECT id FROM t", dialect="None")
        assert result is None
