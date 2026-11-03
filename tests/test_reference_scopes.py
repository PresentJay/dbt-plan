"""Do not mistake an unresolved derived read or a shadowed alias for no read."""

import pytest

from dbt_plan.columns import columns_read_by_relation, columns_read_from
from tests.test_audit24_cli import check, project, save

SCHEMA = {
    "stg_orders": {"order_id": "INT", "customer_id": "INT"},
    "dim_customers": {"customer_id": "INT", "tier": "TEXT"},
}


@pytest.mark.parametrize(
    "sql",
    [
        "WITH orders AS (SELECT * FROM stg_orders) SELECT o.customer_id FROM orders o",
        "SELECT o.customer_id FROM (SELECT * FROM stg_orders) o",
        "WITH orders AS (SELECT * FROM stg_orders), recent AS (SELECT * FROM orders) "
        "SELECT customer_id FROM recent",
    ],
)
def test_explicit_read_through_derived_source_requires_review(sql):
    assert columns_read_by_relation(sql, SCHEMA, dialect="duckdb") is None


def test_alias_reuse_in_another_scope_cannot_hide_a_physical_read():
    sql = (
        "SELECT c.customer_id FROM stg_orders c WHERE EXISTS "
        "(SELECT 1 FROM dim_customers c WHERE c.tier = 'premium')"
    )
    assert columns_read_from(sql, "stg_orders", SCHEMA, dialect="duckdb") == ["customer_id"]
    assert columns_read_from(sql, "dim_customers", SCHEMA, dialect="duckdb") == ["tier"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM stg_orders",
        "WITH orders AS (SELECT * FROM stg_orders) SELECT * FROM orders",
    ],
)
def test_pure_stars_still_have_no_explicit_column_read(sql):
    assert columns_read_from(sql, "stg_orders", SCHEMA, dialect="duckdb") == []


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_view_column_loss_with_a_cte_reader_is_destructive(tmp_path, capsys, prefix):
    manifest = project(tmp_path, config={"materialized": "view"})
    manifest["nodes"]["model.bookshop.orders"].pop("unrendered_config")
    sql = prefix + "WITH recent AS (SELECT * FROM orders) SELECT r.tax FROM recent r"
    manifest["nodes"]["model.bookshop.reader"] = {
        "name": "reader",
        "resource_type": "model",
        "original_file_path": "models/reader.sql",
        "config": {"materialized": "table"},
        "depends_on": {"nodes": ["model.bookshop.orders"]},
    }
    manifest["child_map"] = {"model.bookshop.orders": ["model.bookshop.reader"]}
    for directory in ("target/compiled/bookshop/models", ".dbt-plan/base/compiled/models"):
        (tmp_path / directory / "reader.sql").write_text(sql, encoding="utf-8")
    save(tmp_path, manifest, True)
    save(tmp_path, manifest)

    code, report = check(tmp_path, capsys)

    assert code == 1
    assert report["summary"]["destructive"] == 1
    assert any(
        impact["model_name"] == "reader" and impact["risk"] == "broken_ref"
        for model in report["models"]
        for impact in model.get("downstream_impacts", [])
    )
