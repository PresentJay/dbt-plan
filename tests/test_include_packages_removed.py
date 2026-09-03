"""`include_packages` is recognised, ignored, and says so.

The setting claimed to "also check models from dbt packages". It never did. It
widened `build_node_index` to keep package models and nothing else, while the
compiled scan covers only the root project's directory — so those models were
added to the index and then never looked at.

Worse than a no-op: the uncompiled-model check added in 0.7.0 cross-references
the manifest against the compiled directory, saw those entries with no compiled
SQL, and reported "the compile is incomplete -- fix the compile and rerun" about
a compile that was perfectly fine.

Making it work would mean scanning several project directories, which runs into
`diff_compiled_dirs` refusing on a duplicate file stem — dbt requires model names
to be unique per package, not globally, so a collision is legal. That is a real
design question, and it is not worth opening for a feature whose only observable
effect was a wrong warning. A package model that drops a column your model reads
is already caught as a broken ref by cascade analysis, which is the finding that
actually matters.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot
from dbt_plan.config import Config
from dbt_plan.manifest import build_node_index

MANIFEST = {
    "nodes": {
        "model.my_project.fct_orders": {
            "name": "fct_orders",
            "config": {
                "materialized": "incremental",
                "on_schema_change": "sync_all_columns",
                "enabled": True,
            },
            "columns": {},
        },
        "model.elementary.elementary_model": {
            "name": "elementary_model",
            "config": {
                "materialized": "incremental",
                "on_schema_change": "sync_all_columns",
                "enabled": True,
            },
            "columns": {},
        },
    },
    "child_map": {},
    "metadata": {"project_name": "my_project"},
}


def _write(root: Path, mine: str, theirs: str) -> None:
    for pkg, body in (("my_project", mine), ("elementary", theirs)):
        d = root / "target" / "compiled" / pkg / "models"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("fct_orders.sql" if pkg == "my_project" else "elementary_model.sql")).write_text(
            body, encoding="utf-8"
        )
    (root / "target" / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")


def _check(root: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _do_check(
            argparse.Namespace(
                project_dir=str(root),
                target_dir="target",
                base_dir=".dbt-plan/base",
                manifest=None,
                format="text",
                no_color=True,
                select=None,
                verbose=False,
                dialect="duckdb",
            )
        )
    return code, buf.getvalue()


@pytest.fixture
def project(tmp_path):
    _write(tmp_path, "SELECT a, b FROM raw", "SELECT x, y FROM raw")
    _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
    _write(tmp_path, "SELECT a FROM raw", "SELECT x FROM raw")
    return tmp_path


class TestTheFalseWarningIsGone:
    def test_setting_it_no_longer_claims_the_compile_is_incomplete(self, project):
        (project / ".dbt-plan.yml").write_text("include_packages: true\n", encoding="utf-8")

        _, out = _check(project)

        assert "compile is incomplete" not in out
        assert "Fix the compile" not in out

    def test_the_root_project_finding_is_unaffected(self, project):
        (project / ".dbt-plan.yml").write_text("include_packages: true\n", encoding="utf-8")

        code, out = _check(project)

        assert code == 1
        assert "DROP COLUMN" in out and "fct_orders" in out

    def test_the_same_holds_without_the_setting(self, project):
        code, out = _check(project)

        assert code == 1
        assert "compile is incomplete" not in out


class TestItSaysItIsIgnored:
    def test_a_deprecation_warning_names_the_key(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text("include_packages: true\n", encoding="utf-8")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            Config.load(tmp_path)

        message = err.getvalue()
        assert "include_packages" in message
        assert "ignored" in message.lower()

    def test_the_warning_carries_a_line_number(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text(
            "dialect: duckdb\ninclude_packages: true\n", encoding="utf-8"
        )

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            Config.load(tmp_path)

        assert ":2:" in err.getvalue()

    def test_nothing_is_said_when_it_is_absent(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text("dialect: duckdb\n", encoding="utf-8")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            config = Config.load(tmp_path)

        assert err.getvalue() == ""
        assert config.dialect == "duckdb"


class TestPackageModelsStayOutOfTheIndex:
    def test_they_are_excluded(self):
        assert sorted(build_node_index(MANIFEST)) == ["fct_orders"]

    def test_no_config_key_can_ask_for_them(self, tmp_path):
        """The parameter on build_node_index survives; the way to reach it does not.

        `build_node_index(include_packages=True)` is the half that worked and is
        still tested in tests/test_manifest.py. What is gone is the config key and
        the environment variable that promised the CLI would use it.
        """
        (tmp_path / ".dbt-plan.yml").write_text("include_packages: true\n", encoding="utf-8")
        assert not hasattr(Config.load(tmp_path), "include_packages")

    def test_the_environment_variable_is_gone_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_PLAN_INCLUDE_PACKAGES", "true")
        assert not hasattr(Config.load(tmp_path), "include_packages")
