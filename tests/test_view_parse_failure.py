"""A view dbt-plan could not read must not exit 0.

`CREATE OR REPLACE VIEW` really is safe for the view itself, whatever its
columns are. dbt-plan used that to skip recording a parse failure for `table`
and `view` -- and those are what most dbt projects are made of.

The reasoning holds for the model and breaks for everything reading it. When
the columns cannot be extracted, cascade cannot see which one was dropped, so
`BROKEN_REF` never fires. The result was `SAFE`, exit 0, on a change that
breaks a downstream model. See #131.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot

# A Jinja tag that survived the compile. Nothing parses this, in any dialect,
# which is what makes it a stand-in for a dialect mismatch or a sqlglot gap.
UNPARSEABLE = "SELECT id, customer_id, {% if x %}a{% endif %} FROM raw.orders"
UNPARSEABLE_AFTER = "SELECT id, {% if x %}a{% endif %} FROM raw.orders"


def _manifest(models: dict[str, dict], project_name: str = "my_project") -> dict:
    nodes, child_map = {}, {}
    for name, o in models.items():
        node_id = f"model.{project_name}.{name}"
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": o.get("materialized", "view"),
                "on_schema_change": o.get("on_schema_change"),
                "enabled": True,
            },
            "columns": {},
        }
        child_map[node_id] = [f"model.{project_name}.{c}" for c in o.get("children", ())]
    return {"nodes": nodes, "child_map": child_map, "metadata": {"project_name": project_name}}


def _write_target(project_dir: Path, sql: dict[str, str], manifest: dict) -> None:
    models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, body in sql.items():
        (models_dir / f"{name}.sql").write_text(body)
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))


def _check_args(project_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format="text",
        no_color=True,
        select=None,
        verbose=False,
        dialect="duckdb",
    )


def _snapshot(project_dir: Path) -> None:
    _do_snapshot(argparse.Namespace(project_dir=str(project_dir), target_dir="target"))


@pytest.fixture
def project(tmp_path):
    return tmp_path


def _unreadable_change(project: Path, materialized: str) -> None:
    """stg loses customer_id, which fct reads, and stg's SQL does not parse."""
    manifest = _manifest(
        {
            "stg": {"materialized": materialized, "children": ["fct"]},
            "fct": {"materialized": materialized},
        }
    )
    _write_target(
        project,
        {"stg": UNPARSEABLE, "fct": 'SELECT customer_id FROM "db"."sch"."stg"'},
        manifest,
    )
    _snapshot(project)
    _write_target(
        project,
        {"stg": UNPARSEABLE_AFTER, "fct": 'SELECT customer_id FROM "db"."sch"."stg"'},
        manifest,
    )


class TestUnreadableColumnsAreNotSafe:
    @pytest.mark.parametrize("materialized", ["view", "table"])
    def test_a_model_whose_columns_could_not_be_read_does_not_exit_zero(
        self, project, capsys, materialized
    ):
        _unreadable_change(project, materialized)

        code = _do_check(_check_args(project))
        out = capsys.readouterr().out

        assert code != 0, f"a {materialized} dbt-plan could not read cannot be reported as safe"
        assert "stg" in out

    def test_the_report_names_the_model_it_could_not_read(self, project, capsys):
        _unreadable_change(project, "view")

        _do_check(_check_args(project))

        assert "Could not extract columns" in capsys.readouterr().out

    def test_the_ddl_verdict_on_the_model_itself_stays_safe(self, project, capsys):
        """CREATE OR REPLACE VIEW is still what dbt will run.

        The refusal is reported through `parse_failures`, beside the verdict
        rather than folded into it, so "this DDL is safe and I cannot tell you
        what it does to anything downstream" stays sayable.
        """
        _unreadable_change(project, "view")

        _do_check(_check_args(project))

        assert "SAFE  stg" in capsys.readouterr().out


class TestNoNewFalseWarnings:
    def test_a_view_whose_sql_parses_still_exits_zero(self, project):
        manifest = _manifest({"stg": {"children": ["fct"]}, "fct": {}})
        _write_target(
            project,
            {"stg": "SELECT id, customer_id FROM raw.orders", "fct": "SELECT id FROM stg"},
            manifest,
        )
        _snapshot(project)
        _write_target(
            project,
            {"stg": "SELECT id, customer_id, extra FROM raw.orders", "fct": "SELECT id FROM stg"},
            manifest,
        )

        assert _do_check(_check_args(project)) == 0
