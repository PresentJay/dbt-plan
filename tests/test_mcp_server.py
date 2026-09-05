"""The MCP server must not turn a refusal into a clean bill of health.

A person reading "safe" may still glance at the diff. An agent reading it runs the
change. So everything dbt-plan declined to judge has to arrive as itself rather than
being folded into the verdict -- these tests exist to stop that folding.

The server is a separate package from `dbt_plan` on purpose: it is async and speaks a
protocol, and tests/test_invariants.py forbids both in the analysis core.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

mcp_server = pytest.importorskip(
    "dbt_plan_mcp.server", reason="needs the optional mcp extra: uv sync --extra mcp"
)


def _manifest(models: dict[str, dict]) -> dict:
    return {
        "nodes": {
            f"model.p.{n}": {
                "name": n,
                "config": {
                    "materialized": o.get("materialized", "incremental"),
                    "on_schema_change": o.get("on_schema_change", "sync_all_columns"),
                    "enabled": True,
                },
                "columns": {},
            }
            for n, o in models.items()
        },
        "child_map": {},
        "metadata": {"project_name": "p"},
    }


def _write(root: Path, sql: dict[str, str], manifest: dict) -> None:
    d = root / "target" / "compiled" / "p" / "models"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in sql.items():
        (d / f"{name}.sql").write_text(body, encoding="utf-8")
    (root / "target" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestVerdicts:
    def test_a_dropped_column_is_destructive(self, tmp_path):
        m = _manifest({"fct_orders": {}})
        _write(tmp_path, {"fct_orders": "SELECT a, b FROM raw"}, m)
        mcp_server.snapshot(str(tmp_path))
        _write(tmp_path, {"fct_orders": "SELECT a FROM raw"}, m)

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        assert out["verdict"] == "destructive"
        assert out["exit_code"] == 1
        assert out["models"][0]["columns_removed"] == ["b"]

    def test_an_unchanged_project_is_safe_with_no_refusals(self, tmp_path):
        m = _manifest({"fct_orders": {}})
        _write(tmp_path, {"fct_orders": "SELECT a, b FROM raw"}, m)
        mcp_server.snapshot(str(tmp_path))

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        assert out["verdict"] == "safe"
        assert out["refusals"] == [], "safe must mean nothing was left unjudged"


class TestRefusalsSurvive:
    """Each of these would be reported as `safe` by a wrapper that collapsed them."""

    def test_an_incomplete_compile_is_reported_not_hidden(self, tmp_path):
        m = _manifest({"fct_orders": {}, "never_compiled": {}})
        _write(tmp_path, {"fct_orders": "SELECT a, b FROM raw"}, m)
        mcp_server.snapshot(str(tmp_path))
        _write(tmp_path, {"fct_orders": "SELECT a, b FROM raw WHERE x"}, m)

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        reasons = {r["reason"] for r in out["refusals"]}
        assert "never_compiled" in reasons
        assert out["verdict"] != "safe"

    def test_a_model_missing_from_the_manifest_is_reported(self, tmp_path):
        empty = _manifest({})
        _write(tmp_path, {"fct_orders": "SELECT a, b FROM raw"}, empty)
        mcp_server.snapshot(str(tmp_path))
        _write(tmp_path, {"fct_orders": "SELECT a FROM raw"}, empty)

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        reasons = {r["reason"] for r in out["refusals"]}
        assert "missing_from_manifest" in reasons
        assert out["verdict"] != "safe"

    def test_an_undecidable_model_names_itself(self, tmp_path):
        """`SELECT *` over a physical table with no manifest columns to fall back on."""
        m = _manifest({"fct_orders": {}})
        _write(tmp_path, {"fct_orders": "SELECT * FROM raw_orders"}, m)
        mcp_server.snapshot(str(tmp_path))
        _write(tmp_path, {"fct_orders": "SELECT * FROM raw_orders_v2"}, m)

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        assert out["verdict"] != "safe"
        assert any(r["reason"] == "not_decidable" for r in out["refusals"])


class TestItFailsLegibly:
    def test_no_baseline_returns_an_error_with_a_next_step(self, tmp_path):
        _write(tmp_path, {"fct_orders": "SELECT a FROM raw"}, _manifest({"fct_orders": {}}))

        out = mcp_server.plan(str(tmp_path), dialect="duckdb")

        assert out["verdict"] == "error"
        assert "snapshot" in out["hint"]

    def test_snapshot_without_compiled_sql_says_so(self, tmp_path):
        out = mcp_server.snapshot(str(tmp_path))

        assert out["ok"] is False
        assert "compile" in out["hint"]


class TestTheCoreStaysClean:
    def test_the_server_does_not_import_the_analysis_core_into_its_process(self):
        """It shells out instead, so stdio transport cannot be corrupted by a stray print."""
        source = (Path(mcp_server.__file__)).read_text(encoding="utf-8")
        assert "subprocess.run" in source
        assert "from dbt_plan.cli import" not in source
