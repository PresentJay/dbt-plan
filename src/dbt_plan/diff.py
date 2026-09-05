"""Compiled SQL directory comparison."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path


def iter_model_sql(root: Path, model_dirs: Sequence[str] | None = None) -> Iterator[Path]:
    """Yield the compiled SQL files under `root` that are models.

    dbt writes YAML-declared nodes -- unit tests, generic data tests -- into a
    directory named after the schema file that declared them:

        target/compiled/my_project/models/schema.yml/models/test_orders_shape.sql

    so anything under a `*.yml` path segment is not a model. Left in, a unit test
    is diffed as one and reported as "not found in manifest", and two models that
    each carry a `test_shape` collide on the duplicate-stem check below and abort
    the whole run.

    `model_dirs` names the top-level directories dbt wrote models into. That is
    `model-paths` from `dbt_project.yml`, which is configurable and is not always
    `models`; it is read from the manifest rather than assumed. None means no
    prefix restriction, which is what a snapshot taken before 0.14 needs -- those
    were copied from inside the model directory, so the prefix is already gone.

    Symlinks are skipped so a link cannot pull in a file outside the project.
    """
    for f in root.rglob("*.sql"):
        if f.is_symlink():
            continue
        parts = f.relative_to(root).parts
        if model_dirs is not None and parts[0] not in model_dirs:
            continue
        if any(part.endswith((".yml", ".yaml")) for part in parts[:-1]):
            continue
        yield f


@dataclass(frozen=True)
class ModelDiff:
    """A change detected between base and current compiled SQL."""

    model_name: str  # filename stem, e.g. "dim_customers"
    status: str  # "modified", "added", "removed"
    base_path: Path | None  # None if added
    current_path: Path | None  # None if removed
    base_sql: str | None = None  # cached content to avoid re-reading
    current_sql: str | None = None  # cached content to avoid re-reading


def iter_non_model_sql(root: Path, model_dirs: Sequence[str]) -> Iterator[Path]:
    """Yield the compiled SQL of everything under `root` that is not a model.

    Data tests and unit tests, in the two places dbt writes them:

        target/compiled/p/tests/singular_customer_tier.sql        <- test-paths
        target/compiled/p/models/schema.yml/not_null_orders_x.sql <- declared in YAML

    Neither is a model, so this is the complement of `iter_model_sql` -- the same
    rule read the other way round.
    """
    for f in root.rglob("*.sql"):
        if f.is_symlink():
            continue
        parts = f.relative_to(root).parts
        under_yaml = any(part.endswith((".yml", ".yaml")) for part in parts[:-1])
        if parts[0] in model_dirs and not under_yaml:
            continue
        yield f


def diff_compiled_dirs(
    base_dir: str | Path,
    current_dir: str | Path,
    base_model_dirs: Sequence[str] | None = None,
    current_model_dirs: Sequence[str] | None = None,
) -> list[ModelDiff]:
    """Compare two directories of compiled SQL files.

    Recursively finds .sql files, extracts model names from filename stems,
    and compares file contents. Unchanged models are excluded.

    Returns:
        List of ModelDiff sorted by model_name.
    """
    base_dir = Path(base_dir)
    current_dir = Path(current_dir)

    if not base_dir.is_dir():
        raise FileNotFoundError(f"Base directory does not exist: {base_dir}")
    if not current_dir.is_dir():
        raise FileNotFoundError(f"Current directory does not exist: {current_dir}")

    base_models: dict[str, Path] = {}
    for f in iter_model_sql(base_dir, base_model_dirs):
        if f.stem in base_models:
            raise ValueError(
                f"Duplicate model name '{f.stem}' in {base_dir}: {base_models[f.stem]} vs {f}"
            )
        base_models[f.stem] = f

    current_models: dict[str, Path] = {}
    for f in iter_model_sql(current_dir, current_model_dirs):
        if f.stem in current_models:
            raise ValueError(
                f"Duplicate model name '{f.stem}' in {current_dir}: "
                f"{current_models[f.stem]} vs {f}"
            )
        current_models[f.stem] = f

    all_names = sorted(set(base_models) | set(current_models))
    diffs: list[ModelDiff] = []

    for name in all_names:
        base_path = base_models.get(name)
        current_path = current_models.get(name)

        if base_path and current_path:
            # Read content eagerly so callers don't re-read.
            # Normalize line endings and strip BOM to avoid false diffs
            # from cross-platform editing (Windows CRLF vs Unix LF).
            try:
                base_text = (
                    base_path.read_text(encoding="utf-8").replace("\r\n", "\n").lstrip("\ufeff")
                )
                current_text = (
                    current_path.read_text(encoding="utf-8").replace("\r\n", "\n").lstrip("\ufeff")
                )
            except UnicodeDecodeError:
                # Non-UTF-8 file: treat as modified with no cached SQL.
                # Callers will see base_sql=None / current_sql=None and
                # produce REVIEW REQUIRED — consistent with the false-safe-ban rule.
                diffs.append(ModelDiff(name, "modified", base_path, current_path))
                continue
            if base_text != current_text:
                diffs.append(
                    ModelDiff(name, "modified", base_path, current_path, base_text, current_text)
                )
        elif current_path and not base_path:
            diffs.append(ModelDiff(name, "added", None, current_path))
        elif base_path and not current_path:
            diffs.append(ModelDiff(name, "removed", base_path, None))

    return diffs
