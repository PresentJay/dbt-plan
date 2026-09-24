"""Executable contract for reports emitted by the real check CLI (no warehouse)."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/check-report-v1.schema.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema):
    # Registry has no retrieval callback: non-local references fail, never fetch.
    return Draft202012Validator(schema, registry=Registry())


@pytest.fixture(scope="module")
def cli_report(tmp_path_factory):
    def run(scenario="destructive"):
        tmp_path = tmp_path_factory.mktemp(scenario)
        compiled = tmp_path / "target/compiled/bookstore/models"
        compiled.mkdir(parents=True)
        before = "select 1 as order_id, 2 as revenue"
        after = "select 1 as order_id"
        materialization = "table" if scenario in ("safe", "stale") else "incremental"
        osc = "fail" if scenario == "warning" else "sync_all_columns"
        node = {
            "name": "orders",
            "resource_type": "model",
            "unique_id": "model.bookstore.orders",
            "config": {"materialized": materialization},
            "unrendered_config": {} if scenario == "safe" else {"on_schema_change": osc},
        }
        manifest = {
            "metadata": {"project_name": "bookstore", "adapter_type": "snowflake"},
            "nodes": {"model.bookstore.orders": node},
            "child_map": {},
        }
        if scenario == "cascade":
            manifest["nodes"]["model.bookstore.order_totals"] = {
                "name": "order_totals",
                "resource_type": "model",
                "unique_id": "model.bookstore.order_totals",
                "config": {"materialized": "table"},
                "depends_on": {"nodes": ["model.bookstore.orders"]},
            }
            (compiled / "order_totals.sql").write_text(
                "select revenue from orders", encoding="utf-8"
            )
            manifest["child_map"] = {
                "model.bookstore.orders": [
                    "model.bookstore.order_totals",
                    "exposure.bookstore.sales",
                ]
            }
            manifest["exposures"] = {
                "exposure.bookstore.sales": {
                    "name": "sales",
                    "type": "dashboard",
                    "owner": {"name": "Bookstore team"},
                    "url": "https://example.com/sales",
                }
            }
        manifest_path = tmp_path / "target/manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (compiled / "orders.sql").write_text(before, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DBT_PLAN_")}

        def invoke(*args):
            return subprocess.run(
                [sys.executable, "-m", "dbt_plan.cli", *args, "--project-dir", str(tmp_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
            )

        snapshot = invoke("snapshot")
        assert snapshot.returncode == 0, snapshot.stderr
        if scenario in ("unchanged", "baseline_missing", "baseline_corrupt"):
            after = before
        elif scenario == "refusal":
            after = "select ("
        (compiled / "orders.sql").write_text(after, encoding="utf-8")
        if scenario == "baseline_missing":
            (tmp_path / ".dbt-plan/base/manifest.json").unlink()
        elif scenario == "baseline_corrupt":
            (tmp_path / ".dbt-plan/base/manifest.json").write_text("{", encoding="utf-8")
        elif scenario == "stale":
            manifest["nodes"]["model.bookstore.orders"]["compiled_code"] = before
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        args = ["check", "--format", "json"]
        if scenario == "acknowledged":
            args += ["--acknowledge", "orders"]
        result = invoke(*args)
        assert result.stdout, result.stderr
        return json.loads(result.stdout), result.returncode

    return run


def assert_counts(report):
    summary, models = report["summary"], report["models"]
    assert summary["total"] == len(models)
    for safety in ("safe", "warning", "destructive"):
        assert summary[safety] == sum(m["safety"] == safety for m in models)
    assert summary.get("acknowledged", 0) == sum(m["acknowledged"] for m in models)
    assert summary.get("cascade_risks", 0) == sum(
        len(m.get("downstream_impacts", [])) for m in models
    )


def test_schema_is_valid_and_all_references_are_local(schema):
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef"):
                    assert child.startswith("#/$defs/")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


@pytest.mark.parametrize(
    "scenario,code,safety",
    [
        ("unchanged", 0, None),
        ("safe", 0, "safe"),
        ("warning", 2, "warning"),
        ("destructive", 1, "destructive"),
        ("acknowledged", 0, "destructive"),
        ("refusal", 2, "warning"),
        ("baseline_missing", 2, None),
        ("baseline_corrupt", 2, None),
        ("stale", 2, None),
        ("cascade", 1, "destructive"),
    ],
)
def test_real_cli_reports(validator, cli_report, scenario, code, safety):
    report, actual_code = cli_report(scenario)
    validator.validate(report)
    assert actual_code == code
    assert_counts(report)
    assert "schema_version" not in report
    if safety:
        assert report["models"][0]["safety"] == safety
    if scenario == "unchanged":
        assert report["models"] == []
    if scenario == "refusal":
        assert report["parse_failures"] == ["orders"]
    if scenario.startswith("baseline_"):
        assert report["baseline_problem"] == scenario.removeprefix("baseline_")
    if scenario == "stale":
        assert report["stale_sources"]
    if scenario == "acknowledged":
        assert report["models"][0]["acknowledged"] is True
    if scenario == "cascade":
        model = report["models"][0]
        assert model["downstream"]
        assert model["downstream_impacts"]
        assert model["downstream_exposures"]
    assert report["analysis"]["dialect"] == "snowflake"
    assert report["analysis"]["baseline"]["created_at"]


@pytest.fixture(scope="module")
def cascade_report(cli_report):
    return cli_report("cascade")[0]


@pytest.fixture
def rich_report(cascade_report):
    return copy.deepcopy(cascade_report)


@pytest.mark.parametrize(
    "path",
    [
        ("summary",),
        ("models",),
        ("parse_failures",),
        ("stale_sources",),
        ("skipped_models",),
        ("uncompiled_models",),
        *[("summary", k) for k in ("total", "safe", "warning", "destructive")],
        *[
            ("models", 0, k)
            for k in (
                "model_name",
                "materialization",
                "on_schema_change",
                "safety",
                "operations",
                "columns_added",
                "columns_removed",
                "acknowledged",
            )
        ],
        ("models", 0, "operations", 0, "column"),
        ("models", 0, "operations", 0, "operation"),
        ("models", 0, "downstream_impacts", 0, "risk"),
        ("models", 0, "downstream_exposures", 0, "owner"),
    ],
)
def test_missing_required_fields_fail(validator, rich_report, path):
    validator.validate(rich_report)  # Positive control before each mutation.
    parent = rich_report
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(ValidationError):
        validator.validate(rich_report)


@pytest.mark.parametrize("bad", ["1", True, False, -1, 1.5, None])
@pytest.mark.parametrize(
    "key", ["total", "safe", "warning", "destructive", "acknowledged", "cascade_risks"]
)
def test_counts_are_nonnegative_integers(validator, rich_report, key, bad):
    validator.validate(rich_report)
    rich_report["summary"][key] = bad
    with pytest.raises(ValidationError):
        validator.validate(rich_report)


@pytest.mark.parametrize(
    "path,bad",
    [
        *(
            ((k,), None)
            for k in (
                "models",
                "parse_failures",
                "stale_sources",
                "skipped_models",
                "uncompiled_models",
            )
        ),
        *(
            (("models", 0, k), None)
            for k in (
                "operations",
                "columns_added",
                "columns_removed",
                "downstream",
                "downstream_impacts",
                "downstream_exposures",
            )
        ),
        (("models", 0, "model_name"), 1),
        (("models", 0, "materialization"), None),
        (("models", 0, "on_schema_change"), False),
        (("models", 0, "safety"), 1),
        (("models", 0, "acknowledged"), "true"),
        (("models", 0, "columns_removed", 0), 1),
        (("models", 0, "operations", 0, "column"), []),
        (("models", 0, "downstream_impacts", 0, "risk"), None),
        (("models", 0, "downstream_exposures", 0, "owner"), {}),
        (("baseline_problem",), None),
        (("analysis",), None),
        (("analysis", "baseline", "revision"), 1),
        (("analysis", "selection"), []),
    ],
)
def test_invalid_field_types_fail(validator, rich_report, path, bad):
    validator.validate(rich_report)
    parent = rich_report
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = bad
    with pytest.raises(ValidationError):
        validator.validate(rich_report)


def test_legacy_optional_and_nullable_fields(validator, rich_report):
    rich_report.pop("analysis")
    rich_report["summary"].pop("cascade_risks")
    model = rich_report["models"][0]
    for key in ("downstream", "downstream_impacts", "downstream_exposures"):
        model.pop(key)
    model["on_schema_change"] = None
    model["operations"][0]["column"] = None
    validator.validate(rich_report)
    rich_report["analysis"] = {
        "adapter_type": None,
        "selection": None,
        "baseline": {"revision": None, "created_at": None},
    }
    validator.validate(rich_report)


def test_additive_fields_and_unknown_risks_are_forward_compatible(validator, rich_report):
    def extend(value):
        if isinstance(value, dict):
            for child in list(value.values()):
                extend(child)
            value["future_metadata"] = {"anything": [None, True, 1]}
        elif isinstance(value, list):
            for child in value:
                extend(child)

    extend(rich_report)
    rich_report["waivers"] = [{"reason": "reviewed", "expires": None}]
    rich_report["models"][0]["waiver"] = {"reason": "reviewed"}
    rich_report["models"][0]["safety"] = "future_needs_review"
    rich_report["models"][0]["downstream_impacts"][0]["risk"] = "future_risk"
    validator.validate(rich_report)


def test_schema_validity_does_not_prove_count_consistency(validator, rich_report):
    inconsistent = copy.deepcopy(rich_report)
    inconsistent["summary"]["total"] += 1
    validator.validate(inconsistent)
    with pytest.raises(AssertionError):
        assert_counts(inconsistent)
