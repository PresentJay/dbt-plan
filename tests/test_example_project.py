"""Test that the sample project produces expected predictions."""

from pathlib import Path

import pytest

from dbt_plan.cli import main

EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "sample-project"


@pytest.fixture
def example_project():
    """Verify example project exists."""
    assert EXAMPLE_DIR.exists(), f"Example project not found: {EXAMPLE_DIR}"
    return EXAMPLE_DIR


class TestExampleProject:
    def test_detects_destructive_change(self, example_project, monkeypatch, capsys):
        """int_order_enriched DROP COLUMN → exit 1."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

        output = capsys.readouterr().out
        assert "DESTRUCTIVE" in output
        assert "int_order_enriched" in output
        assert "DROP COLUMN" in output
        assert "shipping_info" in output
        assert "billing_info" in output

    def test_detects_safe_table_rebuild(self, example_project, monkeypatch, capsys):
        """dim_customers (table) → SAFE CREATE OR REPLACE."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "dim_customers" in output
        assert "CREATE OR REPLACE TABLE" in output

    def test_detects_append_column(self, example_project, monkeypatch, capsys):
        """fct_daily_sales (append_new_columns) → SAFE ADD COLUMN."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "fct_daily_sales" in output
        assert "ADD COLUMN" in output
        assert "total_sales" in output

    def test_detects_new_model(self, example_project, monkeypatch, capsys):
        """dim_publishers (new) → SAFE."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "dim_publishers" in output

    def test_downstream_impact(self, example_project, monkeypatch, capsys):
        """int_order_enriched downstream → dim_customers, fct_daily_sales."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "Downstream:" in output
        assert "dim_customers" in output
        assert "fct_daily_sales" in output

    def test_github_format(self, example_project, monkeypatch, capsys):
        """--format github produces markdown."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "github",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "###" in output
        assert "**DESTRUCTIVE**" in output
        assert "`int_order_enriched`" in output

    def test_cascade_broken_ref(self, example_project, monkeypatch, capsys):
        """fct_daily_sales references dropped column shipping_info → BROKEN_REF cascade."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "BROKEN_REF" in output
        assert "fct_daily_sales" in output
        assert "shipping_info" in output
        assert "cascade risk" in output

    def test_cascade_in_json(self, example_project, monkeypatch, capsys):
        """JSON output includes downstream_impacts with cascade risks."""
        import json

        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "json",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["summary"]["cascade_risks"] == 1
        int_order_enriched = next(
            m for m in data["models"] if m["model_name"] == "int_order_enriched"
        )
        assert "downstream_impacts" in int_order_enriched
        assert int_order_enriched["downstream_impacts"][0]["risk"] == "broken_ref"

    def test_model_count(self, example_project, monkeypatch, capsys):
        """4 models changed."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "dbt-plan",
                "check",
                "--base-dir",
                str(example_project / "base"),
                "--project-dir",
                str(example_project / "current"),
                "--format",
                "text",
            ],
        )
        with pytest.raises(SystemExit):
            main()

        output = capsys.readouterr().out
        assert "4 model(s) changed" in output
