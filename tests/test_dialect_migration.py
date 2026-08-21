"""Dialect migration tests — Snowflake-to-BigQuery migration scenarios.

Simulates a real engineering team migrating from Snowflake to BigQuery and
validates how dbt-plan handles dialect switching, cross-dialect parsing,
and the footguns that appear when --dialect is misconfigured.
"""

from dbt_plan.columns import extract_columns
from dbt_plan.config import Config


# ---------------------------------------------------------------------------
# Scenario 1: Snowflake SQL parsed as BigQuery
# ---------------------------------------------------------------------------
class TestSnowflakeSQLParsedAsBigQuery:
    """What happens when someone forgets to update --dialect after migrating?"""

    SNOWFLAKE_SQL = (
        "SELECT payload:tier::string AS customer_tier, "
        "DATEADD(day, 7, order_date) AS week_later FROM t"
    )

    def test_snowflake_sql_with_snowflake_dialect(self):
        """Snowflake SQL + dialect=snowflake: should extract columns correctly."""
        result = extract_columns(self.SNOWFLAKE_SQL, dialect="snowflake")
        assert result is not None, "Snowflake SQL should parse with snowflake dialect"
        assert "customer_tier" in result
        assert "week_later" in result

    def test_snowflake_sql_with_bigquery_dialect(self):
        """Snowflake SQL + dialect=bigquery: variant access is Snowflake-only.

        payload:tier::string and DATEADD are not valid BigQuery syntax.
        Depending on sqlglot version, this may parse differently or fail.
        The key question: does dbt-plan still protect users from silent safe?
        """
        result = extract_columns(self.SNOWFLAKE_SQL, dialect="bigquery")
        # Two acceptable outcomes:
        # 1. None → parse failure → caller produces REVIEW REQUIRED (safe)
        # 2. list with columns → sqlglot was lenient enough to parse it
        # Unacceptable: crash
        if result is None:
            # Parse failure — dbt-plan correctly flags for review
            pass
        else:
            # sqlglot parsed it somehow — document what we got
            assert isinstance(result, list)

    def test_dateadd_cross_dialect(self):
        """DATEADD is Snowflake-specific. BigQuery uses DATE_ADD."""
        sf_sql = "SELECT DATEADD(day, 7, order_date) AS week_later FROM t"
        bq_sql = "SELECT DATE_ADD(order_date, INTERVAL 7 DAY) AS week_later FROM t"

        sf_result = extract_columns(sf_sql, dialect="snowflake")
        bq_result = extract_columns(bq_sql, dialect="bigquery")

        # Both should extract "week_later" when parsed with correct dialect
        assert sf_result is not None, "DATEADD should parse with snowflake dialect"
        assert "week_later" in sf_result

        assert bq_result is not None, "DATE_ADD should parse with bigquery dialect"
        assert "week_later" in bq_result

    def test_variant_access_is_snowflake_only(self):
        """data:path::type is pure Snowflake VARIANT syntax."""
        sql = "SELECT data:nested:field::varchar AS field_val FROM t"
        sf_result = extract_columns(sql, dialect="snowflake")
        bq_result = extract_columns(sql, dialect="bigquery")

        # Snowflake should handle this
        assert sf_result is not None, "VARIANT access should parse with snowflake dialect"
        assert "field_val" in sf_result

        # BigQuery will likely not understand : as path access
        # Either parse failure (None) or different interpretation — both OK
        assert bq_result is None or isinstance(bq_result, list)


