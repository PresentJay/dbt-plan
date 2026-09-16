"""Local PostgreSQL harness for adapter validation (issue #262).

This module is a function library: subprocess plumbing and SQL that is
independent of any fixture. The fixtures live in tests/test_pg_e2e.py so
issue #263 can reuse the library for a CI container without importing a test
module. Nothing here imports psycopg2 or pgserver at module load; the
dependencies are probed lazily so the module stays importable everywhere and
the suite skips (never breaks) when a piece is missing.

Connection model:
  * `DBT_PG_HOST`, `DBT_PG_PORT`, `DBT_PG_USER`, `DBT_PG_PASSWORD`,
    `DBT_PG_DBNAME` all set -> an already-running external server (the CI
    container path). Per-test isolation is one schema, created and dropped.
  * Otherwise an embedded pgserver (PostgreSQL 16.2, pinned) is started under
    the pytest data dir, localhost + trust auth, and the same schema scheme
    applies.
Each test writes into its own schema, never into `public`.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import subprocess
import sys
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_EXTERNAL_ENV = (
    "DBT_PG_HOST",
    "DBT_PG_PORT",
    "DBT_PG_USER",
    "DBT_PG_PASSWORD",
    "DBT_PG_DBNAME",
)

_POSTGRES_VERSION = "16.2"


def _venv_bin() -> Path:
    return Path(sys.executable).parent


def _dbt() -> Path:
    return _venv_bin() / ("dbt.exe" if os.name == "nt" else "dbt")


def external_configured() -> bool:
    """True when every DBT_PG_* variable is set, pointing at a live server."""
    return all(os.environ.get(name) for name in _EXTERNAL_ENV)


def missing_requirement() -> str | None:
    """Name the missing piece so a skip never hides the wrong problem."""
    if importlib.util.find_spec("dbt_plan") is None:
        return "dbt_plan is not importable -- run `uv sync` or `pip install -e .`"
    if not _dbt().exists():
        return "dbt-core is not installed"
    if importlib.util.find_spec("dbt.adapters.postgres") is None:
        return "dbt-postgres adapter is not installed"
    if importlib.util.find_spec("psycopg2") is None:
        return "psycopg2 is not installed"
    if not external_configured() and importlib.util.find_spec("pgserver") is None:
        return (
            "neither DBT_PG_HOST/DBT_PG_PORT/DBT_PG_USER/DBT_PG_PASSWORD/DBT_PG_DBNAME "
            "nor the pgserver package is present"
        )
    return None


@dataclass(frozen=True)
class PgDb:
    """Everything a connection to one schema-by-test needs."""

    host: str
    port: int
    user: str
    password: str
    dbname: str
    schema: str

    def kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": self.dbname,
        }


def new_schema_id() -> str:
    return "t_" + secrets.token_hex(6)


def _connect_psycopg2(kwargs: dict):
    import psycopg2

    return psycopg2.connect(**kwargs)


@contextmanager
def embedded_server(data_dir: Path, *, version: str = _POSTGRES_VERSION) -> Iterator:
    """Start the embedded PostgreSQL server pinned to the given major.

    `get_server` is importable on demand so this module can be imported in
    environments without pgserver -- the check belongs to the caller.
    """
    from pgserver import get_server

    server = get_server(data_dir, cleanup_mode="stop")
    try:
        yield server
    finally:
        server.cleanup()


def host_and_port(server) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(server.get_uri("postgres"))
    return parsed.hostname, parsed.port


def create_schema(db: PgDb) -> None:
    connection = _connect_psycopg2(db.kwargs())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{db.schema}"')
    finally:
        connection.close()


def drop_schema(db: PgDb) -> None:
    connection = _connect_psycopg2(db.kwargs())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{db.schema}" CASCADE')
    finally:
        connection.close()


def make_project(project_dir: Path, db: PgDb) -> Path:
    """Write a minimal dbt project wired to `db` and return its directory."""
    (project_dir / "models").mkdir(parents=True)
    (project_dir / "dbt_project.yml").write_text(
        "name: pg_rules\nversion: '1.0'\nprofile: pg_rules\n", encoding="utf-8"
    )
    (project_dir / "profiles.yml").write_text(
        "pg_rules:\n  target: dev\n  outputs:\n    dev:\n"
        "      type: postgres\n"
        f"      host: {db.host}\n"
        f"      port: {db.port}\n"
        f"      user: {db.user}\n"
        f'      password: "{db.password}"\n'
        f"      dbname: {db.dbname}\n"
        f"      schema: {db.schema}\n"
        "      threads: 1\n",
        encoding="utf-8",
    )
    return project_dir


def _dbt_command(*args: str, project: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_dbt()), *args],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def dbt_compile(project: Path) -> subprocess.CompletedProcess:
    return _dbt_command(
        "compile", "--profiles-dir", ".", "--target-path", "target", project=project
    )


def dbt_run(project: Path) -> subprocess.CompletedProcess:
    return _dbt_command("run", "--profiles-dir", ".", "--target-path", "target", project=project)


def dbt_plan(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "dbt_plan.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def read_columns(db: PgDb, table: str) -> list[str]:
    connection = _connect_psycopg2(db.kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "select column_name from information_schema.columns "
                "where table_schema = %s and table_name = %s "
                "order by ordinal_position",
                (db.schema, table),
            )
            return [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()


def read_table_type(db: PgDb, table: str) -> str:
    connection = _connect_psycopg2(db.kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "select table_type from information_schema.tables "
                "where table_schema = %s and table_name = %s",
                (db.schema, table),
            )
            row = cursor.fetchone()
            return row[0] if row else "MISSING"
    finally:
        connection.close()


def count_rows(db: PgDb, table: str) -> int:
    connection = _connect_psycopg2(db.kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT count(*) FROM "{db.schema}"."{table}"')
            return cursor.fetchone()[0]
    finally:
        connection.close()


def read_values(db: PgDb, table: str, column: str) -> list[tuple]:
    connection = _connect_psycopg2(db.kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT "{column}" FROM "{db.schema}"."{table}" '
                f'ORDER BY "{column}" ASC NULLS LAST'
            )
            return [row for row in cursor.fetchall()]
    finally:
        connection.close()
