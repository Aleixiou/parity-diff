"""The identical check across engines, over key types and values the core
integration suite does not reach.

`test_integration.py` proves identical tables match across PostgreSQL and DuckDB
with an *integer* key. This file widens the identical check where a cross-engine
abnormality would actually hide: the *hashed* key paths (text, composite, uuid),
which bucket by a hash of the key's rendered text, and edge-case values
(bigint extremes, non-finite floats, high-precision decimals, astral-plane
Unicode, an all-NULL row). Every table is built by a deterministic expression
spelled identically on both engines, so any reported difference is the tool
rendering the same value two ways - the exact false-positive the identical check
must never produce.

Skips cleanly without PostgreSQL.
"""

from __future__ import annotations

import hashlib

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb, open_pg

from parity.engine import diff

pytestmark = pytest.mark.postgres

N = 5_000

# md5(i) agrees across engines, giving a deterministic text/uuid key. Each SELECT
# is portable SQL that produces byte-identical data on PostgreSQL and DuckDB.
TABLES = {
    "text_key": (
        "uid",
        f"""
        select md5(i::varchar)                            as uid,
               (i % 97)::integer                          as customer_id,
               ((i * 7 % 100000) / 100.0)::decimal(12,2)  as amount,
               case when i % 13 = 0 then null
                    else 'n' || i::varchar end            as note
        from generate_series(1, {N}) as s(i)
        """,
    ),
    "composite_key": (
        "grp,id",
        f"""
        select (i % 100)::integer                         as grp,
               i::bigint                                  as id,
               ((i * 3 % 50000) / 100.0)::decimal(12,2)   as amount,
               (i % 7 = 0)                                as flag
        from generate_series(1, {N}) as s(i)
        """,
    ),
    "uuid_key": (
        "uid",
        f"""
        select cast(
                 substr(md5(i::varchar), 1, 8) || '-' ||
                 substr(md5(i::varchar), 9, 4) || '-' ||
                 substr(md5(i::varchar), 13, 4) || '-' ||
                 substr(md5(i::varchar), 17, 4) || '-' ||
                 substr(md5(i::varchar), 21, 12) as uuid)  as uid,
               (i % 97)::integer                           as customer_id
        from generate_series(1, {N}) as s(i)
        """,
    ),
    # One row per hostile value: bigint extremes, a non-finite float, a
    # high-precision decimal, an astral-plane emoji, an empty string, and an
    # all-NULL row. `{{f}}` is the engine's double type, filled at build time.
    "edge_values": (
        "id",
        """
        select cast(1 as bigint)                          as id,
               cast('9223372036854775807' as bigint)      as big,
               cast(123456789012.345678 as decimal(38,6)) as dec,
               cast('Infinity' as {f})                    as flt,
               timestamp '2024-02-29 13:04:05.123456'     as ts,
               'añ日\U0001f600'                  as txt
        union all select cast(2 as bigint), cast('-9223372036854775808' as bigint),
               cast(-0.000001 as decimal(38,6)), cast('-Infinity' as {f}),
               timestamp '2000-01-01 00:00:00', ''
        union all select cast(3 as bigint), cast(0 as bigint),
               cast(0 as decimal(38,6)), cast('NaN' as {f}),
               timestamp '2099-12-31 23:59:59.999999', null
        union all select cast(4 as bigint), null,
               null, cast(1.5 as {f}), null, 'plain'
        """,
    ),
}


def _build(cursor_exec, table_sql, float_type: str) -> None:
    """Create every table via the given execute callable (one engine).

    `float_type` is that engine's double spelling - PostgreSQL wants
    ``double precision`` where DuckDB wants ``double`` - filled into the edge
    table's non-finite-float columns.
    """
    for name, (_key, select) in TABLES.items():
        cursor_exec(f"create table {table_sql(name)} as {select.format(f=float_type)}")


@pytest.fixture(scope="module")
def duck(tmp_path_factory):
    """Side B: all tables in one DuckDB file, opened read-only."""
    path = str(tmp_path_factory.mktemp("ident_live") / "b.duckdb")
    con = duckdb_write(path)
    try:
        _build(con.execute, lambda n: f"main.{n}", "double")
    finally:
        con.close()
    d = open_duckdb(path, side="B")
    yield d
    d.close()


SCHEMA = f"{PG_SCHEMA}_identlive"


