"""Manifest.json parsing and downstream model discovery."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelNode:
    """A dbt model node from manifest.json."""

    node_id: str  # "model.my_project.int_order_enriched"
    name: str  # "int_order_enriched"
    materialization: str  # "table", "view", "incremental", "ephemeral"
    on_schema_change: str | None  # "ignore", "fail", "append_new_columns", "sync_all_columns"
    columns: tuple[str, ...] = ()  # from manifest column definitions (fallback for SELECT *)


def load_manifest(manifest_path: str | Path) -> dict:
    """Load and parse manifest.json, keeping only nodes and child_map.

    Parses the full JSON then extracts only the needed sections.
    Discards unused sections (macros, sources, docs, etc.) to reduce
    long-term memory usage for large manifests.
    """
    path = Path(manifest_path)
    with path.open("r", encoding="utf-8") as f:
        full = json.load(f)
    result = {
        "nodes": full.get("nodes") or {},
        "child_map": full.get("child_map") or {},
        "metadata": full.get("metadata") or {},
    }
    del full
    return result


def build_node_index(manifest: dict, *, include_packages: bool = False) -> dict[str, ModelNode]:
    """Build a name → ModelNode index for O(1) lookups.

    Args:
        include_packages: If False (default), only include models from the
            root project, skipping dbt package models. The root project is
            detected as the most common package_name in the manifest.
    """
    # Detect root project name: prefer metadata.project_name (dbt v1.5+),
    # fall back to most common package heuristic for older manifests
    root_project = None
    if not include_packages:
        metadata = manifest.get("metadata") or {}
        root_project = metadata.get("project_name")
        if not root_project:
            from collections import Counter

            pkg_counts: Counter[str] = Counter()
            for node_id in manifest.get("nodes") or {}:
                if node_id.startswith("model."):
                    pkg = node_id.split(".")[1]
                    pkg_counts[pkg] += 1
            if pkg_counts:
                root_project = pkg_counts.most_common(1)[0][0]

    index: dict[str, ModelNode] = {}
    for node_id, node in (manifest.get("nodes") or {}).items():
        if not node_id.startswith("model."):
            continue
        # Filter out package models unless include_packages=True
        if root_project and node_id.split(".")[1] != root_project:
            continue
        name = node.get("name")
        config = node.get("config") or {}
        # Skip disabled models (dbt won't run them, no DDL will occur)
        if config.get("enabled") is False:
            continue
        if name and name not in index:
            # Extract column names from manifest (used as fallback for SELECT *)
            manifest_cols = tuple(c.lower() for c in (node.get("columns") or {}))
            index[name] = ModelNode(
                node_id=node_id,
                name=name,
                materialization=config.get("materialized") or "table",
                on_schema_change=config.get("on_schema_change"),
                columns=manifest_cols,
            )
    return index


def find_node_by_name(name: str, manifest: dict) -> ModelNode | None:
    """Find a model node in manifest by short name.

    Returns None if not found.
    Note: For batch lookups, prefer build_node_index() for O(1) access.
    """
    for node_id, node in (manifest.get("nodes") or {}).items():
        if node_id.startswith("model.") and node.get("name") == name:
            config = node.get("config") or {}
            return ModelNode(
                node_id=node_id,
                name=name,
                materialization=config.get("materialized") or "table",
                on_schema_change=config.get("on_schema_change"),
            )
    return None


def find_downstream(
    node_id: str,
    child_map: dict[str, list[str]],
    models_only: bool = True,
) -> list[str]:
    """Find all recursive downstream dependents of a node.

    Uses BFS with visited set for cycle protection.
    Filters out test/source nodes when models_only=True.

    Returns:
        Sorted list of downstream node_ids (excluding starting node).
    """
    visited = {node_id}
    queue: deque[str] = deque()
    result: list[str] = []

    for child in child_map.get(node_id) or []:
        if child not in visited:
            visited.add(child)
            queue.append(child)

    while queue:
        current = queue.popleft()
        if not models_only or current.startswith("model."):
            result.append(current)
        for child in child_map.get(current) or []:
            if child not in visited:
                visited.add(child)
                queue.append(child)

    return sorted(result)


def find_downstream_batch(
    node_ids: list[str],
    child_map: dict[str, list[str]],
    models_only: bool = True,
) -> dict[str, list[str]]:
    """Find downstream dependents for multiple nodes with memoization.

    Caches per-node downstream sets so overlapping subtrees are only
    traversed once. For 200 changed models in a 2000-node DAG, this
    eliminates 50-80% of redundant BFS work.

    Returns:
        Dict mapping each node_id to its sorted list of downstream node_ids.
    """
    cache: dict[str, frozenset[str]] = {}

    def _downstream(nid: str) -> frozenset[str]:
        if nid in cache:
            return cache[nid]
        visited = {nid}
        queue: deque[str] = deque()
        result: set[str] = set()

        for child in child_map.get(nid) or []:
            if child not in visited:
                visited.add(child)
                queue.append(child)

        while queue:
            current = queue.popleft()
            if current in cache:
                # Reuse cached downstream for this subtree
                result.update(cache[current])
                visited.update(cache[current])
                if not models_only or current.startswith("model."):
                    result.add(current)
                continue
            if not models_only or current.startswith("model."):
                result.add(current)
            for child in child_map.get(current) or []:
                if child not in visited:
                    visited.add(child)
                    queue.append(child)

        # Exclude the starting node itself from its own downstream set
        result.discard(nid)
        frozen = frozenset(result)
        cache[nid] = frozen
        return frozen

    return {nid: sorted(_downstream(nid)) for nid in node_ids}
