"""Inline CSV unit-test fixture header regressions for issue #227."""

from __future__ import annotations

import pytest

from dbt_plan.manifest import _fixture_columns, build_unit_test_index
from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl


def _unit_test(*, expect: dict | None = None, given: list[dict] | None = None) -> dict:
    return {
        "unique_id": "unit_test.p.dim_orders.csv_shape",
        "name": "csv_shape",
        "model": "dim_orders",
        "expect": expect or {"format": "dict", "rows": [{"order_id": 1}]},
        "given": given or [],
        "config": {"enabled": True},
    }


def _manifest(unit_test: dict) -> dict:
    return {"unit_tests": {unit_test["unique_id"]: unit_test}}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ('"order_id","customer_id"\n1,2', frozenset({"order_id", "customer_id"})),
        (
            '"customer, id","customer ""nickname"""\r\n1,2',
            frozenset({"customer, id", 'customer "nickname"'}),
        ),
        ("\ufefforder_id,customer_id\r\n1,2", frozenset({"order_id", "customer_id"})),
    ],
)
def test_inline_csv_header_decodes_standard_csv_escaping(
    rows: str, expected: frozenset[str]
) -> None:
    columns, reason = _fixture_columns({"format": "csv", "rows": rows})
    assert columns == expected
    assert reason == ""


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "\n1,2",
        "order_id, ,customer_id\n1,2,3",
        "order_id,ORDER_ID\n1,2",
        '"unterminated,customer_id\n1,2',
        'order_id,"customer"oops\n1,2',
    ],
)
def test_unreadable_inline_csv_never_claims_a_known_schema(rows: str) -> None:
    columns, reason = _fixture_columns({"format": "csv", "rows": rows})
    assert columns is None
    assert "CSV" in reason


def test_csv_fixture_columns_drive_the_unit_test_cascade() -> None:
    unit_test = _unit_test(
        given=[
            {
                "input": "ref('stg_orders')",
                "format": "csv",
                "rows": '"order_id","customer_id"\n1,2',
            }
        ]
    )
    unit_index = build_unit_test_index(_manifest(unit_test))
    prediction = predict_ddl(
        model_name="stg_orders",
        materialization="view",
        on_schema_change=None,
        base_columns=["order_id", "customer_id"],
        current_columns=["order_id"],
    )

    updated, _ = analyze_cascade_impacts(
        predictions=[prediction],
        model_node_ids={"stg_orders": "model.p.stg_orders"},
        model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
        all_downstream={"model.p.stg_orders": ["model.p.dim_orders"]},
        node_index={},
        base_node_index={},
        compiled_sql_index={},
        child_map={
            "model.p.stg_orders": ["model.p.dim_orders"],
            "model.p.dim_orders": ["unit_test.p.dim_orders.csv_shape"],
        },
        unit_test_index=unit_index,
    )

    assert updated[0].safety == Safety.WARNING
    assert updated[0].downstream_impacts[0].risk == "unit_test_failure"
    assert (
        "given for stg_orders names dropped column(s): customer_id"
        in updated[0].downstream_impacts[0].reason
    )


def test_malformed_csv_fixture_stays_a_review_required_refusal() -> None:
    unit_index = build_unit_test_index(
        _manifest(
            _unit_test(
                given=[
                    {
                        "input": "ref('stg_orders')",
                        "format": "csv",
                        "rows": '"unterminated,customer_id\n1,2',
                    }
                ]
            )
        )
    )
    fixture = unit_index["unit_test.p.dim_orders.csv_shape"].fixtures[1]
    assert fixture.columns is None
    assert "CSV" in fixture.unreadable_reason
