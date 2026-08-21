"""CLI entry point for dbt-plan — static analysis tool for dbt DDL risk warnings."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from dbt_plan.formatter import CheckResult, format_github, format_json, format_text


def _find_compiled_dir(target_dir: Path) -> Path | None:
    """Find compiled SQL models directory inside target/.

    Supports two dbt layouts:
    1. target/compiled/{project_name}/models/  (standard)
    2. target/compiled/models/  (flat, some dbt versions/configs)

    If multiple project directories exist, raises ValueError.
    """
    compiled = target_dir / "compiled"
    if not compiled.exists():
        return None

    # Flat layout: target/compiled/models/ (no project subdir)
    flat_models = compiled / "models"
    if flat_models.is_dir():
        return flat_models

    # Standard layout: target/compiled/{project_name}/models/
    candidates = [
        d / "models" for d in sorted(compiled.iterdir()) if d.is_dir() and (d / "models").exists()
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        project_names = [c.parent.name for c in candidates]
        raise ValueError(
            f"Multiple dbt projects found in {compiled}: {project_names}. "
            "Use --project-dir to specify which project to check."
        )
    return candidates[0]


def _do_snapshot(args: argparse.Namespace) -> None:
    """Save current compiled state as baseline (compiled SQL + manifest)."""
    project_dir = Path(args.project_dir)
    target_dir = project_dir / args.target_dir
    base_dir = project_dir / ".dbt-plan" / "base"

    try:
        compiled_dir = _find_compiled_dir(target_dir)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if compiled_dir is None:
        print(
            "Error: No compiled SQL found. "
            "Run 'dbt compile' first to generate compiled SQL in the target/ directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    if base_dir.exists():
        # Validate base_dir is inside project to prevent path traversal via symlinks
        resolved_base = base_dir.resolve()
        resolved_project = project_dir.resolve()
        if not str(resolved_base).startswith(str(resolved_project) + "/"):
            print(
                "Error: snapshot base directory escapes project directory",
                file=sys.stderr,
            )
            sys.exit(2)
        if base_dir.is_file():
            base_dir.unlink()
        else:
            shutil.rmtree(base_dir)

    # Save compiled SQL (symlinks=True prevents following symlinks outside project)
    compiled_dest = base_dir / "compiled"
    shutil.copytree(compiled_dir, compiled_dest, symlinks=True)

    # Save manifest.json alongside compiled SQL
    manifest_src = target_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, base_dir / "manifest.json")
    else:
        print(
            "Warning: manifest.json not found in target/. "
            "Run 'dbt compile' to generate it. "
            "Without it, 'dbt-plan check' will fail.",
            file=sys.stderr,
        )

    print(f"Snapshot saved to {base_dir}")


_SAMPLE_CONFIG = """\
# dbt-plan configuration
# Place this file in your dbt project root as .dbt-plan.yml
# All settings can also be set via environment variables (DBT_PLAN_*)

# Models to skip during check (e.g., known-safe scratch models)
# ignore_models: [scratch_model, staging_temp]

# Exit code when warnings occur (default: 2, set to 0 to treat as pass)
# warning_exit_code: 2

# Output format: text, github, json (default: text)
# format: text

# SQL dialect for parsing (default: snowflake)
# Supports any sqlglot dialect: snowflake, bigquery, postgres, mysql, etc.
# dialect: snowflake

# Disable colored terminal output (default: false)
# no_color: false

# Include models from dbt packages in analysis (default: false)
# By default, only models from the root project are checked
# include_packages: false

