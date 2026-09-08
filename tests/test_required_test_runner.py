"""Required CI suites must fail on collection and execution skips."""

import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / ".github/scripts/required-tests.py"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("def test_ok(): assert True", 0),
        ('import pytest\ndef test_skip(): pytest.skip("missing dependency")', 1),
        ('import pytest\npytest.importorskip("nonexistent_dbt_plan_dependency")', 1),
        ("def test_fail(): assert False", 1),
        ("", 5),
    ],
)
def test_required_suite(tmp_path, body, expected):
    test = tmp_path / "test_sample.py"
    test.write_text(body)
    result = subprocess.run(
        [sys.executable, str(RUNNER), "-q", str(test)], capture_output=True, text=True
    )
    assert result.returncode == expected, result.stdout + result.stderr
