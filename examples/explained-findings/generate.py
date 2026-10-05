"""Reproduce three reports using only prepared compiled files and the real CLI.

No dbt/warehouse required. The fixed fixture provenance is deliberately synthetic;
report output itself is unmodified CLI stdout. Writes only --output-dir.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def cli(project, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DBT_PLAN_")}
    return subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", *args, "--project-dir", str(project)],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    for scenario in ("direct", "multihop", "unknown"):
        with tempfile.TemporaryDirectory(prefix="dbt-plan-explanation-") as temporary:
            project = Path(temporary)
            sqls = {"root": "select 1 as id, 2 as amount"}
            edges = [("root", "reader")]
            sqls["reader"] = "select amount from root"
            if scenario == "unknown":
                sqls["reader"] = "select * from root where"
            if scenario == "multihop":
                sqls.update(
                    mid="select * from root",
                    reader="select * from mid",
                    report="select amount from reader",
                )
                edges = [("root", "mid"), ("mid", "reader"), ("reader", "report")]
            compiled = project / "target/compiled/shop/models"
            compiled.mkdir(parents=True)
            manifest = {
                "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
                "nodes": {},
                "child_map": {},
            }
            for name, sql in sqls.items():
                uid = f"model.shop.{name}"
                materialization = (
                    "ephemeral"
                    if name == "mid"
                    else "incremental"
                    if scenario == "multihop" and name == "reader"
                    else "view"
                )
                config = {"materialized": materialization, "enabled": True}
                if materialization == "incremental":
                    config["on_schema_change"] = "sync_all_columns"
                manifest["nodes"][uid] = {
                    "unique_id": uid,
                    "name": name,
                    "package_name": "shop",
                    "resource_type": "model",
                    "path": f"models/{name}.sql",
                    "original_file_path": f"models/{name}.sql",
                    "relation_name": name,
                    "config": config,
                    "unrendered_config": config,
                    "columns": {"id": {}, "amount": {}},
                    "depends_on": {"nodes": [f"model.shop.{a}" for a, b in edges if b == name]},
                }
                manifest["child_map"][uid] = [f"model.shop.{b}" for a, b in edges if a == name]
                (compiled / f"{name}.sql").write_text(sql + "\n", encoding="utf-8")
                source = project / f"models/{name}.sql"
                source.parent.mkdir(exist_ok=True)
                source.write_text(sql + "\n", encoding="utf-8")
            (project / "target/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            snap = cli(project, "snapshot")
            if snap.returncode:
                raise RuntimeError(snap.stderr)
            provenance_path = project / ".dbt-plan/base/provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance.update(revision="file-only-example", created_at="2000-01-01T00:00:00+00:00")
            provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
            (compiled / "root.sql").write_text("select 1 as id\n", encoding="utf-8")
            (project / "models/root.sql").write_text("select 1 as id\n", encoding="utf-8")
            # Prepared current manifest is newer than the corresponding source,
            # just as after a successful compile; schema declarations stay fixed.
            (project / "target/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            expected = 2 if scenario == "unknown" else 1
            for fmt, suffix in (("text", "txt"), ("github", "md"), ("json", "json")):
                result = cli(project, "check", "--format", fmt, "--no-color")
                if result.returncode != expected:
                    raise RuntimeError(
                        f"{scenario}: expected {expected}, got {result.returncode}\n{result.stdout}\n{result.stderr}"
                    )
                (output / f"{scenario}.{suffix}").write_text(result.stdout, encoding="utf-8")
            print(f"{scenario}: CLI exit {expected}; text/GitHub/JSON generated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    generate(parser.parse_args().output_dir)
