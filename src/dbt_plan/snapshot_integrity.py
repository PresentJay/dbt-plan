"""Streaming SHA256 inventories for accidental snapshot drift, not authentication.

Callers must serialize snapshot writers and readers. The inventory format is
independent of the compiled layout and the dbt-plan package version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

INVENTORY_FILENAME = "inventory.json"
INVENTORY_VERSION = 1
_CHUNK_SIZE = 1024 * 1024


class SnapshotIntegrityError(ValueError):
    """An inventory cannot establish the complete baseline's integrity."""


def _error(message: str) -> SnapshotIntegrityError:
    return SnapshotIntegrityError(
        f"Snapshot integrity error: {message}. "
        "Run 'dbt-plan snapshot' again to recreate the baseline."
    )


def _canonical_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _error(f"Invalid inventory path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or any(char in value for char in ':<>"|?*')
        or PureWindowsPath(value).drive
        or any(
            ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
        or any(part.endswith((".", " ")) for part in path.parts)
        or any(
            re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", part.split(".")[0], re.I)
            for part in path.parts
        )
        or not (
            value == "manifest.json"
            or (
                len(path.parts) > 1
                and path.parts[0] == "compiled"
                and path.suffix.lower() == ".sql"
            )
        )
    ):
        raise _error(f"Invalid inventory path: {value!r}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(info: os.stat_result) -> bool:
    # Also detect junctions on Python versions without Path.is_junction().
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _inputs(base: Path, *, validating: bool) -> list[str]:
    """Walk without following links, and never hide unreadable directories.

    Capture keeps the existing symlink-preserving copy contract; validation
    refuses links so that none can turn an inventoried input into omitted SQL.
    Scan the entire snapshot to detect SQL moved outside compiled/ as well.
    """
    found = []
    pending = [base]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            info = path.lstat()
            mode = info.st_mode
            if _is_link(info):
                if validating:
                    raise _error(f"Symlink or junction in snapshot: {path.relative_to(base)}")
                continue
            if stat.S_ISDIR(mode):
                if path.suffix.lower() == ".sql":
                    raise _error(f"SQL input is a directory: {path.relative_to(base)}")
                pending.append(path)
                continue
            name = path.relative_to(base).as_posix()
            if not stat.S_ISREG(mode):
                raise _error(f"Input is not a regular file: {name}")
            if name == "manifest.json" or path.suffix.lower() == ".sql":
                found.append(_canonical_path(name))
    return sorted(found)


def write_snapshot_inventory(base: Path) -> None:
    """Write only inside the caller's unpublished staging directory."""
    entries = [
        {"path": name, "sha256": _sha256(base / name)} for name in _inputs(base, validating=False)
    ]
    (base / INVENTORY_FILENAME).write_text(
        json.dumps({"version": INVENTORY_VERSION, "files": entries}, ensure_ascii=True, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _error(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def validate_snapshot_inventory(base: Path) -> str:
    """Return verified/unrecorded; never partially accept an existing inventory."""
    inventory = base / INVENTORY_FILENAME
    try:
        try:
            info = inventory.lstat()
        except FileNotFoundError:
            return "unrecorded"
        if not stat.S_ISREG(info.st_mode) or _is_link(info):
            raise _error("inventory.json must be a regular file")
        if _is_link(base.lstat()):
            raise _error("Snapshot root must not be a symlink or junction")
        # Inspect filesystem types before opening any input or metadata.
        names = _inputs(base, validating=True)
        stored = json.loads(
            inventory.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        if not isinstance(stored, dict) or set(stored) != {"version", "files"}:
            raise _error("Inventory must contain exactly version and files")
        version = stored["version"]
        if type(version) is not int or version != INVENTORY_VERSION:
            raise _error(f"Unsupported inventory version: {version!r}")
        if not isinstance(stored["files"], list):
            raise _error("Inventory files must be an array")
        expected = {}
        for entry in stored["files"]:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise _error("Inventory entries must contain exactly path and sha256")
            name = _canonical_path(entry["path"])
            digest = entry["sha256"]
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise _error(f"Invalid SHA256 digest for {name}")
            if name in expected:
                raise _error(f"Duplicate inventory path: {name}")
            expected[name] = digest
        if list(expected) != sorted(expected):
            raise _error("Inventory paths must be sorted")
        if "manifest.json" not in expected:
            raise _error("Inventory requires manifest.json")
        if list(expected) != names:
            missing = sorted(set(expected) - set(names))
            unexpected = sorted(set(names) - set(expected))
            raise _error(f"Input membership changed (missing={missing}, unexpected={unexpected})")
        for name, digest in expected.items():
            if _sha256(base / name) != digest:
                raise _error(f"SHA256 mismatch: {name}")
    except SnapshotIntegrityError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise _error(str(exc)) from exc
    return "verified"