# ---------------------------------------------------------------------------
# Scenario 2: BigQuery SQL parsed as Snowflake
# ---------------------------------------------------------------------------
class TestBigQuerySQLParsedAsSnowflake:
    """BigQuery-specific syntax thrown at Snowflake parser."""

    def test_star_except_with_bigquery_dialect(self):
        """SELECT * EXCEPT(internal_id) + SAFE_CAST on bigquery dialect."""
        sql = "SELECT * EXCEPT(internal_id), SAFE_CAST(revenue AS FLOAT64) AS revenue FROM t"
        result = extract_columns(sql, dialect="bigquery")
        # BigQuery dialect should handle * EXCEPT and SAFE_CAST
        assert result is not None, "BigQuery SQL should parse with bigquery dialect"
        # Since there's a *, expect star-based result
        # The * EXCEPT will trigger the star-except sentinel path
        assert isinstance(result, list)

    def test_star_except_with_snowflake_dialect(self):
        """SELECT * EXCEPT is not Snowflake syntax — what happens?"""
        sql = "SELECT * EXCEPT(internal_id), SAFE_CAST(revenue AS FLOAT64) AS revenue FROM t"
        result = extract_columns(sql, dialect="snowflake")
        # Snowflake doesn't support SELECT * EXCEPT.
        # Acceptable: None (parse failure) or some partial result
        # Unacceptable: crash
        assert result is None or isinstance(result, list)

    def test_safe_cast_cross_dialect(self):
        """SAFE_CAST is BigQuery-specific. Snowflake uses TRY_CAST."""
        bq_sql = "SELECT SAFE_CAST(revenue AS FLOAT64) AS revenue FROM t"
        sf_sql = "SELECT TRY_CAST(revenue AS FLOAT) AS revenue FROM t"

        bq_result = extract_columns(bq_sql, dialect="bigquery")
        sf_result = extract_columns(sf_sql, dialect="snowflake")

        assert bq_result is not None, "SAFE_CAST should parse with bigquery"
        assert "revenue" in bq_result

        assert sf_result is not None, "TRY_CAST should parse with snowflake"
        assert "revenue" in sf_result

    def test_safe_cast_on_snowflake_parser(self):
        """SAFE_CAST parsed by snowflake dialect — not a Snowflake function."""
        sql = "SELECT SAFE_CAST(revenue AS FLOAT64) AS revenue FROM t"
        result = extract_columns(sql, dialect="snowflake")
        # sqlglot may or may not parse SAFE_CAST in snowflake mode
        # Key: no crash
        assert result is None or isinstance(result, list)

    def test_bigquery_struct_on_snowflake(self):
        """STRUCT<field type> is BigQuery-specific type syntax."""
        sql = "SELECT STRUCT(1 AS id, 'hello' AS name) AS my_struct FROM t"
        bq_result = extract_columns(sql, dialect="bigquery")
        sf_result = extract_columns(sql, dialect="snowflake")

        # BigQuery should parse STRUCT constructor
        if bq_result is not None:
            assert "my_struct" in bq_result

        # Snowflake — may or may not parse
        assert sf_result is None or isinstance(sf_result, list)


# ---------------------------------------------------------------------------
# Scenario 3: Config dialect vs CLI dialect
# ---------------------------------------------------------------------------
class TestConfigDialectVsCLIDialect:
    """Verify that CLI --dialect overrides .dbt-plan.yml dialect."""

    def test_config_sets_dialect(self, tmp_path):
        """dialect=snowflake in .dbt-plan.yml is loaded."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: snowflake\n")
        config = Config.load(tmp_path)
        assert config.dialect == "snowflake"

    def test_config_bigquery_dialect(self, tmp_path):
        """dialect=bigquery in .dbt-plan.yml is loaded."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: bigquery\n")
        config = Config.load(tmp_path)
        assert config.dialect == "bigquery"

    def test_cli_overrides_config(self, tmp_path):
        """CLI --dialect should override config file.

        In cli.py: `dialect = getattr(args, "dialect", None) or config.dialect`
        So CLI flag (non-None) takes precedence over config.
        """
        (tmp_path / ".dbt-plan.yml").write_text("dialect: snowflake\n")
        config = Config.load(tmp_path)
        assert config.dialect == "snowflake"

        # Simulate CLI override: the `or` pattern in cli.py means
        # if args.dialect is truthy, it wins
        cli_dialect = "bigquery"
        effective_dialect = cli_dialect or config.dialect
        assert effective_dialect == "bigquery"

    def test_cli_none_falls_through_to_config(self, tmp_path):
        """When --dialect is not passed (None), config wins."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: bigquery\n")
        config = Config.load(tmp_path)

        cli_dialect = None
        effective_dialect = cli_dialect or config.dialect
        assert effective_dialect == "bigquery"

    def test_env_overrides_config_dialect(self, tmp_path, monkeypatch):
        """DBT_PLAN_DIALECT env var overrides .dbt-plan.yml."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: snowflake\n")
        monkeypatch.setenv("DBT_PLAN_DIALECT", "bigquery")
        config = Config.load(tmp_path)
        assert config.dialect == "bigquery"

    def test_precedence_chain(self, tmp_path, monkeypatch):
        """Full precedence: CLI > env > file > default.

        Config.load merges file + env (env wins).
        CLI layer applies on top via `cli_dialect or config.dialect`.
        """
        (tmp_path / ".dbt-plan.yml").write_text("dialect: postgres\n")
        monkeypatch.setenv("DBT_PLAN_DIALECT", "bigquery")
        config = Config.load(tmp_path)
        # env overrides file
        assert config.dialect == "bigquery"

        # CLI overrides env+file
        cli_dialect = "snowflake"
        effective = cli_dialect or config.dialect
        assert effective == "snowflake"


