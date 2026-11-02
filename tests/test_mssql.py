"""SQL Server against DuckDB (and PostgreSQL), end to end, on a live server.

The proof the dialect has to pass before "supported" is earned: the same rows
built on both engines must render to byte-identical canonical text and sum to
the same checksum, every planted difference must be found exactly - nothing
missed, nothing extra - and each SQL Server trap that could produce a false
match (CONCAT truncation at 8,000 bytes, decimal division in the bucket
expression, collation-merged keys) must be shown to be closed with a planted
difference that would have slipped through it.

Skips unless PARITY_TEST_MSSQL points at a reachable server, so it never runs
in CI. The reference setup is Microsoft's Developer-edition container, bound to
localhost only:

    docker run -d --name parity-mssql -e ACCEPT_EULA=Y -e MSSQL_PID=Developer \
      -e "MSSQL_SA_PASSWORD=<strong password>" \
      -p 127.0.0.1:14330:1433 mcr.microsoft.com/mssql/server:2022-latest
    pip install -e ".[duckdb,postgres,mssql]" pytest
    set PARITY_TEST_MSSQL=mssql://sa:<pw>@127.0.0.1:14330/parity_utf8?trust_server_certificate=yes
    set PARITY_TEST_MSSQL_RO=mssql://parity_ro:<pw2>@127.0.0.1:14330/parity_utf8?trust_server_certificate=yes
    pytest tests/test_mssql.py -v

`trust_server_certificate=yes` is for that container's self-signed certificate
and nothing else. The fixtures, run through the PARITY_TEST_MSSQL login, create
three databases - UTF-8 with snapshot isolation allowed, Latin1 (code page
1252) without it, and a case-sensitive one - plus, if PARITY_TEST_MSSQL_RO is
set, its login as a member of db_datareader only. Fixture tables live in a
`parity_test` schema and are dropped at the end of the module.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import struct
import threading
from urllib.parse import unquote, urlparse, urlunparse

import pytest
from conftest import MSSQL_RO_URL, PG_SCHEMA, duckdb_write, open_duckdb, open_mssql
from float_sample import adversarial_doubles

from parity.cli import main
from parity.dialects.base import FLOAT_MAX_MAGNITUDE, canonical_float_text
from parity.dialects.mssql_dialect import parse_mssql_url
from parity.engine import bucket_bounds, diff
from parity.types import Column, LogicalType

pytestmark = pytest.mark.mssql

N = 5_000
SCHEMA = "parity_test"
WIDE_COLS = 130  # past MAX_CONCAT_ARGS (64), so the row text nests

#: The three test databases: (collation, ALLOW_SNAPSHOT_ISOLATION).
DATABASES = {
    "parity_utf8": ("Latin1_General_100_CI_AS_SC_UTF8", True),
    "parity_latin1": ("SQL_Latin1_General_CP1_CI_AS", False),
    "parity_cs": ("Latin1_General_100_CS_AS_SC", False),
}

#: Keys at both ends of bigint, so the key span is 2^64.
EXTREME_KEYS = [-(2**63), -1, 0, 1, 2**63 - 1]


def db_url(url: str, database: str) -> str:
    """The same server and login, another database."""
    u = urlparse(url)
    return urlunparse(u._replace(path="/" + database))


# --------------------------------------------------------------- reference

#: Side B, the DuckDB reference. Every awkward value the encoding has to agree
#: on is here: NULLs in every type, a NULL boolean, 1/3-style doubles and a
#: real, a decimal(38,10) rounded to six places, a year-999 date, datetime2
#: ticks whose seventh digit is truncated, datetime's 1/300 s display, an
#: instant at three offsets, char(10) padding, a GUID, money, and emoji.
DUCKDB_ORDERS = f"""
create table orders as
select i::bigint                                              as id,
       'k' || i::varchar                                      as code,
       (i % 97)::integer                                      as customer_id,
       (i % 256)::utinyint                                    as tiny,
       (i % 30000)::smallint                                  as small,
       ((i * 7 % 100000) / 100.0)::decimal(12,2)              as amount,
       case when i % 23 = 0 then null else
           (i::varchar || '000000000000000.'
            || lpad(((i * 12345) % 10000000000)::varchar, 10, '0'))::decimal(38,10)
       end                                                    as big_dec,
       case when i % 19 = 0 then null
            when i = 9 then 1e20::double / 3
            else i::double / 7 end                            as ratio,
       case when i % 29 = 0 then null else (i::double / 3)::float end as ratio4,
       case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end        as status,
       case when i % 17 = 0 then null else i % 11 = 0 end     as is_refunded,
       case i % 3 when 0 then 'ab' when 1 then 'abc' else '' end as fixed,
       date '0999-12-25' + i::integer                         as day,
       timestamp '2024-01-01 00:00:00'
           + (i % 86400) * interval '1 second'
           + (((i * 7919) % 10000000) // 10) * interval '1 microsecond' as created_at,
       timestamp '2024-01-01 00:00:00'
           + (i % 86400) * interval '1 second'
           + ((i * 10) % 1000 + case i % 3 when 1 then 3 when 2 then 7 else 0 end)
             * interval '1 millisecond'                       as legacy_at,
       (strftime(timestamp '2024-06-15 12:00:00' + (i * 61) * interval '1 second',
                 '%Y-%m-%d %H:%M:%S')
        || case i % 3 when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end
       )::timestamptz                                         as paid_at,
       ('abcdef00-0000-4000-8000-' || lpad(i::varchar, 12, '0'))::uuid as guid,
       (i * 1.2345)::decimal(19,4)                            as price,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar || ' ü✓😀日本' end     as note
from generate_series(1, {N}) as s(i)
"""

#: The same rows in T-SQL. `top (n) ... cross join sys.all_objects` is the
#: numbers table that works on 2019 too (GENERATE_SERIES is 2022+).
MSSQL_ORDERS = f"""
with n as (
    select top ({N}) cast(row_number() over (order by (select null)) as bigint) as i
    from sys.all_objects a cross join sys.all_objects b
)
select i                                                      as id,
       cast('k' + cast(i as varchar(20)) as varchar(20))      as code,
       cast(i % 97 as int)                                    as customer_id,
       cast(i % 256 as tinyint)                               as tiny,
       cast(i % 30000 as smallint)                            as small,
       cast((i * 7 % 100000) / 100.0 as decimal(12,2))        as amount,
       cast(case when i % 23 = 0 then null else
           cast(i as varchar(20)) + '000000000000000.'
           + right('0000000000' + cast((i * 12345) % 10000000000 as varchar(20)), 10)
       end as decimal(38,10))                                 as big_dec,
       case when i % 19 = 0 then null
            when i = 9 then cast(1e20 as float) / 3
            else cast(i as float) / 7 end                     as ratio,
       cast(case when i % 29 = 0 then null else cast(i as float) / 3 end as real) as ratio4,
       cast(case when i % 3 = 0 then 'paid'
            when i % 3 = 1 then 'open' else 'void' end as varchar(10)) as status,
       cast(case when i % 17 = 0 then null when i % 11 = 0 then 1 else 0 end as bit)
                                                              as is_refunded,
       cast(case i % 3 when 0 then 'ab' when 1 then 'abc' else '' end as char(10)) as fixed,
       dateadd(day, i, cast('0999-12-25' as date))            as day,
       dateadd(nanosecond, ((i * 7919) % 10000000) * 100,
           dateadd(second, i % 86400, cast('2024-01-01' as datetime2(7)))) as created_at,
       dateadd(millisecond, (i * 10) % 1000 + case i % 3 when 1 then 3 when 2 then 7 else 0 end,
           dateadd(second, i % 86400, cast('2024-01-01' as datetime))) as legacy_at,
       todatetimeoffset(dateadd(second, i * 61, cast('2024-06-15 12:00:00' as datetime2(7))),
           case i % 3 when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end) as paid_at,
       cast('ABCDEF00-0000-4000-8000-' + right('000000000000' + cast(i as varchar(12)), 12)
           as uniqueidentifier)                               as guid,
       cast(i * 1.2345 as money)                              as price,
       cast(case when i % 13 = 0 then null
            else N'note ' + cast(i as nvarchar(20)) + N' ü✓😀日本' end as nvarchar(100)) as note
{{into}} from n
"""

#: Doubles neither engine can be trusted to parse from a literal - SQL Server's
#: string-to-float conversion misrounds some 16-17 digit values and flushes
#: subnormals to zero (both verified) - so they travel as bound parameters.
SPECIAL_RATIOS = {10: 123456789.1234565, 11: -1000000000000000.5}


def _wide_select(rows: int, mssql: bool) -> str:
    """A WIDE_COLS-column table, identical in both engines' SQL."""
    cols = []
    for k in range(WIDE_COLS):
        if k % 3 == 0:
            cols.append(f"i + {k} as c{k}")
        elif k % 3 == 1:
            s = f"'v' + cast(i * {k} as varchar(20))" if mssql else f"'v' || (i * {k})"
            cols.append(f"case when i % {k + 2} = 0 then null else {s} end as c{k}")
        else:
            cols.append(
                f"cast(i * {k} as float) / 8 as c{k}"
                if mssql
                else f"(i * {k})::double / 8 as c{k}"
            )
    body = ", ".join(cols)
    if mssql:
        return (
            f"with n as (select top ({rows}) cast(row_number() over (order by (select null)) "
            "as bigint) as i from sys.all_objects) "
            f"select i as id, {body} {{into}} from n"
        )
    return f"select i::bigint as id, {body} from generate_series(1, {rows}) as s(i)"


# ------------------------------------------------------------------ fixtures


class Admin:
    """A read-write session per database, for fixtures only - never the tool."""

    def __init__(self, url: str) -> None:
        """Remember the admin login; connections open lazily per database."""
        self.url = url
        self._conns: dict[str, object] = {}

    def conn(self, database: str):
        """An autocommit admin connection to `database`."""
        import mssql_python

        if database not in self._conns:
            odbc, _ = parse_mssql_url(db_url(self.url, database))
            self._conns[database] = mssql_python.connect(odbc, autocommit=True)
        return self._conns[database]

    def run(self, database: str, sql: str, params=None):
        """Run one statement; return its rows if it produced any."""
        cur = self.conn(database).cursor()
        try:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []
        finally:
            cur.close()

    def many(self, database: str, sql: str, rows) -> None:
        """Insert many parameter rows."""
        cur = self.conn(database).cursor()
        try:
            cur.executemany(sql, rows)
        finally:
            cur.close()

    def close(self) -> None:
        """Close every admin connection."""
        for c in self._conns.values():
            c.close()


@pytest.fixture(scope="module")
def admin(mssql_url):
    """Create the three databases, the test schema and the read-only user."""
    adm = Admin(mssql_url)
    for name, (collation, snapshot) in DATABASES.items():
        if not adm.run("master", f"select 1 from sys.databases where name = '{name}'"):
            adm.run("master", f"create database [{name}] collate {collation}")
        adm.run(
            "master",
            f"alter database [{name}] set allow_snapshot_isolation "
            + ("on" if snapshot else "off"),
        )
        adm.run(
            name,
            f"if schema_id('{SCHEMA}') is null exec('create schema {SCHEMA}')",
        )
    if MSSQL_RO_URL:
        ro = urlparse(MSSQL_RO_URL)
        user, pw = unquote(ro.username or ""), unquote(ro.password or "")
        if not adm.run(
            "master", f"select 1 from sys.server_principals where name = '{user}'"
        ):
            adm.run("master", f"create login [{user}] with password = '{pw}'")
        for name in DATABASES:
            if not adm.run(
                name, f"select 1 from sys.database_principals where name = '{user}'"
            ):
                adm.run(name, f"create user [{user}] for login [{user}]")
            adm.run(name, f"alter role db_datareader add member [{user}]")
    yield adm
    for name in DATABASES:
        tables = adm.run(
            name,
            "select t.name from sys.tables t where schema_name(t.schema_id) = ?",
            (SCHEMA,),
        )
        for (t,) in tables:
            adm.run(name, f"drop table [{SCHEMA}].[{t}]")
    adm.close()


@pytest.fixture(scope="module")
def duck(tmp_path_factory) -> str:
    """Side B: the DuckDB reference tables, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("mssql_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(DUCKDB_ORDERS)
        for i, x in SPECIAL_RATIOS.items():
            con.execute("update orders set ratio = ? where id = ?", [x, i])
        con.execute(f"create table wide as {_wide_select(200, mssql=False)}")
        con.execute("create table extreme (id bigint, v varchar)")
        con.executemany(
            "insert into extreme values (?, ?)",
            [[k, f"x{i}"] for i, k in enumerate(EXTREME_KEYS)],
        )
        con.execute(
            "create table longtext as select i::bigint as id, "
            "repeat('a', 4000) as a, repeat('b', 4000) as b, repeat('c', 4000) as c, "
            "repeat('m', 10000) as m from generate_series(1, 5) as s(i)"
        )
        con.execute(
            "create table ci_keys (k varchar, v varchar); "
            "insert into ci_keys values ('a', 'lower'), ('A', 'upper'), "
            "('a ', 'trailing'), ('b', 'plain')"
        )
        cols = ", ".join(f"i * {k} as c{k}" for k in range(64))
        con.execute(
            f"create table w65 as select i::bigint as id, {cols} "
            "from generate_series(1, 50) as s(i)"
        )
        con.execute(
            "create table keyonly as select i::bigint as id from range(1, 101) s(i)"
        )
    finally:
        con.close()
    return path


def _table(admin: Admin, database: str, name: str, select: str) -> str:
    """(Re)create `parity_test.<name>` in `database` from a SELECT whose
    `{into}` placeholder marks where the INTO clause goes."""
    admin.run(database, f"drop table if exists [{SCHEMA}].[{name}]")
    admin.run(database, select.replace("{into}", f"into [{SCHEMA}].[{name}]"))
    return f"{SCHEMA}.{name}"


@pytest.fixture(scope="module")
def orders(admin) -> dict[str, str]:
    """The SQL Server `orders` table in each test database, identical to DuckDB's."""
    out = {}
    for database in DATABASES:
        out[database] = _table(admin, database, "orders", MSSQL_ORDERS)
        for i, x in SPECIAL_RATIOS.items():
            admin.run(
                database, f"update [{SCHEMA}].[orders] set ratio = ? where id = ?", (x, i)
            )
    return out


def _planted(admin: Admin, base: str, *dml: str, database: str = "parity_utf8") -> str:
    """A copy of `orders` with planted defects applied, on the SQL Server side."""
    admin.run(database, f"drop table if exists [{SCHEMA}].[{base}]")
    admin.run(database, f"select * into [{SCHEMA}].[{base}] from [{SCHEMA}].[orders]")
    for statement in dml:
        admin.run(database, statement.format(t=f"[{SCHEMA}].[{base}]"))
    return f"{SCHEMA}.{base}"


def _diff(url: str, duck: str, a_table: str, b_table: str = "main.orders", **kw):
    """Diff a SQL Server table against a DuckDB one, with a deep enough walk that
    the bucket expression runs at several levels rather than one fetch."""
    a = open_mssql(url, side="A")
    b = open_duckdb(duck, side="B")
    kw.setdefault("key", "id")
    kw.setdefault("threshold", 64)
    kw.setdefault("bisection_factor", 8)
    try:
        return diff(a, b, a_table, b_table, **kw)
    finally:
        a.close()
        b.close()


def _found(result) -> list[tuple]:
    """(key, kind, columns) for every reported difference."""
    return [(d.key, d.kind, d.columns) for d in result.diffs]


# ----------------------------------------------------------------- the hash


@pytest.mark.parametrize("database", sorted(DATABASES))
def test_the_hash_constant_agrees_for_varchar_and_nvarchar(mssql_url, admin, database):
    """648541476951500027 for 'abc' - a sixth path to it - through a varchar
    literal, an nvarchar literal, a varchar column and an nvarchar column, in a
    UTF-8, a Latin1 and a case-sensitive database. 'abd' must not reach it, so a
    constant-returning bug cannot pass."""
    admin.run(database, f"drop table if exists [{SCHEMA}].[h]")
    admin.run(
        database,
        f"create table [{SCHEMA}].[h] (v varchar(10), n nvarchar(10)); "
        f"insert into [{SCHEMA}].[h] values ('abc', N'abc'), ('abd', N'abd')",
    )
    a = open_mssql(db_url(mssql_url, database))
    try:
        varchar, nvarchar = "'abc'", "N'abc'"
        rows = a.query(
            f"select {a.hash_expr(varchar)}, {a.hash_expr(nvarchar)}, "
            f"{a.hash_expr('[v]')}, {a.hash_expr('[n]')} "
            f"from [{SCHEMA}].[h] order by v"
        )
        assert rows[0] == (648541476951500027,) * 4
        assert rows[1][2] == rows[1][3] != 648541476951500027
    finally:
        a.close()


#: Strings whose UTF-8 bytes differ from their UTF-16 and code-page bytes.
UNICODE = ["é", "ü ß", "日本語", "😀", "é", "a\x1fb", "\\N", "Ω≈ç√∫", "x" * 9000]


@pytest.mark.parametrize("database", ["parity_utf8", "parity_latin1"])
def test_text_is_hashed_as_utf8_whatever_the_database_collation(
    mssql_url, admin, database
):
    """MD5 of SQL Server's row text equals Python's MD5 of the UTF-8 bytes, for
    accents, CJK, an emoji outside the BMP and a combining mark - in a UTF-8
    and a Latin1 database. A cp1252 varchar 'é' in the Latin1 database hashes
    the same as the nvarchar one. Negative control: the composed and the
    decomposed 'é' look alike and must still hash differently."""
    admin.run(database, f"drop table if exists [{SCHEMA}].[u]")
    admin.run(
        database, f"create table [{SCHEMA}].[u] (i int, n nvarchar(max), v varchar(20))"
    )
    admin.many(
        database,
        f"insert into [{SCHEMA}].[u] values (?, ?, ?)",
        [(i, s, "é" if s == "é" else None) for i, s in enumerate(UNICODE)],
    )
    a = open_mssql(db_url(mssql_url, database))
    try:
        rows = a.query(
            "select i, convert(varchar(32), hashbytes('MD5', "
            f"{a._utf8('n')}), 2), convert(varchar(32), hashbytes('MD5', "
            f"{a._utf8('v')}), 2) from [{SCHEMA}].[u] order by i"
        )
    finally:
        a.close()
    for (_, got, got_v), s in zip(rows, UNICODE, strict=True):
        assert got.lower() == hashlib.md5(s.encode()).hexdigest(), s
        if s == "é":
            assert got_v == got
    assert rows[0][1] != rows[4][1]  # 'é' composed vs 'e' + combining acute


# ------------------------------------------------------- canonical text


def test_every_row_renders_byte_identically_to_duckdb(mssql_url, duck, orders, admin):
    """The correctness contract itself: per-row canonical text and the
    full-table checksum agree exactly - and a one-cent change moves the
    checksum but not the count."""
    a = open_mssql(mssql_url, side="A")
    b = open_duckdb(duck, side="B")
    t = orders["parity_utf8"]
    try:
        cols_a = {c.name: c for c in a.columns(t)}
        cols_b = {c.name: c for c in b.columns("main.orders")}
        assert list(cols_a) == list(cols_b)
        key_a, key_b = a.key_spec(t, "id"), b.key_spec("main.orders", "id")
        rest_a = [c for n, c in cols_a.items() if n != "id"]
        rest_b = [c for n, c in cols_b.items() if n != "id"]
        names = [c.name for c in rest_a]

        rows_a = a.fetch_range(t, key_a, rest_a, 1, N + 1)
        rows_b = b.fetch_range("main.orders", key_b, rest_b, 1, N + 1)
        assert len(rows_a) == len(rows_b) == N
        bad = [k for k in rows_b if rows_a[k] != rows_b[k]]
        assert not bad, [
            (n, x, y)
            for n, x, y in zip(names, rows_a[bad[0]], rows_b[bad[0]], strict=True)
            if x != y
        ]
        # Spot-check by value, not just by agreement - two engines can agree on
        # a wrong answer.
        r = {k: dict(zip(names, v, strict=True)) for k, v in rows_a.items() if k <= 17}
        assert r[1]["ratio"] == "0.142857" and r[1]["amount"] == "0.070000"
        assert r[10]["ratio"] == "123456789.123457"  # shortest repr, then half up
        assert r[1]["day"] == "0999-12-26"
        assert r[1]["fixed"] == "abc" and r[3]["fixed"] == "ab" and r[2]["fixed"] == ""
        assert r[1]["is_refunded"] == "false" and r[11]["is_refunded"] == "true"
        assert r[17]["is_refunded"] == "\\N"  # a NULL bit, not 'false'
        assert r[13]["note"] == "\\N"
        assert r[1]["note"] == "note 1 ü✓😀日本"
        assert r[1]["guid"] == "abcdef00-0000-4000-8000-000000000001"
        assert r[1]["price"] == "1.234500"
        assert r[1]["big_dec"] == "1000000000000000.000001"  # .0000012345
        assert r[3]["big_dec"] == "3000000000000000.000004"  # .0000037035, rounded up
        # i=1: 7919 ticks = 791.9 us, truncated to 791 - never rounded to 792.
        assert r[1]["created_at"] == "2024-01-01 00:00:01.000791"
        assert r[1]["legacy_at"] == "2024-01-01 00:00:01.013000"
        # i=1 is -08:00, so 12:01:01 local is 20:01:01 UTC.
        assert r[1]["paid_at"] == "2024-06-15 20:01:01.000000"

        sums_a = a.segment_checksums(t, key_a, rest_a, 1, N + 1, 1)
        sums_b = b.segment_checksums("main.orders", key_b, rest_b, 1, N + 1, 1)
        assert sums_a == sums_b

    finally:
        a.close()
        b.close()
    # A fresh dialect: under SNAPSHOT, a table created after the snapshot began
    # is not invisible, as on PostgreSQL - reading it raises error 3961.
    changed = _planted(
        admin, "orders_cent", "update {t} set amount = amount + 0.01 where id = 4321"
    )
    a = open_mssql(mssql_url, side="A")
    try:
        moved = a.segment_checksums(
            changed, a.key_spec(changed, "id"), rest_a, 1, N + 1, 1
        )
        assert moved[0][0] == sums_b[0][0] and moved[0][1] != sums_b[0][1]
    finally:
        a.close()


@pytest.mark.parametrize("database", sorted(DATABASES))
def test_identical_tables_match_and_download_nothing(mssql_url, duck, orders, database):
    """The headline claim, in every database collation: 5,000 mixed-type rows
    agree in four queries with zero rows downloaded. The datetime2(7) caveat
    is printed; a snapshot caveat only where snapshot isolation is off; and
    the datetimeoffset/timestamptz pair raises no timezone warning."""
    result = _diff(db_url(mssql_url, database), duck, orders[database])
    assert result.identical, _found(result)[:5]
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 4
    assert not [w for w in result.warnings if "timezone" in w], result.warnings
    assert [w for w in result.warnings if "100-nanosecond" in w and "created_at" in w]
    snapshot_warned = any("ALLOW_SNAPSHOT_ISOLATION OFF" in w for w in result.warnings)
    assert snapshot_warned is not DATABASES[database][1]


def test_the_float_sample_matches_the_canonical_spec(mssql_url, admin):
    """FLOAT is where "equivalent" spellings are not: every adversarial double
    plus 10,000 random ones render exactly `canonical_float_text`, at scale 6
    and 2, both as float and (for the values it holds) as real. Doubles go in as
    parameters - SQL Server's own literal parsing is not correctly rounded."""
    rnd = random.Random(11)
    xs = [x for x in adversarial_doubles() if abs(x) < FLOAT_MAX_MAGNITUDE]
    xs += [2.0**50 + 0.25, -(2.0**50 + 0.25), 562949953421312.25, 2.0**116 * 0.999]
    while len(xs) < 11_600:
        x = struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(64)))[0]
        if math.isfinite(x) and abs(x) < FLOAT_MAX_MAGNITUDE:
            xs.append(x)
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[fsample]")
    admin.run(
        "parity_utf8",
        f"create table [{SCHEMA}].[fsample] (i int primary key, x float, r real)",
    )
    reals = [
        struct.unpack("<f", struct.pack("<f", x))[0] if abs(x) < 3e38 else None
        for x in xs
    ]
    admin.many(
        "parity_utf8",
        f"insert into [{SCHEMA}].[fsample] values (?, ?, ?)",
        [(i, x, r) for i, (x, r) in enumerate(zip(xs, reals, strict=True))],
    )
    for scale in (6, 2):
        a = open_mssql(mssql_url, float_scale=scale)
        try:
            fx = a.normalize(Column("x", LogicalType.FLOAT, "float"))
            fr = a.normalize(Column("r", LogicalType.FLOAT, "real"))
            rows = a.query(
                f"select i, x, {fx}, {fr} from [{SCHEMA}].[fsample] order by i"
            )
        finally:
            a.close()
        bad = []
        for (_, back, text, rtext), want, r in zip(rows, xs, reals, strict=True):
            assert back == want or back == want == 0, (want, back)
            if text != canonical_float_text(want, scale):
                bad.append((want, text))
            if r is not None and rtext != canonical_float_text(r, scale):
                bad.append((r, rtext))
        assert not bad, bad[:5]


