"""`model-paths` is configurable, and dbt-plan used to assume it was `models`.

    # dbt_project.yml
    model-paths: ["transformations"]

    $ dbt compile
    $ dbt-plan snapshot
    Error: No compiled SQL found. Run 'dbt compile' first to generate compiled SQL
    in the target/ directory.

The compile was right there. The message named the one thing the user had already
done, which sends them to check dbt rather than dbt-plan.

A project listing several paths was worse: `models/` was scanned and the rest were
skipped without a word, so a change in one of them was never diffed at all.

No `dbt_project.yml` parsing is needed for any of this -- every model node records
where it was declared, in `original_file_path`.
"""

from __future__ import annotations

import json

import pytest

from dbt_plan.cli import _find_compiled_dir, _manifest_layout
from dbt_plan.diff import iter_model_sql, iter_non_model_sql


def _project(tmp_path, model_paths=("models",), *, project="p", extra_nodes=None):
    target = tmp_path / "target"
    nodes = {
        f"model.{project}.m_{i}": {
            "name": f"m_{i}",
            "path": f"m_{i}.sql",
            "original_file_path": f"{path}/m_{i}.sql",
            "config": {},
        }
        for i, path in enumerate(model_paths)
    }
    nodes.update(extra_nodes or {})
    (target).mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"metadata": {"project_name": project}, "nodes": nodes}), encoding="utf-8"
    )
    for path in model_paths:
        d = target / "compiled" / project / path
        d.mkdir(parents=True, exist_ok=True)
        # Distinct stems: dbt model names are unique project-wide, and
        # diff_compiled_dirs refuses a duplicate rather than picking one.
        (d / f"m_in_{path}.sql").write_text("SELECT 1", encoding="utf-8")
    return target


class TestManifestLayout:
    def test_it_reads_the_declared_directory(self, tmp_path):
        target = _project(tmp_path, ("transformations",))
        assert _manifest_layout(target) == ("p", ("transformations",))

    def test_several_paths_are_all_returned(self, tmp_path):
        target = _project(tmp_path, ("transformations", "extras"))
        assert _manifest_layout(target)[1] == ("transformations", "extras")

    def test_a_package_model_does_not_add_its_directory(self, tmp_path):
        """A dependency compiles into its own tree and is not ours to scan."""
        target = _project(
            tmp_path,
            ("models",),
            extra_nodes={
                "model.some_package.their_model": {
                    "name": "their_model",
                    "original_file_path": "vendor_models/their_model.sql",
                    "config": {},
                }
            },
        )
        assert _manifest_layout(target)[1] == ("models",)

    def test_an_unreadable_manifest_says_so_rather_than_guessing(self, tmp_path):
        (tmp_path / "manifest.json").write_text("{ not json", encoding="utf-8")
        assert _manifest_layout(tmp_path) == (None, ())


class TestFindCompiledDir:
    def test_a_renamed_model_path_is_found(self, tmp_path):
        target = _project(tmp_path, ("transformations",))
        assert _find_compiled_dir(target) == (
            target / "compiled" / "p",
            ("transformations",),
        )

    def test_several_model_paths_are_all_reported(self, tmp_path):
        target = _project(tmp_path, ("transformations", "extras"))
        found = _find_compiled_dir(target)
        assert found.model_dirs == ("transformations", "extras")

    def test_the_flat_layout_still_works_when_renamed(self, tmp_path):
        target = _project(tmp_path, ("transformations",))
        # Move the project subdirectory's contents up a level.
        (target / "compiled" / "p" / "transformations").rename(
            target / "compiled" / "transformations"
        )
        (target / "compiled" / "p").rmdir()
        assert _find_compiled_dir(target) == (target / "compiled", ("transformations",))

    def test_a_manifest_that_names_no_models_falls_back_to_dbts_default(self, tmp_path):
        """Better than scanning nothing, and it is what every previous version did."""
        target = tmp_path / "target"
        (target / "compiled" / "p" / "models").mkdir(parents=True)
        (target / "manifest.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
        assert _find_compiled_dir(target) == (target / "compiled" / "p", ("models",))


class TestIterModelSql:
    def _tree(self, root, *relative):
        for rel in relative:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SELECT 1", encoding="utf-8")
        return root

    def test_only_the_declared_directories_count_as_models(self, tmp_path):
        root = self._tree(
            tmp_path,
            "transformations/stg_orders.sql",
            "tests/singular.sql",
            "transformations/schema.yml/models/not_null_x.sql",
        )
        assert [p.name for p in iter_model_sql(root, ("transformations",))] == ["stg_orders.sql"]

    def test_the_complement_is_everything_else(self, tmp_path):
        root = self._tree(
            tmp_path,
            "transformations/stg_orders.sql",
            "tests/singular.sql",
            "transformations/schema.yml/models/not_null_x.sql",
        )
        assert sorted(p.name for p in iter_non_model_sql(root, ("transformations",))) == [
            "not_null_x.sql",
            "singular.sql",
        ]

    def test_no_restriction_means_the_pre_0_14_snapshot_layout(self, tmp_path):
        """Those were copied from inside the model directory, so the prefix is gone."""
        root = self._tree(tmp_path, "staging/stg_orders.sql", "marts/fct_orders.sql")
        assert sorted(p.name for p in iter_model_sql(root)) == [
            "fct_orders.sql",
            "stg_orders.sql",
        ]

    def test_a_model_directory_is_not_confused_with_a_sibling_of_the_same_name(self, tmp_path):
        """`tests/` under the model path is still a model path; only the root segment counts."""
        root = self._tree(tmp_path, "models/tests/helper.sql", "tests/singular.sql")
        assert [p.name for p in iter_model_sql(root, ("models",))] == ["helper.sql"]


class TestEndToEnd:
    """The reported failure, through the CLI."""

    @pytest.fixture
    def renamed(self, tmp_path):
        from dbt_plan.cli import _do_snapshot

        project_dir = tmp_path / "project"
        target = _project(project_dir, ("transformations", "extras"))
        import argparse

        _do_snapshot(
            argparse.Namespace(project_dir=str(project_dir), target_dir="target", base_dir=None)
        )
        return project_dir, target

    def test_snapshot_no_longer_refuses(self, renamed):
        project_dir, _ = renamed
        base = project_dir / ".dbt-plan" / "base" / "compiled"
        assert (base / "transformations" / "m_in_transformations.sql").exists()
        assert (base / "extras" / "m_in_extras.sql").exists()

    def test_a_change_in_the_second_path_is_diffed(self, renamed, capsys):
        """It used to be skipped in silence, which is a missed finding, not an error."""
        from dbt_plan.diff import diff_compiled_dirs

        project_dir, target = renamed
        (target / "compiled" / "p" / "extras" / "m_in_extras.sql").write_text(
            "SELECT 2", encoding="utf-8"
        )
        diffs = diff_compiled_dirs(
            project_dir / ".dbt-plan" / "base" / "compiled",
            target / "compiled" / "p",
            ("transformations", "extras"),
            ("transformations", "extras"),
        )
        assert [(d.model_name, d.status) for d in diffs] == [("m_in_extras", "modified")]
