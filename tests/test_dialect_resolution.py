"""Which SQL dialect dbt-plan parses with.

`--dialect` defaults to snowflake, and the manifest has said which adapter
produced the project all along:

    "metadata": { "project_name": "test_project", "adapter_type": "duckdb" }

So a BigQuery or Postgres user who passed no flag had their SQL parsed as
Snowflake. sqlglot is tolerant enough that this mostly produced parse failures
rather than wrong answers -- dbt-plan reports those as review required, so it was
noise rather than danger -- but it was unexplained noise with a fix sitting in the
file it already reads.
"""

from __future__ import annotations

import pytest

from dbt_plan.config import DEFAULT_DIALECT, Config, sqlglot_dialect_for_adapter


class TestAdapterToDialect:
    @pytest.mark.parametrize(
        "adapter,expected",
        [
            ("snowflake", "snowflake"),
            ("bigquery", "bigquery"),
            ("postgres", "postgres"),
            ("redshift", "redshift"),
            ("databricks", "databricks"),
            ("duckdb", "duckdb"),
            ("spark", "spark"),
            ("trino", "trino"),
            ("athena", "athena"),
            ("BigQuery", "bigquery"),
            ("  postgres  ", "postgres"),
        ],
    )
    def test_adapters_sqlglot_already_knows(self, adapter, expected):
        assert sqlglot_dialect_for_adapter(adapter) == expected

    @pytest.mark.parametrize(
        "adapter,expected",
        [
            ("sqlserver", "tsql"),
            ("synapse", "tsql"),
            ("glue", "spark"),
            ("spark_session", "spark"),
        ],
    )
    def test_the_few_that_are_spelled_differently(self, adapter, expected):
        assert sqlglot_dialect_for_adapter(adapter) == expected

    @pytest.mark.parametrize("adapter", ["vertica", "firebolt", "something_new", "", None])
    def test_an_adapter_sqlglot_has_no_dialect_for_falls_back(self, adapter):
        """A project on one of these should still get a report, not a crash."""
        assert sqlglot_dialect_for_adapter(adapter) is None


class TestPrecedence:
    def test_the_manifest_decides_when_nobody_else_did(self):
        assert Config().resolve_dialect("bigquery") == "bigquery"

    def test_the_config_file_beats_the_manifest(self, tmp_path):
        (tmp_path / ".dbt-plan.yml").write_text("dialect: postgres\n", encoding="utf-8")
        config = Config.load(tmp_path)
        assert config.dialect_explicit is True
        assert config.resolve_dialect("bigquery") == "postgres"

    def test_the_environment_beats_the_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DBT_PLAN_DIALECT", "postgres")
        assert Config.load(tmp_path).resolve_dialect("bigquery") == "postgres"

    def test_snowflake_written_down_is_not_mistaken_for_the_default(self, tmp_path):
        """The reason `dialect_explicit` exists: it is both the fallback and an answer."""
        (tmp_path / ".dbt-plan.yml").write_text("dialect: snowflake\n", encoding="utf-8")
        assert Config.load(tmp_path).resolve_dialect("bigquery") == "snowflake"

    def test_no_adapter_and_nobody_speaking_leaves_the_default(self, tmp_path):
        assert Config.load(tmp_path).resolve_dialect(None) == DEFAULT_DIALECT


class TestCheckUsesIt:
    def test_the_dialect_is_reported_under_verbose(self, tmp_path, capsys):
        """`check` and `stats` both resolve it the same way; verbose names the winner."""
        from dbt_plan.cli import _do_check
        from tests.test_verbose_debugging import _destructive_scenario, _make_args

        project_dir = _destructive_scenario(tmp_path)
        manifest_path = tmp_path / "project" / "target" / "manifest.json"
        if manifest_path.exists():
            import json

            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data.setdefault("metadata", {})["adapter_type"] = "postgres"
            manifest_path.write_text(json.dumps(data), encoding="utf-8")

        _do_check(_make_args(project_dir, verbose=True, dialect=None))
        assert "Dialect: postgres" in capsys.readouterr().err
