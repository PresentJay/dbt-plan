"""Resolving `select * from {{ ref(other_model) }}` through the project's own DAG.

After CTE resolution landed, this was the last shape dbt-plan could not read on
the measured corpus. The columns are not in the file, but they are not in the
warehouse either -- they are in the *other model's* compiled SQL, which dbt-plan
already has on disk, indexed by the manifest it already parses.

Resolution keeps the same contract as everywhere else: it refuses rather than
guesses. If the referenced model is itself unreadable, or is not in the manifest,
or the chain loops, the answer is ["*"] and the verdict stays "review required".
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import extract_columns


def lookup(mapping: dict[str, list[str]]):
    """A table resolver backed by a plain dict, keyed as cli.py keys it."""

    def resolve(key: str) -> list[str] | None:
        return mapping.get(key)

    return resolve


class TestResolvesThroughTheDag:
    def test_star_over_a_referenced_model(self):
        sql = 'SELECT * FROM "j"."main"."stg_orders"'
        got = extract_columns(
            sql,
            dialect="duckdb",
            table_columns=lookup({"j.main.stg_orders": ["order_id", "status"]}),
        )
        assert got == ["order_id", "status"]

    def test_falls_back_to_the_bare_model_name(self):
        """dbt model names are unique across a project, so the bare name is safe."""
        sql = 'SELECT * FROM "j"."main"."stg_orders"'
        got = extract_columns(
            sql, dialect="duckdb", table_columns=lookup({"stg_orders": ["order_id"]})
        )
        assert got == ["order_id"]

    def test_star_mixed_with_explicit_columns(self):
        sql = 'SELECT *, 1 AS extra FROM "j"."main"."stg_orders"'
        got = extract_columns(
            sql, dialect="duckdb", table_columns=lookup({"j.main.stg_orders": ["a", "b"]})
        )
        assert got == ["a", "b", "extra"]

    def test_a_cte_wrapping_a_referenced_model(self):
        sql = 'WITH s AS (SELECT * FROM "j"."main"."stg_orders") SELECT * FROM s'
        got = extract_columns(
            sql, dialect="duckdb", table_columns=lookup({"j.main.stg_orders": ["a", "b"]})
        )
        assert got == ["a", "b"]


class TestRefusals:
    def test_unknown_relation_refuses(self):
        sql = 'SELECT * FROM "j"."main"."not_a_model"'
        assert extract_columns(sql, dialect="duckdb", table_columns=lookup({})) == ["*"]

    def test_resolver_returning_nothing_refuses(self):
        sql = 'SELECT * FROM "j"."main"."stg_orders"'
        got = extract_columns(
            sql, dialect="duckdb", table_columns=lookup({"j.main.stg_orders": []})
        )
        assert got == ["*"]

    def test_join_guard_still_applies(self):
        """Cross-model resolution must not weaken the rule it was built beside."""
        sql = 'SELECT * FROM "j"."main"."a" JOIN "j"."main"."b" ON 1 = 1'
        got = extract_columns(
            sql,
            dialect="duckdb",
            table_columns=lookup({"j.main.a": ["x"], "j.main.b": ["y"]}),
        )
        assert got == ["*"]

    def test_qualified_star_over_a_table_refuses(self):
        """`t.*` needs alias-to-table mapping, which is deliberately not attempted."""
        sql = 'SELECT t.* FROM "j"."main"."stg_orders" AS t'
        got = extract_columns(
            sql, dialect="duckdb", table_columns=lookup({"j.main.stg_orders": ["a"]})
        )
        assert got == ["*"]

    def test_without_a_resolver_behaviour_is_unchanged(self):
        assert extract_columns('SELECT * FROM "j"."main"."stg_orders"', dialect="duckdb") == ["*"]


class TestEndToEnd:
    """Through _do_check, with a real manifest and two compiled directories."""

    @pytest.fixture
    def check(self, tmp_path):
        import argparse
        import contextlib
        import io
        import json
        from pathlib import Path

        from dbt_plan.cli import _do_check, _do_snapshot

        def run(base: dict[str, str], current: dict[str, str]):
            names = sorted(set(base) | set(current))
            manifest = {
                "nodes": {
                    f"model.p.{n}": {
                        "name": n,
                        "relation_name": f'"j"."main"."{n}"',
                        "config": {
                            "materialized": "incremental",
                            "on_schema_change": "sync_all_columns",
                            "enabled": True,
                        },
                        "columns": {},
                    }
                    for n in names
                },
                "child_map": {},
                "metadata": {"project_name": "p"},
            }

            def write(sqls):
                d = Path(tmp_path) / "target" / "compiled" / "p" / "models"
                d.mkdir(parents=True, exist_ok=True)
                for f in d.glob("*.sql"):
                    f.unlink()
                for n, s in sqls.items():
                    (d / f"{n}.sql").write_text(s)
                (Path(tmp_path) / "target" / "manifest.json").write_text(json.dumps(manifest))

            write(base)
            _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
            write(current)
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
                        dialect="duckdb",
                    )
                )
            return code, json.loads(buf.getvalue())

        return run

    def test_a_star_over_a_ref_is_analysed_instead_of_deferred(self, check):
        """The gap this closes: `fct` used to be permanently "review required".

        dbt-plan diffs compiled SQL, so a model only gets a verdict when its own
        file changed. Here it did -- and before this change its columns were
        ["*"] on both sides, so the only possible answer was "a human has to
        look". Now the columns come from the referenced model.
        """
        code, payload = check(
            base={
                "stg": "SELECT order_id, customer_id, status FROM raw",
                "fct": 'SELECT * FROM "j"."main"."stg"',
            },
            current={
                "stg": "SELECT order_id, customer_id, status FROM raw",
                "fct": 'SELECT *, 1 AS extra FROM "j"."main"."stg"',
            },
        )
        fct = {m["model_name"]: m for m in payload["models"]}["fct"]
        assert fct["columns_added"] == ["extra"]
        assert not any("REVIEW REQUIRED" in op["operation"] for op in fct["operations"])
        assert code == 0

    def test_a_drop_behind_the_star_becomes_visible(self, check):
        """When `fct`'s own file changed too, the upstream drop is now readable.

        Its columns come from `stg`, which is read separately on each side, so
        the removal shows up as a real DROP COLUMN on `fct` rather than ["*"].
        """
        code, payload = check(
            base={
                "stg": "SELECT order_id, customer_id, status FROM raw",
                "fct": 'SELECT * FROM "j"."main"."stg"',
            },
            current={
                "stg": "SELECT order_id, status FROM raw",
                "fct": 'SELECT * FROM "j"."main"."stg" WHERE order_id IS NOT NULL',
            },
        )
        fct = {m["model_name"]: m for m in payload["models"]}["fct"]
        assert "customer_id" in fct["columns_removed"]
        assert fct["safety"] == "destructive"
        assert code == 1

    def test_an_unchanged_chain_is_still_safe(self, check):
        code, payload = check(
            base={"stg": "SELECT a, b FROM raw", "fct": 'SELECT * FROM "j"."main"."stg"'},
            current={
                "stg": "SELECT a, b FROM raw WHERE x",
                "fct": 'SELECT * FROM "j"."main"."stg"',
            },
        )
        assert all(m["safety"] == "safe" for m in payload["models"])
        assert code == 0

    def test_a_cycle_does_not_hang(self, check):
        code, _ = check(
            base={
                "a": 'SELECT * FROM "j"."main"."b"',
                "b": 'SELECT * FROM "j"."main"."a"',
            },
            current={
                "a": 'SELECT * FROM "j"."main"."b"',
                "b": 'SELECT * FROM "j"."main"."a" WHERE x',
            },
        )
        assert code in (0, 1, 2)


class TestStarMacroDegradation:
    """`dbt_utils.star()` emits a literal `*` when the relation does not exist yet.

    It introspects the warehouse at compile time, so against a fresh CI schema it
    returns nothing and writes a bare `*` plus an explanatory comment. dbt-plan
    then reports "review required (SELECT *)" on every such model forever, with
    no hint that the cause is upstream of it. That reads as dbt-plan being broken,
    and the reasonable next step looks like `ignore_models` -- which is exactly
    the wrong one.
    """

    COMPILED = """
    SELECT
    *
    /* No columns were returned. Maybe the relation doesn't exist yet
    or all columns were excluded. This star is only output during
    dbt compile, and exists to keep SQLFluff happy. */
                , 1 AS extra
    FROM "j"."main"."stg_orders"
    """

    def test_the_output_names_the_macro_as_the_cause(self, tmp_path, capsys):
        import argparse
        import json
        from pathlib import Path

        from dbt_plan.cli import _do_check, _do_snapshot

        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                        "enabled": True,
                    },
                    "columns": {},
                }
            },
            "child_map": {},
            "metadata": {"project_name": "p"},
        }
        d = Path(tmp_path) / "target" / "compiled" / "p" / "models"
        d.mkdir(parents=True)
        (d / "m.sql").write_text(self.COMPILED)
        (Path(tmp_path) / "target" / "manifest.json").write_text(json.dumps(manifest))
        _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
        (d / "m.sql").write_text(self.COMPILED + "\nWHERE 1 = 1")
        capsys.readouterr()

        _do_check(
            argparse.Namespace(
                project_dir=str(tmp_path),
                target_dir="target",
                base_dir=".dbt-plan/base",
                manifest=None,
                format="text",
                no_color=True,
                select=None,
                verbose=False,
                dialect="duckdb",
            )
        )

        out = capsys.readouterr().out
        assert "star()" in out
        assert "ignore_models" not in out

    def test_an_ordinary_select_star_does_not_get_the_macro_message(self, tmp_path, capsys):
        from dbt_plan.cli import _star_macro_degraded

        assert _star_macro_degraded("SELECT * FROM t") is False
        assert _star_macro_degraded(self.COMPILED) is True