def test_a_float_past_2_116_is_refused_naming_the_side(mssql_url, admin):
    """Past 2^116 no engine can render a double exactly; the refusal must be an
    error naming side and value, never a rounded or clamped text."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[fbig]")
    admin.run("parity_utf8", f"create table [{SCHEMA}].[fbig] (x float)")
    admin.many("parity_utf8", f"insert into [{SCHEMA}].[fbig] values (?)", [(1e35,)])
    a = open_mssql(mssql_url, side="B")
    try:
        fx = a.normalize(Column("x", LogicalType.FLOAT, "float"))
        with pytest.raises(ValueError, match=r"side B: mssql.*2\^116.*e\+034"):
            a.query(f"select {fx} from [{SCHEMA}].[fbig]")
    finally:
        a.close()


def test_datetime2_is_truncated_to_microseconds_not_rounded(mssql_url, admin, tmp_path):
    """The boundary the decision is about: 23:59:59.9999995 stays on its day
    (rounding would roll it to the next one), .1234567 reads .123456 - and a
    one-microsecond difference is still found."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[ts]")
    admin.run(
        "parity_utf8",
        f"create table [{SCHEMA}].[ts] (id bigint, t datetime2(7)); "
        f"insert into [{SCHEMA}].[ts] values (1, '2024-12-31 23:59:59.9999995'), "
        "(2, '2024-02-29 13:04:05.1234567'), (3, '2024-01-01 00:00:00.0000019')",
    )
    path = str(tmp_path / "ts.duckdb")
    con = duckdb_write(path)
    con.execute(
        "create table ts as select * from (values "
        "(1::bigint, timestamp '2024-12-31 23:59:59.999999'), "
        "(2, timestamp '2024-02-29 13:04:05.123456'), "
        "(3, timestamp '2024-01-01 00:00:00.000002')) t(id, t)"
    )
    con.close()
    result = _diff(mssql_url, path, f"{SCHEMA}.ts", "main.ts")
    # Row 3 holds 1.9 us: truncated to .000001, one microsecond from .000002.
    assert _found(result) == [(3, "different", ["t"])]
    assert result.diffs[0].values_a == {"t": "2024-01-01 00:00:00.000001"}


