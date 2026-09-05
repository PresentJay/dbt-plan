"""dbt-plan reads `target/`, and nothing in `target/` says whether it is current.

When `dbt compile` fails, the compiled SQL is whatever was there before. The diff
comes out empty, and an empty diff used to read as "nothing changed". Measured on
jaffle_shop: drop a column, break the parse so nothing recompiles, and

    $ dbt compile
    exit=2
    $ dbt-plan check
    dbt-plan -- no model changes detected
    exit=0

The column really was gone. This is not a rule getting the wrong answer; it is
every rule answering about code the user no longer has.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from dbt_plan.cli import _stale_sources
from dbt_plan.formatter import CheckResult, format_json, format_text
from dbt_plan.manifest import load_manifest


def _project(tmp_path, *, source_dirs=("models",)):
    project = tmp_path / "proj"
    for name in source_dirs:
        (project / name).mkdir(parents=True)
        (project / name / "m.sql").write_text("SELECT 1", encoding="utf-8")
    (project / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    manifest = project / "target" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    # The manifest is written after the sources it describes, which is what dbt
    # does; the tolerance in _stale_sources is smaller than this gap.
    _touch(manifest, offset=10)
    return project, manifest


def _touch(path, *, offset: float):
    stamp = time.time() + offset
    os.utime(path, (stamp, stamp))


class TestStaleSources:
    def test_a_fresh_compile_reports_nothing(self, tmp_path):
        project, manifest = _project(tmp_path)
        assert _stale_sources(project, manifest, ("models",)) == []

    def test_a_model_edited_after_the_compile_is_named(self, tmp_path):
        project, manifest = _project(tmp_path)
        _touch(project / "models" / "m.sql", offset=30)
        assert _stale_sources(project, manifest, ("models",)) == ["models/m.sql"]

    def test_it_looks_in_every_source_directory_the_manifest_named(self, tmp_path):
        """`macro-paths` and `test-paths` are configurable too, and a macro change
        moves compiled SQL without touching a model file at all."""
        project, manifest = _project(tmp_path, source_dirs=("models", "macros"))
        _touch(project / "macros" / "m.sql", offset=30)
        assert _stale_sources(project, manifest, ("models", "macros")) == ["macros/m.sql"]

    def test_dbt_project_yml_counts_without_being_asked(self, tmp_path):
        project, manifest = _project(tmp_path)
        _touch(project / "dbt_project.yml", offset=30)
        assert _stale_sources(project, manifest, ("models",)) == ["dbt_project.yml"]

    def test_paths_are_posix_on_every_platform(self, tmp_path):
        """Read next to the manifest's own `original_file_path`, which is posix."""
        project, manifest = _project(tmp_path, source_dirs=("models",))
        nested = project / "models" / "staging"
        nested.mkdir()
        (nested / "stg.sql").write_text("SELECT 1", encoding="utf-8")
        _touch(nested / "stg.sql", offset=30)

        assert _stale_sources(project, manifest, ("models",)) == ["models/staging/stg.sql"]

    def test_a_directory_that_does_not_exist_is_skipped(self, tmp_path):
        project, manifest = _project(tmp_path)
        assert _stale_sources(project, manifest, ("models", "no_such_dir")) == []

    def test_a_missing_manifest_reports_nothing(self, tmp_path):
        """There is a separate, louder error for that; this must not add noise."""
        project, _ = _project(tmp_path)
        assert _stale_sources(project, project / "target" / "nope.json", ("models",)) == []

    def test_the_list_is_capped(self, tmp_path):
        """Naming a couple points at the compile; naming forty is a wall."""
        project, manifest = _project(tmp_path)
        for i in range(10):
            path = project / "models" / f"m{i}.sql"
            path.write_text("SELECT 1", encoding="utf-8")
            _touch(path, offset=30)
        assert len(_stale_sources(project, manifest, ("models",), limit=3)) == 3


class TestSourceDirsComeFromTheManifest:
    def test_every_declared_directory_is_collected(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "metadata": {"project_name": "p"},
                    "nodes": {
                        "model.p.a": {"original_file_path": "transformations/a.sql"},
                        "test.p.t": {"original_file_path": "checks/t.sql"},
                    },
                    "macros": {"macro.p.m": {"original_file_path": "helpers/m.sql"}},
                }
            ),
            encoding="utf-8",
        )
        assert set(load_manifest(manifest)["source_dirs"]) == {
            "transformations",
            "checks",
            "helpers",
        }

    def test_package_files_are_left_out(self, tmp_path):
        """Their mtimes move when `dbt deps` runs, not when anyone edits this project."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "metadata": {"project_name": "p"},
                    "nodes": {"model.p.a": {"original_file_path": "models/a.sql"}},
                    "macros": {
                        "macro.dbt_utils.star": {"original_file_path": "macros/sql/star.sql"}
                    },
                }
            ),
            encoding="utf-8",
        )
        assert load_manifest(manifest)["source_dirs"] == ("models",)


class TestItSurvivesToTheOutput:
    """An empty diff is exactly what a failed compile produces, so this has to
    be reported when there is nothing else to report at all."""

    def _result(self):
        return CheckResult(stale_sources=["models/staging/stg_orders.sql"])

    def test_it_is_not_swallowed_by_no_changes_detected(self):
        out = format_text(self._result(), color=False)
        assert "no model changes detected" not in out
        assert "target/ may be out of date" in out
        assert "models/staging/stg_orders.sql" in out

    def test_it_drives_the_exit_code(self):
        from dbt_plan.cli import _exit_code_for

        assert _exit_code_for(self._result(), 2) == 2
        assert _exit_code_for(CheckResult(), 2) == 0

    def test_json_carries_it(self):
        assert json.loads(format_json(self._result()))["stale_sources"] == [
            "models/staging/stg_orders.sql"
        ]

    @pytest.mark.parametrize("count,word", [(1, " is newer"), (2, " are newer")])
    def test_the_sentence_agrees_with_itself(self, count, word):
        result = CheckResult(stale_sources=[f"models/m{i}.sql" for i in range(count)])
        assert word in format_text(result, color=False)
