#!/usr/bin/env python3
"""Reproducible subprocess CLI benchmark; synthetic compiled artifacts, no dbt run.

Usage: python scripts/benchmark_cli.py --models 50 200 1000 --repeat 5 --warmup 1
Times include process startup, file reads, analysis and JSON serialization. Fixture
creation, snapshotting and dbt compilation are outside the measured interval.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def cli(project, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    # Ambient user configuration must not change the workload or policy.
    env = {key: value for key, value in env.items() if not key.startswith("DBT_PLAN_")}
    start = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    elapsed = time.perf_counter() - start
    return result, elapsed


def create_project(project, count):
    compiled = project / "target/compiled/benchmark/models"
    compiled.mkdir(parents=True)
    nodes, child_map = {}, {}
    # Groups of ten: one root and up to nine direct consumers. This exercises
    # cascade without an unrealistically dense graph growing quadratically.
    for i in range(count):
        name = f"model_{i:04d}"
        root = i - i % 10
        parent = f"model.benchmark.model_{root:04d}" if i != root else None
        node_id = f"model.benchmark.{name}"
        nodes[node_id] = {
            "name": name,
            "resource_type": "model",
            "package_name": "benchmark",
            "path": f"models/{name}.sql",
            "original_file_path": f"models/{name}.sql",
            "database": "memory",
            "schema": "main",
            "alias": name,
            "config": {"materialized": "incremental", "on_schema_change": "sync_all_columns"},
            "unrendered_config": {"on_schema_change": "sync_all_columns"},
            "depends_on": {"nodes": [parent] if parent else []},
        }
        child_map.setdefault(node_id, [])
        if parent:
            child_map[parent].append(node_id)
        sql = (
            f"select id, amount from model_{root:04d}" if parent else "select 1 as id, 2 as amount"
        )
        (compiled / f"{name}.sql").write_text(sql, encoding="utf-8")
    manifest = {
        "metadata": {"project_name": "benchmark", "adapter_type": "duckdb"},
        "nodes": nodes,
        "child_map": child_map,
    }
    (project / "target/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result, _ = cli(project, "snapshot")
    if result.returncode:
        raise RuntimeError(result.stderr)
    return compiled


def run_case(project, count, scenario, repeat, warmup):
    compiled = create_project(project, count)
    changed = {"unchanged": 0, "one_changed": 1, "all_changed": count}[scenario]
    for i in range(changed):
        path = compiled / f"model_{i:04d}.sql"
        path.write_text(path.read_text().replace(", 2 as amount", "").replace(", amount", ""))
    seconds = []
    expected_code = 1 if changed else 0
    for sample in range(warmup + repeat):
        result, elapsed = cli(project, "check", "--format", "json")
        if result.returncode != expected_code:
            raise RuntimeError(
                f"{scenario}: expected exit {expected_code}, got {result.returncode}: {result.stderr}"
            )
        report = json.loads(result.stdout)
        if report["summary"]["total"] != changed or report["parse_failures"]:
            raise RuntimeError(f"benchmark did not analyze the intended workload: {report}")
        if changed and any(
            model["safety"] != "destructive" or model["columns_removed"] != ["amount"]
            for model in report["models"]
        ):
            raise RuntimeError(f"missing destructive finding: {report}")
        if sample >= warmup:
            seconds.append(elapsed)
    return {
        "models": count,
        "scenario": scenario,
        "changed_models": changed,
        "exit_code": expected_code,
        "seconds": seconds,
        "min_seconds": min(seconds),
        "median_seconds": statistics.median(seconds),
        "max_seconds": max(seconds),
    }


def environment():
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        )
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/dbt_plan").glob("*.py")) + [Path(__file__)]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    source_version, _ = cli(ROOT, "--version")
    if source_version.returncode:
        raise RuntimeError(source_version.stderr)
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "sqlglot": importlib.metadata.version("sqlglot"),
        "dbt_plan": source_version.stdout.strip(),
        "git_revision": revision,
        "working_tree_dirty": dirty,
        "source_sha256": digest.hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", type=positive, default=[50, 200, 1000])
    parser.add_argument("--repeat", type=positive, default=5)
    parser.add_argument("--warmup", type=positive, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment(),
        "repeat": args.repeat,
        "warmup": args.warmup,
        "measurement": {
            "includes_dbt_compile": False,
            "includes_process_startup": True,
            "clock": "perf_counter",
            "format": "json",
            "fixture": "synthetic compiled SQL; fan-out groups of ten",
        },
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="dbt-plan-benchmark-") as directory:
        for count in args.models:
            for scenario in ("unchanged", "one_changed", "all_changed"):
                result = run_case(
                    Path(directory) / f"{count}-{scenario}",
                    count,
                    scenario,
                    args.repeat,
                    args.warmup,
                )
                report["results"].append(result)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