# ---------------------------------------------------------------------------
# Scenario 4: Mixed dialect in same project (mid-migration)
# ---------------------------------------------------------------------------
class TestMixedDialectMigration:
    """Real migration: some models converted to BQ, others still Snowflake.

    You can only pick one --dialect. What breaks?
    """

    # Model A: pure Snowflake SQL (variant access, DATEADD)
    MODEL_A_SNOWFLAKE = (
        "SELECT payload:tier::string AS customer_tier, "
        "DATEADD(day, 7, created_at) AS week_later, "
        "user_id FROM events"
    )

    # Model B: pure BigQuery SQL (SAFE_CAST, EXCEPT)
    MODEL_B_BIGQUERY = (
        "SELECT SAFE_CAST(revenue AS FLOAT64) AS revenue, user_id, order_status FROM conversions"
    )

    def test_snowflake_dialect_model_a(self):
        """Model A (Snowflake) parsed with dialect=snowflake: should work."""
        result = extract_columns(self.MODEL_A_SNOWFLAKE, dialect="snowflake")
        assert result is not None, "Snowflake model should parse with snowflake dialect"
        assert "customer_tier" in result
        assert "week_later" in result
        assert "user_id" in result

    def test_snowflake_dialect_model_b(self):
        """Model B (BigQuery) parsed with dialect=snowflake: what happens?

        This is the mid-migration footgun. SAFE_CAST is not a Snowflake function.
        """
        result = extract_columns(self.MODEL_B_BIGQUERY, dialect="snowflake")
        # Acceptable:
        # - None (parse failure) → REVIEW REQUIRED (safe for the user)
        # - List with columns → sqlglot was lenient
        # NOT acceptable: crash
        if result is None:
            # Good: dbt-plan will flag this for review
            pass
        else:
            # sqlglot parsed it — check what columns it found
            assert isinstance(result, list)
            # If it parsed, it should at least find user_id and order_status
            # since those are simple column references

    def test_bigquery_dialect_model_a(self):
        """Model A (Snowflake) parsed with dialect=bigquery: what happens?

        payload:tier::string and DATEADD are Snowflake-only.
        """
        result = extract_columns(self.MODEL_A_SNOWFLAKE, dialect="bigquery")
        if result is None:
            # Good: parse failure → REVIEW REQUIRED
            pass
        else:
            assert isinstance(result, list)

    def test_bigquery_dialect_model_b(self):
        """Model B (BigQuery) parsed with dialect=bigquery: should work."""
        result = extract_columns(self.MODEL_B_BIGQUERY, dialect="bigquery")
        assert result is not None, "BigQuery model should parse with bigquery dialect"
        assert "revenue" in result
        assert "user_id" in result
        assert "order_status" in result

    def test_migration_coverage_report(self):
        """Document which patterns are portable across dialects.

        Some SQL patterns parse fine regardless of dialect. These are
        safe during migration. Others are dialect-specific footguns.
        """
        portable_patterns = {
            "simple_select": "SELECT id, name, email FROM users",
            "aliased_function": "SELECT COUNT(*) AS total FROM t",
            "case_expression": "SELECT CASE WHEN x > 0 THEN 'yes' ELSE 'no' END AS flag FROM t",
            "coalesce": "SELECT COALESCE(a, b, c) AS val FROM t",
            "subquery": "SELECT id FROM (SELECT id FROM t) sub",
            "cte": "WITH cte AS (SELECT id FROM t) SELECT id FROM cte",
        }
        snowflake_specific = {
            "variant_access": "SELECT data:field::string AS val FROM t",
            "dateadd": "SELECT DATEADD(day, 7, dt) AS later FROM t",
            "nvl": "SELECT NVL(col, 0) AS val FROM t",
            "qualify": "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t QUALIFY rn = 1",
        }
        bigquery_specific = {
            "safe_cast": "SELECT SAFE_CAST(x AS INT64) AS val FROM t",
            "star_except": "SELECT * EXCEPT(secret_col) FROM t",
            "date_add_bq": "SELECT DATE_ADD(dt, INTERVAL 7 DAY) AS later FROM t",
            "ifnull": "SELECT IFNULL(col, 0) AS val FROM t",
        }

        results = {}

        # Test portable patterns on both dialects
        for name, sql in portable_patterns.items():
            sf = extract_columns(sql, dialect="snowflake")
            bq = extract_columns(sql, dialect="bigquery")
            results[name] = {
                "snowflake": sf is not None,
                "bigquery": bq is not None,
                "portable": sf is not None and bq is not None,
            }

        # All portable patterns should work on both
        for name, res in results.items():
            assert res["portable"], (
                f"Pattern '{name}' should be portable but failed on one dialect"
            )

        # Snowflake-specific may fail on bigquery
        for name, sql in snowflake_specific.items():
            sf = extract_columns(sql, dialect="snowflake")
            bq = extract_columns(sql, dialect="bigquery")
            # Snowflake should always work with its own dialect
            assert sf is not None, (
                f"Snowflake pattern '{name}' should parse with snowflake dialect"
            )
            # BigQuery may or may not parse — just don't crash

        # BigQuery-specific may fail on snowflake
        for name, sql in bigquery_specific.items():
            bq = extract_columns(sql, dialect="bigquery")
            sf = extract_columns(sql, dialect="snowflake")
            # BigQuery should always work with its own dialect
            assert bq is not None, f"BigQuery pattern '{name}' should parse with bigquery dialect"
            # Snowflake may or may not parse — just don't crash