# -------------------------------------------------------- planted differences

PLANTS = {
    "changed_decimal": (
        ["update {t} set amount = amount + 0.01 where id = 1234"],
        [(1234, "different", ["amount"])],
    ),
    "deleted_row": (["delete from {t} where id = 777"], [(777, "only_in_b", None)]),
    "inserted_row": (
        ["insert into {t} (id, code, status) values (999999999, 'k999999999', 'new')"],
        [(999999999, "only_in_a", None)],
    ),
    # id 13 is a multiple of 13, so `note` is NULL on the DuckDB side.
    "null_versus_empty": (
        ["update {t} set note = N'' where id = 13"],
        [(13, "different", ["note"])],
    ),
    # id 2 is FALSE on the DuckDB side (2 % 11 != 0, 2 % 17 != 0).
    "null_versus_false": (
        ["update {t} set is_refunded = null where id = 2"],
        [(2, "different", ["is_refunded"])],
    ),
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_each_planted_difference_is_found_exactly(mssql_url, duck, orders, admin, plant):
    """Every planted defect is reported as that row and that column, and
    nothing else is - a parity tool that misses one, or invents one, is broken."""
    dml, expected = PLANTS[plant]
    table = _planted(admin, f"orders_{plant}", *dml)
    result = _diff(mssql_url, duck, table)
    got = [
        (d.key, d.kind, d.columns if cols is not None else None)
        for d, (_, _, cols) in zip(result.diffs, expected, strict=True)
    ]
    assert got == expected
    assert len(result.diffs) == len(expected)
    assert not result.identical


@pytest.mark.parametrize("key", ["code", "guid"])
def test_text_and_guid_keys_are_hashed_and_reported_by_their_real_value(
    mssql_url, duck, orders, admin, key
):
    """A varchar key and a uniqueidentifier key go through the hashed-bucket
    path; the planted change comes back under the key's real (lower-case for a
    GUID) text, not its hash."""
    table = _planted(
        admin, f"orders_{key}key", "update {t} set status = 'gone' where id = 4242"
    )
    result = _diff(mssql_url, duck, table, key=key)
    want = "k4242" if key == "code" else "abcdef00-0000-4000-8000-000000004242"
    assert _found(result) == [(want, "different", ["status"])]


def test_keys_that_a_case_insensitive_collation_merges_are_still_unique(
    mssql_url, duck, admin
):
    """'a', 'A' and 'a ' are three keys - but equal under the default CI
    collation, and trailing spaces are ignored under every collation. Counted
    as text they would raise a false "not unique"; counted by SHA-256 they do
    not, and a planted change on 'A' is found as exactly 'A'."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[ci_keys]")
    admin.run(
        "parity_utf8",
        f"create table [{SCHEMA}].[ci_keys] (k nvarchar(10), v nvarchar(20)); "
        f"insert into [{SCHEMA}].[ci_keys] values (N'a', N'lower'), (N'A', N'upper'), "
        "(N'a ', N'trailing'), (N'b', N'plain')",
    )
    a = open_mssql(mssql_url)
    try:
        assert a.query(f"select count(distinct k) from [{SCHEMA}].[ci_keys]")[0][0] == 2
    finally:
        a.close()
    clean = _diff(mssql_url, duck, f"{SCHEMA}.ci_keys", "main.ci_keys", key="k")
    assert clean.identical, _found(clean)
    admin.run(
        "parity_utf8",
        f"update [{SCHEMA}].[ci_keys] set v = N'UPPER' where k = N'A' collate Latin1_General_100_BIN2",
    )
    result = _diff(mssql_url, duck, f"{SCHEMA}.ci_keys", "main.ci_keys", key="k")
    assert _found(result) == [("A", "different", ["v"])]


# ---------------------------------------------- false-match traps: width


def test_a_table_wider_than_the_nesting_limit_agrees(mssql_url, duck, admin):
    """130 columns nest the concat_ws tree (64 + 64 + 3 counting the key); they
    must agree, and one changed cell deep in the row must be found as exactly
    that cell."""
    wide = _table(admin, "parity_utf8", "wide", _wide_select(200, mssql=True))
    clean = _diff(mssql_url, duck, wide, "main.wide", threshold=16)
    assert clean.identical and clean.stats.rows_downloaded == 0
    admin.run("parity_utf8", f"update [{SCHEMA}].[wide] set c99 = c99 + 1 where id = 150")
    result = _diff(mssql_url, duck, wide, "main.wide", threshold=16)
    assert _found(result) == [(150, "different", ["c99"])]


def test_a_difference_past_8000_characters_of_row_text_is_found(mssql_url, duck, admin):
    """The CONCAT truncation trap. Three nvarchar(4000) columns join to 12,002
    characters, and CONCAT_WS of non-max types returns at most 4,000 - so a
    change at character 9,500 of the row text would vanish from the checksum.
    So would one at 9,500 of a 10,000-character nvarchar(max). Both must be
    found, and the clean table must still download nothing."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[longtext]")
    admin.run(
        "parity_utf8",
        f"create table [{SCHEMA}].[longtext] (id bigint, a nvarchar(4000), "
        "b nvarchar(4000), c nvarchar(4000), m nvarchar(max))",
    )
    admin.many(
        "parity_utf8",
        f"insert into [{SCHEMA}].[longtext] values (?, ?, ?, ?, ?)",
        [(i, "a" * 4000, "b" * 4000, "c" * 4000, "m" * 10000) for i in range(1, 6)],
    )
    t = f"{SCHEMA}.longtext"
    # Negative control: the trap is real on this server. Joined as stored, the
    # three columns come back cut to 4,000 characters; the dialect's row text,
    # every part nvarchar(max), keeps all 12,002.
    a = open_mssql(mssql_url)
    try:
        cols = [c for c in a.columns(t) if c.name in ("a", "b", "c")]
        raw, ours = a.query(
            f"select len(concat_ws(nchar(31), a, b, c)), len({a.row_text(cols)}) "
            f"from [{SCHEMA}].[longtext] where id = 1"
        )[0]
        assert (raw, ours) == (4000, 12002)
    finally:
        a.close()
    clean = _diff(mssql_url, duck, t, "main.longtext")
    assert clean.identical and clean.stats.rows_downloaded == 0
    # Row 2: character 1,500 of column c is character ~9,500 of the row text.
    admin.run(
        "parity_utf8",
        f"update [{SCHEMA}].[longtext] set c = stuff(c, 1500, 1, N'X') where id = 2",
    )
    admin.run(
        "parity_utf8",
        f"update [{SCHEMA}].[longtext] set m = stuff(m, 9500, 1, N'X') where id = 4",
    )
    result = _diff(mssql_url, duck, t, "main.longtext")
    assert _found(result) == [(2, "different", ["c"]), (4, "different", ["m"])]


