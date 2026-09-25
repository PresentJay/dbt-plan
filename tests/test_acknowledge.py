"""Acknowledged models: an escape hatch for intentional destructive changes.

Acknowledging a model does NOT hide it (that is what ignore_models does).
The change is still reported in full; it just stops driving the exit code,
so an intentional DROP COLUMN can land without disabling the check.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from dbt_plan.config import Config
from dbt_plan.formatter import CheckResult, format_github, format_json, format_text
from dbt_plan.predictor import DDLOperation, DDLPrediction, DownstreamImpact, Safety


def _destructive(name="int_orders", cols_removed=("revenue",)):
    return DDLPrediction(
        model_name=name,
        materialization="incremental",
        on_schema_change="sync_all_columns",
        safety=Safety.DESTRUCTIVE,
        operations=[DDLOperation("DROP COLUMN", c) for c in cols_removed],
        columns_removed=list(cols_removed),
    )


def _safe(name="dim_customers"):
    return DDLPrediction(
        model_name=name,
        materialization="table",
        on_schema_change=None,
        safety=Safety.SAFE,
        operations=[DDLOperation("CREATE OR REPLACE TABLE", None)],
    )


class TestConfig:
    def test_defaults_to_empty(self):
        assert Config().acknowledge_models == []

    def test_reads_yaml_key(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [int_orders, fct_orders]\n")
        assert Config.load(tmp_path).acknowledge_models == ["int_orders", "fct_orders"]

    def test_env_var_overrides_file(self, tmp_path, monkeypatch):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [from_file]\n")
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", "from_env,other")
        assert Config.load(tmp_path).acknowledge_models == ["from_env", "other"]

    def test_env_var_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", " a , b ")
        assert Config.load(tmp_path).acknowledge_models == ["a", "b"]

    def test_empty_env_var_is_ignored(self, tmp_path, monkeypatch):
        (tmp_path / ".dbt-plan.yml").write_text("acknowledge_models: [from_file]\n")
        monkeypatch.setenv("DBT_PLAN_ACKNOWLEDGE", "")
        assert Config.load(tmp_path).acknowledge_models == ["from_file"]


class TestTextOutput:
    def test_marks_the_model(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        out = format_text(r, color=False)
        assert "ACKNOWLEDGED" in out
        # still reported in full -- acknowledging is not hiding
        assert "int_orders" in out
        assert "DROP COLUMN  revenue" in out

    def test_unacknowledged_model_is_not_marked(self):
        out = format_text(CheckResult([_destructive()]), color=False)
        assert "ACKNOWLEDGED" not in out

    def test_summary_counts_acknowledged_separately(self):
        r = CheckResult([_destructive(), _safe()], acknowledge_models=["int_orders"])
        out = format_text(r, color=False)
        assert "1 destructive (1 acknowledged)" in out

    def test_summary_omits_note_when_none_acknowledged(self):
        out = format_text(CheckResult([_destructive(), _safe()]), color=False)
        assert "acknowledged" not in out

    def test_acknowledging_absent_model_changes_nothing(self):
        r = CheckResult([_destructive()], acknowledge_models=["some_other_model"])
        assert "ACKNOWLEDGED" not in format_text(r, color=False)


class TestGithubOutput:
    def test_marks_the_model(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert "ACKNOWLEDGED" in format_github(r)


class TestJsonOutput:
    def test_flags_the_model(self):
        r = CheckResult([_destructive(), _safe()], acknowledge_models=["int_orders"])
        data = json.loads(format_json(r))
        by_name = {m["model_name"]: m for m in data["models"]}
        assert by_name["int_orders"]["acknowledged"] is True
        assert by_name["dim_customers"]["acknowledged"] is False

    def test_summary_reports_count(self):
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert json.loads(format_json(r))["summary"]["acknowledged"] == 1

    def test_safety_value_is_unchanged(self):
        """Acknowledging is a CI policy, not a re-classification of the risk."""
        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        data = json.loads(format_json(r))
        assert data["models"][0]["safety"] == "destructive"
        assert data["summary"]["destructive"] == 1


class TestExitCode:
    """The whole point: an acknowledged destructive change stops failing CI."""

    def test_acknowledged_destructive_exits_zero(self):
        from dbt_plan.cli import _exit_code_for

        r = CheckResult([_destructive()], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 0

    def test_unacknowledged_destructive_still_exits_one(self):
        from dbt_plan.cli import _exit_code_for

        assert _exit_code_for(CheckResult([_destructive()]), warning_exit_code=2) == 1

    def test_one_acknowledged_does_not_excuse_another(self):
        """Named models only -- a second destructive model still fails the build."""
        from dbt_plan.cli import _exit_code_for

        r = CheckResult(
            [_destructive("int_orders"), _destructive("fct_orders")],
            acknowledge_models=["int_orders"],
        )
        assert _exit_code_for(r, warning_exit_code=2) == 1

    def test_acknowledging_does_not_mask_parse_failures(self):
        from dbt_plan.cli import _exit_code_for

        r = CheckResult(
            [_destructive()], parse_failures=["mystery_model"], acknowledge_models=["int_orders"]
        )
        assert _exit_code_for(r, warning_exit_code=2) == 2

    def test_acknowledging_does_not_mask_an_unrelated_warning(self):
        from dbt_plan.cli import _exit_code_for

        warned = DDLPrediction(
            model_name="snap_x",
            materialization="snapshot",
            on_schema_change=None,
            safety=Safety.WARNING,
        )
        r = CheckResult([_destructive(), warned], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 2

    def test_acknowledged_model_does_not_cover_its_cascade_block(self):
        """Only the affected resource can waive its finding."""
        from dbt_plan.cli import _exit_code_for

        pred = DDLPrediction(
            model_name="int_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            safety=Safety.DESTRUCTIVE,
            columns_removed=["revenue"],
            downstream_impacts=[
                DownstreamImpact(
                    model_name="fct_daily_sales",
                    materialization="incremental",
                    on_schema_change="append_new_columns",
                    risk="broken_ref",
                    reason="references dropped column(s): revenue",
                )
            ],
        )
        r = CheckResult([pred], acknowledge_models=["int_orders"])
        assert _exit_code_for(r, warning_exit_code=2) == 1

    def test_safe_only_is_still_zero(self):
        from dbt_plan.cli import _exit_code_for

        assert _exit_code_for(CheckResult([_safe()]), warning_exit_code=2) == 0


@pytest.mark.parametrize("own", [Safety.SAFE, Safety.WARNING, Safety.DESTRUCTIVE])
@pytest.mark.parametrize("risk,code", [("inherited_drop", 1), ("build_failure", 7)])
@pytest.mark.parametrize("names", [[], ["up"], ["down"], ["up", "down", "down"]])
def test_resource_policy_matrix(own, risk, code, names):
    from dbt_plan.cli import _exit_code_for
    from dbt_plan.predictor import RISK_SAFETY, worst_safety

    pred = replace(
        _destructive("up"),
        own_safety=own,
        safety=worst_safety([own, RISK_SAFETY[risk]]),
        downstream_impacts=[DownstreamImpact("down", "incremental", "fail", risk, "known")],
    )
    active = []
    if "up" not in names:
        active.append({Safety.SAFE: 0, Safety.WARNING: 7, Safety.DESTRUCTIVE: 1}[own])
    if "down" not in names:
        active.append(code)
    expected = 1 if 1 in active else 7 if 7 in active else 0
    result = CheckResult([pred], acknowledge_models=names)
    assert _exit_code_for(result, 7) == expected
    assert _exit_code_for(result, 0) == (1 if expected == 1 else 0)


@pytest.mark.parametrize("risk", ["future_risk", "data_test_unreadable", "unit_test_unreadable"])
def test_uncertainty_cannot_be_acknowledged(risk):
    from dbt_plan.cli import _exit_code_for

    pred = replace(
        _safe("up"),
        downstream_impacts=[DownstreamImpact("down", "data_test", None, risk, "cannot determine")],
    )
    assert _exit_code_for(CheckResult([pred], acknowledge_models=["up", "down"]), 2) == 2


def test_shared_target_and_rendering():
    from dbt_plan.cli import _exit_code_for

    impact = DownstreamImpact("down", "incremental", "sync_all_columns", "inherited_drop", "lost")
    parents = [
        replace(
            _safe(n),
            safety=Safety.DESTRUCTIVE,
            own_safety=Safety.SAFE,
            downstream_impacts=[impact],
        )
        for n in ("up", "other")
    ]
    result = CheckResult(parents, acknowledge_models=["up", "other"])
    assert _exit_code_for(result, 2) == 1
    for output in (format_text(result, color=False), format_github(result)):
        assert "ACKNOWLEDGED: own findings only" in output
        assert "ACTIVE" in output
    result.acknowledge_models = ["down", "down"]
    assert _exit_code_for(result, 2) == 0
    report = json.loads(format_json(result))
    assert report["summary"]["destructive"] == 2
    for model in report["models"]:
        assert model["safety"] == "destructive"
        assert model["own_safety"] == "safe"
        assert model["downstream_impacts"][0]["waived"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("parse_failures", ["up"]),
        ("skipped_models", ["up"]),
        ("uncompiled_models", ["up"]),
        ("stale_sources", ["up"]),
        ("baseline_problem", "corrupt"),
    ],
)
def test_refusals_survive_all_acknowledgements(field, value):
    from dbt_plan.cli import _exit_code_for

    result = CheckResult([_destructive("up")], acknowledge_models=["up"], **{field: value})
    assert _exit_code_for(result, 2) == 2


def test_unknown_own_operation_is_not_waived():
    from dbt_plan.cli import _exit_code_for

    pred = replace(
        _safe("up"),
        safety=Safety.WARNING,
        operations=[DDLOperation("REVIEW REQUIRED (could not extract columns)")],
    )
    assert _exit_code_for(CheckResult([pred], acknowledge_models=["up"]), 2) == 2


def test_warning_without_known_operations_is_not_waived():
    from dbt_plan.cli import _exit_code_for

    pred = replace(_safe("up"), safety=Safety.WARNING, operations=[])
    assert _exit_code_for(CheckResult([pred], acknowledge_models=["up"]), 2) == 2


@pytest.mark.parametrize(
    "operation",
    [
        "MATERIALIZATION CHANGED: table -> incremental",
        "on_schema_change CHANGED: ignore -> sync_all_columns",
        "RELATION CHANGED (alias): old -> new; existing incremental history is not moved",
        "BUILD FAILURE RISK: removed columns remain in the target under on_schema_change=ignore",
        "REVIEW REQUIRED: added columns are not written to the target under on_schema_change=ignore; downstream readers may fail",
    ],
)
def test_known_config_and_ignore_findings_can_be_acknowledged(operation):
    from dbt_plan.cli import _exit_code_for

    pred = replace(_safe("up"), safety=Safety.WARNING, operations=[DDLOperation(operation)])
    assert _exit_code_for(CheckResult([pred], acknowledge_models=["up"]), 2) == 0


def test_manifest_resource_collisions_are_not_waived():
    from dbt_plan.cli import _ambiguous_acknowledgement_names, _exit_code_for

    manifest = {
        "nodes": {
            "model.p.up": {"name": "up", "path": "up.sql"},
            "model.p.down": {"name": "down", "path": "down.sql"},
            "model.pkg.down": {"name": "down", "path": "down.sql"},
            "test.p.up.hash": {"name": "up"},
        }
    }
    ambiguous = _ambiguous_acknowledgement_names(manifest, manifest)
    assert ambiguous == {"up", "down"}
    pred = replace(
        _safe("up"),
        downstream_impacts=[
            DownstreamImpact("down", "incremental", None, "inherited_drop", "lost")
        ],
    )
    result = CheckResult([pred], acknowledge_models=["up", "down"], ambiguous_resources=ambiguous)
    assert _exit_code_for(result, 2) == 1
    assert "AMBIGUOUS" in format_text(result, color=False)


@pytest.mark.parametrize(
    "kind,risk", [("unit_test", "unit_test_failure"), ("data_test", "data_test_failure")]
)
def test_test_resource_does_not_inherit_parent_acknowledgement(kind, risk):
    from dbt_plan.cli import _exit_code_for

    pred = replace(
        _safe("up"),
        downstream_impacts=[DownstreamImpact("test_orders", kind, None, risk, "lost column")],
    )
    result = CheckResult([pred], acknowledge_models=["up"])
    assert _exit_code_for(result, 2) == 2
    result.acknowledge_models.append("test_orders")
    assert _exit_code_for(result, 2) == 0


def test_predictor_preserves_own_safety_and_unresolved_cascade():
    from dbt_plan.cli import _exit_code_for
    from tests.test_downstream_star import TestInheritedColumnLoss, _node

    run = TestInheritedColumnLoss()._run
    pred = run(_node("fct_orders"), ["order_id", "customer_id"], ["order_id"])
    assert pred.own_safety == Safety.SAFE
    result = CheckResult([pred], acknowledge_models=["fct_orders"])
    assert _exit_code_for(result, 2) == 0
    pred = run(_node("fct_orders"), None, None)
    result.predictions = [pred]
    result.acknowledge_models.append("stg_orders")
    assert _exit_code_for(result, 2) == 2


@pytest.mark.parametrize("up_mat", ["view", "incremental"])
@pytest.mark.parametrize(
    "down_mat,osc",
    [
        ("table", None),
        ("incremental", "fail"),
        ("incremental", "sync_all_columns"),
    ],
)
@pytest.mark.parametrize("names", ["up", "down", "up,down"])
def test_cli_resource_acknowledgements(tmp_path, up_mat, down_mat, osc, names):
    from tests.test_cli import _make_project
    from tests.test_error_contract import invoke

    manifest = {
        "metadata": {"project_name": "p"},
        "nodes": {
            f"model.p.{name}": {
                "name": name,
                "config": {"materialized": mat},
                "unrendered_config": {"on_schema_change": policy},
                "depends_on": {"nodes": ["model.p.up"] if name == "down" else []},
            }
            for name, mat, policy in [("up", up_mat, "sync_all_columns"), ("down", down_mat, osc)]
        },
        "child_map": {"model.p.up": ["model.p.down"]},
    }
    project = _make_project(
        tmp_path,
        manifest=manifest,
        base_manifest=manifest,
        base_sql={"up": "SELECT 1 AS id, 2 AS amount", "down": "SELECT * FROM up"},
        models_sql={"up": "SELECT 1 AS id", "down": "SELECT * FROM up"},
    )
    proc = invoke(project, "check", "--format", "json", "--acknowledge", names)
    expected = 0
    if up_mat == "incremental" and "up" not in names.split(","):
        expected = 1
    elif down_mat == "incremental" and "down" not in names.split(","):
        expected = 1 if osc == "sync_all_columns" else 2
    assert proc.returncode == expected, proc.stderr + proc.stdout
    report = json.loads(proc.stdout)
    model = report["models"][0]
    assert model["own_safety"] == ("safe" if up_mat == "view" else "destructive")
    raw = (
        "destructive"
        if up_mat == "incremental" or osc == "sync_all_columns"
        else ("warning" if osc == "fail" else "safe")
    )
    assert model["safety"] == raw

    # The MCP uses raw findings even when the CLI policy waived every resource.
    server = pytest.importorskip("dbt_plan_mcp.server")
    (project / ".dbt-plan.yml").write_text(f"acknowledge_models: [{names}]\n", encoding="utf-8")
    result = server.plan(str(project))
    assert result["exit_code"] == expected
    assert result["verdict"] == ("review_required" if raw == "warning" else raw)


@pytest.mark.parametrize("risk", ["inherited_drop", "build_failure"])
@pytest.mark.parametrize("names", [["up"], ["up", "down"]])
@pytest.mark.parametrize("fail_on", ["never", "destructive", "warning"])
def test_action_gate_uses_resource_policy(risk, names, fail_on):
    import os
    import shutil
    import subprocess
    import textwrap

    from dbt_plan.cli import _exit_code_for
    from tests.test_action_yml import ACTION_TEXT

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Action gate needs bash")
    pred = replace(
        _safe("up"),
        downstream_impacts=[DownstreamImpact("down", "incremental", None, risk, "known finding")],
    )
    code = _exit_code_for(CheckResult([pred], acknowledge_models=names), 2)
    gate = textwrap.dedent(
        ACTION_TEXT.split("    - name: Gate", 1)[1].split("      run: |\n", 1)[1]
    )
    proc = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", gate],
        env={**os.environ, "CODE": str(code), "FAIL_ON": fail_on, "VERDICT": "test"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    blocked = "down" not in names and (
        fail_on == "warning" or (fail_on == "destructive" and risk == "inherited_drop")
    )
    assert proc.returncode == int(blocked), proc.stdout + proc.stderr


def test_contract_only_cascade_preserves_own_safety(tmp_path):
    from tests.test_cli import _make_project
    from tests.test_error_contract import invoke

    manifest = {
        "metadata": {"project_name": "p"},
        "nodes": {
            "model.p.up": {"name": "up", "config": {"materialized": "view"}},
            "model.p.down": {
                "name": "down",
                "config": {"materialized": "table", "contract": {"enforced": True}},
                "columns": {"id": {}, "amount": {}},
                "depends_on": {"nodes": ["model.p.up"]},
            },
        },
        "child_map": {"model.p.up": ["model.p.down"]},
    }
    project = _make_project(
        tmp_path,
        manifest=manifest,
        base_manifest=manifest,
        base_sql={"up": "SELECT 1 AS id, 2 AS amount", "down": "SELECT * FROM up"},
        models_sql={"up": "SELECT 1 AS id", "down": "SELECT * FROM up"},
    )
    result = invoke(project, "check", "--format", "json", "--acknowledge", "down")
    assert result.returncode == 0, result.stdout + result.stderr
    model = json.loads(result.stdout)["models"][0]
    assert model["own_safety"] == "safe"
    assert model["safety"] == "warning"
    assert model["downstream_impacts"][0]["risk"] == "contract_violation"
