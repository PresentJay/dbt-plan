"""The published GitHub Action must stay in step with the CLI it wraps.

The action translates dbt-plan's exit code into a verdict and a pass/fail gate.
If that translation drifts from what the CLI actually returns, the action reports
"safe" on a destructive change -- a false all-clear, which is the one failure this
project does not tolerate. These tests pin the translation to the real thing.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from dbt_plan.cli import _exit_code_for
from dbt_plan.formatter import CheckResult
from dbt_plan.predictor import DDLOperation, DDLPrediction, Safety

ACTION = Path(__file__).parent.parent / "action.yml"
ACTION_TEXT = ACTION.read_text()

# `dbt-plan <args>` inside the action's run: blocks, stopping at any redirect or pipe.
_INVOCATION = re.compile(r"^\s*dbt-plan\s+(?P<args>.+?)\s*(?:[|>]|$)", re.M)
# `  0) verdict=safe ;;` from the exit-code case statement.
_VERDICT_CASE = re.compile(r"^\s*(?P<code>\d+)\)\s*verdict=(?P<verdict>\w+)", re.M)

ARGPARSE_REJECTIONS = ("invalid choice", "unrecognized arguments", "expected one argument")


def _pred(name, safety, mat="incremental", osc="sync_all_columns"):
    return DDLPrediction(
        model_name=name,
        materialization=mat,
        on_schema_change=osc,
        safety=safety,
        operations=[DDLOperation("DROP COLUMN", "revenue")],
        columns_removed=["revenue"] if safety == Safety.DESTRUCTIVE else [],
    )


def _verdict_map() -> dict[int, str]:
    return {int(m["code"]): m["verdict"] for m in _VERDICT_CASE.finditer(ACTION_TEXT)}


class TestVerdictMapping:
    """The case statement must agree with _exit_code_for, not with a comment."""

    def test_action_declares_all_three_verdicts(self):
        assert _verdict_map() == {0: "safe", 1: "destructive", 2: "warning"}

    @pytest.mark.parametrize(
        "safety,expected",
        [
            (Safety.SAFE, "safe"),
            (Safety.DESTRUCTIVE, "destructive"),
            (Safety.WARNING, "warning"),
        ],
    )
    def test_real_exit_code_maps_to_the_intended_verdict(self, safety, expected):
        code = _exit_code_for(CheckResult([_pred("int_orders", safety)]), warning_exit_code=2)
        assert _verdict_map()[code] == expected

    def test_destructive_is_never_reported_as_safe(self):
        """The false-safe guard, stated directly rather than implied."""
        code = _exit_code_for(
            CheckResult([_pred("int_orders", Safety.DESTRUCTIVE)]), warning_exit_code=2
        )
        assert _verdict_map()[code] != "safe"


class TestGate:
    def test_default_fail_on_is_destructive(self):
        assert re.search(r"fail-on:.*?default:\s*destructive", ACTION_TEXT, re.S)

    def test_gate_fails_the_job_on_the_destructive_exit_code(self):
        gate = ACTION_TEXT.split("name: Gate", 1)[1]
        assert re.search(r'destructive\)\s*\[\s*"\$CODE"\s*=\s*"1"\s*\]\s*&&\s*fail', gate)

    def test_gate_rejects_an_unknown_fail_on(self):
        gate = ACTION_TEXT.split("name: Gate", 1)[1]
        assert "*)" in gate and "exit 1" in gate.split("*)", 1)[1]


class TestInvocations:
    """Every command the action runs has to exist, with the flags it passes."""

    def test_the_action_actually_invokes_dbt_plan(self):
        assert len(_INVOCATION.findall(ACTION_TEXT)) >= 3

    def test_every_invocation_survives_argument_parsing(self, tmp_path):
        for raw in _INVOCATION.findall(ACTION_TEXT):
            # Shell variables stand in for caller-supplied values.
            args = [
                {"$DIALECT": "snowflake"}.get(tok, "x" if tok.startswith("$") else tok)
                for tok in shlex.split(raw)
            ]
            proc = subprocess.run(
                [sys.executable, "-m", "dbt_plan.cli", *args],
                capture_output=True,
                text=True,
                cwd=tmp_path,
            )
            stderr = proc.stderr.lower()
            for rejection in ARGPARSE_REJECTIONS:
                assert rejection not in stderr, f"`dbt-plan {raw}` was rejected: {proc.stderr}"


class TestSecrets:
    """Inputs reach the shell through env, never through string interpolation.

    An input spliced into a run: block becomes a command, which is how a workflow
    that compiles pull-request-authored code turns into arbitrary execution.
    """

    def test_no_input_is_interpolated_into_a_run_block(self):
        for block in re.findall(r"run: \|\n(.*?)(?=\n    - |\Z)", ACTION_TEXT, re.S):
            assert "${{" not in block, f"input interpolated into a run: block:\n{block}"

    def test_metadata_is_present_for_the_marketplace(self):
        for key in ("name:", "description:", "branding:", "icon:", "color:"):
            assert key in ACTION_TEXT
