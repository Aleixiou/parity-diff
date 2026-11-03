"""Endpoints and fixture helpers for the RT-G red team (server settings, versions).

Server-wide settings (GLOBAL variables, `sp_configure`, a database encoding)
and old engine versions cannot be planted on the shared development servers,
so these suites run against disposable Docker containers, each named
`rt-ver-*`, and skip cleanly when one is not running. The defaults below are
the ports the campaign used; every one can be overridden by an env var.

    docker run -d --name rt-ver-pg17 -e POSTGRES_USER=rtg -e POSTGRES_PASSWORD=rtg \\
        -e POSTGRES_DB=rtg -p 15517:5432 postgres:17-alpine
    # likewise 9.5, 9.6, 10 .. 18 on 15595, 15596, 15510 .. 15518
    docker run -d --name rt-ver-my80 -e MYSQL_ROOT_PASSWORD=rtg -e MYSQL_DATABASE=rtg \\
        -e MYSQL_USER=rtg -e MYSQL_PASSWORD=rtg -p 13380:3306 mysql:8.0
    # likewise mysql:8.4 on 13384 and mysql:9 on 13390
    docker run -d --name rt-ver-ms22 -e ACCEPT_EULA=Y -e 'MSSQL_SA_PASSWORD=Rtg!2026xyzQ' \\
        -e MSSQL_PID=Developer -p 14322:1433 mcr.microsoft.com/mssql/server:2022-latest
    # likewise 2019-latest on 14319

The container passwords are throwaway values for those local containers only.
Older DuckDB libraries run in their own virtualenvs, named by
`PARITY_RTG_DUCKDB_PYTHONS` (a `;`-separated list of python executables).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest


def _env_list(name: str, default: list[str]) -> list[str]:
    """A comma-separated env var as a list, or `default` when it is unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return [u.strip() for u in raw.split(",") if u.strip()]


def _pg(port: int) -> str:
    """The superuser URL of the Docker PostgreSQL on `port`."""
    return f"postgres://rtg:rtg@127.0.0.1:{port}/rtg"


#: PostgreSQL 9.5 .. 18, oldest first; the last is the cross-version reference.
PG_VERSION_URLS = _env_list(
    "PARITY_RTG_PG_VERSIONS",
    [
        _pg(p)
        for p in (15595, 15596, 15510, 15511, 15512, 15513, 15514, 15515, 15516, 15517)
    ],
)
PG_REFERENCE_URL = os.environ.get("PARITY_RTG_PG_REFERENCE", _pg(15518))
#: A PostgreSQL on which the `rtg` role is superuser (CREATE DATABASE, roles).
PG_ADMIN_URL = os.environ.get("PARITY_RTG_PG_ADMIN", _pg(15517))

#: MySQL root, for GLOBAL settings, and a plain user on the same server - the
#: one parity logs in as, so `init_connect` (skipped for root) applies.
MYSQL_ROOT_URL = os.environ.get(
    "PARITY_RTG_MYSQL_ROOT", "mysql://root:rtg@127.0.0.1:13380/rtg"
)
MYSQL_USER_URL = os.environ.get(
    "PARITY_RTG_MYSQL_USER", "mysql://rtg:rtg@127.0.0.1:13380/rtg"
)
#: MySQL 8.4 LTS and 9.x; the last (8.0) is the cross-version reference.
MYSQL_VERSION_URLS = _env_list(
    "PARITY_RTG_MYSQL_VERSIONS",
    [f"mysql://rtg:rtg@127.0.0.1:{p}/rtg" for p in (13384, 13390, 13380)],
)

#: Root on a MySQL 8.0 started with --character-set-server=latin1
#: --collation-server=latin1_swedish_ci --skip-character-set-client-handshake
#: --lower-case-table-names=1 (container `rt-ver-my80hs`, port 13381).
MYSQL_LATIN1_ROOT_URL = os.environ.get(
    "PARITY_RTG_MYSQL_LATIN1_ROOT", "mysql://root:rtg@127.0.0.1:13381/rtg"
)
#: Root on the MySQL 9.x container, to install component_classic_hashing.
MYSQL97_ROOT_URL = os.environ.get(
    "PARITY_RTG_MYSQL9_ROOT", "mysql://root:rtg@127.0.0.1:13390/rtg"
)

_MS = "mssql://sa:Rtg!2026xyzQ@127.0.0.1:{}/master?trust_server_certificate=yes"
#: A SQL Server where server-wide settings may be changed (never the shared one).
MSSQL_ADMIN_URL = os.environ.get("PARITY_RTG_MSSQL_ADMIN", _MS.format(14322))
#: SQL Server 2019 then 2022; the last is the cross-version reference.
MSSQL_VERSION_URLS = _env_list(
    "PARITY_RTG_MSSQL_VERSIONS", [_MS.format(14319), _MS.format(14322)]
)