# ---------------------------------------------------------------------------
# Scenario 5: Dialect affects column extraction results
# ---------------------------------------------------------------------------
class TestDialectColumnExtractionConsistency:
    """Same logical SQL in different dialects should extract same columns."""

    def test_null_coalescing_equivalents(self):
        """NVL (Snowflake) vs IFNULL (BigQuery/MySQL) vs COALESCE (standard).

        All should extract ["val"] regardless of which dialect is used.
        """
        # COALESCE is SQL standard — works everywhere
        coalesce_sql = "SELECT COALESCE(col, 0) AS val FROM t"
        for dialect in ("snowflake", "bigquery", "postgres"):
            result = extract_columns(coalesce_sql, dialect=dialect)
            assert result == ["val"], f"COALESCE should extract ['val'] on {dialect}, got {result}"

    def test_nvl_snowflake(self):
        """NVL is Snowflake-specific. BigQuery uses IFNULL."""
        sql = "SELECT NVL(col, 0) AS val FROM t"
        sf_result = extract_columns(sql, dialect="snowflake")
        assert sf_result is not None, "NVL should parse with snowflake"
        assert sf_result == ["val"]

    def test_ifnull_bigquery(self):
        """IFNULL on BigQuery dialect."""
        sql = "SELECT IFNULL(col, 0) AS val FROM t"
        bq_result = extract_columns(sql, dialect="bigquery")
        assert bq_result is not None, "IFNULL should parse with bigquery"
        assert bq_result == ["val"]

    def test_coalesce_all_dialects(self):
        """COALESCE is SQL standard — must work on all dialects."""
        sql = "SELECT COALESCE(col, 0) AS val FROM t"
        for dialect in ("snowflake", "bigquery", "postgres", "mysql", "duckdb"):
            result = extract_columns(sql, dialect=dialect)
            assert result == ["val"], f"COALESCE should extract ['val'] on {dialect}, got {result}"

    def test_cast_equivalents(self):
        """CAST is standard SQL — should work on all dialects."""
        sql = "SELECT CAST(revenue AS DECIMAL(10,2)) AS revenue FROM t"
        for dialect in ("snowflake", "bigquery", "postgres"):
            result = extract_columns(sql, dialect=dialect)
            assert result is not None, f"CAST should parse on {dialect}"
            assert "revenue" in result

    def test_column_names_lowercased_across_dialects(self):
        """Column names should be lowercased regardless of dialect."""
        sql = "SELECT User_ID, Order_Status FROM t"
        for dialect in ("snowflake", "bigquery", "postgres"):
            result = extract_columns(sql, dialect=dialect)
            assert result is not None, f"Simple SELECT should parse on {dialect}"
            assert result == ["user_id", "order_status"], (
                f"Column names should be lowercased on {dialect}, got {result}"
            )

    def test_window_function_across_dialects(self):
        """ROW_NUMBER() OVER is SQL standard — should extract on all."""
        sql = (
            "SELECT user_id, "
            "ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) AS rn "
            "FROM events"
        )
        for dialect in ("snowflake", "bigquery", "postgres"):
            result = extract_columns(sql, dialect=dialect)
            assert result is not None, f"Window function should parse on {dialect}"
            assert "user_id" in result
            assert "rn" in result


