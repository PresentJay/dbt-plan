"""Manifest.json parsing and downstream model discovery."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_VERSION_TAIL = re.compile(r"^v[0-9]+$")


def model_key(node_id: str) -> str:
    """The name a model's compiled SQL is written under, derived from its node_id.

    Everything else in dbt-plan is keyed by that name, because the diff is a
    comparison of files. For an ordinary model it is the last segment of the
    node_id, but a versioned model carries the version there instead:

        model.p.fct_orders      -> fct_orders     -> fct_orders.sql
        model.p.fct_orders.v2   -> fct_orders_v2  -> fct_orders_v2.sql

    Reading `v2` as the model name is what made a versioned model report as
    missing from the manifest and then go unanalysed entirely.

    `defined_in:` can name the file something else. `build_node_index` reads the
    node's own `path` for that reason and registers this as an alias, so the two
    spellings both resolve.
    """
    parts = node_id.split(".")
    if len(parts) >= 4 and _VERSION_TAIL.match(parts[-1]):
        return f"{parts[-2]}_{parts[-1]}"
    return parts[-1]


@dataclass(frozen=True)
class UnitTestFixture:
    """One hand-written table inside a unit test: an `expect` block or a `given` input.

    dbt checks every fixture's column names against the real relation it stands
    for and errors out on a name that is not there, so a fixture is broken by an
    upstream column drop the same way a SELECT is:

        Invalid column name: 'customer_id' in unit test fixture for 'stg_orders'.
        Accepted columns for 'stg_orders' are: ['order_id', 'store_id', 'order_date']

    `columns` is None when the fixture is not readable from manifest.json alone --
    a `fixture:` file reference or a SQL block. That is the honest answer: it may
    or may not break, and saying nothing would read as "fine".
    """

    label: str  # "expect", or "given for stg_orders"
    model: str  # the model whose columns this fixture has to match
    columns: frozenset[str] | None  # lowercased, None if unreadable
    unreadable_reason: str = ""  # only set when columns is None


@dataclass(frozen=True)
class UnitTestNode:
    """A dbt unit test (1.8+), which names the columns it expects by hand."""

    node_id: str  # "unit_test.my_project.stg_orders.test_stg_orders_shape"
    name: str  # "test_stg_orders_shape"
    model: str  # the model under test, "stg_orders"
    fixtures: tuple[UnitTestFixture, ...] = ()


@dataclass(frozen=True)
class DataTestNode:
    """A dbt data test: generic (`not_null`) or singular (a `.sql` file in `tests/`).

    A generic test names its column in the manifest, so `columns_by_model` answers
    it outright. A singular test, and a generic one with no `column_name` such as
    `dbt_utils.expression_is_true`, names nothing there -- for those the models it
    depends on are recorded instead, and the caller reads its compiled SQL.
    """

    node_id: str  # "test.my_project.not_null_stg_orders_customer_id.af79d5e4b5"
    name: str  # "not_null_stg_orders_customer_id"
    columns_by_model: dict[str, frozenset[str]] = field(default_factory=dict)
    depends_on_models: tuple[str, ...] = ()


def build_data_test_index(manifest: dict) -> dict[str, DataTestNode]:
    """Build a node_id -> DataTestNode index over the manifest's data tests.

    Data tests live in `nodes` next to the models, unlike unit tests, so nothing
    extra has to be kept at load time.
    """
    index: dict[str, DataTestNode] = {}
    for node_id, node in (manifest.get("nodes") or {}).items():
        if not node_id.startswith("test."):
            continue
        config = node.get("config") or {}
        if config.get("enabled") is False:
            continue

        columns: dict[str, set[str]] = {}
        column_name = node.get("column_name")
        attached = node.get("attached_node")
        if column_name and attached:
            columns.setdefault(model_key(attached), set()).add(str(column_name).lower())

        metadata = node.get("test_metadata") or {}
        if metadata.get("name") == "relationships":
            # The only built-in test that reads a second model. It is a child of
            # both, and the far side is named only here.
            kwargs = metadata.get("kwargs") or {}
            far_model = _input_model(kwargs.get("to") or "")
            far_column = kwargs.get("field")
            if far_model and far_column:
                columns.setdefault(far_model, set()).add(str(far_column).lower())

        depends = tuple(
            model_key(nid)
            for nid in ((node.get("depends_on") or {}).get("nodes") or [])
            if nid.startswith("model.")
        )
        index[node_id] = DataTestNode(
            node_id=node_id,
            name=node.get("name") or node_id.split(".")[-1],
            columns_by_model={model: frozenset(cols) for model, cols in columns.items()},
            depends_on_models=depends,
        )
    return index


@dataclass(frozen=True)
class ExposureNode:
    """A dbt exposure: something outside the project that reads a model.

    An exposure declares its dependencies at model granularity -- there are no
    columns in it -- so dbt-plan can never say a dashboard breaks. What it can
    say is which ones read a model that is losing a column, and who owns them.
    """

    node_id: str  # "exposure.my_project.orders_dashboard"
    name: str  # "orders_dashboard"
    type: str  # "dashboard", "notebook", "analysis", "ml", "application"
    owner_name: str = ""
    owner_email: str = ""
    url: str = ""

    def owner(self) -> str:
        """The owner as one readable string, empty when dbt has neither field."""
        if self.owner_name and self.owner_email:
            return f"{self.owner_name} <{self.owner_email}>"
        return self.owner_name or self.owner_email


def build_exposure_index(manifest: dict) -> dict[str, ExposureNode]:
    """Build a node_id -> ExposureNode index over the manifest's exposures."""
    index: dict[str, ExposureNode] = {}
    for node_id, node in (manifest.get("exposures") or {}).items():
        config = node.get("config") or {}
        if config.get("enabled") is False:
            continue
        owner = node.get("owner") or {}
        index[node_id] = ExposureNode(
            node_id=node_id,
            name=node.get("name") or node_id.split(".")[-1],
            type=node.get("type") or "",
            owner_name=owner.get("name") or "",
            owner_email=owner.get("email") or "",
            url=node.get("url") or "",
        )
    return index


