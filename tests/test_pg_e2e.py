"""Audit of the DDL rules against a real PostgreSQL target (issue #262).

Mirrors TestAudit24RulesAgainstRealDbt in test_dbt_e2e.py, but against a
pinned local PostgreSQL instead of DuckDB. The dbt-plan exit-code contract is
asserted at parity with the DuckDB harness. What a *real* `dbt run` does with
`on_schema_change` is adapter reality, and the two adapters disagree in places;
those assertions record the measured behaviour, and docs/adapter-validation.md
names each divergence.

Requires: dbt-core, dbt-postgres, and either the pgserver package (embedded
PostgreSQL 16.2) or DBT_PG_HOST/DBT_PG_PORT/DBT_PG_USER/DBT_PG_PASSWORD/
DBT_PG_DBNAME pointing at a running server. The module skips otherwise.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from . import pg_harness

pytestmark = pytest.mark.skipif(
    pg_harness.missing_requirement() is not None,
    reason=pg_harness.missing_requirement() or "",
)


@pytest.fixture(scope="module")
def _pg_server(tmp_path_factory):
    if pg_harness.external_configured():
        yield None
        return
    data_dir = tmp_path_factory.mktemp("pg_harness_data")
    with pg_harness.embedded_server(data_dir) as server:
        yield server


@pytest.fixture
def pg(_pg_server):
    """One disposable schema against a shared server, dropped afterwards."""
    if pg_harness.external_configured():
        base = pg_harness.PgDb(
            host=os.environ["DBT_PG_HOST"],
            port=int(os.environ["DBT_PG_PORT"]),
            user=os.environ["DBT_PG_USER"],
            password=os.environ["DBT_PG_PASSWORD"],
            dbname=os.environ["DBT_PG_DBNAME"],
            schema="",
        )
    else:
        host, port = pg_harness.host_and_port(_pg_server)
        base = pg_harness.PgDb(host, port, "postgres", "", "postgres", "")
    db = replace(base, schema=pg_harness.new_schema_id())
    pg_harness.create_schema(db)
    try:
        yield db
    finally:
        pg_harness.drop_schema(db)


def _snapshot_built(project: Path) -> None:
    built = pg_harness.dbt_run(project)
    assert built.returncode == 0, built.stdout + built.stderr
    snapshot = pg_harness.dbt_plan(["snapshot", "--project-dir", str(project)])
    assert snapshot.returncode == 0, snapshot.stdout + snapshot.stderr


def _check(project: Path):
    compiled = pg_harness.dbt_compile(project)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = pg_harness.dbt_plan(
        ["check", "--project-dir", str(project), "--dialect", "postgres", "--format", "json"]
    )
    assert result.returncode in (0, 1, 2), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["stale_sources"] == [], result.stdout
    assert report["uncompiled_models"] == [], result.stdout
    return result, report


class TestAudit24RulesAgainstRealPostgres:
    """The DuckDB audit, re-run against PostgreSQL 16.2 (issue #262)."""

    @pytest.fixture
    def rule_project(self, tmp_path, pg):
        return pg_harness.make_project(tmp_path / "audit24_pg", pg)

    @pytest.mark.parametrize("osc", ["ignore", "fail", "append_new_columns", "sync_all_columns"])
    @pytest.mark.parametrize("change", ["add", "remove"])
    def test_incremental_schema_change_on_existing_target(self, rule_project, pg, osc, change):
        config = "{{ config(materialized='incremental', on_schema_change='" + osc + "') }}\n"
        narrow = "select 1 as order_id, 'open' as status\n"
        wide = "select 1 as order_id, 'open' as status, 2 as amount\n"
        old, new = (narrow, wide) if change == "add" else (wide, narrow)
        model = rule_project / "models/fct_orders.sql"
        model.write_text(config + old, encoding="utf-8")
        _snapshot_built(rule_project)
        assert pg_harness.read_columns(pg, "fct_orders") == (
            ["order_id", "status"] if change == "add" else ["order_id", "status", "amount"]
        )

        model.write_text(config + new, encoding="utf-8")
        check, report = _check(rule_project)
        expected_code = (
            2
            if osc in {"ignore", "fail"} or (osc == "append_new_columns" and change == "remove")
            else (1 if osc == "sync_all_columns" and change == "remove" else 0)
        )
        assert check.returncode == expected_code, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert any(item["model_name"] == "fct_orders" for item in report["models"])
        if osc == "sync_all_columns" and change == "remove":
            assert "DROP COLUMN" in check.stdout and "amount" in check.stdout

        built = pg_harness.dbt_run(rule_project)
        should_fail = osc == "fail" or (osc == "ignore" and change == "remove")
        assert (built.returncode != 0) == should_fail, built.stdout + built.stderr

        expected = ["order_id", "status"]
        if (change == "remove" and osc != "sync_all_columns") or (
            change == "add" and osc in {"append_new_columns", "sync_all_columns"}
        ):
            expected.append("amount")
        assert pg_harness.read_columns(pg, "fct_orders") == expected
        assert pg_harness.count_rows(pg, "fct_orders") == (1 if should_fail else 2)
        if osc == "append_new_columns":
            # Schema retention is not backfill: an added column has one original
            # value and one NULL across the two successful runs.
            assert pg_harness.read_values(pg, "fct_orders", "amount") == [(2,), (None,)]

    def test_ephemeral_star_uses_real_inlined_cte(self, rule_project, pg):
        stage = rule_project / "models/stg_orders.sql"
        stage.write_text(
            "{{ config(materialized='ephemeral') }}\nselect 1 as order_id, 2 as amount\n",
            encoding="utf-8",
        )
        downstream = rule_project / "models/fct_orders.sql"
        downstream.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "select * from {{ ref('stg_orders') }}\n",
            encoding="utf-8",
        )
        _snapshot_built(rule_project)
        compiled = rule_project / "target/compiled/pg_rules/models/fct_orders.sql"
        assert "__dbt__cte__stg_orders" in compiled.read_text(encoding="utf-8")

        stage.write_text(
            "{{ config(materialized='ephemeral') }}\nselect 1 as order_id\n", encoding="utf-8"
        )
        check, report = _check(rule_project)
        assert report["parse_failures"] == []
        assert check.returncode == 1, check.stdout + check.stderr
        assert "DROP COLUMN" in check.stdout and "amount" in check.stdout

        built = pg_harness.dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id"]

    def test_materialization_change_is_reported_and_executed(self, rule_project, pg):
        model = rule_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='view') }}\nselect 1 as order_id\n", encoding="utf-8"
        )
        _snapshot_built(rule_project)
        model.write_text(
            "{{ config(materialized='table') }}\nselect 1 as order_id\n", encoding="utf-8"
        )
        check, report = _check(rule_project)
        assert "materialization" in check.stdout.lower(), check.stdout
        assert "view" in check.stdout and "table" in check.stdout
        assert report["parse_failures"] == []
        built = pg_harness.dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert pg_harness.read_table_type(pg, "fct_orders") == "BASE TABLE"

    def test_macro_only_edit_changes_compiled_schema(self, rule_project, pg):
        (rule_project / "macros").mkdir()
        macro = rule_project / "macros/order_columns.sql"
        macro.write_text(
            "{% macro order_columns() %}1 as order_id, 2 as amount{% endmacro %}\n",
            encoding="utf-8",
        )
        model = rule_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\n"
            "select {{ order_columns() }}\n",
            encoding="utf-8",
        )
        _snapshot_built(rule_project)
        original = model.read_bytes()
        macro.write_text(
            "{% macro order_columns() %}1 as order_id{% endmacro %}\n", encoding="utf-8"
        )
        check, report = _check(rule_project)
        assert model.read_bytes() == original
        assert check.returncode == 1, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert "DROP COLUMN" in check.stdout and "amount" in check.stdout
        built = pg_harness.dbt_run(rule_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id"]


class TestAQuotedAliasAndCaseRoundTrip:
    """A mixed-case `alias:` config is a quote somewhere and lowercase nowhere.

    dbt-plan's relation index strips quotes and folds case; PostgreSQL keeps the
    quoted spelling. The worry from #97 is that the two disagree about identity.
    """

    @pytest.fixture
    def alias_project(self, tmp_path, pg):
        project = pg_harness.make_project(tmp_path / "pg_alias", pg)
        model = project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns', "
            "alias='Fct_Orders') }}\n"
            "select 1 as order_id, 'open' as status\n",
            encoding="utf-8",
        )
        return project

    def test_quoted_alias_survives_a_column_drop(self, alias_project, pg):
        _snapshot_built(alias_project)
        assert pg_harness.read_columns(pg, "Fct_Orders") == ["order_id", "status"]

        model = alias_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='incremental', on_schema_change='sync_all_columns', "
            "alias='Fct_Orders') }}\n"
            "select 1 as order_id\n",
            encoding="utf-8",
        )
        check, report = _check(alias_project)
        assert check.returncode == 1, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert "DROP COLUMN" in check.stdout and "status" in check.stdout
        built = pg_harness.dbt_run(alias_project)
        assert built.returncode == 0, built.stdout + built.stderr
        assert pg_harness.read_columns(pg, "Fct_Orders") == ["order_id"]