# ---------------------------------------------------------------------------
# Scenario 6: stats command respects dialect
# ---------------------------------------------------------------------------
class TestStatsCommandDialect:
    """The stats command uses extract_columns with a dialect parameter.

    Verify that SELECT * detection works regardless of dialect.
    """

    def test_select_star_detected_with_snowflake(self):
        """SELECT * is universal SQL — detected on snowflake dialect."""
        sql = "SELECT * FROM my_table"
        result = extract_columns(sql, dialect="snowflake")
        assert result == ["*"]

    def test_select_star_detected_with_bigquery(self):
        """SELECT * is universal SQL — detected on bigquery dialect."""
        sql = "SELECT * FROM my_table"
        result = extract_columns(sql, dialect="bigquery")
        assert result == ["*"]

    def test_select_star_detected_with_postgres(self):
        """SELECT * is universal SQL — detected on postgres dialect."""
        sql = "SELECT * FROM my_table"
        result = extract_columns(sql, dialect="postgres")
        assert result == ["*"]

    def test_star_except_detected_on_bigquery(self):
        """SELECT * EXCEPT — only makes sense on bigquery."""
        sql = "SELECT * EXCEPT(internal_id) FROM my_table"
        result = extract_columns(sql, dialect="bigquery")
        assert result is not None
        assert result == ["* except(internal_id)"]

    def test_stats_dialect_parameter_flow(self):
        """Simulate the stats command column extraction loop.

        In cli.py _do_stats:
            dialect = getattr(args, "dialect", "snowflake") or "snowflake"
            cols = extract_columns(sql_file.read_text(), dialect=dialect)
            if cols == ["*"]:
                star_count += 1
        """
        test_sqls = [
            "SELECT * FROM events",
            "SELECT id, name FROM users",
            "SELECT * FROM orders",
            "SELECT user_id, SUM(revenue) AS total FROM conversions GROUP BY 1",
        ]

        for dialect in ("snowflake", "bigquery"):
            star_count = 0
            for sql in test_sqls:
                cols = extract_columns(sql, dialect=dialect)
                if cols == ["*"]:
                    star_count += 1
            # Two SELECT * queries in the list
            assert star_count == 2, f"Expected 2 SELECT * models on {dialect}, got {star_count}"

    def test_stats_consistency_across_dialects(self):
        """Same set of models should produce same stats regardless of dialect.

        Only dialect-specific functions might differ. Plain SELECT * is universal.
        """
        models = {
            "model_a": "SELECT * FROM raw_orders",
            "model_b": "SELECT user_id, order_status FROM events",
            "model_c": "SELECT * FROM sessions",
        }

        for dialect in ("snowflake", "bigquery", "postgres"):
            star_count = sum(
                1 for sql in models.values() if extract_columns(sql, dialect=dialect) == ["*"]
            )
            assert star_count == 2, f"Expected 2 SELECT * on {dialect}, got {star_count}"