@dataclass(frozen=True)
class ModelNode:
    """A dbt model node from manifest.json."""

    node_id: str  # "model.my_project.int_order_enriched"
    name: str  # "int_order_enriched"
    materialization: str  # "table", "view", "incremental", "ephemeral"
    on_schema_change: str | None  # "ignore", "fail", "append_new_columns", "sync_all_columns"
    version: str | None = None  # set only for a versioned model; `name` already carries it
    columns: tuple[str, ...] = ()  # from manifest column definitions (fallback for SELECT *)
    # `contract: {enforced: true}`. When set, dbt requires every column to be
    # declared, so `columns` above stops being documentation and becomes the shape
    # dbt itself checks the SQL against.
    contract_enforced: bool = False


def load_manifest(manifest_path: str | Path) -> dict:
    """Load and parse manifest.json, keeping only the sections dbt-plan reads.

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
        "unit_tests": full.get("unit_tests") or {},
        "exposures": full.get("exposures") or {},
        "source_dirs": _source_dirs(full),
    }
    del full
    return result


def _source_dirs(manifest: dict) -> tuple[str, ...]:
    """Top-level directories this project's own source files were declared in.

    `model-paths`, `macro-paths`, `test-paths` and the rest, as written on disk
    rather than as configured -- every node records where it came from:

        "original_file_path": "transformations/stg_orders.sql"

    Package files are excluded: their mtimes move when `dbt deps` runs, not when
    anyone edits this project. Only names are kept, so the macro bodies this
    walks past are not retained.
    """
    project = (manifest.get("metadata") or {}).get("project_name")
    dirs: dict[str, None] = {}
    for section in ("nodes", "macros"):
        for node_id, node in (manifest.get(section) or {}).items():
            parts = node_id.split(".")
            if project and len(parts) > 1 and parts[1] != project:
                continue
            declared = node.get("original_file_path")
            if isinstance(declared, str) and declared:
                dirs[PurePosixPath(declared).parts[0]] = None
    return tuple(dirs)


def _fixture_columns(block: dict) -> tuple[frozenset[str] | None, str]:
    """Read the column names one unit test fixture pins down.

    Returns (columns, reason). `columns` is None when the block cannot be read
    from the manifest, and `reason` then says why -- never treat that as clean.
    """
    if block.get("fixture"):
        return None, f"comes from fixture '{block['fixture']}'"

    rows = block.get("rows")
    fmt = block.get("format") or "dict"

    if fmt == "dict":
        if not isinstance(rows, list) or not rows:
            return None, "has no inline rows to read column names from"
        columns: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return None, "has rows that are not name/value pairs"
            columns.update(str(key).lower() for key in row)
        return frozenset(columns), ""

    if fmt == "csv":
        # Inline CSV carries its header on the first line; a `fixture:` CSV was
        # already handled above.
        if not isinstance(rows, str) or not rows.strip():
            return None, "is CSV with no inline header to read"
        header = rows.strip().splitlines()[0]
        return frozenset(c.strip().lower() for c in header.split(",") if c.strip()), ""

    # format: sql, or something dbt added after this was written.
    return None, f"is in '{fmt}' format, which dbt-plan does not read"


_REF_CALL = re.compile(r"^\s*ref\s*\((.*)\)\s*$", re.DOTALL)
_QUOTED = re.compile(r"""['\"]([^'\"]+)['\"]""")


