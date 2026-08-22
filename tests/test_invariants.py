"""Structural guards for the promises that define this tool.

These are checked by reading the source rather than running it, because they
are claims about what the code *cannot* do. dbt-plan is advertised as safe to
run on a fork's pull request, with no credentials and no warehouse; that is
only true for as long as nothing here opens a connection.

A change that violates one of these is not a bug to review on its merits -- it
contradicts the reason the tool exists. Failing in CI is cheaper than finding
it in review, which matters when plausible-looking contributions arrive faster
than they can be read.

Behavioural invariants live elsewhere: false-safe handling in
tests/test_false_safe_hunt.py, the dependency list in tests/test_packaging.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "dbt_plan"

# Database drivers and warehouse SDKs. Importing any of these would mean
# dbt-plan had gained a live connection to the thing it is meant to reason
# about statically.
WAREHOUSE_MODULES = {
    "snowflake",
    "psycopg",
    "psycopg2",
    "pymysql",
    "MySQLdb",
    "sqlalchemy",
    "duckdb",
    "pyodbc",
    "cx_Oracle",
    "oracledb",
    "clickhouse_driver",
    "clickhouse_connect",
    "trino",
    "presto",
    "pyhive",
    "databricks",
    "google.cloud",
    "boto3",
    "botocore",
    "redshift_connector",
    "dbt",  # the dbt package itself: this tool reads dbt's output, it does not embed dbt
}

# Anything that could reach the network. The tool reads files and prints text.
NETWORK_MODULES = {
    "socket",
    "ssl",
    "http",
    "urllib",
    "urllib2",
    "urllib3",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "telnetlib",
    "smtplib",
    "xmlrpc",
    "asyncio",
}


def _source_files() -> list[Path]:
    files = sorted(SRC.glob("*.py"))
    assert files, f"No source files found under {SRC}"
    return files


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by a file, including inside functions."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _matches(imported: str, banned: set[str]) -> str | None:
    """Match a dotted import against a banned root, e.g. google.cloud.bigquery."""
    parts = imported.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in banned:
            return candidate
    return None


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
class TestNoWarehouseConnection:
    def test_imports_no_database_driver(self, path: Path) -> None:
        """dbt-plan analyses compiled SQL; it never connects to a warehouse.

        This is what lets it run on an untrusted pull request with no
        credentials in scope. See docs/design-notes.md.
        """
        for imported in sorted(_imported_modules(path)):
            hit = _matches(imported, WAREHOUSE_MODULES)
            assert hit is None, (
                f"{path.name} imports {imported!r}, a warehouse/database module. "
                "dbt-plan reads files only -- it must never open a connection."
            )

    def test_imports_nothing_that_reaches_the_network(self, path: Path) -> None:
        """No telemetry, no update checks, no fetching. Files in, text out."""
        for imported in sorted(_imported_modules(path)):
            hit = _matches(imported, NETWORK_MODULES)
            assert hit is None, (
                f"{path.name} imports {imported!r}, which can reach the network. "
                "dbt-plan runs offline by design."
            )


class TestSubprocessSurface:
    """The tool shells out for git and dbt compile. Nothing else, and never
    through a shell -- `shell=True` on a config-supplied command would turn
    `compile_command` into arbitrary code execution."""

    def test_no_shell_true(self) -> None:
        offenders = []
        for path in _source_files():
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "shell" and getattr(kw.value, "value", False) is True:
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            f"subprocess called with shell=True at {offenders}. "
            "compile_command comes from user config; a shell would make it "
            "arbitrary code execution."
        )