def test_concat_ws_never_receives_a_single_value(mssql_url, duck, admin):
    """T-SQL's CONCAT_WS refuses one value. It would get one for a 65-part row
    (64 + a remainder of 1) and for a key-only table; both must run, agree, and
    find a planted difference."""
    cols = ", ".join(f"i * {k} as c{k}" for k in range(64))
    w65 = _table(
        admin,
        "parity_utf8",
        "w65",
        "with n as (select top (50) cast(row_number() over (order by (select null)) "
        f"as bigint) as i from sys.all_objects)\nselect i as id, {cols} {{into}} from n",
    )
    assert _diff(mssql_url, duck, w65, "main.w65").identical
    admin.run("parity_utf8", f"update [{SCHEMA}].[w65] set c63 = -1 where id = 7")
    assert _found(_diff(mssql_url, duck, w65, "main.w65")) == [(7, "different", ["c63"])]

    keyonly = _table(
        admin,
        "parity_utf8",
        "keyonly",
        "with n as (select top (100) cast(row_number() over (order by (select null)) "
        "as bigint) as i from sys.all_objects)\nselect i as id {into} from n",
    )
    assert _diff(mssql_url, duck, keyonly, "main.keyonly").identical
    admin.run("parity_utf8", f"delete from [{SCHEMA}].[keyonly] where id = 50")
    result = _diff(mssql_url, duck, keyonly, "main.keyonly")
    assert [(d.key, d.kind) for d in result.diffs] == [(50, "only_in_b")]


