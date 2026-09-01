"""A partially compiled project must never read as "all safe".

dbt Core aborts a compile on the first failure, so `target/compiled/` was
effectively all-or-nothing. The Fusion engine keeps compiling the rest of the
DAG after a node fails, which makes a *partial* target directory an ordinary
outcome rather than an exceptional one.

That matters because dbt-plan trusts the compiled directory as a complete
picture. If a model is missing from it, dbt-plan simply never examines that
model -- and when nothing else changed, it printed "0 model(s) changed" and
exited 0. A green check on a pull request whose compile silently dropped half
the DAG is a false all-clear, which is the one verdict this tool must not emit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot


def _manifest(models: dict[str, dict], project_name: str = "my_project") -> dict:
    nodes, child_map = {}, {}
    for name, o in models.items():
        node_id = f"model.{o.get('package', project_name)}.{name}"
        nodes[node_id] = {
            "name": name,
            "config": {
                "materialized": o.get("materialized", "table"),
                "on_schema_change": o.get("on_schema_change"),
                "enabled": o.get("enabled", True),
            },
            "columns": o.get("columns", {}),
        }
        child_map[node_id] = []
    return {"nodes": nodes, "child_map": child_map, "metadata": {"project_name": project_name}}


def _write_target(project_dir: Path, sql: dict[str, str], manifest: dict) -> Path:
    models_dir = project_dir / "target" / "compiled" / "my_project" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, body in sql.items():
        (models_dir / f"{name}.sql").write_text(body)
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))
    return models_dir


def _check_args(project_dir: Path, fmt: str = "text") -> argparse.Namespace:
    return argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        select=None,
        verbose=False,
        dialect=None,
    )


def _snapshot(project_dir: Path) -> None:
    _do_snapshot(argparse.Namespace(project_dir=str(project_dir), target_dir="target"))


@pytest.fixture
def project(tmp_path):
    return tmp_path


class TestFalseSafeOnPartialCompile:
    """The reason this feature exists."""

    def test_nothing_changed_but_a_model_never_compiled_does_not_exit_zero(self, project, capsys):
        """The exact Fusion shape: same partial compile on both sides.

        `fct_orders` fails to compile on the base revision and on the head, so it
        appears in neither compiled directory and produces no diff entry. Before
        this check, dbt-plan reported "0 model(s) changed" and exited 0 while
        never having looked at the model at all.
        """
        manifest = _manifest({"dim_books": {}, "fct_orders": {"materialized": "incremental"}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        code = _do_check(_check_args(project))

        assert code != 0, "a compile that never produced fct_orders cannot be reported as safe"
        assert "fct_orders" in capsys.readouterr().out

    def test_a_complete_compile_still_exits_zero(self, project):
        """No new false warnings: every manifest model has compiled SQL."""
        manifest = _manifest({"dim_books": {}, "fct_orders": {}})
        _write_target(
            project, {"dim_books": "SELECT id FROM raw", "fct_orders": "SELECT a FROM b"}, manifest
        )
        _snapshot(project)

        assert _do_check(_check_args(project)) == 0

    def test_the_warning_survives_alongside_a_real_finding(self, project, capsys):
        manifest = _manifest(
            {
                "dim_books": {},
                "fct_orders": {
                    "materialized": "incremental",
                    "on_schema_change": "sync_all_columns",
                },
                "never_compiled": {},
            }
        )
        _write_target(
            project,
            {"dim_books": "SELECT id FROM r", "fct_orders": "SELECT a, b FROM r"},
            manifest,
        )
        _snapshot(project)
        _write_target(
            project, {"dim_books": "SELECT id FROM r", "fct_orders": "SELECT a FROM r"}, manifest
        )

        code = _do_check(_check_args(project))
        out = capsys.readouterr().out

        assert code == 1, "a destructive change still outranks an incomplete compile"
        assert "DROP COLUMN" in out
        assert "never_compiled" in out


class TestNoFalsePositives:
    def test_a_disabled_model_is_not_expected_to_compile(self, project):
        manifest = _manifest({"dim_books": {}, "turned_off": {"enabled": False}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        assert _do_check(_check_args(project)) == 0

    def test_a_package_model_is_not_expected_to_compile(self, project):
        """Package models are excluded from the index, so they cannot be missing from it."""
        manifest = _manifest({"dim_books": {}, "dbt_utils_thing": {"package": "dbt_utils"}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        assert _do_check(_check_args(project)) == 0

    def test_fusion_sidecar_files_are_not_mistaken_for_models(self, project):
        """Fusion writes <model>.macro_spans.json beside each compiled .sql."""
        manifest = _manifest({"dim_books": {}})
        models_dir = _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        (models_dir / "dim_books.macro_spans.json").write_text("{}")
        _snapshot(project)

        assert _do_check(_check_args(project)) == 0


class TestReporting:
    def test_json_names_the_uncompiled_models(self, project, capsys):
        manifest = _manifest({"dim_books": {}, "ghost": {}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        capsys.readouterr()  # discard the snapshot's own output
        _do_check(_check_args(project, fmt="json"))
        payload = json.loads(capsys.readouterr().out)

        assert payload["uncompiled_models"] == ["ghost"]

    def test_github_output_names_the_uncompiled_models(self, project, capsys):
        manifest = _manifest({"dim_books": {}, "ghost": {}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        _do_check(_check_args(project, fmt="github"))

        assert "ghost" in capsys.readouterr().out

    def test_the_message_blames_the_compile_not_a_deletion(self, project, capsys):
        """ "MODEL REMOVED" would send someone hunting for a deletion that never happened."""
        manifest = _manifest({"dim_books": {}, "ghost": {}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)

        _do_check(_check_args(project))
        out = capsys.readouterr().out.lower()

        assert "compile" in out
        assert "removed" not in out


class TestSkippedModelsWereAlsoSilent:
    """The same failure mode, found while fixing the first one.

    `skipped_models` -- a model present in the compiled diff but absent from the
    manifest -- was computed, then dropped twice over: the text and markdown
    formatters returned "no model changes detected" before reaching the warning
    block, and the exit code never consulted it. Through v0.6.0 a model that
    dropped a column reported clean and exited 0 whenever the manifest did not
    contain it, which a stale manifest or a wrong --manifest path is enough to
    cause.
    """

    def _project_with_a_model_missing_from_the_manifest(self, project_dir: Path) -> None:
        empty_manifest = _manifest({})
        _write_target(project_dir, {"fct_orders": "SELECT a, b FROM r"}, empty_manifest)
        _snapshot(project_dir)
        _write_target(project_dir, {"fct_orders": "SELECT a FROM r"}, empty_manifest)

    def test_a_dropped_column_does_not_exit_zero_just_because_the_manifest_lacks_it(self, project):
        self._project_with_a_model_missing_from_the_manifest(project)

        assert _do_check(_check_args(project)) != 0

    def test_the_skipped_model_is_named_in_the_output(self, project, capsys):
        self._project_with_a_model_missing_from_the_manifest(project)

        _do_check(_check_args(project))

        out = capsys.readouterr().out
        assert "fct_orders" in out
        assert "no model changes detected" not in out

    def test_markdown_output_keeps_the_warning_too(self, project, capsys):
        self._project_with_a_model_missing_from_the_manifest(project)

        _do_check(_check_args(project, fmt="github"))

        assert "fct_orders" in capsys.readouterr().out

    def test_a_genuinely_empty_result_still_says_so(self, project, capsys):
        """The early return has to survive: no findings must not become noise."""
        manifest = _manifest({"dim_books": {}})
        _write_target(project, {"dim_books": "SELECT id FROM raw"}, manifest)
        _snapshot(project)
        capsys.readouterr()

        code = _do_check(_check_args(project))

        assert code == 0
        assert "no model changes detected" in capsys.readouterr().out
