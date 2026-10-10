"""Compiled snapshot layout versions, independent of package/inventory versions."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

LAYOUT_VERSION = 1


class SnapshotLayout(NamedTuple):
    root: Path
    model_dirs: tuple[str, ...] | None
    provenance: dict


class SnapshotLayoutError(ValueError):
    """Keep the layout problem separate from advice for an existing baseline."""

    def __init__(self, problem: str):
        self.problem = problem
        super().__init__(f"{problem}. Run 'dbt-plan snapshot' again to recreate the baseline.")


def _error(message: str) -> SnapshotLayoutError:
    return SnapshotLayoutError(message)


def manifest_path_root(declared: str) -> str:
    """Read a manifest-relative root without accepting empty or escaping paths."""
    path = PurePosixPath(declared)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in declared
        or PureWindowsPath(declared).drive
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in declared)
    ):
        raise _error(f"Invalid manifest original_file_path: {declared!r}")
    return path.parts[0]


def read_snapshot_layout(
    base: Path, current_model_dirs: tuple[str, ...], baseline_model_dirs: tuple[str, ...]
) -> SnapshotLayout:
    """Resolve a baseline without mutating it; explicit versions never use heuristics.

    Missing metadata is legacy. Unreadable metadata is not: it could conceal an
    incompatible explicit version. Layout validity is not content integrity.
    """
    provenance = {"revision": None, "created_at": None}
    try:
        stored = json.loads((base / "provenance.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        stored = {}
    except (OSError, ValueError) as exc:
        raise _error(f"Cannot read snapshot provenance.json: {exc}") from exc
    if not isinstance(stored, dict):
        raise _error("Snapshot provenance.json must be an object")
    provenance.update(stored)
    root = base / "compiled"
    if "layout_version" not in stored:
        provenance["layout_version"] = None
        provenance["layout_status"] = "legacy"
        if not root.exists():
            root = base
        # Keep the pre-versioning compatibility branch for existing snapshots.
        dirs = (
            current_model_dirs
            if any((root / name).is_dir() for name in current_model_dirs)
            else None
        )
        return SnapshotLayout(root, dirs, provenance)

    version = stored["layout_version"]
    if type(version) is not int or version != LAYOUT_VERSION:
        raise _error(f"Unsupported snapshot layout_version: {version!r}")
    # Version 1 stores the project's compiled root, including model-path prefixes.
    if not root.is_dir():
        raise _error("Snapshot layout_version 1 requires a compiled/ directory")
    dirs = baseline_model_dirs or ("models",)
    non_model_dirs = {"tests", "snapshots", "analyses", "macros"}
    required_model_dirs: set[str] = set()
    manifest_readable = True
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # The check's existing manifest validation reports missing/corrupt input.
        manifest = {}
        manifest_readable = False
        # Without the baseline manifest we cannot know its model paths. Scan
        # conservatively; the existing baseline_problem warning prevents a clean
        # report. Do not mistake an unknown custom path for a layout mismatch.
        dirs = None
    if not isinstance(manifest, dict):
        raise _error("Snapshot manifest.json must be an object")
    project = (manifest.get("metadata") or {}).get("project_name")
    for node_id, node in (manifest.get("nodes") or {}).items():
        declared = node.get("original_file_path")
        if node_id.startswith("model."):
            if project and node_id.split(".")[1] != project:
                continue
            if node.get("language") == "python" or not (node.get("config") or {}).get(
                "enabled", True
            ):
                continue
            required_model_dirs.add(
                manifest_path_root(declared)
                if isinstance(declared, str) and declared
                else "models"
            )
        elif isinstance(declared, str) and declared:
            non_model_dirs.add(manifest_path_root(declared))
    for name in sorted(required_model_dirs):
        if not (root / name).is_dir():
            raise _error(f"Snapshot layout_version 1 is missing model directory: {name}")
    for sql in root.rglob("*.sql"):
        parts = sql.relative_to(root).parts
        if len(parts) < 2 or (
            manifest_readable and parts[0] not in {*(dirs or ()), *non_model_dirs}
        ):
            raise _error(f"Snapshot layout_version 1 has misplaced SQL: {sql.relative_to(root)}")
    provenance["layout_status"] = "versioned"
    return SnapshotLayout(root, dirs, provenance)