# --------------------------------------------- false-match traps: buckets


def test_keys_at_both_ends_of_bigint(mssql_url, duck, admin):
    """A key span of 2^64: the walk must complete, and still find a change on
    the largest key, the one an off-by-one at the top of the range would skip."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[extreme]")
    admin.run(
        "parity_utf8", f"create table [{SCHEMA}].[extreme] (id bigint, v varchar(20))"
    )
    admin.many(
        "parity_utf8",
        f"insert into [{SCHEMA}].[extreme] values (?, ?)",
        [(k, f"x{i}") for i, k in enumerate(EXTREME_KEYS)],
    )
    t = f"{SCHEMA}.extreme"
    clean = _diff(mssql_url, duck, t, "main.extreme", threshold=1, bisection_factor=2)
    assert clean.identical
    admin.run(
        "parity_utf8",
        f"update [{SCHEMA}].[extreme] set v = 'changed' where id = {2**63 - 1}",
    )
    result = _diff(mssql_url, duck, t, "main.extreme", threshold=1, bisection_factor=2)
    assert [(d.key, d.kind) for d in result.diffs] == [(2**63 - 1, "different")]


def test_the_sql_bucket_is_exactly_the_python_one_at_every_boundary(mssql_url, admin):
    """The decimal-division trap. Decimal `/` in T-SQL rounds its quotient, so
    a key one below a bucket boundary can land in the next bucket on this side
    only. For random ranges up to 2^64 wide, every key at and either side of
    every boundary must get the bucket `bucket_bounds` assigns it.

    The documented risk did not show on SQL Server 2022: its decimal division
    *truncates* at the reduced scale (2/3 is 0.666666), so the naive `/` placed
    every key correctly too - the dialect's modulo form is exact by
    construction rather than by an undocumented rounding mode. The negative
    control is therefore on the data: the keys include ones whose quotient is
    within 5e-7 below an integer, which a rounding division would misplace."""
    rnd = random.Random(5)
    cases = []
    for _ in range(40):
        lo = rnd.randint(-(2**63), 2**63 - 2)
        hi = rnd.randint(
            lo + 2, min(2**63, lo + rnd.choice([10**3, 10**9, 10**15, 2**64]))
        )
        cases.append((lo, hi, rnd.choice([2, 7, 32, 250])))
    cases.append((-(2**63), 2**63, 32))
    keys: set[int] = set()
    for lo, hi, n in cases:
        for i in range(n):
            b_lo, _ = bucket_bounds(i, lo, hi, n)
            keys |= {k for k in (b_lo - 1, b_lo, b_lo + 1) if lo <= k < hi}
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[bkeys]")
    admin.run("parity_utf8", f"create table [{SCHEMA}].[bkeys] (k bigint)")
    admin.many(
        "parity_utf8", f"insert into [{SCHEMA}].[bkeys] values (?)", [(k,) for k in keys]
    )
    a = open_mssql(mssql_url)
    near_integer = 0
    try:
        for lo, hi, n in cases:
            offset = f"({a.wide_int('[k]')} - ({lo}))"
            exact = a.int_div(f"{offset} * {n}", f"({hi - lo})")
            naive = f"cast(({offset} * {n}) / ({hi - lo}) as bigint)"
            rows = a.query(
                f"select k, {exact}, {naive} from [{SCHEMA}].[bkeys] "
                f"where k >= {lo} and k <= {hi - 1}"
            )
            for k, got, rough in rows:
                assert got == (k - lo) * n // (hi - lo), (lo, hi, n, k)
                assert rough == got  # truncation, as observed on 2022
                num, den = (k - lo) * n, hi - lo
                fraction = num % den
                near_integer += 2 * 10**6 * (den - fraction) < den
    finally:
        a.close()
    assert near_integer > 0


# ------------------------------------------------- snapshot and read-only


def _session_isolation(admin: Admin, spid: int) -> int:
    """transaction_isolation_level of a session: 2 read committed, 3
    repeatable read, 5 snapshot."""
    return admin.run(
        "master",
        "select transaction_isolation_level from sys.dm_exec_sessions where session_id = ?",
        (spid,),
    )[0][0]


def test_with_snapshot_isolation_a_live_table_is_read_at_one_snapshot(mssql_url, admin):
    """CLAUDE.md 4.9: a row committed mid-walk must not be seen by later levels.
    A pinned dialect keeps its count; a fresh one sees the new row - the
    negative control proving the insert really landed. And the open snapshot
    does not block a writer."""
    admin.run("parity_utf8", f"drop table if exists [{SCHEMA}].[live]")
    admin.run(
        "parity_utf8",
        f"select top (100) cast(row_number() over (order by (select null)) as bigint) "
        f"as id into [{SCHEMA}].[live] from sys.all_objects",
    )
    t = f"{SCHEMA}.live"
    a = open_mssql(mssql_url)
    try:
        assert a.snapshot and not a.caveats([])
        key = a.key_spec(t, "id")
        assert a.key_stats(t, key).rows == 100
        assert _session_isolation(admin, a.query("select @@spid")[0][0]) == 5
        admin.run("parity_utf8", "set lock_timeout 5000")
        admin.run("parity_utf8", f"insert into [{SCHEMA}].[live] (id) values (101)")
        admin.run("parity_utf8", f"update [{SCHEMA}].[live] set id = -id where id = 1")
        assert a.key_stats(t, key).rows == 100
        assert a.key_stats(t, key).lo == 1
    finally:
        a.close()
    fresh = open_mssql(mssql_url)
    try:
        stats = fresh.key_stats(t, fresh.key_spec(t, "id"))
        assert (stats.rows, stats.lo) == (101, -1)
    finally:
        fresh.close()


def test_without_snapshot_isolation_the_run_warns_and_never_blocks_writers(
    mssql_url, duck, orders, admin
):
    """In a database without ALLOW_SNAPSHOT_ISOLATION the session is READ
    COMMITTED - not REPEATABLE READ, whose held locks would stall a live
    system - so a writer is not blocked by an open diff, and the output says
    the walk was not single-snapshot."""
    url = db_url(mssql_url, "parity_latin1")
    a = open_mssql(url)
    try:
        assert not a.snapshot
        t = orders["parity_latin1"]
        key = a.key_spec(t, "id")
        a.key_stats(t, key)  # opens the transaction and reads the table
        assert _session_isolation(admin, a.query("select @@spid")[0][0]) == 2
        done = threading.Event()

        def write() -> None:
            """Update a row from another session, under a lock timeout."""
            admin.run("parity_latin1", "set lock_timeout 5000")
            admin.run(
                "parity_latin1",
                f"update [{SCHEMA}].[orders] set status = status where id = 1",
            )
            done.set()

        writer = threading.Thread(target=write)
        writer.start()
        writer.join(10)
        assert done.is_set()
        assert a.key_stats(t, key).rows == N
    finally:
        a.close()
    result = _diff(url, duck, orders["parity_latin1"])
    assert [w for w in result.warnings if "side A (mssql)" in w and "READ COMMITTED" in w]


@pytest.fixture
def ro_url(admin) -> str:
    """The read-only login's URL, or skip."""
    if not MSSQL_RO_URL:
        pytest.skip("PARITY_TEST_MSSQL_RO is not set")
    return MSSQL_RO_URL


