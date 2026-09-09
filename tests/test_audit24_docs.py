"""Execute the published showcase and compare its committed report."""

import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples/sample-project"


def run_sample(fmt):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dbt_plan.cli",
            "check",
            "--base-dir",
            str(SAMPLE / "base"),
            "--project-dir",
            str(SAMPLE / "current"),
            "--format",
            fmt,
            "--no-color",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_showcase_uses_resolved_column_reader():
    result = run_sample("json")
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    model = next(m for m in report["models"] if m["model_name"] == "int_order_enriched")
    assert model["downstream_impacts"] == [
        {
            "model_name": "fct_daily_sales",
            "risk": "broken_ref",
            "reason": "reads dropped column(s): shipping_info",
        }
    ]
    assert report["summary"]["cascade_risks"] == 1


def test_committed_showcase_matches_current_cli():
    result = run_sample("text")
    assert result.returncode == 1, result.stderr
    expected = (SAMPLE / "output.txt").read_text(encoding="utf-8")
    assert result.stdout == expected
    for document in (
        "docs/use-cases.md",
        "README.md",
        "README.ko.md",
        "examples/sample-project/README.md",
    ):
        assert expected.rstrip() in (ROOT / document).read_text(encoding="utf-8")
    page = (ROOT / "docs/index.html").read_text(encoding="utf-8")
    rendered = html.unescape(re.sub(r"<[^>]+>", "", page))
    assert expected.rstrip() in rendered
