"""Regression coverage for verbose in .dbt-plan.yml."""

import json
import os

import pytest

from dbt_plan.cli import _do_check
from dbt_plan.config import Config
from tests.test_verbose_debugging import _destructive_scenario, _make_args


@pytest.fixture(autouse=True)
def clear_dbt_plan_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("DBT_PLAN_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", '"true"', "yes # debug"])
def test_file_truthy_values_enable_verbose(tmp_path, value):
    (tmp_path / ".dbt-plan.yml").write_text(f"verbose: {value}\n")

    assert Config.load(tmp_path).verbose is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "'false'", "no # quiet"])
def test_file_false_values_disable_verbose_after_true(tmp_path, value):
    (tmp_path / ".dbt-plan.yml").write_text(f"verbose: true\nverbose: {value}\n")

    assert Config.load(tmp_path).verbose is False


def test_invalid_file_value_warns_and_keeps_prior_value(tmp_path, capsys):
    (tmp_path / ".dbt-plan.yml").write_text("verbose: true\nverbose: banana\n")

    assert Config.load(tmp_path).verbose is True
    assert (
        f"{tmp_path / '.dbt-plan.yml'}:2: warning: cannot understand verbose"
        in capsys.readouterr().err
    )


def test_truthy_environment_value_enables_over_file_false(tmp_path, monkeypatch):
    (tmp_path / ".dbt-plan.yml").write_text("verbose: false\n")
    monkeypatch.setenv("DBT_PLAN_VERBOSE", "yes")

    assert Config.load(tmp_path).verbose is True


def test_false_environment_value_does_not_disable_file_true(tmp_path, monkeypatch):
    (tmp_path / ".dbt-plan.yml").write_text("verbose: true\n")
    monkeypatch.setenv("DBT_PLAN_VERBOSE", "false")

    assert Config.load(tmp_path).verbose is True


def test_file_verbose_keeps_json_stdout_parseable(tmp_path, capsys):
    project_dir = _destructive_scenario(tmp_path)
    (project_dir / ".dbt-plan.yml").write_text("verbose: true\n")
    args = _make_args(project_dir, fmt="json")

    _do_check(args)

    captured = capsys.readouterr()
    assert "summary" in json.loads(captured.out)
    assert "[verbose]" in captured.err