# Command to compile dbt project (default: dbt compile)
# Use this if you run dbt through a wrapper like uv, poetry, or a custom script
# compile_command: uv run dbt compile
"""


def _do_init(args: argparse.Namespace) -> None:
    """Generate a sample .dbt-plan.yml config file."""
    project_dir = Path(args.project_dir)
    config_path = project_dir / ".dbt-plan.yml"

    if config_path.exists():
        print(
            f"Config already exists: {config_path}\n"
            "Edit it directly, or delete it and re-run 'dbt-plan init'.",
            file=sys.stderr,
        )
        sys.exit(2)

    config_path.write_text(_SAMPLE_CONFIG)
    print(f"Created {config_path}")

    # Add .dbt-plan/ to .gitignore if not already there
    gitignore = project_dir / ".gitignore"
    entry = ".dbt-plan/"
    if gitignore.exists():
        content = gitignore.read_text()
        if entry not in content:
            with gitignore.open("a") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write(f"\n# dbt-plan snapshots (ephemeral, do not commit)\n{entry}\n")
            print(f"Added {entry} to .gitignore")
    else:
        gitignore.write_text(f"# dbt-plan snapshots (ephemeral, do not commit)\n{entry}\n")
        print(f"Created .gitignore with {entry}")


def _do_stats(args: argparse.Namespace) -> None:
    """Show project analysis: materializations, schema change settings, SELECT * usage."""
    from collections import Counter

    from dbt_plan.columns import extract_columns
    from dbt_plan.manifest import load_manifest

    project_dir = Path(args.project_dir)
    target_dir = project_dir / args.target_dir
    manifest_path = Path(args.manifest if args.manifest else str(target_dir / "manifest.json"))

    if not manifest_path.exists():
        print(
            f"Error: manifest.json not found: {manifest_path}\n"
            "Run 'dbt compile' to generate it, or use --manifest to specify a custom path.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        manifest = load_manifest(manifest_path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"Error: Could not parse manifest.json: {e}", file=sys.stderr)
        sys.exit(2)

    # Count materializations and on_schema_change
    mat_counts: Counter[str] = Counter()
    osc_counts: Counter[str] = Counter()
    incremental_osc: Counter[str] = Counter()
    total = 0

    for nid, node in (manifest.get("nodes") or {}).items():
        if not nid.startswith("model."):
            continue
        total += 1
        config = node.get("config") or {}
        mat = config.get("materialized") or "table"
        osc = config.get("on_schema_change") or "ignore"
        mat_counts[mat] += 1
        osc_counts[osc] += 1
        if mat == "incremental":
            incremental_osc[osc] += 1

    # Count SELECT * in compiled SQL
    try:
        compiled_dir = _find_compiled_dir(target_dir)
    except ValueError:
        compiled_dir = None
    star_count = 0
    sql_count = 0
    if compiled_dir:
        dialect = getattr(args, "dialect", "snowflake") or "snowflake"
        for sql_file in compiled_dir.rglob("*.sql"):
            sql_count += 1
            cols = extract_columns(sql_file.read_text(), dialect=dialect)
            if cols == ["*"]:
                star_count += 1

    # Output
    print(f"dbt-plan stats -- {total} model(s) in manifest\n")
    print("Materializations:")
    for mat, count in mat_counts.most_common():
        print(f"  {mat:20s} {count:>4}")

    print("\non_schema_change (incremental only):")
    for osc, count in incremental_osc.most_common():
        risk = "  ← dbt-plan monitors this" if osc in ("sync_all_columns", "fail") else ""
        print(f"  {osc:20s} {count:>4}{risk}")

    # Count manifest column fallback availability
    manifest_fallback = 0
    if sql_count:
        for nid, node in (manifest.get("nodes") or {}).items():
            if nid.startswith("model.") and node.get("columns"):
                manifest_fallback += 1

        pct = star_count * 100 // sql_count
        print(f"\nSELECT * usage: {star_count}/{sql_count} models ({pct}%)")
        if star_count > 0:
            print(f"  Manifest column fallback available: {manifest_fallback}/{total} models")
            remaining = star_count - min(star_count, manifest_fallback)
            if remaining:
                print(f"  Remaining without fallback: {remaining} (add column docs to resolve)")

    # Cascade risk: reuse already-computed counter instead of re-scanning manifest
    fail_chains = incremental_osc.get("fail", 0)
    if fail_chains:
        print(f"\nCascade risk: {fail_chains} incremental model(s) with on_schema_change=fail")
        print("  These will break if upstream schema changes")

    # Readiness score
    monitorable = incremental_osc.get("sync_all_columns", 0) + incremental_osc.get("fail", 0)
    safe = mat_counts.get("table", 0) + mat_counts.get("view", 0) + mat_counts.get("ephemeral", 0)
    print(f"\nCoverage: {safe + monitorable}/{total} models fully analyzed by dbt-plan")


def _do_check(args: argparse.Namespace) -> int:
    """Analyze compiled SQL changes and warn about DDL risks.

    Returns:
        Exit code: 0=safe, 1=destructive, 2=warning/error.
    """
    # Lazy imports: sqlglot and heavy modules only loaded when actually needed
    from dataclasses import replace as _replace

    from dbt_plan.columns import extract_columns
    from dbt_plan.config import Config
    from dbt_plan.diff import diff_compiled_dirs
    from dbt_plan.manifest import (
        build_node_index,
        find_downstream_batch,
        load_manifest,
    )
    from dbt_plan.predictor import (
        DDLOperation,
        Safety,
        analyze_cascade_impacts,
        predict_ddl,
    )

    project_dir = Path(args.project_dir)

    # Load config: .dbt-plan.yml → env vars → CLI flags (highest precedence)
    config = Config.load(project_dir)
    # CLI flags override config/env (getattr for backward compat with tests)
    fmt = getattr(args, "format", None)
    if fmt is None:
        fmt = config.format
    no_color = getattr(args, "no_color", False) or config.no_color
    verbose = getattr(args, "verbose", False) or config.verbose
    dialect = getattr(args, "dialect", None) or config.dialect

    def _log(msg: str) -> None:
        if verbose:
            print(f"  [verbose] {msg}", file=sys.stderr)

    _log(f"Config: dialect={dialect}, ignore={config.ignore_models}")

    target_dir = project_dir / args.target_dir
    base_dir = project_dir / Path(args.base_dir)
    manifest_path = Path(args.manifest if args.manifest else str(target_dir / "manifest.json"))

    # Validate paths
    if not base_dir.exists():
        print(
            f"Error: Base directory not found: {base_dir}. Run 'dbt-plan snapshot' first.",
            file=sys.stderr,
        )
        return 2

    # Resolve compiled SQL directories
    base_compiled = base_dir / "compiled"
    if not base_compiled.exists():
        # Backward compat: old snapshot format stored SQL directly in base_dir
        base_compiled = base_dir
        _log(f"Using legacy snapshot format: {base_dir}")

    try:
        current_compiled = _find_compiled_dir(target_dir)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    _log(f"Base compiled: {base_compiled}")
    _log(f"Current compiled: {current_compiled}")
    if current_compiled is None:
        print(
            "Error: No compiled SQL found. "
            "Run 'dbt compile' first to generate compiled SQL in the target/ directory.",
            file=sys.stderr,
        )
        return 2

    if not manifest_path.exists():
        print(
            f"Error: manifest.json not found: {manifest_path}\n"
            "Run 'dbt compile' to generate it, or use --manifest to specify a custom path.",
            file=sys.stderr,
        )
        return 2

    # 1. Diff compiled dirs
    _log(f"Manifest: {manifest_path}")
    try:
        model_diffs = diff_compiled_dirs(base_compiled, current_compiled)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    _log(f"Found {len(model_diffs)} changed model(s)")
    # Filter: --select (positive filter, like dbt --select)
    select_models = getattr(args, "select", None)
    if select_models:
        select_set = {s.strip() for s in select_models.split(",") if s.strip()}
        before_select = len(model_diffs)
        model_diffs = [d for d in model_diffs if d.model_name in select_set]
        _log(f"Selected {len(model_diffs)} model(s) matching: {select_set}")
        if before_select > 0 and not model_diffs:
            unmatched = select_set - {d.model_name for d in model_diffs}
            print(
                f"Warning: --select matched no changed models. "
                f"Filter: {', '.join(sorted(unmatched))}",
                file=sys.stderr,
            )

    # Filter ignored models from config
    if config.ignore_models:
        before = len(model_diffs)
        model_diffs = [d for d in model_diffs if d.model_name not in config.ignore_models]
        ignored = before - len(model_diffs)
        if ignored:
            _log(f"Ignored {ignored} model(s) per config: {config.ignore_models}")

    if not model_diffs:
        empty = CheckResult()
        if fmt == "json":
            print(format_json(empty))
        elif fmt == "github":
            print(format_github(empty))
        else:
            print(format_text(empty, color=not no_color))
        return 0

    # 2. Load manifests (current + base for removed model fallback)
    try:
        manifest = load_manifest(manifest_path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        # JSONDecodeError: invalid JSON; OSError: file I/O error;
        # UnicodeDecodeError: non-UTF-8 file (not a subclass of OSError)
        print(f"Error: Could not parse manifest.json: {e}", file=sys.stderr)
        return 2

    base_manifest_path = base_dir / "manifest.json"
    base_manifest = None
    if base_manifest_path.exists():
        try:
            base_manifest = load_manifest(base_manifest_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass  # base manifest is best-effort

    child_map = manifest.get("child_map") or {}
    if base_manifest:
        # Merge base child_map for removed models
        for k, v in (base_manifest.get("child_map") or {}).items():
            if k not in child_map:
                child_map[k] = v

    # Build O(1) lookup indexes instead of O(N) scan per model
    node_index = build_node_index(manifest, include_packages=config.include_packages)
    base_node_index = (
        build_node_index(base_manifest, include_packages=config.include_packages)
        if base_manifest
        else {}
    )
    _log(f"Manifest: {len(node_index)} model(s) indexed")
    if base_node_index:
        _log(f"Base manifest: {len(base_node_index)} model(s) indexed")

    # 3. For each changed model: extract columns, predict DDL
    predictions = []
    parse_failures: list[str] = []
    skipped_models: list[str] = []
    model_node_ids: dict[str, str] = {}  # model_name → node_id for batch downstream
    model_cols: dict[str, tuple[list[str] | None, list[str] | None]] = {}  # for cascade

    for diff in model_diffs:
        # O(1) lookup via index instead of O(N) scan
        node = node_index.get(diff.model_name)
        if node is None:
            node = base_node_index.get(diff.model_name)
        if node is None:
            skipped_models.append(diff.model_name)
            _log(f"SKIP {diff.model_name}: not found in any manifest")
            continue

        _log(
            f"{diff.status.upper()} {diff.model_name}: "
            f"{node.materialization}, on_schema_change={node.on_schema_change}"
        )
        base_cols = None
        current_cols = None
        if diff.base_sql is not None:
            base_cols = extract_columns(diff.base_sql, dialect=dialect)
        elif diff.base_path:
            base_cols = extract_columns(diff.base_path.read_text(), dialect=dialect)
        if diff.current_sql is not None:
            current_cols = extract_columns(diff.current_sql, dialect=dialect)
        elif diff.current_path:
            current_cols = extract_columns(diff.current_path.read_text(), dialect=dialect)

        # Fallback: use manifest columns when SELECT * detected
        base_node = base_node_index.get(diff.model_name)
        if base_cols == ["*"] and base_node and base_node.columns:
            base_cols = list(base_node.columns)
            _log(f"  base_cols fallback from manifest: {len(base_cols)} columns")
        if current_cols == ["*"] and node.columns:
            current_cols = list(node.columns)
            _log(f"  current_cols fallback from manifest: {len(current_cols)} columns")

        _log(f"  base_cols={base_cols}")
        _log(f"  current_cols={current_cols}")

        # Track parse failures only for models where it matters
        # (table/view are always safe via CREATE OR REPLACE, so parse failure is irrelevant)
        if (
            diff.status == "modified"
            and node.materialization not in ("table", "view")
            and (base_cols is None or current_cols is None)
        ):
            parse_failures.append(diff.model_name)
            if base_cols == ["*"] or current_cols == ["*"]:
                _log(
                    "  SELECT * detected — cannot diff columns. "
                    "Add explicit column list or use ignore_models in .dbt-plan.yml"
                )

        prediction = predict_ddl(
            model_name=diff.model_name,
            materialization=node.materialization,
            on_schema_change=node.on_schema_change,
            base_columns=base_cols,
            current_columns=current_cols,
            status=diff.status,
        )

        # Detect materialization or on_schema_change config changes
        if base_node and diff.status == "modified":
            extra_ops: list[DDLOperation] = []
            config_safety = prediction.safety

            if base_node.materialization != node.materialization:
                extra_ops.append(
                    DDLOperation(
                        f"MATERIALIZATION CHANGED: "
                        f"{base_node.materialization} -> {node.materialization}"
                    )
                )
                config_safety = Safety.WARNING
                _log(
                    f"  Config change: materialization "
                    f"{base_node.materialization} -> {node.materialization}"
                )

            base_osc = base_node.on_schema_change or "ignore"
            curr_osc = node.on_schema_change or "ignore"
            if base_osc != curr_osc:
                extra_ops.append(
                    DDLOperation(f"on_schema_change CHANGED: {base_osc} -> {curr_osc}")
                )
                # ignore -> sync_all_columns is especially dangerous
                if curr_osc == "sync_all_columns" and base_osc == "ignore":
                    config_safety = Safety.WARNING
                _log(f"  Config change: on_schema_change {base_osc} -> {curr_osc}")

            if extra_ops:
                # Use the higher severity between config change and DDL prediction
                _severity = {Safety.SAFE: 0, Safety.WARNING: 1, Safety.DESTRUCTIVE: 2}
                final_safety = (
                    config_safety
                    if _severity[config_safety] > _severity[prediction.safety]
                    else prediction.safety
                )
                prediction = _replace(
                    prediction,
                    operations=extra_ops + list(prediction.operations),
                    safety=final_safety,
                )

        predictions.append(prediction)
        model_node_ids[diff.model_name] = node.node_id
        model_cols[diff.model_name] = (base_cols, current_cols)

    # 3b. Batch downstream computation (memoized, avoids redundant BFS)
    all_downstream = find_downstream_batch(list(model_node_ids.values()), child_map)

    # 3c. Build compiled SQL index once (O(1) lookup instead of rglob per downstream)
    compiled_sql_index: dict[str, Path] = {}
    if current_compiled:
        for sql_file in current_compiled.rglob("*.sql"):
            compiled_sql_index[sql_file.stem] = sql_file

    # 3d. Cascade impact analysis (extracted to predictor module)
    predictions, downstream_map = analyze_cascade_impacts(
        predictions=predictions,
        model_node_ids=model_node_ids,
        model_cols=model_cols,
        all_downstream=all_downstream,
        node_index=node_index,
        base_node_index=base_node_index,
        compiled_sql_index=compiled_sql_index,
    )
    for pred in predictions:
        if pred.downstream_impacts:
            _log(f"  Cascade impacts for {pred.model_name}: {len(pred.downstream_impacts)}")

    # 4. Format output
    check_result = CheckResult(predictions, downstream_map, parse_failures, skipped_models)
    if fmt == "json":
        print(format_json(check_result))
    elif fmt == "github":
        print(format_github(check_result))
    else:
        print(format_text(check_result, color=not no_color))

    # 5. Exit code
    if any(p.safety == Safety.DESTRUCTIVE for p in predictions):
        return 1
    if any(p.safety == Safety.WARNING for p in predictions):
        return config.warning_exit_code
    if parse_failures:
        return config.warning_exit_code
    return 0


_CI_WORKFLOW = """\
name: dbt-plan
on:
  pull_request:
    paths: ['models/**', 'macros/**', 'dbt_project.yml']

