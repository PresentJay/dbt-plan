"""A fresh compile must agree with the committed lightweight-test artifacts."""

import copy
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PROJECT = ROOT / "tests/dbt_project"
PINS = {"dbt-core": "1.11.7", "dbt-duckdb": "1.10.1"}


def stable_manifest(manifest):
    """Keep project semantics; omit runtime IDs, paths, adapter macros and timings.

    Keep raw/compiled SQL byte-for-byte (apart from newline normalization by
    read_text), author config, relation identity, schemas, lineage and tests.
    """
    fields = (
        "unique_id",
        "name",
        "resource_type",
        "package_name",
        "path",
        "original_file_path",
        "raw_code",
        "compiled_code",
        "database",
        "schema",
        "alias",
        "relation_name",
        "config",
        "unrendered_config",
        "columns",
        "depends_on",
        "refs",
        "sources",
        "column_name",
        "test_metadata",
        "attached_node",
        "given",
        "expect",
        "model",
        "owner",
        "type",
        "maturity",
        "url",
        "description",
    )
    result = {
        "metadata": {
            key: manifest["metadata"][key]
            for key in ("dbt_version", "adapter_type", "project_name")
        }
    }
    for section in ("nodes", "unit_tests", "exposures", "sources"):
        result[section] = {
            key: {field: node[field] for field in fields if field in node}
            for key, node in manifest.get(section, {}).items()
        }
    for section in ("child_map", "parent_map"):
        result[section] = {key: sorted(value) for key, value in manifest[section].items()}
    return result


def stable_target(target):
    return {
        "manifest": stable_manifest(json.loads((target / "manifest.json").read_text())),
        "compiled": {
            p.relative_to(target / "compiled").as_posix(): p.read_text()
            for p in sorted((target / "compiled").rglob("*.sql"))
        },
    }


def test_normalization_ignores_runtime_metadata_but_detects_semantic_drift():
    original = json.loads((PROJECT / "target/manifest.json").read_text())
    changed = copy.deepcopy(original)
    changed["metadata"].update(invocation_id="different", generated_at="later")
    changed["nodes"]["model.test_project.stg_orders"]["compiled_path"] = "/another/machine"
    assert stable_manifest(changed) == stable_manifest(original)
    for field, value in [
        ("compiled_code", "select 0 as changed"),
        ("unrendered_config", {"on_schema_change": "ignore"}),
        ("columns", {}),
        ("depends_on", {"nodes": []}),
    ]:
        changed = copy.deepcopy(original)
        # fct_orders has dependencies; stg_orders has schema columns.
        name = "stg_orders" if field == "columns" else "fct_orders"
        changed["nodes"][f"model.test_project.{name}"][field] = value
        assert stable_manifest(changed) != stable_manifest(original), field


def test_committed_fixture_matches_fresh_compile(tmp_path):
    for package, expected in PINS.items():
        try:
            actual = version(package)
        except PackageNotFoundError:
            pytest.skip(f"fixture regeneration needs {package}=={expected}")
        assert actual == expected, f"fixture check requires {package}=={expected}, got {actual}"
    project = tmp_path / "fresh project"
    shutil.copytree(PROJECT, project, ignore=shutil.ignore_patterns("target", "logs", ".user.yml"))
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "dbt"),
            "compile",
            "--profiles-dir",
            ".",
            "--no-partial-parse",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = stable_target(PROJECT / "target")
    actual = stable_target(project / "target")
    assert actual == expected
    # Control for a stale compiled file independently of manifest normalization.
    sql = next((project / "target/compiled").rglob("*.sql"))
    sql.write_text(sql.read_text() + "\nselect 0 as unintended_change\n")
    assert stable_target(project / "target") != expected
