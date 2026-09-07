"""Shared fixtures.

Every database-backed fixture skips - never fails - when its engine is not
reachable, so `pytest` is useful on a laptop with only DuckDB installed.
"""

from __future__ import annotations

import os

import pytest

from parity.dialects.base import get_dialect

#: Multiplier for Hypothesis example counts. `PARITY_DEEP=20 pytest` runs the
#: generative suites twenty times deeper - the repeatable "make sure there is no
#: abnormality" hunt for the identical check, without changing the fast default.
DEEP = max(1, int(os.environ.get("PARITY_DEEP", "1")))


def deep_examples(n: int) -> int:
    """Scale a Hypothesis `max_examples` by the PARITY_DEEP factor (default 1)."""
    return n * DEEP

#: CLAUDE.md section 7 documents a Docker container on port 55432. A native
#: install on 5432 is equally valid, so the endpoint is configurable.
PG_URL = os.environ.get(
    "PARITY_TEST_PG", "postgres://parity:parity@127.0.0.1:5432/parity"
)

#: Schema the tests own outright. Kept separate from `public` so a stray run
#: against a real database cannot collide with anything a user cares about.
PG_SCHEMA = "parity_test"

#: MySQL, if one is running. MySQL has no schema inside a database, so the
#: `parity_test` database created by the setup grant is where fixtures live.
MYSQL_URL = os.environ.get(
    "PARITY_TEST_MYSQL", "mysql://parity:parity@127.0.0.1:3306/parity_test"
)


def _pg_available() -> tuple[bool, str]:
    """Can we reach PostgreSQL? Returns (yes/no, why not).

    The reason is carried through to the skip message, so a skipped run says
    what was wrong rather than just vanishing.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "psycopg is not installed"
    try:
        psycopg.connect(PG_URL, connect_timeout=5).close()
    except Exception as exc:  # pragma: no cover - depends on local services
        return False, f"no PostgreSQL at {PG_URL}: {type(exc).__name__}"
    return True, ""


def _mysql_available() -> tuple[bool, str]:
    """Can we reach MySQL? Returns (yes/no, why not)."""
    try:
        import mysql.connector
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "mysql-connector-python is not installed"
    try:
        from urllib.parse import unquote, urlparse

        u = urlparse(MYSQL_URL)
        mysql.connector.connect(
            host=u.hostname or "127.0.0.1", port=u.port or 3306,
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            database=u.path.lstrip("/") or None, connection_timeout=5,
        ).close()
    except Exception as exc:  # pragma: no cover - depends on local services
        return False, f"no MySQL at {MYSQL_URL}: {type(exc).__name__}"
    return True, ""


#: Snowflake, if one is configured. There is no local Snowflake and no free
#: service container, so this is opt-in via an env var and skips otherwise -
#: it never runs in CI, only when someone points it at a real account.
SNOWFLAKE_URL = os.environ.get("PARITY_TEST_SNOWFLAKE", "")


def _snowflake_available() -> tuple[bool, str]:
    """Can we reach Snowflake? Returns (yes/no, why not).

    Gated on the env var first so an unconfigured run skips instantly without
    importing a driver or opening a network connection.
    """
    if not SNOWFLAKE_URL:
        return False, "PARITY_TEST_SNOWFLAKE is not set"
    try:
        import snowflake.connector  # noqa: F401 - importing it is the probe
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "snowflake-connector-python is not installed"
    try:
        get_dialect(SNOWFLAKE_URL, side="A").close()
    except Exception as exc:  # pragma: no cover - depends on the account
        return False, f"no Snowflake at the configured URL: {type(exc).__name__}"
    return True, ""


def _duckdb_available() -> tuple[bool, str]:
    """Is the duckdb driver installed? Returns (yes/no, why not)."""
    try:
        import duckdb  # noqa: F401  - importing it *is* the probe
    except ImportError:  # pragma: no cover - depends on install extras
        return False, "duckdb is not installed"
    return True, ""


@pytest.fixture(scope="session")
def pg_url() -> str:
    """The PostgreSQL endpoint, or skip the test if nothing is listening."""
    ok, why = _pg_available()
    if not ok:
        pytest.skip(why)
    return PG_URL


@pytest.fixture(scope="session")
def mysql_url() -> str:
    """The MySQL endpoint, or skip the test if nothing is listening."""
    ok, why = _mysql_available()
    if not ok:
        pytest.skip(why)
    return MYSQL_URL


@pytest.fixture(scope="session")
def snowflake_url() -> str:
    """The Snowflake endpoint, or skip if none is configured/reachable."""
    ok, why = _snowflake_available()
    if not ok:
        pytest.skip(why)
    return SNOWFLAKE_URL


@pytest.fixture(scope="session")
def duckdb_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A scratch path for a DuckDB file, unique to this test session."""
    ok, why = _duckdb_available()
    if not ok:
        pytest.skip(why)
    return str(tmp_path_factory.mktemp("duckdb") / "parity_test.duckdb")


def duckdb_write(path: str):
    """Open a writable DuckDB connection for building fixture data.

    The dialect itself is read-only by construction (CLAUDE.md section 6), so
    fixtures must create their tables through the driver directly and close the
    handle before any dialect opens the file - DuckDB allows one writer.
    """
    import duckdb

    return duckdb.connect(path)


def open_duckdb(path: str, side: str = "B", float_scale: int = 6):
    """Open a read-only DuckDB dialect over an already-written file."""
    return get_dialect(f"duckdb:///{path}", side=side, float_scale=float_scale)


def open_pg(url: str, side: str = "A", float_scale: int = 6):
    """Open a read-only PostgreSQL dialect."""
    return get_dialect(url, side=side, float_scale=float_scale)


def open_mysql(url: str, side: str = "A", float_scale: int = 6):
    """Open a read-only, UTC-pinned MySQL dialect."""
    return get_dialect(url, side=side, float_scale=float_scale)


def open_snowflake(url: str, side: str = "A", float_scale: int = 6):
    """Open a UTC-pinned Snowflake dialect (DRAFT - see snowflake_dialect.py)."""
    return get_dialect(url, side=side, float_scale=float_scale)
