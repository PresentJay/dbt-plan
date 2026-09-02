"""Tests for `dbt-plan agent-setup`.

The generated AGENTS.md lands in someone else's dbt repo and is read by coding
agents, so these tests pin two things: that appending never destroys existing
content, and that the guidance keeps saying the things that make it useful --
notably the two config edits that silence a real finding.
"""

from __future__ import annotations

import argparse

import pytest

from dbt_plan.cli import _AGENTS_GUIDE, _AGENTS_MARKER, _do_agent_setup


def _run(project_dir):
    args = argparse.Namespace(project_dir=str(project_dir))
    _do_agent_setup(args)
    return project_dir / "AGENTS.md"


class TestCreatesFile:
    def test_creates_agents_md_when_missing(self, tmp_path):
        path = _run(tmp_path)
        assert path.exists()
        assert _AGENTS_MARKER in path.read_text(encoding="utf-8")

    def test_new_file_starts_with_a_heading(self, tmp_path):
        """A fresh AGENTS.md needs its own H1; the guide itself starts at H2."""
        content = _run(tmp_path).read_text(encoding="utf-8")
        assert content.startswith("# AGENTS.md\n")

    def test_reports_creation(self, tmp_path, capsys):
        _run(tmp_path)
        assert "Created" in capsys.readouterr().out


class TestAppendsToExisting:
    def test_preserves_existing_content(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_text("# AGENTS.md\n\n## house rules\n\nRun the linter.\n")
        _run(tmp_path)
        content = path.read_text(encoding="utf-8")
        assert "## house rules" in content
        assert "Run the linter." in content
        assert _AGENTS_MARKER in content

    def test_appends_after_existing_content(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_text("# AGENTS.md\n\nexisting\n")
        _run(tmp_path)
        content = path.read_text(encoding="utf-8")
        assert content.index("existing") < content.index(_AGENTS_MARKER)

    def test_handles_file_without_trailing_newline(self, tmp_path):
        """Appending to a file that lacks a final newline must not join two lines."""
        path = tmp_path / "AGENTS.md"
        path.write_text("# AGENTS.md\n\nno trailing newline")
        _run(tmp_path)
        assert "no trailing newline\n" in path.read_text(encoding="utf-8")

    def test_does_not_add_a_second_h1(self, tmp_path):
        path = tmp_path / "AGENTS.md"
        path.write_text("# AGENTS.md\n\nexisting\n")
        _run(tmp_path)
        assert path.read_text(encoding="utf-8").count("# AGENTS.md") == 1

    def test_reports_append(self, tmp_path, capsys):
        (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n")
        _run(tmp_path)
        assert "Appended" in capsys.readouterr().out


class TestIdempotency:
    def test_second_run_exits_2(self, tmp_path):
        _run(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run(tmp_path)
        assert exc.value.code == 2

    def test_second_run_leaves_content_unchanged(self, tmp_path):
        path = _run(tmp_path)
        before = path.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            _run(tmp_path)
        assert path.read_text(encoding="utf-8") == before

    def test_second_run_does_not_duplicate_the_section(self, tmp_path):
        path = _run(tmp_path)
        with pytest.raises(SystemExit):
            _run(tmp_path)
        assert path.read_text(encoding="utf-8").count(_AGENTS_MARKER) == 1


class TestGuidanceContent:
    def test_names_the_command_to_run(self):
        assert "dbt-plan run" in _AGENTS_GUIDE

    def test_documents_all_three_exit_codes(self):
        for code in ("`0`", "`1`", "`2`"):
            assert code in _AGENTS_GUIDE, f"exit code {code} not explained"

    def test_warns_against_ignore_models_as_a_silencer(self):
        """The single most damaging edit an agent could make to pass the check."""
        assert "ignore_models" in _AGENTS_GUIDE
        assert "What not to do" in _AGENTS_GUIDE

    def test_warns_against_downgrading_on_schema_change(self):
        assert "sync_all_columns" in _AGENTS_GUIDE
        assert "`ignore`" in _AGENTS_GUIDE

    def test_states_that_dbt_compile_needs_credentials(self):
        """The nuance that cost real debugging time: dbt-plan doesn't connect, compile does."""
        assert "dbt compile" in _AGENTS_GUIDE
        assert "credentials" in _AGENTS_GUIDE
        assert "never connects" in _AGENTS_GUIDE

    def test_states_that_parse_failure_is_not_safe(self):
        assert "review required" in _AGENTS_GUIDE
        assert "false all-clear" in _AGENTS_GUIDE

    def test_risk_table_covers_every_materialization(self):
        for term in ("table", "view", "incremental", "snapshot", "append_new_columns", "fail"):
            assert term in _AGENTS_GUIDE, f"risk table missing {term}"

    def test_distinguishes_the_three_kinds_of_exit_2(self):
        """An agent that reads "warning" and stops has learned nothing actionable.

        Each of the three produces a different next step, and conflating them is how
        "the compile is incomplete" gets treated as a flaky check to rerun.
        """
        for phrase in ("review required", "not found in manifest", "the compile is incomplete"):
            assert phrase in _AGENTS_GUIDE, f"exit 2 case not distinguished: {phrase}"

    def test_warns_against_ignoring_an_uncompiled_model(self):
        """The newest way to silence the check, and the most tempting to an agent.

        Adding the uncompiled model to ignore_models turns the check green while
        leaving the one model nobody has examined unexamined.
        """
        assert "Fix the compile instead" in _AGENTS_GUIDE

    def test_explains_why_a_partial_compile_happens(self):
        """Without the cause, "incomplete compile" reads as a dbt-plan bug."""
        assert "Fusion" in _AGENTS_GUIDE

    def test_names_the_manifest_fallback_and_type_change_cases(self):
        """0.8.0 added two more ways to reach exit 2; the table has to keep up."""
        for phrase in ("came from the manifest", "TYPE CHANGED"):
            assert phrase in _AGENTS_GUIDE, f"exit 2 case not documented: {phrase}"

    def test_warns_against_papering_over_the_fallback_with_partial_docs(self):
        """Documenting *some* columns is what caused the problem in the first place."""
        assert "documenting *some* of them" in _AGENTS_GUIDE

    def test_guide_starts_with_the_marker(self):
        """The marker must be first so idempotency detection cannot be fooled."""
        assert _AGENTS_GUIDE.startswith(_AGENTS_MARKER)

    def test_guide_uses_h2_not_h1(self):
        """It gets appended under an existing H1, so it must not open a second one."""
        for line in _AGENTS_GUIDE.splitlines():
            assert not line.startswith("# "), f"guide must not contain an H1: {line}"
