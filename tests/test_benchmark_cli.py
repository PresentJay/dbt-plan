"""The benchmark must run the CLI and verify its work, not time an empty check."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_benchmark_executes_and_validates_all_workloads(tmp_path):
    output = tmp_path / "result.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/benchmark_cli.py"),
            "--models",
            "12",
            "--repeat",
            "2",
            "--warmup",
            "1",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["environment"]["python"]
    assert report["environment"]["sqlglot"]
    assert report["environment"]["source_sha256"]
    assert report["measurement"]["includes_dbt_compile"] is False
    assert report["measurement"]["includes_process_startup"] is True
    assert report["warmup"] == 1
    assert report["repeat"] == 2
    cases = {case["scenario"]: case for case in report["results"]}
    assert set(cases) == {"unchanged", "one_changed", "all_changed"}
    for name, case in cases.items():
        assert case["models"] == 12
        assert len(case["seconds"]) == 2
        assert all(value > 0 for value in case["seconds"])
        assert case["min_seconds"] <= case["median_seconds"] <= case["max_seconds"]
        assert (
            case["changed_models"] == {"unchanged": 0, "one_changed": 1, "all_changed": 12}[name]
        )
        assert case["exit_code"] == (0 if name == "unchanged" else 1)


def test_benchmark_rejects_empty_sample(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/benchmark_cli.py"), "--repeat", "0"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "positive" in result.stderr
