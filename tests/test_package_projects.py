"""A dbt package that ships models must not stop dbt-plan from running.

dbt compiles every package into `target/compiled/<package>/models/`, so a project
depending on elementary, dbt_project_evaluator, dbt_artifacts or anything else
with models has more than one directory there. `_find_compiled_dir` treated that
as unresolvable and aborted, which meant dbt-plan could not run on such a project
at all -- and the error told the user to pass `--project-dir`, which points at the
dbt project and therefore changes nothing.

The manifest says which project owns the target directory, and dbt-plan already
trusts that field: `build_node_index` uses `metadata.project_name` to keep package
models out of the index. This makes the file side agree with the manifest side.
"""

from __future__ import annotations

import json

import pytest

from dbt_plan.cli import _find_compiled_dir


def _sql(path, body="SELECT 1 AS a"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _manifest(target, project_name):
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": {}, "child_map": {}, "metadata": {"project_name": project_name}}),
        encoding="utf-8",
    )


class TestPackagesNoLongerBlock:
    def test_the_root_project_is_chosen_by_name(self, tmp_path):
        target = tmp_path / "target"
        _sql(target / "compiled" / "my_project" / "models" / "m.sql")
        _sql(target / "compiled" / "elementary" / "models" / "e.sql")
        _manifest(target, "my_project")

        assert _find_compiled_dir(target) == (target / "compiled" / "my_project", ("models",))

    def test_it_works_regardless_of_alphabetical_order(self, tmp_path):
        """`elementary` sorts before `my_project`; picking the first would be luck."""
        target = tmp_path / "target"
        _sql(target / "compiled" / "aaa_package" / "models" / "a.sql")
        _sql(target / "compiled" / "zzz_project" / "models" / "z.sql")
        _manifest(target, "zzz_project")

        assert _find_compiled_dir(target) == (target / "compiled" / "zzz_project", ("models",))

    def test_several_packages_are_fine(self, tmp_path):
        target = tmp_path / "target"
        for name in ("elementary", "dbt_artifacts", "dbt_project_evaluator", "my_project"):
            _sql(target / "compiled" / name / "models" / "m.sql")
        _manifest(target, "my_project")

        assert _find_compiled_dir(target) == (target / "compiled" / "my_project", ("models",))


class TestItStillRefusesWhenItCannotTell:
    """Guessing which project to check would be worse than stopping."""

    def test_no_manifest_still_raises(self, tmp_path):
        target = tmp_path / "target"
        _sql(target / "compiled" / "proj_a" / "models" / "m.sql")
        _sql(target / "compiled" / "proj_b" / "models" / "m.sql")

        with pytest.raises(ValueError, match="Multiple dbt projects"):
            _find_compiled_dir(target)

    def test_a_manifest_naming_none_of_them_raises(self, tmp_path):
        target = tmp_path / "target"
        _sql(target / "compiled" / "proj_a" / "models" / "m.sql")
        _sql(target / "compiled" / "proj_b" / "models" / "m.sql")
        _manifest(target, "some_other_project")

        with pytest.raises(ValueError, match="Multiple dbt projects"):
            _find_compiled_dir(target)

    def test_an_unreadable_manifest_raises_rather_than_crashing(self, tmp_path):
        target = tmp_path / "target"
        _sql(target / "compiled" / "proj_a" / "models" / "m.sql")
        _sql(target / "compiled" / "proj_b" / "models" / "m.sql")
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError, match="Multiple dbt projects"):
            _find_compiled_dir(target)

    def test_the_error_does_not_suggest_project_dir(self, tmp_path):
        """It used to, and it does not work: the directories are inside that project."""
        target = tmp_path / "target"
        _sql(target / "compiled" / "proj_a" / "models" / "m.sql")
        _sql(target / "compiled" / "proj_b" / "models" / "m.sql")

        with pytest.raises(ValueError) as err:
            _find_compiled_dir(target)

        message = str(err.value)
        assert "proj_a" in message and "proj_b" in message
        assert "--project-dir" not in message
        assert "manifest.json" in message


class TestTheOrdinaryCaseIsUntouched:
    def test_a_single_project_needs_no_manifest(self, tmp_path):
        """The common path must not start requiring a manifest to be parsed."""
        target = tmp_path / "target"
        _sql(target / "compiled" / "my_project" / "models" / "m.sql")

        assert _find_compiled_dir(target) == (target / "compiled" / "my_project", ("models",))

    def test_flat_layout_is_unaffected(self, tmp_path):
        target = tmp_path / "target"
        _sql(target / "compiled" / "models" / "m.sql")

        assert _find_compiled_dir(target) == (target / "compiled", ("models",))


class TestEndToEnd:
    def test_snapshot_and_check_run_with_a_package_present(self, tmp_path, capsys):
        import argparse

        from dbt_plan.cli import _do_check, _do_snapshot

        target = tmp_path / "target"
        models = target / "compiled" / "my_project" / "models"
        _sql(models / "fct_orders.sql", "SELECT order_id, amount FROM raw")
        _sql(target / "compiled" / "elementary" / "models" / "e.sql")
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "nodes": {
                        "model.my_project.fct_orders": {
                            "name": "fct_orders",
                            "config": {
                                "materialized": "incremental",
                                "on_schema_change": "sync_all_columns",
                                "enabled": True,
                            },
                            "columns": {},
                        }
                    },
                    "child_map": {},
                    "metadata": {"project_name": "my_project"},
                }
            ),
            encoding="utf-8",
        )

        _do_snapshot(argparse.Namespace(project_dir=str(tmp_path), target_dir="target"))
        _sql(models / "fct_orders.sql", "SELECT order_id FROM raw")
        capsys.readouterr()

        code = _do_check(
            argparse.Namespace(
                project_dir=str(tmp_path),
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

        out = capsys.readouterr().out
        assert code == 1, out
        assert "DROP COLUMN" in out and "amount" in out
