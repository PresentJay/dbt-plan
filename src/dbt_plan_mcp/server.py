"""Expose dbt-plan to coding agents over the Model Context Protocol.

An agent editing dbt models cannot pause and eyeball a diff the way a person does,
and one holding dbt's own MCP server can *execute*. That combination is the reason
this exists: the more able the agent is to run things, the more it needs to be told
what a change will do first.

Two design decisions worth knowing before changing anything here.

**The CLI is invoked as a subprocess rather than imported.** stdio transport owns
this process's stdout, and `dbt_plan.cli` writes its report there. Importing it and
redirecting would work until something printed outside the redirect and silently
corrupted the protocol stream. A subprocess cannot do that.

**A refusal is never flattened into a boolean.** A person reading "safe" may still
glance at the diff; an agent reading it proceeds. So "could not extract columns",
"the compile is incomplete" and "columns came from the manifest" are returned as
themselves. If this ever answers `{"safe": true}` for something dbt-plan actually
declined to judge, it is worse than not existing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="dbt-plan",
    instructions=(
        "Predicts the DDL a dbt change will execute, before running it. Call `plan` "
        "after editing models or macros and before `dbt run` or `dbt build`. A macro "
        "edit is the case to be most careful about: no model file has a diff, yet every "
        "model calling it may gain or lose columns.\n\n"
        "Requires compiled SQL. Run `dbt compile` first; on the dbt Fusion engine that "
        "needs no warehouse credentials.\n\n"
        "Treat `verdict` as the answer and `refusals` as a stop sign. A non-empty "
        "`refusals` means dbt-plan could not judge part of the project -- do not read "
        "that as safe."
    ),
)

# 0 safe, 1 destructive, 2 could not decide. Anything else is dbt-plan failing to run.
_VERDICTS = {0: "safe", 1: "destructive", 2: "review_required"}


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@server.tool(
    description=(
        "Report the DDL a dbt run would execute for the changes since the last "
        "snapshot, without executing anything. Requires `dbt compile` to have run."
    )
)
def plan(
    project_dir: str,
    dialect: str | None = None,
    select: str | None = None,
) -> dict[str, Any]:
    """Compare compiled SQL against the baseline and report what dbt would do.

    Args:
        project_dir: dbt project directory, the one containing `target/`.
        dialect: sqlglot dialect for parsing compiled SQL. Defaults to the
            configured value, or snowflake.
        select: Comma-separated model names to restrict the check to.
    """
    args = ["check", "--project-dir", project_dir, "--format", "json", "--no-color"]
    if dialect:
        args += ["--dialect", dialect]
    if select:
        args += ["--select", select]

    result = _run_cli(args)

    # Exit 2 means two different things: "I could not decide" and "I could not run".
    # The CLI returns it for a review-required verdict and for a missing baseline
    # alike, so the exit code alone cannot tell them apart. A parseable report can:
    # dbt-plan only emits one when it actually analysed something.
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "verdict": "error",
            "error": result.stderr.strip() or "dbt-plan produced no report",
            "hint": (
                "Most often there is no baseline yet, or `dbt compile` has not run. "
                "Call `snapshot` on the revision you are changing from, then compile."
            ),
        }

    if result.returncode not in _VERDICTS:
        return {
            "verdict": "error",
            "error": result.stderr.strip() or f"dbt-plan exited {result.returncode}",
        }

    # Everything dbt-plan declined to judge, kept apart from the verdict rather than
    # folded into it. An empty list here is the only thing that makes "safe" mean safe.
    refusals: list[dict[str, Any]] = []
    for kind, names in (
        ("columns_unreadable", report.get("parse_failures") or []),
        ("missing_from_manifest", report.get("skipped_models") or []),
        ("never_compiled", report.get("uncompiled_models") or []),
    ):
        if names:
            refusals.append({"reason": kind, "models": names})
    for model in report.get("models") or []:
        for op in model.get("operations") or []:
            if "REVIEW REQUIRED" in op.get("operation", ""):
                refusals.append(
                    {
                        "reason": "not_decidable",
                        "models": [model["model_name"]],
                        "detail": op["operation"],
                    }
                )

    return {
        "verdict": _VERDICTS[result.returncode],
        "exit_code": result.returncode,
        "summary": report.get("summary", {}),
        "models": report.get("models", []),
        "refusals": refusals,
    }


@server.tool(
    description=(
        "Record the current compiled SQL as the baseline that `plan` compares "
        "against. Run this on the revision you are changing from."
    )
)
def snapshot(project_dir: str) -> dict[str, Any]:
    """Save `target/compiled` and `manifest.json` as the comparison baseline.

    Args:
        project_dir: dbt project directory, the one containing `target/`.
    """
    result = _run_cli(["snapshot", "--project-dir", project_dir])
    if result.returncode != 0:
        return {
            "ok": False,
            "error": result.stderr.strip() or "snapshot failed",
            "hint": "`dbt compile` has to have run first; there is nothing to snapshot without it.",
        }
    return {"ok": True, "detail": result.stdout.strip()}


def main() -> None:
    server.run(transport="stdio")