class TestContractEnforcementOnPostgres:
    """An enforced contract that dbt refuses, measured on the adapter."""

    @pytest.fixture
    def contract_project(self, tmp_path, pg):
        project = pg_harness.make_project(tmp_path / "pg_contract", pg)
        model = project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='table') }}\n"
            "select 1 as order_id, 'cust_abc' as customer_id\n",
            encoding="utf-8",
        )
        (project / "models" / "schema.yml").write_text(
            """version: 2
models:
  - name: fct_orders
    config:
      materialized: table
    contract:
      enforced: true
    columns:
      - name: order_id
        data_type: integer
      - name: customer_id
        data_type: varchar
""",
            encoding="utf-8",
        )
        return project

    def _build(self, project):
        _snapshot_built(project)

    def test_dropping_a_contracted_column_is_flagged_and_dbt_refuses(self, contract_project, pg):
        self._build(contract_project)
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id", "customer_id"]

        model = contract_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='table') }}\nselect 1 as order_id\n",
            encoding="utf-8",
        )
        check, report = _check(contract_project)
        assert check.returncode == 2, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert "customer_id" in check.stdout

        built = pg_harness.dbt_run(contract_project)
        assert built.returncode != 0, built.stdout + built.stderr
        assert "contract" in (built.stdout + built.stderr).lower()
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id", "customer_id"]

    def test_an_undeclared_column_is_a_warning_and_dbt_refuses(self, contract_project, pg):
        self._build(contract_project)

        model = contract_project / "models/fct_orders.sql"
        model.write_text(
            "{{ config(materialized='table') }}\n"
            "select 1 as order_id, 'cust_abc' as customer_id, 2 as amount\n",
            encoding="utf-8",
        )
        check, report = _check(contract_project)
        assert check.returncode == 2, check.stdout + check.stderr
        assert report["parse_failures"] == []
        assert "missing in contract" in check.stdout

        built = pg_harness.dbt_run(contract_project)
        assert built.returncode != 0, built.stdout + built.stderr
        assert "contract" in (built.stdout + built.stderr).lower()
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id", "customer_id"]

    def test_a_matching_shape_builds(self, contract_project, pg):
        self._build(contract_project)
        assert pg_harness.read_columns(pg, "fct_orders") == ["order_id", "customer_id"]
        check, report = _check(contract_project)
        assert check.returncode == 0, check.stdout + check.stderr
        assert report["parse_failures"] == []
        built = pg_harness.dbt_run(contract_project)
        assert built.returncode == 0, built.stdout + built.stderr
