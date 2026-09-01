"""Detecting a column's type change from the explicit CASTs in compiled SQL.

The README listed this under "Deliberately Not Planned", reasoning that deciding
whether a type *changed* needs the warehouse's current type. That is true in
general and false in the case that matters: when both revisions carry an explicit
CAST on the same column and the two casts differ, the change is visible by
comparing compiled SQL to compiled SQL -- which is the only thing dbt-plan ever
does. No connection is involved.

It matters because dbt acts on it. From dbt's docs on `sync_all_columns`: "Adds
any new columns to the existing table, and removes any columns that are now
missing. Note that this is _inclusive_ of data type changes." So dbt will alter
the column, and dbt-plan reported SAFE because the column *names* matched.

Whether a particular type change loses data (VARCHAR -> INT) or is a harmless
widening (INT -> BIGINT) is not decidable from the SQL, so the verdict is
"review required", never destructive and never safe.
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import extract_cast_types

D = "duckdb"


def casts(sql: str):
    return extract_cast_types(sql, dialect=D)


class TestExtraction:
    def test_reads_an_explicit_cast(self):
        assert casts("SELECT CAST(a AS VARCHAR) AS amount FROM t") == {"amount": "TEXT"}

    def test_reads_the_double_colon_form(self):
        assert casts("SELECT a::INT AS n FROM t") == {"n": "INT"}

    def test_columns_without_a_cast_are_absent(self):
        assert casts("SELECT a, CAST(b AS INT) AS n FROM t") == {"n": "INT"}

    def test_no_casts_at_all_is_an_empty_mapping(self):
        assert casts("SELECT a, b FROM t") == {}

    def test_resolves_through_a_cte_star(self):
        """After #20 this is the ordinary shape, so it has to work here too."""
        sql = """
        with renamed as (select CAST(amount AS VARCHAR) as amount_str, id from raw)
        select * from renamed
        """
        assert casts(sql) == {"amount_str": "TEXT"}

    def test_unparseable_sql_returns_none(self):
        assert casts("!!! not sql !!!") is None

    def test_an_unresolvable_star_returns_none(self):
        """Same refusal contract as column extraction: no guessing."""
        assert casts("SELECT * FROM some_table") is None


class TestPrecision:
    def test_a_length_change_is_visible_where_the_dialect_keeps_it(self):
        """duckdb is excluded on purpose: there, VARCHAR(10) *is* VARCHAR."""
        a = extract_cast_types("SELECT CAST(a AS VARCHAR(10)) AS c FROM t", dialect="snowflake")
        b = extract_cast_types("SELECT CAST(a AS VARCHAR(20)) AS c FROM t", dialect="snowflake")
        assert a == {"c": "VARCHAR(10)"}
        assert b == {"c": "VARCHAR(20)"}

    def test_a_scale_change_is_visible(self):
        assert casts("SELECT CAST(a AS DECIMAL(10,2)) AS c FROM t") != casts(
            "SELECT CAST(a AS DECIMAL(10,4)) AS c FROM t"
        )

    def test_the_same_type_written_two_ways_does_not_differ(self):
        """`CAST(x AS INT)` and `x::INT` are the same instruction."""
        assert casts("SELECT CAST(a AS INT) AS c FROM t") == casts("SELECT a::INT AS c FROM t")


class TestVerdict:
    """End to end: a type change must not report safe."""

    @pytest.fixture
    def check(self, tmp_path):
        import argparse
        import contextlib
        import io
        import json
        from pathlib import Path

        from dbt_plan.cli import _do_check, _do_snapshot

        def run(before: str, after: str, materialized="incremental", osc="sync_all_columns"):
            manifest = {
                "nodes": {
                    "model.p.m": {
                        "name": "m",
                        "config": {
                            "materialized": materialized,
                            "on_schema_change": osc,
                            "enabled": True,
                        },
                        "columns": {},
                    }
                },
                "child_map": {},
                "metadata": {"project_name": "p"},
            }

            def write(sql):
                d = Path(tmp_path) / "target" / "compiled" / "p" / "models"
                d.mkdir(parents=True, exist_ok=True)
                (d / "m.sql").write_text(sql)
                (Path(tmp_path) / "target" / "manifest.json").write_text(json.dumps(manifest))

            write(before)
            _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
            write(after)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = _do_check(
                    argparse.Namespace(
                        project_dir=str(tmp_path),
                        target_dir="target",
                        base_dir=".dbt-plan/base",
                        manifest=None,
                        format="json",
                        no_color=True,
                        select=None,
                        verbose=False,
                        dialect=D,
                    )
                )
            return code, json.loads(buf.getvalue())

        return run

    def test_a_changed_cast_is_not_safe(self, check):
        code, payload = check(
            "SELECT id, CAST(amount AS VARCHAR) AS amount_v FROM t",
            "SELECT id, CAST(amount AS DECIMAL(10,2)) AS amount_v FROM t",
        )
        pred = payload["models"][0]
        assert pred["safety"] == "warning"
        assert any("TYPE CHANGED" in op["operation"] for op in pred["operations"])
        assert code == 2

    def test_the_message_names_both_types(self, check):
        _, payload = check(
            "SELECT CAST(a AS VARCHAR) AS c FROM t",
            "SELECT CAST(a AS INT) AS c FROM t",
        )
        op = next(
            o["operation"]
            for o in payload["models"][0]["operations"]
            if "TYPE CHANGED" in o["operation"]
        )
        assert "TEXT" in op and "INT" in op

    def test_an_unchanged_cast_is_still_safe(self, check):
        code, payload = check(
            "SELECT id, CAST(a AS INT) AS c FROM t WHERE x = 1",
            "SELECT id, CAST(a AS INT) AS c FROM t WHERE x = 2",
        )
        assert payload["models"][0]["safety"] == "safe"
        assert code == 0

    def test_a_table_is_unaffected(self, check):
        """CREATE OR REPLACE rebuilds the table with the new type anyway."""
        code, payload = check(
            "SELECT CAST(a AS VARCHAR) AS c FROM t",
            "SELECT CAST(a AS INT) AS c FROM t",
            materialized="table",
            osc=None,
        )
        assert payload["models"][0]["safety"] == "safe"
        assert code == 0

    def test_a_cast_added_on_only_one_side_is_not_reported(self, check):
        """Without a cast on both sides the other type is unknown, so say nothing."""
        code, payload = check(
            "SELECT a AS c FROM t",
            "SELECT CAST(a AS INT) AS c FROM t",
        )
        ops = [o["operation"] for o in payload["models"][0]["operations"]]
        assert not any("TYPE CHANGED" in o for o in ops)