@pytest.fixture(scope="module")
def pg(pg_url):
    """Side A: all tables in a schema the test owns, opened read-only."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {SCHEMA} cascade")
        con.execute(f"create schema {SCHEMA}")
        _build(con.execute, lambda n: f"{SCHEMA}.{n}", "double precision")
    finally:
        con.close()
    d = open_pg(pg_url, side="A")
    yield d
    d.close()


@pytest.mark.parametrize("name", list(TABLES))
def test_identical_across_engines_by_key_type(pg, duck, name):
    """The same table, same data, one hashed key type at a time: identical,
    and not one row moved across the network."""
    key = TABLES[name][0].split(",")  # a list, so composite keys resolve
    result = diff(pg, duck, f"{SCHEMA}.{name}", f"main.{name}", key)
    assert result.identical, (
        f"{name}: identical data reported {len(result.diffs)} diffs "
        f"{[(d.key, d.kind, d.columns) for d in result.diffs[:3]]}"
    )
    assert result.stats.rows_downloaded == 0


def test_a_representation_change_across_engines_reads_identical(pg_url, tmp_path):
    """The migration scenario: the same values stored with *different declared
    types* on each engine - integer vs bigint, numeric(12,2) vs double,
    varchar vs text - must still read identical end to end. Storing a value a
    different way is not changing it; the diff must not mistake it for one.
    """
    import psycopg

    n = 2_000
    schema = f"{PG_SCHEMA}_repr"
    pg_sql = f"""
        create table {schema}.repr as
        select i::bigint                                      as id,
               (i / 100.0)::numeric(12,2)                     as amount,
               ('r' || i::text)::varchar(50)                  as name,
               (timestamp '2024-01-01 00:00:00'
                    + (i % 86400) * interval '1 second')      as ts
        from generate_series(1, {n}) as s(i)
    """
    duck_sql = f"""
        create table repr as
        select i::integer                                    as id,
               (i / 100.0)::double                           as amount,
               ('r' || i::varchar)                           as name,
               (timestamp '2024-01-01 00:00:00'
                    + (i % 86400) * interval '1 second')      as ts
        from generate_series(1, {n}) as s(i)
    """
    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {schema} cascade")
        con.execute(f"create schema {schema}")
        con.execute(pg_sql)
    finally:
        con.close()
    path = str(tmp_path / "repr.duckdb")
    dcon = duckdb_write(path)
    try:
        dcon.execute(duck_sql)
    finally:
        dcon.close()

    a = open_pg(pg_url, side="A")
    b = open_duckdb(path, side="B")
    try:
        result = diff(a, b, f"{schema}.repr", "main.repr", "id")
    finally:
        a.close()
        b.close()
        con = psycopg.connect(pg_url, autocommit=True)
        try:
            con.execute(f"drop schema if exists {schema} cascade")
        finally:
            con.close()

    assert result.identical, (
        "a pure representation change was reported as a data difference: "
        f"{[(d.key, d.columns) for d in result.diffs[:5]]}"
    )
    assert result.stats.rows_downloaded == 0


def test_a_hashed_key_table_still_finds_a_planted_difference(pg, tmp_path):
    """The other half of the promise: the identical check on a hashed key must
    never MISS a real difference either. Plant one changed row in a text-keyed
    copy and confirm the walk reports exactly it, keyed by the real text
    identity - never by the 60-bit bucket hash, which could collide.
    """
    path = str(tmp_path / "text_key_perturbed.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(f"create table text_key as {TABLES['text_key'][1]}")
        # md5('1234') is the uid of the row generated for i = 1234.
        con.execute("update text_key set amount = amount + 0.01 where uid = md5('1234')")
    finally:
        con.close()

    b = open_duckdb(path, side="B")
    try:
        result = diff(pg, b, f"{SCHEMA}.text_key", "main.text_key", "uid")
    finally:
        b.close()

    # The same md5 the engines compute, for cross-engine agreement not security.
    uid = hashlib.md5(b"1234", usedforsecurity=False).hexdigest()
    assert [(d.key, d.kind) for d in result.diffs] == [(uid, "different")]
    assert result.diffs[0].columns == ["amount"]


def test_a_very_wide_table_reads_identical_across_engines(pg_url, tmp_path):
    """A table with more columns than PostgreSQL's 100-argument `concat_ws`
    limit forces `row_text` to build a nested tree of `concat_ws` calls. The two
    engines must build the *same* tree over the same values, or an identical
    wide table - an ordinary denormalised fact table - would report every row as
    different. Also plants one change to prove the wide path finds a real one.
    """
    import psycopg

    ncols = 120  # over PostgreSQL's 99-argument concat_ws limit
    cols = ", ".join(f"('v' || (i + {n})::varchar) as c{n}" for n in range(ncols))
    select = f"select i::bigint as id, {cols} from generate_series(1, 500) as s(i)"
    schema = f"{PG_SCHEMA}_wide"

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {schema} cascade")
        con.execute(f"create schema {schema}")
        con.execute(f"create table {schema}.wide as {select}")
    finally:
        con.close()
    path = str(tmp_path / "wide.duckdb")
    dcon = duckdb_write(path)
    try:
        dcon.execute(f"create table wide as {select}")
        dcon.execute("create table wide_p as select * from wide")
        dcon.execute("update wide_p set c50 = 'CHANGED' where id = 321")
    finally:
        dcon.close()

    a = open_pg(pg_url, side="A")
    b = open_duckdb(path, side="B")
    try:
        same = diff(a, b, f"{schema}.wide", "main.wide", "id")
        changed = diff(a, b, f"{schema}.wide", "main.wide_p", "id")
    finally:
        a.close()
        b.close()
        con = psycopg.connect(pg_url, autocommit=True)
        try:
            con.execute(f"drop schema if exists {schema} cascade")
        finally:
            con.close()

    assert same.identical, (
        "a 120-column identical table reported differences - the nested "
        f"concat_ws trees disagree: {[(d.key, d.columns) for d in same.diffs[:3]]}"
    )
    assert same.stats.rows_downloaded == 0
    assert [(d.key, d.kind) for d in changed.diffs] == [(321, "different")]
    assert changed.diffs[0].columns == ["c50"]