def _input_model(input_expr: str) -> str:
    """The model a unit test's `given` input stands in for, or "" if it is not one.

    dbt stores the literal Jinja call: `ref('stg_orders')`, or with a package,
    `ref('a_package', 'stg_orders')` -- the model is the last quoted name either
    way. `source(...)` inputs return "", since sources are outside dbt-plan's scope.
    """
    call = _REF_CALL.match(input_expr or "")
    if not call:
        return ""
    names = _QUOTED.findall(call.group(1))
    return names[-1] if names else ""


def build_unit_test_index(manifest: dict) -> dict[str, UnitTestNode]:
    """Build a node_id -> UnitTestNode index over the manifest's unit tests."""
    index: dict[str, UnitTestNode] = {}
    for node_id, node in (manifest.get("unit_tests") or {}).items():
        config = node.get("config") or {}
        if config.get("enabled") is False:
            continue
        tested_model = node.get("model") or ""

        fixtures: list[UnitTestFixture] = []
        expect_cols, expect_reason = _fixture_columns(node.get("expect") or {})
        fixtures.append(
            UnitTestFixture(
                label="expect",
                model=tested_model,
                columns=expect_cols,
                unreadable_reason=expect_reason,
            )
        )
        for given in node.get("given") or []:
            input_model = _input_model(given.get("input") or "")
            if not input_model:
                continue  # a source, or something that is not a ref()
            given_cols, given_reason = _fixture_columns(given)
            fixtures.append(
                UnitTestFixture(
                    label=f"given for {input_model}",
                    model=input_model,
                    columns=given_cols,
                    unreadable_reason=given_reason,
                )
            )

        index[node_id] = UnitTestNode(
            node_id=node_id,
            name=node.get("name") or node_id.split(".")[-1],
            model=tested_model,
            fixtures=tuple(fixtures),
        )
    return index


def _authored_on_schema_change(node: dict, config: dict) -> str | None:
    """`on_schema_change` as the author wrote it, or None when dbt supplied it.

    dbt resolves this for *every* model, so `config["on_schema_change"]` is
    `"ignore"` on a view, a materialized view and a custom materialization alike.
    Reading that as the author's assertion is what let a materialized view drop a
    column and report `NO DDL / SAFE`: predict_ddl's guard for a materialization it
    has no rule for tests `on_schema_change is None`, which never happened.

    `unrendered_config` carries only what a human wrote, from the model file or
    from `dbt_project.yml`. An explicit setting is still honoured -- a custom
    materialization declared `sync_all_columns` still earns a destructive verdict.

    Older manifests have no `unrendered_config`, and the resolved value cannot say
    who set it. There, keep it only for `incremental`, whose default is dbt's own
    documented rule; refuse for everything else. A false warning is the acceptable
    direction.
    """
    authored = node.get("unrendered_config")
    if isinstance(authored, dict):
        return authored.get("on_schema_change")
    if (config.get("materialized") or "table") == "incremental":
        return config.get("on_schema_change")
    return None


def build_node_index(manifest: dict, *, include_packages: bool = False) -> dict[str, ModelNode]:
    """Build a compiled-SQL-name → ModelNode index for O(1) lookups.

    Keyed by the name the model's compiled file is written under, because every
    lookup against this index starts from a file in the diff. For an ordinary
    model that is the dbt model name; for a versioned one it is `<name>_v<n>`,
    or whatever `defined_in:` says. See `model_key`.

    Args:
        include_packages: If False (default), only include models from the
            root project, skipping dbt package models. The root project is
            detected as the most common package_name in the manifest.

    Nothing in the CLI passes True. The `include_packages` config key was removed
    in 0.11.0 because the compiled scan covers the root project alone, so models
    this let through were indexed and then never examined. The parameter is kept
    because that is the half that worked, and making the scan follow it is an open
    question rather than a rejected one.
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
        if not node.get("name"):
            continue
        config = node.get("config") or {}
        # Skip disabled models (dbt won't run them, no DDL will occur)
        if config.get("enabled") is False:
            continue

        # The file dbt wrote is the authority; the node_id is the fallback for a
        # manifest that predates `path`, and an alias when `defined_in:` renames.
        path = node.get("path")
        key = Path(path).stem if path else model_key(node_id)
        version = node.get("version")
        entry = ModelNode(
            node_id=node_id,
            name=key,
            materialization=config.get("materialized") or "table",
            on_schema_change=_authored_on_schema_change(node, config),
            version=str(version) if version is not None else None,
            # Extract column names from manifest (used as fallback for SELECT *)
            columns=tuple(c.lower() for c in (node.get("columns") or {})),
            contract_enforced=bool((config.get("contract") or {}).get("enforced")),
        )
        for alias in (key, model_key(node_id)):
            index.setdefault(alias, entry)
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