def test_the_diff_runs_as_a_read_only_login(ro_url, duck, orders, admin):
    """A db_datareader-only login runs a full diff and finds a planted change."""
    table = _planted(admin, "orders_ro", "update {t} set amount = 0 where id = 99")
    result = _diff(ro_url, duck, table)
    assert _found(result) == [(99, "different", ["amount"])]


def test_a_write_is_refused_by_the_dialect_and_by_the_server(ro_url, orders):
    """Two layers: `query` refuses a non-SELECT before sending it, and the
    login itself has no write permission - checked by going around `query`."""
    a = open_mssql(ro_url)
    try:
        with pytest.raises(ValueError, match="non-SELECT"):
            a.query(f"delete from [{SCHEMA}].[orders]")
        cur = a._conn.cursor()
        with pytest.raises(Exception, match=r"(?i)permission was denied"):
            cur.execute(f"delete from [{SCHEMA}].[orders] where id = 1")
        cur.close()
    finally:
        a.close()
    probe = open_mssql(ro_url)
    try:
        assert probe.query(f"select count_big(*) from [{SCHEMA}].[orders]")[0][0] == N
    finally:
        probe.close()


def test_a_missing_table_names_the_side_and_hints_at_grants(mssql_url, orders):
    """The error must say which side, and that a missing GRANT looks the same."""
    a = open_mssql(db_url(mssql_url, "parity_cs"), side="B")
    try:
        with pytest.raises(ValueError) as exc:
            a.columns(f"{SCHEMA}.ORDERS")
        message = str(exc.value)
        assert "side B: mssql" in message and "GRANT SELECT" in message
        assert f"{SCHEMA}.orders" in message  # the case-sensitive near miss
    finally:
        a.close()


