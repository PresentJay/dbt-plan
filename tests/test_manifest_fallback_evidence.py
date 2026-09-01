"""Falling back to the manifest's documented columns is not evidence of safety.

When a model's SQL resolves to `["*"]`, dbt-plan substitutes the column list
documented in `manifest.json`. It does that on *both* sides of the diff. But
`schema.yml` conventionally documents only the columns you test -- jaffle_shop's
own `stg_orders` lists 2 of its 4 -- and you edit SQL far more often than docs.
So both sides receive the same incomplete list, the difference is zero, and the
verdict is `SAFE` for a model whose SQL may have dropped a column.

The comparison did not happen. Absence of evidence is not evidence of safety, so
a verdict that rests on the fallback cannot be `SAFE`.

Scoped to materializations where columns matter. `table` and `view` are rebuilt
with CREATE OR REPLACE whatever the columns are, so a fallback there costs
nothing and escalating it would be pure noise.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot


def _manifest(models: dict[str, dict], project: str = "my_project") -> dict:
    nodes, child_map = {}, {}
    for name, o in models.items():
        node_id = f"model.{project}.{name}"
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": o.get("materialized", "incremental"),
                "on_schema_change": o.get("on_schema_change", "sync_all_columns"),
                "enabled": True,
            },
            "columns": {c: {} for c in o.get("columns", [])},
        }
        child_map[node_id] = []
    return {"nodes": nodes, "child_map": child_map, "metadata": {"project_name": project}}


def _write(project_dir: Path, sql: dict[str, str], manifest: dict) -> None:
    d = project_dir / "target" / "compiled" / "my_project" / "models"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in sql.items():
        (d / f"{name}.sql").write_text(body)
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))


def _args(project_dir: Path, fmt: str = "json") -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        select=None,
        verbose=False,
        dialect="duckdb",
    )


def _run(project_dir: Path, before: str, after: str, **model) -> tuple[int, dict]:
    """Snapshot `before`, then check `after`, returning (exit code, json payload)."""
    import contextlib
    import io

    manifest = _manifest({"m": model})
    _write(project_dir, {"m": before}, manifest)
    _do_snapshot(argparse.Namespace(project_dir=str(project_dir), target_dir="target"))
    _write(project_dir, {"m": after}, manifest)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _do_check(_args(project_dir))
    return code, json.loads(buf.getvalue())


@pytest.fixture
def project(tmp_path):
    return tmp_path


class TestFallbackCannotProduceSafe:
    def test_a_dropped_column_hidden_by_an_incomplete_schema_yml_is_not_safe(self, project):
        """The scenario measured on jaffle_shop: docs list 2 of 4 columns.

        Both revisions are `SELECT *` over a physical table, so both fall back to
        the same documented pair, the diff is empty, and 0.7.0 called it SAFE.
        """
        code, payload = _run(
            project,
            before="SELECT * FROM raw_orders",
            after="SELECT * FROM raw_orders_v2",
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] != "safe"
        assert code != 0

    def test_the_reason_says_the_columns_came_from_the_manifest(self, project, capsys):
        _run(
            project,
            before="SELECT * FROM raw_orders",
            after="SELECT * FROM raw_orders_v2",
            columns=["order_id", "status"],
        )
        manifest = _manifest({"m": {"columns": ["order_id", "status"]}})
        _write(project, {"m": "SELECT * FROM raw_orders_v3"}, manifest)
        capsys.readouterr()

        _do_check(_args(project, fmt="text"))

        assert "manifest" in capsys.readouterr().out.lower()

    def test_a_real_destructive_finding_still_outranks_it(self, project):
        """The fallback must not downgrade a finding, only block a clean bill."""
        code, payload = _run(
            project,
            before="SELECT order_id, status, amount FROM t",
            after="SELECT order_id, status FROM t",
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] == "destructive"
        assert code == 1


class TestNoNewNoise:
    def test_a_table_is_unaffected(self, project):
        """CREATE OR REPLACE rebuilds it whatever the columns are."""
        code, payload = _run(
            project,
            before="SELECT * FROM raw_orders",
            after="SELECT * FROM raw_orders_v2",
            materialized="table",
            on_schema_change=None,
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] == "safe"
        assert code == 0

    def test_a_view_is_unaffected(self, project):
        code, payload = _run(
            project,
            before="SELECT * FROM raw_orders",
            after="SELECT * FROM raw_orders_v2",
            materialized="view",
            on_schema_change=None,
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] == "safe"
        assert code == 0

    def test_columns_read_from_the_sql_are_still_trusted(self, project):
        """No fallback involved, so an unchanged column list really is safe."""
        code, payload = _run(
            project,
            before="SELECT order_id, status FROM t WHERE x = 1",
            after="SELECT order_id, status FROM t WHERE x = 2",
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] == "safe"
        assert code == 0

    def test_a_resolved_cte_star_is_trusted(self, project):
        """After #20 this is the common case, and it must not trip the guard."""
        code, payload = _run(
            project,
            before="WITH r AS (SELECT a, b FROM t) SELECT * FROM r",
            after="WITH r AS (SELECT a, b FROM t WHERE z) SELECT * FROM r",
            columns=["order_id", "status"],
        )

        assert payload["models"][0]["safety"] == "safe"
        assert code == 0
