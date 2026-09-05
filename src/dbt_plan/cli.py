"""CLI entry point for dbt-plan — static analysis tool for dbt DDL risk warnings."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from dbt_plan.formatter import CheckResult, format_github, format_json, format_text


def _configure_output_streams() -> None:
    """Write CLI output as UTF-8 even when Windows defaults to a legacy code page.

    The GitHub formatter intentionally emits Unicode status icons and argparse's
    help includes arrows. Leaving the streams at cp1252 makes either path crash
    before the command can report its result.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _root_project_name(target_dir: Path) -> str | None:
    """Name of the project that produced this target/, per its own manifest.

    Read only when `target/compiled/` holds more than one project directory, so
    the ordinary case never pays for parsing the largest file dbt writes.

    This is the same field `build_node_index` uses to keep package models out of
    the index. The two sides of dbt-plan should agree on which project is yours.
    """
    try:
        with (target_dir / "manifest.json").open("r", encoding="utf-8") as f:
            metadata = json.load(f).get("metadata") or {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    name = metadata.get("project_name")
    return name if isinstance(name, str) and name else None


def _find_compiled_dir(target_dir: Path) -> Path | None:
    """Find compiled SQL models directory inside target/.

    Supports two dbt layouts:
    1. target/compiled/{project_name}/models/  (standard)
    2. target/compiled/models/  (flat, some dbt versions/configs)

    dbt compiles every installed package into its own directory here, so a
    project depending on a package that ships models has several. The root
    project is identified from the manifest; only a genuinely undecidable case
    raises.
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
        # A package that ships models, almost always. The manifest names the
        # project that owns this target directory; anything else here is a
        # dependency and is not ours to check.
        root_project = _root_project_name(target_dir)
        for candidate in candidates:
            if candidate.parent.name == root_project:
                return candidate

        project_names = [c.parent.name for c in candidates]
        # Deliberately does not suggest --project-dir: these directories are
        # inside that project's target/, so passing it changes nothing.
        raise ValueError(
            f"Multiple dbt projects found in {compiled}: {project_names}. "
            f"Could not tell which is yours: {target_dir / 'manifest.json'} "
            + (
                f"names project '{root_project}', which is not among them."
                if root_project
                else "is missing or unreadable. Run 'dbt compile' to regenerate it."
            )
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
        # is_relative_to includes equality; require a strict child before deleting.
        if resolved_base == resolved_project or not resolved_base.is_relative_to(resolved_project):
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

# Models whose destructive change has been reviewed and accepted.
# Unlike ignore_models these are still reported in full -- they just stop
# failing the build. Name each model; there is deliberately no "all".
# acknowledge_models: [int_order_enriched]

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

    config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
    print(f"Created {config_path}")

    # Add .dbt-plan/ to .gitignore if not already there
    gitignore = project_dir / ".gitignore"
    entry = ".dbt-plan/"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if entry not in content:
            with gitignore.open("a", encoding="utf-8") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write(f"\n# dbt-plan snapshots (ephemeral, do not commit)\n{entry}\n")
            print(f"Added {entry} to .gitignore")
    else:
        gitignore.write_text(
            f"# dbt-plan snapshots (ephemeral, do not commit)\n{entry}\n", encoding="utf-8"
        )
        print(f"Created .gitignore with {entry}")


def _do_stats(args: argparse.Namespace) -> None:
    """Show project analysis: materializations, schema change settings, SELECT * usage."""
    from collections import Counter

    from dbt_plan.columns import extract_columns
    from dbt_plan.diff import iter_model_sql
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
        for sql_file in iter_model_sql(compiled_dir):
            sql_count += 1
            cols = extract_columns(sql_file.read_text(encoding="utf-8"), dialect=dialect)
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


def _exit_code_for(result: CheckResult, warning_exit_code: int) -> int:
    """Map a check result to a process exit code.

    Acknowledged models are reported but excluded from the verdict -- that is
    the entire point of acknowledging one. Everything else still counts, so
    acknowledging one model never excuses another model's risk, an unrelated
    warning, or a parse failure.
    """
    from dbt_plan.predictor import Safety

    for pred in result.predictions:
        if result.is_acknowledged(pred):
            continue
        if pred.safety == Safety.DESTRUCTIVE:
            return 1
    if any(
        p.safety == Safety.WARNING and not result.is_acknowledged(p) for p in result.predictions
    ):
        return warning_exit_code
    if result.parse_failures:
        return warning_exit_code
    # Models dbt-plan could not examine are "unknown", not "safe", and both of
    # these were previously computed and then dropped on the floor: a model in
    # the diff but absent from the manifest, and a model in the manifest that
    # the compile never produced. Either way the tool did not look, so it must
    # not answer "safe". Ranked below destructive so a real finding still exits 1.
    if result.skipped_models or result.uncompiled_models:
        return warning_exit_code
    return 0


# dbt_utils.star() introspects the warehouse at compile time. Against a schema
# where the relation does not exist yet -- a fresh CI run -- it returns nothing
# and emits a bare `*` with this comment. Matching two phrases rather than one
# keeps it from firing on ordinary SQL that happens to mention columns.
_STAR_MACRO_MARKERS = ("No columns were returned", "star is only output during")


def _star_macro_degraded(sql: str) -> bool:
    """True when a `*` in this SQL came from dbt_utils.star() finding nothing."""
    return all(marker in sql for marker in _STAR_MACRO_MARKERS)


def _build_relation_index(manifest: dict, node_index: dict) -> dict[str, str]:
    """Map the relation a model writes -> the key its compiled SQL is indexed under.

    `select * from {{ ref(x) }}` compiles to the physical relation, not the model
    name, so matching needs the manifest's `relation_name`. The bare name is
    registered too: dbt model names are unique across a project, so it is an
    unambiguous fallback when a manifest predates `relation_name`. For a versioned
    model both spellings point at the same file -- `fct_orders` is the name two
    versions share, `fct_orders_v2` is the one that was written.
    """
    from dbt_plan.manifest import model_key

    index: dict[str, str] = {}
    for node_id, node in (manifest.get("nodes") or {}).items():
        if not node_id.startswith("model."):
            continue
        name = node.get("name")
        path = node.get("path")
        key = Path(path).stem if path else model_key(node_id)
        if not name or key not in node_index:
            continue
        relation = node.get("relation_name")
        if relation:
            index[relation.replace('"', "").replace("`", "").lower()] = key
        index.setdefault(key.lower(), key)
        index.setdefault(name.lower(), key)
    return index


def _make_table_resolver(compiled_dir, relation_index: dict[str, str], dialect: str):
    """Resolve a relation to the columns of the model that produces it.

    dbt-plan already holds every model's compiled SQL and a manifest naming the
    relation each one writes, so `select * from {{ ref(x) }}` is answerable from
    the project itself -- no warehouse, which is the whole reason this tool can
    run on a fork's pull request.

    Memoized per model, and guarded against a chain that loops back on itself.
    A referenced model that is itself unreadable resolves to None, so the refusal
    propagates instead of turning into a shorter, wrong column list.
    """
    from dbt_plan.columns import extract_columns
    from dbt_plan.diff import iter_model_sql

    sql_by_model = {f.stem: f for f in iter_model_sql(compiled_dir)} if compiled_dir else {}
    cache: dict[str, list[str] | None] = {}
    in_progress: set[str] = set()

    def resolve(key: str) -> list[str] | None:
        model = relation_index.get(key)
        if model is None or model in in_progress:
            return None
        if model in cache:
            return cache[model]
        path = sql_by_model.get(model)
        if path is None:
            return None

        in_progress.add(model)
        try:
            columns = extract_columns(
                path.read_text(encoding="utf-8"), dialect=dialect, table_columns=resolve
            )
        except OSError:
            columns = None
        finally:
            in_progress.discard(model)

        unresolved = columns is None or not columns or columns[0].startswith("*")
        cache[model] = None if unresolved else columns
        return cache[model]

    return resolve


def _do_check(args: argparse.Namespace) -> int:
    """Analyze compiled SQL changes and warn about DDL risks.

    Returns:
        Exit code: 0=safe, 1=destructive, 2=warning/error.
    """
    # Lazy imports: sqlglot and heavy modules only loaded when actually needed
    from dataclasses import replace as _replace

    from dbt_plan.columns import extract_cast_types, extract_columns
    from dbt_plan.config import Config
    from dbt_plan.diff import diff_compiled_dirs, iter_model_sql, iter_non_model_sql
    from dbt_plan.manifest import (
        build_data_test_index,
        build_exposure_index,
        build_node_index,
        build_unit_test_index,
        find_downstream_batch,
        load_manifest,
    )
    from dbt_plan.predictor import (
        DDLOperation,
        Safety,
        analyze_cascade_impacts,
        apply_contract,
        attach_downstream_exposures,
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
    if ack_flag := getattr(args, "acknowledge", None):
        config.acknowledge_models = [m.strip() for m in ack_flag.split(",") if m.strip()]

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
    node_index = build_node_index(manifest)
    base_node_index = build_node_index(base_manifest) if base_manifest else {}
    # `select * from {{ ref(x) }}` names a relation, not a model. These let the
    # column resolver follow that reference into the other model's compiled SQL.
    current_table_columns = _make_table_resolver(
        current_compiled, _build_relation_index(manifest, node_index), dialect
    )
    base_table_columns = _make_table_resolver(
        base_compiled,
        _build_relation_index(base_manifest or manifest, base_node_index or node_index),
        dialect,
    )

    _log(f"Manifest: {len(node_index)} model(s) indexed")
    if base_node_index:
        _log(f"Base manifest: {len(base_node_index)} model(s) indexed")

    # Models the manifest declares that the compile never produced. dbt Core
    # aborted on the first failure, so a partial target/ was exceptional; the
    # Fusion engine keeps compiling the rest of the DAG after a node fails, which
    # makes it ordinary. It matters because a model missing from *both* compiled
    # directories yields no diff entry, so it is never examined -- and with
    # nothing else changed that used to print "no model changes" and exit 0.
    compiled_stems = {f.stem for f in iter_model_sql(current_compiled)}
    uncompiled_models = sorted(
        name
        for name in node_index
        if name not in compiled_stems and name not in config.ignore_models
    )
    if uncompiled_models:
        _log(f"Uncompiled: {len(uncompiled_models)} manifest model(s) have no compiled SQL")

    if not model_diffs:
        empty = CheckResult(uncompiled_models=uncompiled_models)
        if fmt == "json":
            print(format_json(empty))
        elif fmt == "github":
            print(format_github(empty))
        else:
            print(format_text(empty, color=not no_color))
        return _exit_code_for(empty, config.warning_exit_code)

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
            base_cols = extract_columns(
                diff.base_sql, dialect=dialect, table_columns=base_table_columns
            )
        elif diff.base_path:
            base_cols = extract_columns(
                diff.base_path.read_text(encoding="utf-8"),
                dialect=dialect,
                table_columns=base_table_columns,
            )
        if diff.current_sql is not None:
            current_cols = extract_columns(
                diff.current_sql, dialect=dialect, table_columns=current_table_columns
            )
        elif diff.current_path:
            current_cols = extract_columns(
                diff.current_path.read_text(encoding="utf-8"),
                dialect=dialect,
                table_columns=current_table_columns,
            )

        # Fallback: use manifest columns when SELECT * detected
        base_node = base_node_index.get(diff.model_name)
        used_manifest_columns = False
        if base_cols == ["*"] and base_node and base_node.columns:
            base_cols = list(base_node.columns)
            used_manifest_columns = True
            _log(f"  base_cols fallback from manifest: {len(base_cols)} columns")
        if current_cols == ["*"] and node.columns:
            current_cols = list(node.columns)
            used_manifest_columns = True
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

        # An enforced contract is checked against the SQL, not against the base
        # revision, so a removed model has nothing left to check.
        if diff.status != "removed":
            prediction = apply_contract(prediction, node, current_cols)

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

        # A bare `*` left behind by dbt_utils.star() is not the user's SELECT --
        # it is the macro finding no relation to introspect. Saying so is the
        # difference between "fix your compile target" and "this tool is noisy,
        # add it to ignore_models".
        if prediction.safety == Safety.WARNING and any(
            _star_macro_degraded(text)
            for text in (
                diff.current_sql
                or (diff.current_path.read_text(encoding="utf-8") if diff.current_path else ""),
                diff.base_sql
                or (diff.base_path.read_text(encoding="utf-8") if diff.base_path else ""),
            )
        ):
            prediction = _replace(
                prediction,
                operations=[
                    DDLOperation(
                        "SELECT * came from dbt_utils.star() returning nothing -- the "
                        "relation did not exist when this compiled. Compile against an "
                        "environment where it does, or list the columns explicitly."
                    ),
                    *prediction.operations,
                ],
            )

        # A column's type is invisible in compiled SQL unless it is cast
        # explicitly. But when both revisions cast the same column and the two
        # casts differ, that comparison is compiled SQL against compiled SQL --
        # no warehouse involved, which is the objection that kept this out. dbt
        # acts on it: its docs describe sync_all_columns as "inclusive of data
        # type changes". Whether a given change loses data (VARCHAR -> INT) or is
        # a harmless widening (INT -> BIGINT) is not decidable from the SQL, so
        # the verdict is review, never destructive and never safe.
        if diff.status == "modified" and node.materialization not in (
            "table",
            "view",
            "ephemeral",
        ):
            base_sql_text = diff.base_sql
            if base_sql_text is None and diff.base_path:
                base_sql_text = diff.base_path.read_text(encoding="utf-8")
            current_sql_text = diff.current_sql
            if current_sql_text is None and diff.current_path:
                current_sql_text = diff.current_path.read_text(encoding="utf-8")

            base_casts = (
                extract_cast_types(base_sql_text, dialect=dialect) if base_sql_text else None
            )
            current_casts = (
                extract_cast_types(current_sql_text, dialect=dialect) if current_sql_text else None
            )
            if base_casts and current_casts:
                type_ops = [
                    DDLOperation(f"TYPE CHANGED: {before} -> {current_casts[col]}", col)
                    for col, before in sorted(base_casts.items())
                    if col in current_casts and current_casts[col] != before
                ]
                if type_ops:
                    prediction = _replace(
                        prediction,
                        operations=[*type_ops, *prediction.operations],
                        safety=(
                            prediction.safety
                            if prediction.safety == Safety.DESTRUCTIVE
                            else Safety.WARNING
                        ),
                    )
                    _log(f"  Cast type change on {len(type_ops)} column(s)")

        # A clean bill that rests on the manifest fallback is not evidence of
        # safety. schema.yml conventionally documents only the columns you test,
        # and the same incomplete list is substituted on *both* sides -- so an
        # empty diff can equally mean "nothing changed" or "we never looked".
        # Only SAFE is escalated; a real finding still stands on its own. table
        # and view are rebuilt by CREATE OR REPLACE whatever the columns are, so
        # escalating those would be noise with nothing behind it.
        if (
            used_manifest_columns
            and prediction.safety == Safety.SAFE
            and node.materialization not in ("table", "view", "ephemeral")
        ):
            prediction = _replace(
                prediction,
                operations=[
                    DDLOperation("REVIEW REQUIRED (columns came from the manifest, not the SQL)"),
                    *prediction.operations,
                ],
                safety=Safety.WARNING,
            )
            _log("  Verdict rests on manifest columns -- escalated to review required")

        predictions.append(prediction)
        model_node_ids[diff.model_name] = node.node_id
        model_cols[diff.model_name] = (base_cols, current_cols)

    # 3b. Batch downstream computation (memoized, avoids redundant BFS)
    all_downstream = find_downstream_batch(list(model_node_ids.values()), child_map)

    # 3c. Build compiled SQL index once (O(1) lookup instead of rglob per downstream)
    compiled_sql_index: dict[str, Path] = {}
    test_sql_index: dict[str, Path] = {}
    if current_compiled:
        for sql_file in iter_model_sql(current_compiled):
            compiled_sql_index[sql_file.stem] = sql_file
        # Singular tests compile outside the models tree, so they are indexed from
        # the project root of target/compiled rather than from current_compiled.
        for sql_file in iter_non_model_sql(current_compiled.parent, current_compiled.name):
            test_sql_index[sql_file.stem] = sql_file

    # 3d. Cascade impact analysis (extracted to predictor module)
    predictions, downstream_map = analyze_cascade_impacts(
        predictions=predictions,
        model_node_ids=model_node_ids,
        model_cols=model_cols,
        all_downstream=all_downstream,
        node_index=node_index,
        base_node_index=base_node_index,
        compiled_sql_index=compiled_sql_index,
        child_map=child_map,
        unit_test_index=build_unit_test_index(manifest),
        data_test_index=build_data_test_index(manifest),
        test_sql_index=test_sql_index,
        base_columns_of=base_table_columns,
        current_columns_of=current_table_columns,
    )
    predictions = attach_downstream_exposures(
        predictions=predictions,
        model_node_ids=model_node_ids,
        all_downstream=all_downstream,
        child_map=child_map,
        exposure_index=build_exposure_index(manifest),
    )
    for pred in predictions:
        if pred.downstream_impacts:
            _log(f"  Cascade impacts for {pred.model_name}: {len(pred.downstream_impacts)}")
        if pred.downstream_exposures:
            _log(f"  Downstream exposures for {pred.model_name}: {len(pred.downstream_exposures)}")

    # 4. Format output
    check_result = CheckResult(
        predictions,
        downstream_map,
        parse_failures,
        skipped_models,
        uncompiled_models,
        acknowledge_models=config.acknowledge_models,
    )
    if fmt == "json":
        print(format_json(check_result))
    elif fmt == "github":
        print(format_github(check_result))
    else:
        print(format_text(check_result, color=not no_color))

    # 5. Exit code
    return _exit_code_for(check_result, config.warning_exit_code)


_CI_WORKFLOW = """\
name: dbt-plan
# dbt-plan itself never connects to your warehouse, but `dbt compile` does.
# That compile runs Jinja and macros authored in the pull request, so treat it
# as running untrusted code with credentials attached:
#
#   * Keep the `pull_request` trigger. NEVER change it to `pull_request_target`
#     -- that hands your warehouse secrets to code from any fork.
#   * Give the CI account the least privilege that still compiles: log in plus
#     USAGE on the warehouse. `dbt compile` reads no tables unless your macros
#     introspect (run_query / get_column_values / get_columns_in_relation).
#   * Fork PRs receive no secrets by design; the Preflight step says so plainly
#     instead of failing later with a confusing driver error.
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
    # Results go to the step summary, which needs no token scope.
    permissions:
      contents: read
    # Optional: gate secret access behind a protected environment.
    # environment: dbt-plan-ci

    # Credentials for `dbt compile`. profiles.yml should read these via
    # env_var(). Declaring them here -- rather than interpolating
    # ${{ secrets.* }} inside a `run:` block -- keeps the values out of the
    # command line and out of any script the PR could influence.
    # Non-secret settings (account, user, role) are better kept in `vars`.
    env:
      # Uncomment if profiles.yml lives in the repo root rather than ~/.dbt/.
      # DBT_PROFILES_DIR: ${{ github.workspace }}
      SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
      SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
      # Key-pair auth; prefer it over a password. Paste the full PEM.
      SNOWFLAKE_PRIVATE_KEY: ${{ secrets.SNOWFLAKE_PRIVATE_KEY }}
      SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: ${{ secrets.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE }}
      # Postgres / Redshift replace the SNOWFLAKE_* lines above with just:
      # PGPASSWORD: ${{ secrets.PGPASSWORD }}
      # BigQuery needs no variable here. Add a google-github-actions/auth step
      # for keyless OIDC, which also requires id-token: write on the job.

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          # dbt compile runs PR-authored code -- leave no git token on disk.
          persist-credentials: false

      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      # Fail fast and legibly. Change the variable below to whichever one your
      # profiles.yml requires -- or drop this step entirely if you authenticate
      # without a secret, as BigQuery OIDC does.
      - name: Preflight
        run: |
          if [ -z "$SNOWFLAKE_PRIVATE_KEY" ]; then
            echo "::error::dbt compile needs warehouse credentials, but none are set."
            echo "Add them under Settings > Secrets and variables > Actions."
            echo "Fork PRs never receive secrets - run this from a branch in this repo."
            exit 1
          fi

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
    workflow_path.write_text(_CI_WORKFLOW, encoding="utf-8")
    print(f"Created {workflow_path}")
    print("Push this file to enable dbt-plan on every PR.")


_AGENTS_MARKER = "<!-- dbt-plan -->"

_AGENTS_GUIDE = """\
<!-- dbt-plan -->
## dbt-plan

This project uses [dbt-plan](https://github.com/PresentJay/dbt-plan) to catch destructive schema
changes before `dbt run` executes them.

### When to run it

After changing any model SQL, materialization, or `on_schema_change` setting, and before
opening or approving a pull request:

```bash
dbt-plan run
```

That compiles the baseline and the current state, then compares them. It shells out to
`dbt compile`, so it needs the same warehouse credentials `dbt compile` normally needs.
dbt-plan itself never connects to the warehouse and never runs SQL.

### Reading the result

- `0` — every change is safe.
- `1` — destructive: a column is being dropped, or a model removed.
- `2` — dbt-plan could not answer. Either it could not read a model, or it never saw one.
  A human has to look.

A warning is not automatically a blocker; it means "explain this before merging."

`2` covers several situations, and they need different responses:

| Message | What it means | What to do |
|---|---|---|
| "review required" | a model's columns could not be extracted | read that model's SQL yourself |
| "columns came from the manifest, not the SQL" | the SQL was `SELECT *`, so documented columns stood in for it — on both sides, which is why the diff came out empty | give the model an explicit column list, or document its columns fully in `schema.yml` |
| "TYPE CHANGED" | an explicit `CAST` on a column changed between revisions | decide whether the new type can hold the existing data |
| "UNKNOWN materialization" | a materialization dbt-plan has no rule for, with no `on_schema_change` set | set `on_schema_change` if your materialization honours it; otherwise review by hand |
| "not found in manifest" | the compiled SQL and the manifest disagree | the manifest is stale — recompile |
| "the compile is incomplete" | a model in the manifest produced no compiled SQL | **fix the compile, then rerun** |

Two of these deserve extra care. The dbt Fusion engine keeps compiling the rest of the DAG
after a node fails, so a broken model leaves a partial `target/` behind while every other
model looks fine — dbt-plan cannot judge what it never received. And a verdict built from
manifest columns is not evidence: `schema.yml` usually documents only the columns you test,
and the same partial list is substituted on both sides, so "no difference" can equally mean
"nothing changed" or "nobody looked".

### What not to do

The failure this tool exists to prevent is a column disappearing without anyone noticing.
These edits will silence a real finding, and none of them makes the change safe:

- Adding the model to `ignore_models` in `.dbt-plan.yml`.
- Switching `on_schema_change` from `sync_all_columns` to `ignore`.
- Adding an uncompiled model to `ignore_models` to clear "the compile is incomplete".
  That model is the one nobody has checked. Fix the compile instead.
- Adding columns to `schema.yml` purely to make "columns came from the manifest" go away.
  Documenting the columns is right; documenting *some* of them is what caused the problem.
  Prefer giving the model an explicit column list instead of `SELECT *`.
- Setting `on_schema_change: ignore` on a custom materialization to clear "UNKNOWN
  materialization". dbt-plan takes that setting at its word, so this converts an honest
  "I do not know" into a confident "safe" without changing what the materialization does.

If a destructive change is intentional, say so in the pull request. Do not edit config to
make the warning disappear.

### Why a change is judged risky

Risk is materialization crossed with `on_schema_change`:

| Config | Result |
|---|---|
| `table` / `view` | `CREATE OR REPLACE` — safe |
| `incremental` + `ignore` | no DDL — safe |
| `incremental` + `append_new_columns` | ADD COLUMN only — safe |
| `incremental` + `fail` | run fails on schema drift — warning |
| `incremental` + `sync_all_columns` | ADD and DROP COLUMN — destructive if a column was removed |
| `snapshot` | review required — warning |
| model deleted | destructive |

When dbt-plan cannot extract columns it reports "review required" rather than "safe", and
when it never received a model at all it says so rather than staying quiet. False warnings
are acceptable here; a false all-clear is not.
"""


def _do_agent_setup(args: argparse.Namespace) -> None:
    """Write dbt-plan guidance into the project's AGENTS.md for coding agents."""
    project_dir = Path(args.project_dir)
    path = project_dir / "AGENTS.md"

    if path.exists():
        content = path.read_text(encoding="utf-8")
        if _AGENTS_MARKER in content:
            print(
                f"AGENTS.md already has a dbt-plan section: {path}\n"
                "Edit it directly, or delete that section and re-run 'dbt-plan agent-setup'.",
                file=sys.stderr,
            )
            sys.exit(2)
        with path.open("a", encoding="utf-8") as f:
            f.write(("" if content.endswith("\n") else "\n") + "\n" + _AGENTS_GUIDE)
        print(f"Appended dbt-plan section to {path}")
    else:
        path.write_text(f"# AGENTS.md\n\n{_AGENTS_GUIDE}", encoding="utf-8")
        print(f"Created {path}")
    print("Coding agents that read AGENTS.md will pick this up automatically.")


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
            encoding="utf-8",
            errors="replace",
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

    # 2-4. Borrow a clean tree for the baseline; the restore is structural.
    from dbt_plan.stash import StashError, clean_worktree

    if has_changes:
        _log("Stashing uncommitted changes...")
    try:
        with clean_worktree(project_dir, has_changes=has_changes) as stash:
            _log("Compiling baseline (current branch HEAD)...")
            compile_base = subprocess.run(
                compile_argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(project_dir),
            )
            if compile_base.returncode != 0:
                print(
                    f"Error: compile failed for baseline:\n{compile_base.stderr}",
                    file=sys.stderr,
                )
                return 2

            _log("Saving snapshot...")
            snapshot_args = argparse.Namespace(
                project_dir=str(project_dir),
                target_dir="target",
            )
            _do_snapshot(snapshot_args)
        if stash.stashed and not stash.restore_failed:
            _log("Restored your changes.")
    except StashError as e:
        print(
            "Error: could not stash your uncommitted changes, so a clean "
            f"baseline cannot be compiled:\n{e}\n"
            "  Your working tree is untouched. Commit or stash manually, or use\n"
            "  the manual workflow: dbt compile -> dbt-plan snapshot -> dbt-plan check",
            file=sys.stderr,
        )
        return 2

    if stash.restore_failed:
        return 2

    # 5. Compile current state
    _log("Compiling current state...")
    compile_curr = subprocess.run(
        compile_argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    _configure_output_streams()
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
            "project setup:\n"
            "  dbt-plan init              # generate .dbt-plan.yml\n"
            "  dbt-plan ci-setup          # generate GitHub Actions workflow\n"
            "  dbt-plan agent-setup       # tell coding agents how to use dbt-plan\n"
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
        "--acknowledge",
        default=None,
        metavar="MODELS",
        help=(
            "Comma-separated models whose destructive change has been reviewed. "
            "They are still reported, but stop failing the build. "
            "Also settable via DBT_PLAN_ACKNOWLEDGE or acknowledge_models in .dbt-plan.yml"
        ),
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

    # agent-setup
    agent_cmd = subparsers.add_parser(
        "agent-setup", help="Write dbt-plan guidance into AGENTS.md for coding agents"
    )
    agent_cmd.add_argument("--project-dir", default=".", help="dbt project directory (default: .)")

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
    elif args.command == "agent-setup":
        _do_agent_setup(args)
    elif args.command == "ci-setup":
        _do_ci_setup(args)
    elif args.command == "run":
        sys.exit(_do_run(args))


if __name__ == "__main__":
    main()
