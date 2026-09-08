"""Reproductions from the post-release issue triage."""

import json
import subprocess
from unittest.mock import patch

import pytest

from dbt_plan.columns import extract_cast_types, extract_columns
from dbt_plan.predictor import Safety, predict_ddl


@pytest.mark.parametrize(
    "sql",
    [
        "(select a, b from t) union all select a, c from u",
        "((select a, b from t)) union all select a, c from u",
    ],
)
def test_set_operation_uses_left_projection(sql):
    assert extract_columns(sql) == ["a", "b"]


def test_union_casts_use_left_projection():
    assert extract_cast_types(
        "(select cast(a as int) as a from t) union all select cast(a as text) as a from u"
    ) == {"a": "INT"}


@pytest.mark.parametrize("projection", ["*", "r.*"])
@pytest.mark.parametrize(
    "suffix", ["pivot (sum(v) for k in ('a','b'))", "unpivot (value for name in (k, v))"]
)
def test_transformed_relation_does_not_reuse_input_columns(projection, suffix):
    sql = f"with r as (select k, v, id from t) select {projection} from r {suffix}"
    assert extract_columns(sql) in (None, ["*"])
    assert extract_cast_types(sql) is None


@pytest.mark.parametrize("projection", ["*", "orders.*"])
def test_qualified_relation_is_not_shadowed_by_cte(projection):
    sql = f"with orders as (select id from db.sch.orders) select {projection} from db.sch.orders"
    assert extract_columns(sql, table_columns=lambda name: ["id", "amount"]) == (
        ["id", "amount"] if projection == "*" else ["*"]
    )


@pytest.mark.parametrize("projection", ["*", "orders.*"])
@pytest.mark.parametrize("relation", ["actual_orders", "db.sch.actual_orders"])
def test_physical_relation_alias_does_not_select_an_unrelated_cte(projection, relation):
    sql = (
        f"with orders as (select id from raw_input) select {projection} from {relation} as orders"
    )
    columns = extract_columns(
        sql, table_columns=lambda name: {relation: ["id", "amount"]}.get(name)
    )
    # Qualified physical stars remain unsupported; a plausible CTE list is never valid.
    assert columns == (["id", "amount"] if projection == "*" else ["*"])


@pytest.mark.parametrize("projection", ["*", "orders.*"])
def test_unknown_physical_alias_cannot_borrow_cte_columns_or_casts(projection):
    sql = (
        "with orders as (select cast(id as int) as id from raw_input) "
        f"select {projection} from actual_orders as orders"
    )
    assert extract_columns(sql) == ["*"]
    assert extract_cast_types(sql) is None


@pytest.mark.parametrize("projection", ["*", "orders.*"])
def test_cte_alias_resolves_the_source_name_and_preserves_casts(projection):
    sql = (
        "with actual_orders as (select cast(id as int) as id, amount from raw_input), "
        "orders as (select wrong_column from raw_input) "
        f"select {projection} from actual_orders as orders"
    )
    assert extract_columns(sql) == ["id", "amount"]
    assert extract_cast_types(sql) == {"id": "INT"}


def test_physical_alias_does_not_inherit_an_unrelated_cte_cast():
    sql = (
        "with orders as (select cast(id as int) as id from raw_input) "
        "select * from actual_orders as orders"
    )
    assert (
        extract_cast_types(
            sql, table_columns=lambda name: {"actual_orders": ["id", "amount"]}.get(name)
        )
        == {}
    )


def test_cte_alias_collision_does_not_hide_a_physical_column_drop():
    sql = "with orders as (select id from raw_input) select * from actual_orders as orders"
    base = extract_columns(
        sql, table_columns=lambda name: {"actual_orders": ["id", "amount"]}.get(name)
    )
    current = extract_columns(sql, table_columns=lambda name: {"actual_orders": ["id"]}.get(name))
    verdict = predict_ddl("orders", "incremental", "sync_all_columns", base, current)
    assert verdict.safety == Safety.DESTRUCTIVE
    assert any(
        op.operation == "DROP COLUMN" and op.column == "amount" for op in verdict.operations
    )


def test_physical_alias_columns_match_duckdb():
    duckdb = pytest.importorskip("duckdb")
    sql = "with orders as (select id from raw_input) select * from actual_orders as orders"
    with duckdb.connect(":memory:") as connection:
        connection.execute("create table raw_input (id integer)")
        connection.execute("create table actual_orders (id integer, amount integer)")
        actual = [column[0] for column in connection.execute(sql).description]
    assert actual == ["id", "amount"]
    assert (
        extract_columns(
            sql, dialect="duckdb", table_columns=lambda name: {"actual_orders": actual}.get(name)
        )
        == actual
    )


@pytest.mark.parametrize("current", [["id"], ["id", "tax", "extra"]])
def test_incremental_ignore_warns_on_column_changes(current):
    result = predict_ddl("orders", "incremental", "ignore", ["id", "tax"], current)
    assert result.safety == Safety.WARNING
    assert not any(op.operation == "DROP COLUMN" for op in result.operations)


def test_new_incremental_ignore_is_still_safe():
    assert (
        predict_ddl("orders", "incremental", "ignore", None, ["id"], status="added").safety
        == Safety.SAFE
    )


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            {"models": [{"model_name": "orders", "safety": "destructive", "acknowledged": True}]},
            "destructive",
        ),
        ({"models": [{"model_name": "orders", "safety": "warning"}]}, "review_required"),
        ({"stale_sources": ["models/orders.sql"]}, "review_required"),
        (
            {
                "models": [
                    {
                        "model_name": "orders",
                        "safety": "safe",
                        "downstream_impacts": [
                            {
                                "model_name": "test.orders",
                                "risk": "data_test_unreadable",
                                "reason": "unknown columns",
                            }
                        ],
                    }
                ]
            },
            "review_required",
        ),
    ],
)
def test_mcp_verdict_describes_findings_even_when_exit_zero(report, expected):
    server = pytest.importorskip("dbt_plan_mcp.server")
    with patch.object(
        server, "_run_cli", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")
    ):
        result = server.plan(".")
    assert result["verdict"] == expected
    if report.get("stale_sources") or any(
        m.get("downstream_impacts") for m in report.get("models", [])
    ):
        assert result["refusals"]