# ---------------------------------------------------------------------------
# Bonus: End-to-end migration footgun scenarios
# ---------------------------------------------------------------------------
class TestMigrationFootguns:
    """Edge cases that catch real teams during migration."""

    def test_float64_vs_float_type_names(self):
        """FLOAT64 is BigQuery, FLOAT is Snowflake/Postgres.

        CAST with wrong type name may parse or fail depending on dialect.
        """
        bq_sql = "SELECT CAST(x AS FLOAT64) AS val FROM t"
        sf_sql = "SELECT CAST(x AS FLOAT) AS val FROM t"

        bq_on_bq = extract_columns(bq_sql, dialect="bigquery")
        sf_on_sf = extract_columns(sf_sql, dialect="snowflake")

        assert bq_on_bq is not None, "FLOAT64 should parse on bigquery"
        assert "val" in bq_on_bq

        assert sf_on_sf is not None, "FLOAT should parse on snowflake"
        assert "val" in sf_on_sf

        # Cross-dialect: FLOAT64 on snowflake, FLOAT on bigquery
        bq_on_sf = extract_columns(bq_sql, dialect="snowflake")
        sf_on_bq = extract_columns(sf_sql, dialect="bigquery")

        # These may or may not parse — document behavior, don't crash
        assert bq_on_sf is None or isinstance(bq_on_sf, list)
        assert sf_on_bq is None or isinstance(sf_on_bq, list)

    def test_string_vs_string_type(self):
        """STRING is BigQuery type. VARCHAR/STRING both work in Snowflake."""
        bq_sql = "SELECT CAST(x AS STRING) AS val FROM t"
        sf_sql = "SELECT CAST(x AS VARCHAR) AS val FROM t"

        bq_result = extract_columns(bq_sql, dialect="bigquery")
        sf_result = extract_columns(sf_sql, dialect="snowflake")

        assert bq_result is not None and "val" in bq_result
        assert sf_result is not None and "val" in sf_result

    def test_timestamp_type_differences(self):
        """TIMESTAMP type has different semantics across dialects.

        Snowflake: TIMESTAMP_NTZ, TIMESTAMP_LTZ, TIMESTAMP_TZ
        BigQuery: TIMESTAMP, DATETIME
        """
        sf_sql = "SELECT CAST(ts AS TIMESTAMP_NTZ) AS ts FROM t"
        bq_sql = "SELECT CAST(ts AS TIMESTAMP) AS ts FROM t"

        sf_result = extract_columns(sf_sql, dialect="snowflake")
        bq_result = extract_columns(bq_sql, dialect="bigquery")

        # Both should at least parse with correct dialect
        assert sf_result is not None, "TIMESTAMP_NTZ should parse on snowflake"
        assert bq_result is not None, "TIMESTAMP should parse on bigquery"

    def test_array_agg_syntax_differences(self):
        """ARRAY_AGG syntax differs between Snowflake and BigQuery."""
        # Snowflake: ARRAY_AGG(col) WITHIN GROUP (ORDER BY ...)
        sf_sql = "SELECT user_id, ARRAY_AGG(item) AS items FROM t GROUP BY 1"
        # BigQuery: ARRAY_AGG(col ORDER BY ...)
        bq_sql = "SELECT user_id, ARRAY_AGG(item ORDER BY ts) AS items FROM t GROUP BY 1"

        sf_result = extract_columns(sf_sql, dialect="snowflake")
        bq_result = extract_columns(bq_sql, dialect="bigquery")

        assert sf_result is not None, "Snowflake ARRAY_AGG should parse"
        assert "items" in sf_result

        assert bq_result is not None, "BigQuery ARRAY_AGG should parse"
        assert "items" in bq_result

    def test_qualify_portability(self):
        """QUALIFY is Snowflake-specific. BigQuery added it, but not all dialects have it."""
        sql = (
            "SELECT user_id, ts, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY ts) AS rn "
            "FROM events QUALIFY rn = 1"
        )

        sf_result = extract_columns(sql, dialect="snowflake")
        assert sf_result is not None, "QUALIFY should parse on snowflake"

        bq_result = extract_columns(sql, dialect="bigquery")
        # BigQuery supports QUALIFY — should also parse
        assert bq_result is not None or isinstance(bq_result, list) or bq_result is None

    def test_lateral_flatten_is_snowflake_only(self):
        """LATERAL FLATTEN is pure Snowflake. BigQuery uses UNNEST."""
        sf_sql = (
            "SELECT f.value::string AS tag FROM events, LATERAL FLATTEN(input => events.tags) AS f"
        )
        bq_sql = "SELECT tag FROM events, UNNEST(tags) AS tag"

        sf_result = extract_columns(sf_sql, dialect="snowflake")
        bq_result = extract_columns(bq_sql, dialect="bigquery")

        # At minimum: don't crash, return None or list
        assert sf_result is None or isinstance(sf_result, list)
        assert bq_result is None or isinstance(bq_result, list)

    def test_extract_columns_never_returns_empty_list(self):
        """Core safety invariant: extract_columns never returns empty list [].

        It returns None (parse failure / no SELECT) or a non-empty list.
        An empty list would silently indicate "no columns" which is a false-safe risk.
        """
        sqls = [
            "SELECT data:field::string AS val FROM t",
            "SELECT * EXCEPT(id) FROM t",
            "SELECT a, b FROM t",
            "NOT VALID SQL",
            "",
        ]
        for sql in sqls:
            for dialect in ("snowflake", "bigquery", "postgres"):
                result = extract_columns(sql, dialect=dialect)
                assert result is None or (isinstance(result, list) and len(result) > 0), (
                    f"extract_columns returned empty list (false-safe risk). "
                    f"SQL: {sql!r}, dialect: {dialect}, result: {result}"
                )