#: Python executables of virtualenvs holding other DuckDB versions.
DUCKDB_PYTHONS = [
    p for p in os.environ.get("PARITY_RTG_DUCKDB_PYTHONS", "").split(";") if p.strip()
]

CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"


def conformance_path() -> None:
    """Put the conformance harness on sys.path (it imports its modules flat)."""
    if str(CONFORMANCE) not in sys.path:
        sys.path.insert(0, str(CONFORMANCE))


def short(url: str) -> str:
    """`host:port` of a URL, for test ids and messages - never the password."""
    u = urlparse(url)
    return f"{u.hostname}:{u.port}"


# ------------------------------------------------------------------ PostgreSQL


def pg_connect(url: str) -> Any:
    """An autocommit psycopg connection to `url`, or skip when none answers."""
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"no PostgreSQL at {short(url)}: {type(exc).__name__}")


def pg_with_db(url: str, database: str) -> str:
    """`url` pointed at another database on the same server."""
    return urlparse(url)._replace(path="/" + database).geturl()


# ----------------------------------------------------------------------- MySQL


def mysql_connect(url: str) -> Any:
    """An autocommit mysql-connector connection to `url`, or skip."""
    mysql_connector = pytest.importorskip("mysql.connector")
    u = urlparse(url)
    try:
        return mysql_connector.connect(
            host=u.hostname,
            port=u.port or 3306,
            user=unquote(u.username or ""),
            password=unquote(u.password or ""),
            database=u.path.lstrip("/") or None,
            autocommit=True,
            connection_timeout=3,
        )
    except Exception as exc:
        pytest.skip(f"no MySQL at {short(url)}: {type(exc).__name__}")


def mysql_run(conn: Any, *statements: str) -> list[list[tuple[Any, ...]]]:
    """Run `statements` on `conn`; the rows of each that returns any."""
    out = []
    cur = conn.cursor()
    try:
        for sql in statements:
            cur.execute(sql)
            out.append(list(cur.fetchall()) if cur.with_rows else [])
    finally:
        cur.close()
    return out


# ------------------------------------------------------------------ SQL Server


def mssql_url(url: str, database: str) -> str:
    """`url` pointed at `database`."""
    return urlparse(url)._replace(path="/" + database).geturl()


def mssql_exec(url: str, database: str, *statements: str) -> list[list[tuple[Any, ...]]]:
    """Run `statements` as the URL's login in `database`, or skip without a server."""
    mssql_python = pytest.importorskip("mssql_python")

    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(mssql_url(url, database))
    try:
        conn = mssql_python.connect(odbc, autocommit=True, timeout=5)
    except Exception as exc:
        pytest.skip(f"no SQL Server at {short(url)}: {type(exc).__name__}")
    out = []
    try:
        cur = conn.cursor()
        for sql in statements:
            cur.execute(sql)
            out.append([tuple(r) for r in cur.fetchall()] if cur.description else [])
        cur.close()
    finally:
        conn.close()
    return out


def mssql_drop_database(url: str, name: str) -> None:
    """Drop database `name` on `url` if it exists, kicking its sessions out."""
    mssql_exec(
        url,
        "master",
        f"if db_id('{name}') is not null begin alter database [{name}] "
        f"set single_user with rollback immediate; drop database [{name}] end",
    )


# ----------------------------------------------------------------------- shared


def found(result) -> list[tuple]:
    """(key, kind, sorted columns) of every reported difference, sorted by key."""
    return sorted(
        ((d.key, d.kind, tuple(sorted(d.columns))) for d in result.diffs),
        key=lambda t: (type(t[0]).__name__, t[0]),
    )


def run_diff(a, b, a_table: str, b_table: str, **kw):
    """`diff` with key `id` by default, always closing both sides."""
    from parity.engine import diff

    kw.setdefault("key", "id")
    try:
        return diff(a, b, a_table, b_table, **kw)
    finally:
        a.close()
        b.close()


def duck_file(directory: Path, name: str, *statements: str) -> str:
    """Write a DuckDB file holding `statements`; return its path."""
    duckdb = pytest.importorskip("duckdb")
    path = str(directory / f"{name}.duckdb")
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    try:
        con.execute("set TimeZone = 'UTC'")
        for sql in statements:
            con.execute(sql)
    finally:
        con.close()
    return path