# ------------------------------------------------------ against PostgreSQL

PG_BUILD = f"""
create table {PG_SCHEMA}_mssql.orders as
select i::bigint                                              as id,
       ((i * 7 % 100000) / 100.0)::numeric(12,2)              as amount,
       (case when i % 3 = 0 then 'paid'
             when i % 3 = 1 then 'open' else 'void' end)::varchar(10) as status,
       case when i % 17 = 0 then null else i % 11 = 0 end     as is_refunded,
       (case when i % 3 = 0 then 'ab' when i % 3 = 1 then 'abc' else '' end)::char(10) as fixed,
       date '0999-12-25' + i::integer                         as day,
       (to_char(timestamp '2024-06-15 12:00:00' + (i * 61) * interval '1 second',
                'YYYY-MM-DD HH24:MI:SS')
        || case i % 3 when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end
       )::timestamptz                                         as paid_at,
       ('abcdef00-0000-4000-8000-' || lpad(i::text, 12, '0'))::uuid as guid,
       case when i % 13 = 0 then null
            else 'note ' || i::text || ' ü✓😀日本' end         as note
from generate_series(1, {N}) as s(i)
"""


def test_the_cli_finds_planted_differences_between_sql_server_and_postgres(
    mssql_url, pg_url, orders, admin, capsys
):
    """The headline workflow, through the CLI, with neither side the DuckDB
    reference: SQL Server -> PostgreSQL, four planted differences (a changed
    decimal, a deleted row, NULL versus '', NULL versus FALSE) found exactly,
    exit code 1. The unchanged pair exits 0."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {PG_SCHEMA}_mssql cascade")
        con.execute(f"create schema {PG_SCHEMA}_mssql")
        con.execute(PG_BUILD)
    finally:
        con.close()
    cols = "amount,status,is_refunded,fixed,day,paid_at,guid,note"
    argv = [
        "diff", "--a", mssql_url, "--a-table", orders["parity_utf8"],
        "--b", pg_url, "--b-table", f"{PG_SCHEMA}_mssql.orders",
        "--key", "id", "--columns", cols, "--json",
    ]  # fmt: skip
    assert main(argv) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["identical"] and clean["stats"]["rows_downloaded"] == 0

    table = _planted(
        admin,
        "orders_pg",
        "update {t} set amount = amount + 0.01 where id = 1234",
        "delete from {t} where id = 777",
        "update {t} set note = N'' where id = 26",
        "update {t} set is_refunded = null where id = 2",
    )
    argv[4] = table
    assert main(argv) == 1
    got = json.loads(capsys.readouterr().out)
    assert [(d["key"], d["kind"], d["columns"]) for d in got["differences"]] == [
        (2, "different", ["is_refunded"]),
        (26, "different", ["note"]),
        (777, "only_in_b", sorted(cols.split(","))),
        (1234, "different", ["amount"]),
    ]
