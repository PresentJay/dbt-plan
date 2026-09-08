"""Real Git/dbt fixture shared by local tests and the composite Action CI steps.

Run `python -m tests.action_project prepare DIR` or `verify DIR CASE OUTCOME
EXIT_CODE VERDICT REPORT`. No dbt/CLI commands are mocked in the CI invocation.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CASES = ("safe", "warning", "destructive", "base-error", "head-error")
BASE_SQL = "{{ config(materialized='incremental', on_schema_change='sync_all_columns') }}\nselect 1 as order_id, 2 as amount\n"
ERROR_SQL = "{{ exceptions.raise_compiler_error('intentional Action compile failure') }}\n"


def git(project, *args):
    return subprocess.check_output(["git", *args], cwd=project, text=True).strip()


def prepare(project, finding):
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: action_project\nversion: '1.0.0'\nprofile: action_profile\n"
    )
    (project / "profiles.yml").write_text(
        "action_profile:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: ':memory:'\n"
    )
    (project / ".gitignore").write_text(
        "target/\ncompiled target/\nlogs/\n.user.yml\n.dbt-plan/\ncompile.log\n"
    )
    model = project / "models/orders.sql"
    model.write_text(ERROR_SQL if finding == "base-error" else BASE_SQL)
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.name", "Action integration test")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "core.autocrlf", "false")
    git(project, "add", ".")
    git(project, "commit", "-qm", "baseline")
    base = git(project, "rev-parse", "HEAD")
    changed = BASE_SQL + "-- current revision\n"
    if finding in ("warning", "destructive"):
        changed = changed.replace(", 2 as amount", "")
    if finding == "warning":
        changed = changed.replace("sync_all_columns", "fail")
    model.write_text(ERROR_SQL if finding == "head-error" else changed)
    git(project, "add", ".")
    git(project, "commit", "-qm", "current")
    return {
        "project": str(project),
        "finding": finding,
        "base": base,
        "head": git(project, "rev-parse", "HEAD"),
    }


def verify(case, outcome, outputs, report_path):
    finding = case["finding"]
    project = Path(case["project"])
    revisions = (project / "compile.log").read_text().splitlines()
    assert revisions == (
        [case["base"]] if finding == "base-error" else [case["base"], case["head"]]
    ), revisions
    if "error" in finding:
        assert outcome == "failure", outcome
        assert not outputs.get("exit-code"), outputs
        # A failed compile has no finding; fail-on: never must not hide it.
        return
    assert git(project, "rev-parse", "HEAD") == case["head"]
    assert outcome == ("failure" if finding == "destructive" else "success"), outcome
    assert outputs["exit-code"] == {"safe": "0", "warning": "2", "destructive": "1"}[finding], (
        outputs
    )
    assert outputs["verdict"] == finding, outputs
    report = json.loads(report_path.read_text())
    assert report["parse_failures"] == [], report
    assert report["summary"]["total"] == 1, report
    assert report["models"][0]["safety"] == finding, report
    if finding != "safe":
        assert report["models"][0]["columns_removed"] == ["amount"], report
    print(f"ACTION_E2E_PASSED: {finding}")


def main():
    command, directory, *args = sys.argv[1:]
    root = Path(directory).resolve()
    if command == "prepare":
        root.mkdir(parents=True)
        cases = {name: prepare(root / name, name) for name in CASES}
        (root / "cases.json").write_text(json.dumps(cases))
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for name, case in cases.items():
                output.write(f"{name}-base={case['base']}\n")
    elif command == "verify":
        name, outcome, code, verdict, report = args
        case = json.loads((root / "cases.json").read_text())[name]
        verify(case, outcome, {"exit-code": code, "verdict": verdict}, Path(report))
    else:
        raise ValueError(command)


if __name__ == "__main__":
    main()
