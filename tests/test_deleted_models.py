"""A model deleted from the source is reported, whatever target/ still contains.

`dbt compile` never removes what it wrote before. Measured on dbt 1.11.7:

    $ rm models/doomed.sql && dbt compile
    $ ls target/compiled/delm/models/
    doomed.sql  keep.sql                 <- still there
    $ dbt-plan check
    dbt-plan -- no model changes detected
    exit=0

The diff found identical bytes on both sides. The MODEL REMOVED -> DESTRUCTIVE rule
that exists for exactly this case could never fire in the normal flow -- only after
`dbt clean`. The manifest is the authority: a model the base manifest had and the
current one does not is gone.
"""

from __future__ import annotations

import argparse
import json

from dbt_plan.cli import _do_check


def _project(tmp_path, *, base_models, current_models):
    """Both sides have every model's compiled SQL (as dbt leaves it); only the
    manifests differ."""
    project = tmp_path / "proj"
    base = project / ".dbt-plan" / "base" / "compiled" / "models"
    current = project / "target" / "compiled" / "p" / "models"
    base.mkdir(parents=True)
    current.mkdir(parents=True)
    for name in set(base_models) | set(current_models):
        for d in (base, current):
            (d / f"{name}.sql").write_text("SELECT 1 AS a, 2 AS b", encoding="utf-8")

    def manifest(models):
        return {
            "metadata": {"project_name": "p"},
            "nodes": {
                f"model.p.{m}": {
                    "name": m,
                    "path": f"{m}.sql",
                    "original_file_path": f"models/{m}.sql",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                    "unrendered_config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                }
                for m in models
            },
            "child_map": {},
        }

    (project / ".dbt-plan" / "base" / "manifest.json").write_text(
        json.dumps(manifest(base_models)), encoding="utf-8"
    )
    (project / "target" / "manifest.json").write_text(
        json.dumps(manifest(current_models)), encoding="utf-8"
    )
    return project


def _check(project, **overrides):
    args = argparse.Namespace(
        project_dir=str(project),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format="text",
        no_color=True,
        verbose=False,
        dialect="duckdb",
        select=None,
        acknowledge=None,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return _do_check(args)


class TestADeletedModelIsReported:
    def test_it_is_destructive_even_though_its_compiled_sql_is_still_there(self, tmp_path, capsys):
        project = _project(tmp_path, base_models=["keep", "doomed"], current_models=["keep"])
        assert _check(project) == 1
        out = capsys.readouterr().out
        assert "DESTRUCTIVE  doomed" in out
        assert "MODEL REMOVED" in out
        assert "no model changes detected" not in out

    def test_a_model_present_in_both_manifests_is_not_touched(self, tmp_path, capsys):
        project = _project(tmp_path, base_models=["keep"], current_models=["keep"])
        assert _check(project) == 0
        assert "no model changes detected" in capsys.readouterr().out

    def test_ignore_models_still_applies(self, tmp_path, capsys, monkeypatch):
        project = _project(tmp_path, base_models=["keep", "doomed"], current_models=["keep"])
        (project / ".dbt-plan.yml").write_text("ignore_models: [doomed]\n", encoding="utf-8")
        assert _check(project) == 0
        assert "doomed" not in capsys.readouterr().out

    def test_a_removal_the_diff_already_saw_is_not_reported_twice(self, tmp_path, capsys):
        """After `dbt clean` the compiled file really is gone; the diff reports it."""
        project = _project(tmp_path, base_models=["keep", "doomed"], current_models=["keep"])
        (project / "target" / "compiled" / "p" / "models" / "doomed.sql").unlink()
        assert _check(project) == 1
        assert capsys.readouterr().out.count("DESTRUCTIVE  doomed") == 1

    def test_no_base_manifest_means_no_synthesis(self, tmp_path, capsys):
        """Without the base side there is nothing to compare against -- and nothing
        to invent."""
        project = _project(tmp_path, base_models=["keep", "doomed"], current_models=["keep"])
        (project / ".dbt-plan" / "base" / "manifest.json").unlink()
        _check(project)
        assert "MODEL REMOVED" not in capsys.readouterr().out


class TestARemovedDownstreamCannotBreak:
    def test_it_is_not_a_broken_ref_of_the_model_it_read(self):
        """Its stale compiled SQL is on disk and mentions the column. It is gone too."""
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import analyze_cascade_impacts, predict_ddl

        doomed = predict_ddl(
            "doomed", "incremental", "sync_all_columns", ["a", "b"], None, "removed"
        )
        reader = predict_ddl("reader", "table", None, ["b"], None, "removed")
        updated, _ = analyze_cascade_impacts(
            predictions=[doomed, reader],
            model_node_ids={"doomed": "model.p.doomed", "reader": "model.p.reader"},
            model_cols={"doomed": (["a", "b"], None), "reader": (["b"], None)},
            all_downstream={"model.p.doomed": ["model.p.reader"], "model.p.reader": []},
            node_index={},
            base_node_index={
                "reader": ModelNode("model.p.reader", "reader", "table", None),
            },
            compiled_sql_index={},
            columns_read_of=lambda d, m: ["b"],
        )
        assert updated[0].downstream_impacts == []
