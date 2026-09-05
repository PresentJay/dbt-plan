"""Exposures downstream of a change that is not safe.

An exposure is how a dbt project records that something outside it -- a
dashboard, a notebook, a reverse-ETL sync -- reads a model. It declares that at
model granularity, with no columns in it, so nothing here claims a dashboard
breaks. It says who is downstream of a change and needs telling.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.manifest import ExposureNode, build_exposure_index
from dbt_plan.predictor import (
    DDLOperation,
    DDLPrediction,
    Safety,
    attach_downstream_exposures,
)

_DASHBOARD = {
    "unique_id": "exposure.p.orders_dashboard",
    "name": "orders_dashboard",
    "type": "dashboard",
    "owner": {"name": "Data Team", "email": "data@example.com"},
    "url": "https://example.com/dashboards/orders",
    "config": {"enabled": True},
}


class TestBuildExposureIndex:
    def test_reads_the_fields_a_report_needs(self):
        index = build_exposure_index({"exposures": {_DASHBOARD["unique_id"]: _DASHBOARD}})
        exposure = index["exposure.p.orders_dashboard"]
        assert exposure.name == "orders_dashboard"
        assert exposure.type == "dashboard"
        assert exposure.owner() == "Data Team <data@example.com>"
        assert exposure.url == "https://example.com/dashboards/orders"

    @pytest.mark.parametrize(
        "owner,expected",
        [
            ({"name": "Data Team", "email": "data@example.com"}, "Data Team <data@example.com>"),
            ({"email": "data@example.com"}, "data@example.com"),
            ({"name": "Data Team"}, "Data Team"),
            ({}, ""),
        ],
    )
    def test_owner_drops_whichever_half_dbt_does_not_carry(self, owner, expected):
        """dbt requires one of the two, not both, and older projects carry only email."""
        index = build_exposure_index(
            {"exposures": {"exposure.p.e": {**_DASHBOARD, "owner": owner}}}
        )
        assert index["exposure.p.e"].owner() == expected

    def test_disabled_exposures_are_left_out(self):
        index = build_exposure_index(
            {"exposures": {"exposure.p.e": {**_DASHBOARD, "config": {"enabled": False}}}}
        )
        assert index == {}

    def test_a_manifest_without_exposures_gives_an_empty_index(self):
        assert build_exposure_index({}) == {}


def _prediction(safety: Safety) -> DDLPrediction:
    return DDLPrediction(
        model_name="stg_orders",
        materialization="incremental",
        on_schema_change="sync_all_columns",
        safety=safety,
        operations=[DDLOperation("DROP COLUMN", "customer_id")],
        columns_removed=["customer_id"],
    )


def _attach(pred, *, child_map, downstream=(), exposures=(_DASHBOARD,)):
    return attach_downstream_exposures(
        predictions=[pred],
        model_node_ids={"stg_orders": "model.p.stg_orders"},
        all_downstream={"model.p.stg_orders": list(downstream)},
        child_map=child_map,
        exposure_index=build_exposure_index({"exposures": {e["unique_id"]: e for e in exposures}}),
    )[0]


class TestAttachDownstreamExposures:
    def test_a_destructive_change_names_the_exposure_reading_the_model(self):
        updated = _attach(
            _prediction(Safety.DESTRUCTIVE),
            child_map={"model.p.stg_orders": ["exposure.p.orders_dashboard"]},
        )
        assert [e.name for e in updated.downstream_exposures] == ["orders_dashboard"]

    def test_an_exposure_never_changes_the_verdict(self):
        """It is information attached to a finding, not a finding of its own."""
        for safety in (Safety.WARNING, Safety.DESTRUCTIVE):
            updated = _attach(
                _prediction(safety),
                child_map={"model.p.stg_orders": ["exposure.p.orders_dashboard"]},
            )
            assert updated.safety == safety

    def test_a_safe_verdict_carries_no_exposures(self):
        """A list of dashboards under a green check is noise."""
        updated = _attach(
            _prediction(Safety.SAFE),
            child_map={"model.p.stg_orders": ["exposure.p.orders_dashboard"]},
        )
        assert updated.downstream_exposures == []

    def test_an_exposure_further_downstream_is_reached(self):
        """The dashboard reads fct_orders, which reads the model losing a column."""
        updated = _attach(
            _prediction(Safety.DESTRUCTIVE),
            downstream=["model.p.fct_orders"],
            child_map={
                "model.p.stg_orders": ["model.p.fct_orders"],
                "model.p.fct_orders": ["exposure.p.orders_dashboard"],
            },
        )
        assert [e.name for e in updated.downstream_exposures] == ["orders_dashboard"]

    def test_an_exposure_reading_two_of_the_models_is_listed_once(self):
        updated = _attach(
            _prediction(Safety.DESTRUCTIVE),
            downstream=["model.p.fct_orders"],
            child_map={
                "model.p.stg_orders": ["model.p.fct_orders", "exposure.p.orders_dashboard"],
                "model.p.fct_orders": ["exposure.p.orders_dashboard"],
            },
        )
        assert len(updated.downstream_exposures) == 1

    def test_a_project_without_exposures_is_untouched(self):
        pred = _prediction(Safety.DESTRUCTIVE)
        assert _attach(pred, child_map={"model.p.stg_orders": []}, exposures=()) is pred


class TestRendering:
    def _result(self, exposure: ExposureNode | None = None) -> CheckResult:
        if exposure is None:
            exposure = build_exposure_index({"exposures": {"e": _DASHBOARD}})["e"]
        pred = replace(_prediction(Safety.DESTRUCTIVE), downstream_exposures=[exposure])
        return CheckResult([pred])

    def test_text_names_the_owner_to_go_and_tell(self):
        out = format_text(self._result(), color=False)
        assert (
            "EXPOSURE  orders_dashboard (dashboard) -- owner: Data Team <data@example.com>" in out
        )

    def test_github_renders_it_as_a_bullet_without_a_risk_icon(self):
        out = format_github(self._result())
        assert "- **EXPOSURE** orders_dashboard (dashboard) -- owner: Data Team" in out

    def test_json_keeps_the_pieces_apart_for_a_bot_to_route_on(self):
        out = json.loads(format_json(self._result()))
        assert out["models"][0]["downstream_exposures"] == [
            {
                "name": "orders_dashboard",
                "type": "dashboard",
                "owner": "Data Team <data@example.com>",
                "url": "https://example.com/dashboards/orders",
            }
        ]
        # Not counted as a risk: it is not one.
        assert "cascade_risks" not in out["summary"]

    def test_an_exposure_with_no_owner_or_type_still_renders(self):
        exposure = ExposureNode(node_id="exposure.p.e", name="orders_dashboard", type="")
        assert "EXPOSURE  orders_dashboard\n" in format_text(self._result(exposure), color=False)
