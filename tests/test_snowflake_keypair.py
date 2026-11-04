"""Snowflake key-pair sign-in, live: a real login with a key, then a diff
against DuckDB that must find exactly one planted difference.

Skips unless PARITY_TEST_SNOWFLAKE_KEYPAIR holds a key-pair URL,
``snowflake://USER@account/DB/SCHEMA?warehouse=WH&private_key_file=<path>``,
with the key's passphrase (if it has one) in PARITY_SNOWFLAKE_KEY_PASSPHRASE.
The user needs the RSA public key registered (`ALTER USER ... SET
RSA_PUBLIC_KEY`) and may create a table in that schema: the fixture table is
built through the same key-pair login, so the test proves the key works for
the whole session, not just the handshake. It never runs in CI.

The offline twin, `test_snowflake_connect_offline.py`, pins exactly what the
connector receives; this file proves Snowflake accepts it.
"""

from __future__ import annotations

import pytest
from conftest import duckdb_write, open_duckdb, secret_env

from parity.dialects.base import get_dialect
from parity.engine import diff

pytestmark = pytest.mark.snowflake

KEYPAIR_URL = secret_env("PARITY_TEST_SNOWFLAKE_KEYPAIR")

N = 500


@pytest.fixture(scope="module")
def keypair_url() -> str:
    """The key-pair URL, or skip. Never printed: it names the account."""
    if not KEYPAIR_URL:
        pytest.skip("PARITY_TEST_SNOWFLAKE_KEYPAIR is not set")
    pytest.importorskip("snowflake.connector")
    return KEYPAIR_URL


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory) -> str:
    """Side B: N rows of (id, amount, note) in DuckDB."""
    path = str(tmp_path_factory.mktemp("sf_keypair") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(
            "create table kp as select i::bigint as id, "
            "(i / 4.0)::decimal(12,2) as amount, 'note ' || i::varchar as note "
            f"from generate_series(1, {N}) as s(i)"
        )
    finally:
        con.close()
    return path


def _build(url: str, plant: bool) -> None:
    """The Snowflake twin of the DuckDB table, through the key-pair login.

    The only write in this file; the parity dialect itself issues SELECT
    only, and this goes through its raw connection for that reason.
    """
    a = get_dialect(url, side="A")
    cur = a._conn.cursor()
    try:
        cur.execute(
            "create or replace table PARITY_KEYPAIR "
            "(id number(38,0), amount number(12,2), note varchar)"
        )
        cur.execute(
            "insert into PARITY_KEYPAIR select id, id / 4.0, 'note ' || id::varchar "
            "from (select row_number() over (order by seq4()) as id "
            f"from table(generator(rowcount => {N})))"
        )
        if plant:
            cur.execute("update PARITY_KEYPAIR set note = 'changed' where id = 321")
    finally:
        cur.close()
        a.close()


def _diff(url: str, duck_path: str):
    """Diff the key-pair Snowflake table against DuckDB."""
    a = get_dialect(url, side="A")
    b = open_duckdb(duck_path, side="B")
    try:
        return diff(a, b, "PARITY_KEYPAIR", "main.kp", "id")
    finally:
        a.close()
        b.close()


def test_a_key_pair_login_runs_the_session_pins(keypair_url):
    """Signed in by key, the session is UTC-pinned like a password one."""
    a = get_dialect(keypair_url, side="A")
    try:
        assert a.query("show parameters like 'TIMEZONE' in session")[0][1] == "UTC"
    finally:
        a.close()


def test_a_key_pair_diff_finds_exactly_the_planted_difference(keypair_url, duck_path):
    """Identical first (no false alarm), then one changed note - found on
    exactly that row and column, and nothing else."""
    _build(keypair_url, plant=False)
    clean = _diff(keypair_url, duck_path)
    assert clean.identical, [(d.key, d.columns) for d in clean.diffs[:5]]
    _build(keypair_url, plant=True)
    result = _diff(keypair_url, duck_path)
    assert [(d.key, d.kind, d.columns) for d in result.diffs] == [
        (321, "different", ["NOTE"])  # reported under side A's stored name
    ]
    assert not result.identical