concurrency:
  group: dbt-plan-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  plan:
    name: DDL Impact Check
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }

      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      - name: Install
        run: |
          pip install uv && uv sync
          pip install dbt-plan

      - name: Snapshot base
        run: |
          git checkout ${{ github.event.pull_request.base.sha }}
          dbt compile && dbt-plan snapshot

      - name: Check current
        run: |
          git checkout ${{ github.event.pull_request.head.sha }}
          dbt compile
          dbt-plan check --format github >> $GITHUB_STEP_SUMMARY

      - name: Gate
        run: dbt-plan check
"""


def _do_ci_setup(args: argparse.Namespace) -> None:
    """Generate a GitHub Actions workflow for dbt-plan CI."""
    project_dir = Path(args.project_dir)
    workflows_dir = project_dir / ".github" / "workflows"
    workflow_path = workflows_dir / "dbt-plan.yml"

    if workflow_path.exists():
        print(
            f"Workflow already exists: {workflow_path}\n"
            "Edit it directly, or delete it and re-run 'dbt-plan ci-setup'.",
            file=sys.stderr,
        )
        sys.exit(2)

    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(_CI_WORKFLOW)
    print(f"Created {workflow_path}")
    print("Push this file to enable dbt-plan on every PR.")


def _do_run(args: argparse.Namespace) -> int:
    """One-command check: compile baseline, compile current, run check.

    Requires dbt to be installed. Uses git to get the baseline state.
    The compile command can be customized via:
      --compile-command flag, DBT_PLAN_COMPILE_COMMAND env var, or
      compile_command in .dbt-plan.yml (default: "dbt compile").

    Returns:
        Exit code from check (0=safe, 1=destructive, 2=warning/error).
    """
    import shlex
    import subprocess

    from dbt_plan.config import Config

    project_dir = Path(args.project_dir)
    fmt = getattr(args, "format", None) or "text"
    no_color = getattr(args, "no_color", False)
    verbose = getattr(args, "verbose", False)
    dialect = getattr(args, "dialect", None)
    select = getattr(args, "select", None)

    # Resolve compile command: CLI flag > config (env + file)
    config = Config.load(project_dir)
    compile_command = getattr(args, "compile_command", None) or config.compile_command

    def _log(msg: str) -> None:
        print(f"  [dbt-plan run] {msg}", file=sys.stderr)

    # 0. Verify compile command is available
    try:
        compile_argv = shlex.split(compile_command)
    except ValueError as e:
        print(
            f"Error: invalid compile command: {e}\n"
            f"  Command: {compile_command}\n"
            "  Check for unmatched quotes in compile_command.",
            file=sys.stderr,
        )
        return 2
    if not compile_argv:
        print(
            "Error: compile command is empty.\n"
            "  Set compile_command in .dbt-plan.yml or DBT_PLAN_COMPILE_COMMAND env var.\n"
            "  Examples: 'dbt compile', 'uv run dbt compile', 'poetry run dbt compile'",
            file=sys.stderr,
        )
        return 2
    try:
        result = subprocess.run([compile_argv[0], "--version"], capture_output=True)
    except FileNotFoundError:
        result = None
    if result is None or result.returncode != 0:
        print(
            f"Error: '{compile_argv[0]}' not found.\n"
            f"  Compile command: {compile_command}\n"
            "  Set compile_command in .dbt-plan.yml or DBT_PLAN_COMPILE_COMMAND env var.\n"
            "  Examples: 'uv run dbt compile', 'poetry run dbt compile'",
            file=sys.stderr,
        )
        return 2

    _log(f"Compile command: {compile_command}")

    # 1. Check for uncommitted changes
    try:
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
        )
    except FileNotFoundError:
        print(
            "Error: git not found. The 'run' command requires git to manage baseline snapshots.\n"
            "Install git or use the manual workflow: dbt compile → dbt-plan snapshot → dbt-plan check",
            file=sys.stderr,
        )
        return 2
    if git_status.returncode != 0:
        print(
            "Error: not a git repository. The 'run' command uses git stash for baseline.\n"
            "Use the manual workflow instead: dbt compile → dbt-plan snapshot → dbt-plan check",
            file=sys.stderr,
        )
        return 2
    has_changes = bool(git_status.stdout.strip())

    # 2. Stash uncommitted changes (we need clean state for baseline)
    if has_changes:
        _log("Stashing uncommitted changes...")
        subprocess.run(
            ["git", "stash", "push", "-m", "dbt-plan-run-temp", "--include-untracked"],
            capture_output=True,
            cwd=str(project_dir),
        )

    # 3. Compile baseline + snapshot
    _log("Compiling baseline (current branch HEAD)...")
    compile_base = subprocess.run(
        compile_argv,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )
    if compile_base.returncode != 0:
        print(
            f"Error: compile failed for baseline:\n{compile_base.stderr}",
            file=sys.stderr,
        )
        if has_changes:
            subprocess.run(["git", "stash", "pop"], capture_output=True, cwd=str(project_dir))
        return 2

    _log("Saving snapshot...")
    snapshot_args = argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
    )
    _do_snapshot(snapshot_args)

    # 4. Restore stashed changes
    if has_changes:
        _log("Restoring your changes...")
        subprocess.run(["git", "stash", "pop"], capture_output=True, cwd=str(project_dir))

    # 5. Compile current state
    _log("Compiling current state...")
    compile_curr = subprocess.run(
        compile_argv,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )
    if compile_curr.returncode != 0:
        print(
            f"Error: compile failed for current:\n{compile_curr.stderr}",
            file=sys.stderr,
        )
        return 2

    # 6. Run check
    _log("Checking for DDL risks...")
    check_args = argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=no_color,
        verbose=verbose,
        dialect=dialect,
        select=select,
    )
    return _do_check(check_args)


def main() -> None:
    from dbt_plan import __version__

    parser = argparse.ArgumentParser(
        prog="dbt-plan",
        description="Static analysis tool that warns about risky DDL changes before dbt run",
        epilog=(
            "quick start:\n"
            "  dbt-plan run               # one command: compile + snapshot + check\n"
            "\n"
            "manual workflow:\n"
            "  dbt compile\n"
            "  dbt-plan snapshot          # save baseline\n"
            "  # ... edit models ...\n"
            "  dbt compile\n"
            "  dbt-plan check             # see what changed\n"
            "\n"
            "ci setup:\n"
            "  dbt-plan ci-setup          # generate GitHub Actions workflow\n"
            "\n"
            "exit codes:\n"
            "  0  all changes are safe\n"
            "  1  destructive changes detected\n"
            "  2  warning or error (parse failure, missing files)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # snapshot
    snap = subparsers.add_parser("snapshot", help="Save current compiled state as baseline")
    snap.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")
    snap.add_argument(
        "--target-dir", default="target", help="dbt target directory (default: target)"
    )

    # check
    check = subparsers.add_parser(
        "check", help="Analyze compiled SQL changes and warn about risks"
    )
    check.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")
    check.add_argument(
        "--target-dir", default="target", help="dbt target directory (default: target)"
    )
    check.add_argument(
        "--base-dir",
        default=".dbt-plan/base",
        help="Baseline snapshot directory (default: .dbt-plan/base)",
    )
    check.add_argument(
        "--manifest",
        default=None,
        help="Path to manifest.json (default: {target-dir}/manifest.json)",
    )
    check.add_argument(
        "--format",
        choices=["text", "github", "json"],
        default=None,
        help="Output format: text (terminal), github (markdown), json (programmatic)",
    )
    check.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable colored output (auto-disabled when piped)",
    )
    check.add_argument(
        "-s",
        "--select",
        default=None,
        help="Only check specific models (comma-separated, like dbt --select)",
    )
    check.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed processing info on stderr (directories, columns, skips)",
    )
    check.add_argument(
        "--dialect",
        default=None,
        help="SQL dialect for parsing (default: snowflake). Supports any sqlglot dialect.",
    )

    # init
    init_cmd = subparsers.add_parser("init", help="Generate a sample .dbt-plan.yml config file")
    init_cmd.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")

    # stats
    stats_cmd = subparsers.add_parser(
        "stats", help="Analyze project: materializations, schema change settings, SELECT * usage"
    )
    stats_cmd.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")
    stats_cmd.add_argument(
        "--target-dir", default="target", help="dbt target directory (default: target)"
    )
    stats_cmd.add_argument(
        "--manifest",
        default=None,
        help="Path to manifest.json (default: {target-dir}/manifest.json)",
    )
    stats_cmd.add_argument(
        "--dialect",
        default=None,
        help="SQL dialect for parsing (default: snowflake). Supports any sqlglot dialect.",
    )

    # ci-setup
    ci_cmd = subparsers.add_parser(
        "ci-setup", help="Generate GitHub Actions workflow for dbt-plan CI"
    )
    ci_cmd.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")

    # run
    run_cmd = subparsers.add_parser(
        "run",
        help="One-command check: compile baseline → compile current → check (requires dbt)",
    )
    run_cmd.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")
    run_cmd.add_argument(
        "--format",
        choices=["text", "github", "json"],
        default=None,
        help="Output format (default: text)",
    )
    run_cmd.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable colored output",
    )
    run_cmd.add_argument(
        "-s",
        "--select",
        default=None,
        help="Only check specific models (comma-separated)",
    )
    run_cmd.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed processing info",
    )
    run_cmd.add_argument(
        "--dialect",
        default=None,
        help="SQL dialect for parsing (default: snowflake)",
    )
    run_cmd.add_argument(
        "--compile-command",
        default=None,
        help="Command to compile dbt project (default: 'dbt compile'). "
        "Examples: 'uv run dbt compile', 'poetry run dbt compile'",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "init":
        _do_init(args)
    elif args.command == "snapshot":
        _do_snapshot(args)
    elif args.command == "check":
        sys.exit(_do_check(args))
    elif args.command == "stats":
        _do_stats(args)
    elif args.command == "ci-setup":
        _do_ci_setup(args)
    elif args.command == "run":
        sys.exit(_do_run(args))


if __name__ == "__main__":
    main()
