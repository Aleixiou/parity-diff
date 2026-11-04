"""Oracle against DuckDB (and PostgreSQL), end to end, on a live server.

The proof the dialect has to pass before "supported" is earned: the same rows
built on both engines render to byte-identical canonical text and sum to the
same checksum, every planted difference is found exactly - nothing missed,
nothing extra - and each Oracle trap that could produce a false match (the
4,000-byte VARCHAR2 limit, a national-character row, empty string as NULL,
hostile session NLS settings, a live table) is shown closed with a planted
difference that would have slipped through it.

Skips unless PARITY_TEST_ORACLE is set, so it never runs in CI. The reference
setup is the Oracle Free container, bound to localhost:

    docker run -d --name parity-oracle -p 1521:1521 -e ORACLE_PASSWORD=parity \
      -e APP_USER=parity -e APP_USER_PASSWORD=parity gvenzl/oracle-free:23-slim-faststart
    pip install -e ".[duckdb,postgres,oracle]" pytest
    set PARITY_TEST_ORACLE=1        (or a full oracle:// URL)
    pytest tests/test_oracle.py -v

`1` means `oracle://parity:parity@127.0.0.1:1521/FREEPDB1`. Fixture tables are
`ORA_*` in that login's schema and are dropped at the end of the module. Tests
that need a second login (read-only, hostile NLS) create it through the
container's SYSTEM account (`PARITY_TEST_ORACLE_SYSTEM`, default password
`parity`), and skip when it is not available.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import struct
import time
from decimal import Decimal
from urllib.parse import urlparse, urlunparse

import pytest
from conftest import PG_SCHEMA, duckdb_write, open_duckdb
from float_sample import adversarial_doubles

from parity.cli import main
from parity.dialects.base import (
    FLOAT_MAX_MAGNITUDE,
    NULL_SENTINEL,
    canonical_float_text,
    get_dialect,
)
from parity.dialects.oracle_dialect import PARITY_MD5, parse_oracle_url
from parity.engine import bucket_bounds, diff
from parity.types import Column, LogicalType

pytestmark = pytest.mark.oracle

N = 5_000
WIDE_COLS = 500  # far past one VARCHAR2's worth of row text

#: SYSTEM's password on the reference container, for the second logins.
SYSTEM_PASSWORD = os.environ.get("PARITY_TEST_ORACLE_SYSTEM", "parity")


def with_login(url: str, user: str, password: str) -> str:
    """The same server and service, another login."""
    u = urlparse(url)
    netloc = f"{user}:{password}@{u.hostname}:{u.port or 1521}"
    return urlunparse(u._replace(netloc=netloc))


class Writer:
    """A plain read-write session for building fixtures - never the tool."""

    def __init__(self, url: str) -> None:
        """Connect, with the writer's own NLS pinned so literals parse alike."""
        import oracledb

        self.con = oracledb.connect(**parse_oracle_url(url))
        self.version = int(str(self.con.version).split(".")[0])
        self.run("alter session set nls_numeric_characters = '.,'")
        self.run("alter session set time_zone = '+00:00'")
        self.tables: set[str] = set()

    def run(self, sql: str, params=None):
        """Run one statement and commit; return its rows if it made any."""
        cur = self.con.cursor()
        try:
            cur.execute(sql, params or {})
            rows = cur.fetchall() if cur.description else []
        finally:
            cur.close()
        self.con.commit()
        _LAST_WRITE[0] = time.monotonic()
        return rows

    def many(self, sql: str, rows, sizes=None) -> None:
        """Insert many parameter rows."""
        cur = self.con.cursor()
        try:
            if sizes:
                cur.setinputsizes(*sizes)
            cur.executemany(sql, rows)
        finally:
            cur.close()
        self.con.commit()
        _LAST_WRITE[0] = time.monotonic()

    def table(self, name: str, ddl: str, select: str | None = None) -> str:
        """(Re)create `name`, optionally filled by `insert into name <select>`."""
        self.drop(name)
        self.run(f"create table {name} ({ddl})")
        self.tables.add(name)
        if select:
            self.run(f"insert into {name} {select}")
        return name

    def drop(self, name: str) -> None:
        """Drop a table if it exists."""
        try:
            self.run(f"drop table {name} purge")
        except Exception:  # noqa: S110 - it may not exist
            pass

    def close(self) -> None:
        """Drop every fixture table and close."""
        for t in sorted(self.tables):
            self.drop(t)
        self.con.close()


@pytest.fixture(scope="module")
def writer(oracle_url):
    """The fixture writer, dropping its tables at the end."""
    w = Writer(oracle_url)
    yield w
    w.close()


#: When a fixture last wrote; see `settle`.
_LAST_WRITE = [0.0]


def settle() -> None:
    """Wait until three seconds after the fixtures' last write.

    A READ ONLY snapshot taken just after a table's creation, or its first
    insert, cannot read it (ORA-01466): verified at 0 s always and at 1.1 s
    sometimes, never at 1.6 s or more. Three seconds is the margin.
    """
    wait = _LAST_WRITE[0] + 3.0 - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def open_oracle(url: str, side: str = "A", float_scale: int = 6):
    """A parity dialect, as the CLI opens it, once the fixtures have settled."""
    settle()
    return get_dialect(url, side=side, float_scale=float_scale)


# --------------------------------------------------------------- reference


def duck_orders(boolean: bool) -> str:
    """Side B, the DuckDB reference: every awkward value the encoding has to
    agree on. `boolean` is False against Oracle before 23ai, which has no
    BOOLEAN column type."""
    flag = (
        "case when i % 17 = 0 then null else i % 11 = 0 end as is_refunded,"
        if boolean
        else ""
    )
    return f"""
create table orders as
select i::bigint                                              as id,
       'k' || i::varchar                                      as code,
       (i % 97)::integer                                      as customer_id,
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
       {flag}
       case i % 3 when 0 then 'ab' when 1 then 'abc' else '' end as fixed,
       (date '1599-12-25' + i::integer)::timestamp            as day,
       timestamp '2024-01-01 00:00:00'
           + (i % 86400) * interval '1 second'
           + (((i * 7919) % 10000000) // 10) * interval '1 microsecond' as created_at,
       (strftime(timestamp '2024-06-15 12:00:00' + (i * 61) * interval '1 second',
                 '%Y-%m-%d %H:%M:%S')
        || case i % 3 when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end
       )::timestamptz                                         as paid_at,
       case when i % 13 = 0 then null
            else 'note ' || i::varchar || ' ü✓😀日本' end     as note,
       case when i % 7 = 0 then null
            else 'memo ' || i::varchar || case i % 5 when 0 then '\\N'
                 when 1 then chr(31) || 'x' else '' end end   as memo,
       unhex(md5(i::varchar))                                 as bin,
       (i % 86400) * interval '1 second'
           + (i % 1000) * interval '1 microsecond'            as iv
from generate_series(1, {N}) as s(i)
"""


def ora_orders(boolean: bool) -> tuple[str, str]:
    """The same rows in Oracle: (column DDL, the SELECT that fills them)."""
    ddl = (
        "id number(18), code varchar2(20), customer_id number(10), "
        "amount number(12,2), big_dec number(38,10), ratio binary_double, "
        "ratio4 binary_float, status varchar2(10), "
        + ("is_refunded boolean, " if boolean else "")
        + "fixed char(10), day date, created_at timestamp(6), "
        "paid_at timestamp(6) with time zone, note nvarchar2(100), memo clob, "
        "bin raw(16), iv interval day(9) to second(6)"
    )
    flag = (
        "case when mod(i, 17) = 0 then null else mod(i, 11) = 0 end, " if boolean else ""
    )
    select = f"""
select i, 'k' || i, mod(i, 97), mod(i * 7, 100000) / 100,
       case when mod(i, 23) = 0 then null else
           to_number(i || '000000000000000.' || lpad(mod(i * 12345, 10000000000), 10, '0'))
       end,
       case when mod(i, 19) = 0 then null when i = 9 then 1e20d / 3
            else cast(i as binary_double) / 7 end,
       case when mod(i, 29) = 0 then null
            else cast(cast(i as binary_double) / 3 as binary_float) end,
       case mod(i, 3) when 0 then 'paid' when 1 then 'open' else 'void' end,
       {flag}
       case mod(i, 3) when 0 then 'ab' when 1 then 'abc' else ' ' end,
       date '1599-12-25' + i,
       timestamp '2024-01-01 00:00:00' + numtodsinterval(mod(i, 86400), 'SECOND')
           + numtodsinterval(trunc(mod(i * 7919, 10000000) / 10) / 1000000, 'SECOND'),
       from_tz(timestamp '2024-06-15 12:00:00' + numtodsinterval(i * 61, 'SECOND'),
           case mod(i, 3) when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end),
       case when mod(i, 13) = 0 then null
            else to_nchar('note ' || i || ' ' || unistr('\\00fc\\2713\\d83d\\de00\\65e5\\672c')) end,
       case when mod(i, 7) = 0 then null
            else to_clob('memo ' || i || case mod(i, 5) when 0 then chr(92) || 'N'
                 when 1 then chr(31) || 'x' end) end,
       standard_hash(to_char(i), 'MD5'),
       numtodsinterval(mod(i, 86400) + mod(i, 1000) / 1000000, 'SECOND')
from (select level as i from dual connect by level <= {N})
"""
    return ddl, select


def _wide(rows: int, oracle: bool) -> tuple[str, str]:
    """A WIDE_COLS-column table: (Oracle DDL or '', the SELECT)."""
    cols, ddl = [], ["id number(18)"]
    for k in range(WIDE_COLS):
        if k % 3 == 0:
            cols.append(f"i + {k}" if oracle else f"i + {k} as c{k}")
            ddl.append(f"c{k} number(18)")
        elif k % 3 == 1:
            if oracle:
                cols.append(
                    f"case when mod(i, {k + 2}) = 0 then null else 'v' || (i * {k}) end"
                )
            else:
                cols.append(
                    f"case when i % {k + 2} = 0 then null else 'v' || (i * {k}) end as c{k}"
                )
            ddl.append(f"c{k} varchar2(40)")
        else:
            cols.append(
                f"cast(i * {k} as binary_double) / 8"
                if oracle
                else f"(i * {k})::double / 8 as c{k}"
            )
            ddl.append(f"c{k} binary_double")
    body = ", ".join(cols)
    if oracle:
        return ", ".join(ddl), (
            f"select i, {body} from (select level as i from dual connect by level <= {rows})"
        )
    return "", f"select i::bigint as id, {body} from generate_series(1, {rows}) as s(i)"


EXTREME_KEYS = [-(2**63), -1, 0, 1, 2**63 - 1]


@pytest.fixture(scope="module")
def duck(tmp_path_factory, writer) -> str:
    """Side B: the DuckDB reference tables, built once and opened read-only."""
    path = str(tmp_path_factory.mktemp("oracle_it") / "b.duckdb")
    con = duckdb_write(path)
    try:
        con.execute(duck_orders(writer.version >= 23))
        con.execute(f"create table wide as {_wide(100, oracle=False)[1]}")
        con.execute(
            "create table longtext as select i::bigint as id, "
            "repeat('a', 4000) as a, repeat('b', 4000) as b, repeat('c', 4000) as c, "
            "repeat('m', 40000) as m from generate_series(1, 5) as s(i)"
        )
        con.execute("create table extreme (id bigint, v varchar)")
        con.executemany(
            "insert into extreme values (?, ?)",
            [[k, f"x{i}"] for i, k in enumerate(EXTREME_KEYS)],
        )
        con.execute(
            "create table big_keys as select * from (values "
            "(-99999999999999999999999999999999999999::hugeint, 'lo'), "
            "(0::hugeint, 'zero'), (12345678901234567890123456789::hugeint, 'mid'), "
            "(99999999999999999999999999999999999999::hugeint, 'hi')) t(id, v)"
        )
        con.execute(
            "create table empties as select * from (values "
            "(1::bigint, ''), (2, null), (3, 'x')) t(id, s)"
        )
        con.execute(
            "create table nanos as select * from (values "
            "(1::bigint, timestamp '2024-12-31 23:59:59.999999'), "
            "(2, timestamp '2024-02-29 13:04:05.123456'), "
            "(3, timestamp '2024-01-01 00:00:00.000002')) t(id, t)"
        )
        con.execute(
            "create table ckeys as select 'k' || (i % 50)::varchar as code, "
            "(i // 50)::integer as part, i::bigint as v from range(0, 500) s(i)"
        )
    finally:
        con.close()
    return path


@pytest.fixture(scope="module")
def orders(writer) -> str:
    """The Oracle `orders` table, identical to DuckDB's."""
    ddl, select = ora_orders(writer.version >= 23)
    return writer.table("ORA_ORDERS", ddl, select)


def _planted(writer: Writer, name: str, *dml: str) -> str:
    """A copy of `orders` with planted defects applied, on the Oracle side."""
    writer.drop(name)
    writer.run(f"create table {name} as select * from ORA_ORDERS")
    writer.tables.add(name)
    for statement in dml:
        writer.run(statement.format(t=name))
    return name


def _diff(url: str, duck: str, a_table: str, b_table: str = "main.orders", **kw):
    """Diff an Oracle table against a DuckDB one, deep enough that the bucket
    expression runs at several levels rather than one fetch."""
    a = open_oracle(url, side="A")
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
    """(key, kind, lower-cased columns) for every reported difference.

    Columns are reported under side A's stored spelling, upper case on Oracle.
    """
    return [
        (d.key, d.kind, [c.lower() for c in d.columns] if d.columns else d.columns)
        for d in result.diffs
    ]


# ----------------------------------------------------------------- the hash


def test_the_hash_constant_agrees_on_both_hash_paths(oracle_url, writer):
    """648541476951500027 for 'abc' - a seventh path to it - through
    STANDARD_HASH of a VARCHAR2 and an NVARCHAR2, and through `parity_md5` of
    a CLOB. 'abd' must not reach it, so a constant-returning bug cannot pass."""
    writer.table("ORA_H", "i number, v varchar2(10), n nvarchar2(10), c clob")
    writer.run("insert into ORA_H values (1, 'abc', 'abc', to_clob('abc'))")
    writer.run("insert into ORA_H values (2, 'abd', 'abd', to_clob('abd'))")
    a = open_oracle(oracle_url)
    try:
        rows = a.query(
            f"select {a.hash_expr('v')}, {a.hash_expr('to_char(n)')}, "
            f"{a._clob_hash('c')} from ORA_H order by i"
        )
        assert tuple(int(x) for x in rows[0]) == (648541476951500027,) * 3
        second = tuple(int(x) for x in rows[1])
        assert second[0] == second[1] == second[2] != 648541476951500027
        empty = a.hash_expr("''")  # Oracle's empty text is NULL
        assert int(a.query(f"select {empty} from dual")[0][0]) == int(
            hashlib.md5(b"").hexdigest()[:15], 16
        )
    finally:
        a.close()


#: Texts whose UTF-8 bytes are not their UTF-16 bytes, and lengths around
#: every MD5 block and `parity_md5` chunk boundary.
UNICODE = [
    "\u00e9",
    "\u00fc \u00df",
    "\u65e5\u672c\u8a9e",
    "\U0001f600",
    "e\u0301",
    "a\x1fb",
    "\\N",
    "a\x00b",
]
UNICODE += ["x" * n for n in (55, 56, 63, 64, 65, 16383, 16384, 16385, 40000)]
UNICODE += ["😀" * 16352 + "b", "é" * 8192 + "😀z", "中" * 70000]


def test_text_is_hashed_as_utf8_on_both_paths(oracle_url, writer):
    """STANDARD_HASH of a VARCHAR2 and of a converted NVARCHAR2, and
    `parity_md5` of a CLOB and an NCLOB, all equal Python's MD5 of the UTF-8
    bytes - for accents, CJK, emoji outside the BMP (straddling every 16 kB
    chunk), NUL and lengths at every block edge. Negative control: the
    composed and decomposed 'é' look alike and still hash differently."""
    import oracledb

    writer.table(
        "ORA_U", "i number, v varchar2(4000), n nvarchar2(2000), c clob, nc nclob"
    )
    rows = []
    for i, s in enumerate(UNICODE):
        short = (
            s if len(s.encode()) <= 4000 and len(s.encode("utf-16-le")) <= 4000 else None
        )
        rows.append((i, short, short, s, s))
    writer.many(
        "insert into ORA_U values (:1, :2, :3, :4, :5)",
        rows,
        sizes=[None, None, None, oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB],
    )
    a = open_oracle(oracle_url)
    try:
        got = a.query(
            "select i, rawtohex(standard_hash(v, 'MD5')), "
            "rawtohex(standard_hash(to_char(n), 'MD5')), parity_md5(c), "
            "parity_md5(to_clob(nc)) from ORA_U order by i"
        )
        # The native path, where this login may call DBMS_CRYPTO (the
        # fixture login can; a bare reader cannot - see the grants test).
        crypto = (
            a.query(
                "select i, rawtohex(dbms_crypto.hash(c, 2)), "
                "rawtohex(dbms_crypto.hash(to_clob(nc), 2)) from ORA_U order by i"
            )
            if a.crypto
            else None
        )
    finally:
        a.close()
    for (i, hv, hn, hc, hnc), s in zip(got, UNICODE, strict=True):
        want = hashlib.md5(s.encode()).hexdigest().upper()
        assert hc == hnc == want, (i, len(s))
        if rows[int(i)][1] is not None:
            assert hv == hn == want, i
    assert got[0][3] != got[4][3]
    if crypto is not None:
        assert [(r[1], r[2]) for r in crypto] == [(r[3], r[4]) for r in got]


# ------------------------------------------------------- canonical text


def test_every_row_renders_byte_identically_to_duckdb(oracle_url, duck, orders, writer):
    """The correctness contract itself: per-row canonical text and the
    full-table checksum agree exactly - and a one-cent change moves the
    checksum but not the count."""
    a = open_oracle(oracle_url, side="A")
    b = open_duckdb(duck, side="B")
    try:
        cols_a = {c.name.lower(): c for c in a.columns(orders)}
        cols_b = {c.name: c for c in b.columns("main.orders")}
        assert list(cols_a) == list(cols_b)
        key_a, key_b = a.key_spec(orders, "ID"), b.key_spec("main.orders", "id")
        rest_a = [c for n, c in cols_a.items() if n != "id"]
        rest_b = [c for n, c in cols_b.items() if n != "id"]
        names = [c.name.lower() for c in rest_a]
        rows_a = a.fetch_range(orders, key_a, rest_a, 1, N + 1)
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
        r = {k: dict(zip(names, v, strict=True)) for k, v in rows_a.items() if k <= 23}
        assert r[1]["ratio"] == "0.142857" and r[1]["amount"] == "0.070000"
        assert r[9]["ratio"] == "33333333333333331968.000000"  # exact past 2^53
        assert r[1]["day"] == "1599-12-26 00:00:00.000000"  # DATE is a timestamp
        assert r[1]["fixed"] == "abc" and r[3]["fixed"] == "ab"
        assert r[2]["fixed"] == ""  # an all-space CHAR: empty text, never \N
        assert r[13]["note"] == "\\N" and r[1]["note"] == "note 1 ü✓😀日本"
        assert r[5]["memo"] == "memo 5\\\\N" and r[1]["memo"] == "memo 1\\sx"
        assert r[7]["memo"] == "\\N"
        assert r[1]["big_dec"] == "1000000000000000.000001"
        assert r[3]["big_dec"] == "3000000000000000.000004"  # half away from zero
        assert r[1]["created_at"] == "2024-01-01 00:00:01.000791"
        assert r[1]["paid_at"] == "2024-06-15 20:01:01.000000"  # -08:00 in UTC
        assert r[1]["bin"] == "\\\\x" + hashlib.md5(b"1").hexdigest()
        assert r[1]["iv"] == "0 mon 0 day 1000001 us"
        if "is_refunded" in r[1]:
            assert r[1]["is_refunded"] == "false" and r[11]["is_refunded"] == "true"
            assert r[17]["is_refunded"] == "\\N"  # a NULL boolean, not 'false'
        sums_a = a.segment_checksums(orders, key_a, rest_a, 1, N + 1, 1)
        sums_b = b.segment_checksums("main.orders", key_b, rest_b, 1, N + 1, 1)
        assert sums_a == sums_b
    finally:
        a.close()
        b.close()
    changed = _planted(
        writer, "ORA_ORDERS_CENT", "update {t} set amount = amount + 0.01 where id = 4321"
    )
    a = open_oracle(oracle_url, side="A")
    try:
        moved = a.segment_checksums(
            changed, a.key_spec(changed, "ID"), rest_a, 1, N + 1, 1
        )
        assert moved[0][0] == sums_b[0][0] and moved[0][1] != sums_b[0][1]
    finally:
        a.close()


def test_identical_tables_match_and_download_nothing(oracle_url, duck, orders):
    """The headline claim: 5,000 mixed-type rows agree in four queries with
    zero rows downloaded, and the empty-string caveat is printed."""
    result = _diff(oracle_url, duck, orders)
    assert result.identical, _found(result)[:5]
    assert result.stats.rows_downloaded == 0
    assert result.stats.queries == 4
    assert [w for w in result.warnings if "empty string as NULL" in w]


def test_the_float_sample_matches_the_canonical_spec(oracle_url, writer):
    """Every adversarial double plus 10,000 random ones render exactly
    `canonical_float_text`, at scales 6, 2, 0 and 20, as BINARY_DOUBLE and (for
    the values it holds) as BINARY_FLOAT. Doubles are bound as BINARY_DOUBLE:
    python-oracledb binds a float as NUMBER by default, which is not the value."""
    import oracledb

    rnd = random.Random(11)
    xs = [x for x in adversarial_doubles() if abs(x) < FLOAT_MAX_MAGNITUDE]
    xs += [2.0**50 + 0.25, -(2.0**50 + 0.25), 562949953421312.25, 2.0**116 * 0.999]
    while len(xs) < 11_600:
        x = struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(64)))[0]
        if math.isfinite(x) and abs(x) < FLOAT_MAX_MAGNITUDE:
            xs.append(x)
    reals = [
        struct.unpack("<f", struct.pack("<f", x))[0] if abs(x) < 3e38 else None
        for x in xs
    ]
    writer.table("ORA_FSAMPLE", "i number primary key, x binary_double, r binary_float")
    writer.many(
        "insert into ORA_FSAMPLE values (:1, :2, :3)",
        [(i, x, r) for i, (x, r) in enumerate(zip(xs, reals, strict=True))],
        sizes=[None, oracledb.DB_TYPE_BINARY_DOUBLE, oracledb.DB_TYPE_BINARY_FLOAT],
    )
    for scale in (6, 2, 0, 20):
        a = open_oracle(oracle_url, float_scale=scale)
        try:
            fx = a.normalize(Column("X", LogicalType.FLOAT, "BINARY_DOUBLE"))
            fr = a.normalize(Column("R", LogicalType.FLOAT, "BINARY_FLOAT"))
            rows = a.query(f"select i, x, {fx}, {fr} from ORA_FSAMPLE order by i")
        finally:
            a.close()
        bad = []
        for (_, back, text, rtext), want, r in zip(rows, xs, reals, strict=True):
            assert back == want or back == want == 0, (want, back)
            if text != canonical_float_text(want, scale):
                bad.append((want, text))
            if r is not None and rtext != canonical_float_text(r, scale):
                bad.append((r, rtext))
        assert not bad, (scale, bad[:5])


def test_non_finite_floats_render_the_shared_tokens(oracle_url, writer):
    """Oracle BINARY_DOUBLE holds Infinity and NaN, which render as the tokens
    every engine emits; -0 renders as 0."""
    writer.table("ORA_NONFIN", "i number, x binary_double")
    writer.run(
        "insert into ORA_NONFIN select 1, binary_double_infinity from dual union all "
        "select 2, -binary_double_infinity from dual union all "
        "select 3, binary_double_nan from dual union all select 4, -0d from dual"
    )
    a = open_oracle(oracle_url)
    try:
        fx = a.normalize(Column("X", LogicalType.FLOAT, "BINARY_DOUBLE"))
        rows = a.query(f"select {fx} from ORA_NONFIN order by i")
    finally:
        a.close()
    assert [r[0] for r in rows] == ["Infinity", "-Infinity", "NaN", "0.000000"]


def test_a_float_past_2_116_is_refused_naming_the_side(oracle_url, writer):
    """Past 2^116 no engine renders a double exactly; the refusal names side,
    value and column, never a rounded text."""
    import oracledb

    writer.table("ORA_FBIG", "x binary_double")
    writer.many(
        "insert into ORA_FBIG values (:1)",
        [(1e35,)],
        sizes=[oracledb.DB_TYPE_BINARY_DOUBLE],
    )
    a = open_oracle(oracle_url, side="B")
    try:
        fx = a.normalize(Column("X", LogicalType.FLOAT, "BINARY_DOUBLE"))
        with pytest.raises(
            ValueError, match=r"side B: oracle.*2\^116.*9\.9+\d*E\+034 in column 'X'"
        ):
            a.query(f"select {fx} from ORA_FBIG")
    finally:
        a.close()


def test_a_decimal_past_10_38_is_refused_never_clamped(oracle_url, writer):
    """An unconstrained NUMBER holds 1e100; no other engine does exactly, and
    its TM9 text would turn into exponent notation. Refused, naming the column."""
    writer.table("ORA_DBIG", "x number")
    writer.run("insert into ORA_DBIG values (1e100)")
    a = open_oracle(oracle_url)
    try:
        dx = a.normalize(Column("X", LogicalType.DECIMAL, "NUMBER"))
        with pytest.raises(
            ValueError, match=r"side A: oracle.*DECIMAL value.*in column 'X'"
        ):
            a.query(f"select {dx} from ORA_DBIG")
    finally:
        a.close()


def test_timestamp_9_is_truncated_to_microseconds_not_rounded(oracle_url, duck, writer):
    """23:59:59.9999995 stays on its day, .1234567 reads .123456 - and a
    one-microsecond difference is still found. The caveat names the column."""
    writer.table("ORA_NANOS", "id number(18), t timestamp(9)")
    writer.run(
        "insert into ORA_NANOS select 1, timestamp '2024-12-31 23:59:59.9999995' from dual "
        "union all select 2, timestamp '2024-02-29 13:04:05.1234567' from dual "
        "union all select 3, timestamp '2024-01-01 00:00:00.0000019' from dual"
    )
    result = _diff(oracle_url, duck, "ORA_NANOS", "main.nanos")
    assert _found(result) == [(3, "different", ["t"])]
    assert result.diffs[0].values_a == {"T": "2024-01-01 00:00:00.000001"}
    assert [w for w in result.warnings if "past the microsecond" in w and "T" in w]


# -------------------------------------------------------- planted differences


def _plants(boolean: bool) -> dict:
    """Every planted defect the other engines' suites plant, on Oracle."""
    plants = {
        "changed_decimal": (
            ["update {t} set amount = amount + 0.01 where id = 1234"],
            [(1234, "different", ["amount"])],
        ),
        "deleted_row": (["delete from {t} where id = 777"], [(777, "only_in_b", None)]),
        "inserted_row": (
            [
                "insert into {t} (id, code, status) values (999999999, 'k999999999', 'new')"
            ],
            [(999999999, "only_in_a", None)],
        ),
        "null_where_text_was": (
            ["update {t} set note = null where id = 27"],
            [(27, "different", ["note"])],
        ),
        "changed_clob": (
            ["update {t} set memo = to_clob('memo 33!') where id = 33"],
            [(33, "different", ["memo"])],
        ),
        "changed_double_at_the_last_compared_place": (
            ["update {t} set ratio = ratio + 1e-6d where id = 40"],
            [(40, "different", ["ratio"])],
        ),
        "changed_interval": (
            [
                "update {t} set iv = iv + numtodsinterval(0.000001, 'SECOND') where id = 41"
            ],
            [(41, "different", ["iv"])],
        ),
        "changed_raw": (
            ["update {t} set bin = hextoraw('00') where id = 42"],
            [(42, "different", ["bin"])],
        ),
    }
    if boolean:
        # id 2 is FALSE on the DuckDB side (2 % 11 != 0, 2 % 17 != 0).
        plants["null_versus_false"] = (
            ["update {t} set is_refunded = null where id = 2"],
            [(2, "different", ["is_refunded"])],
        )
    return plants


@pytest.mark.parametrize(
    "plant",
    sorted(_plants(True)),
)
def test_each_planted_difference_is_found_exactly(
    oracle_url, duck, orders, writer, plant
):
    """Every planted defect is reported as that row and that column, and
    nothing else is - a parity tool that misses one, or invents one, is broken."""
    plants = _plants(writer.version >= 23)
    if plant not in plants:
        pytest.skip("BOOLEAN needs Oracle 23ai")
    dml, expected = plants[plant]
    table = _planted(writer, f"ORA_P_{plant.upper()}"[:120], *dml)
    result = _diff(oracle_url, duck, table)
    got = [
        (key, kind, cols if want[2] is not None else None)
        for (key, kind, cols), want in zip(_found(result), expected, strict=True)
    ]
    assert got == expected
    assert not result.identical


def test_oracle_null_against_an_empty_string_is_a_reported_difference(
    oracle_url, duck, writer
):
    """Oracle cannot store '': a row migrated as '' on the other side reads as
    NULL against '' and is reported, never silently equated - and the caveat
    says why."""
    writer.table("ORA_EMPTIES", "id number(18), s varchar2(10)")
    writer.run(
        "insert into ORA_EMPTIES select 1, '' from dual union all "
        "select 2, null from dual union all select 3, 'x' from dual"
    )
    result = _diff(oracle_url, duck, "ORA_EMPTIES", "main.empties")
    assert _found(result) == [(1, "different", ["s"])]
    # The NULL sentinel against the empty text.
    assert result.diffs[0].values_a == {"S": NULL_SENTINEL}
    assert result.diffs[0].values_b == {"S": ""}
    assert [w for w in result.warnings if "empty string as NULL" in w]


@pytest.mark.parametrize("key", ["CODE", "code"])
def test_a_text_key_is_hashed_and_reported_by_its_real_value(
    oracle_url, duck, orders, writer, key
):
    """A VARCHAR2 key goes through the hashed-bucket path; the planted change
    comes back under the key's real text, not its hash - whatever case the key
    is given in."""
    table = _planted(
        writer, "ORA_TEXTKEY", "update {t} set status = 'gone' where id = 4242"
    )
    result = _diff(oracle_url, duck, table, key=key)
    assert _found(result) == [("k4242", "different", ["status"])]


def test_a_composite_key_is_matched_by_both_parts(oracle_url, duck, writer):
    """(code, part) as one key; a change on one pair is found as that pair."""
    writer.table(
        "ORA_CKEYS",
        "code varchar2(10), part number(10), v number(18)",
        "select 'k' || mod(i, 50), trunc(i / 50), i from "
        "(select level - 1 as i from dual connect by level <= 500)",
    )
    clean = _diff(oracle_url, duck, "ORA_CKEYS", "main.ckeys", key=["code", "part"])
    assert clean.identical, _found(clean)
    writer.run("update ORA_CKEYS set v = -1 where code = 'k7' and part = 3")
    result = _diff(oracle_url, duck, "ORA_CKEYS", "main.ckeys", key=["code", "part"])
    assert [(d.kind, [c.lower() for c in d.columns]) for d in result.diffs] == [
        ("different", ["v"])
    ]
    assert "k7" in str(result.diffs[0].key) and "3" in str(result.diffs[0].key)


# ---------------------------------------------- false-match traps: width


def test_a_500_column_table_agrees_and_a_deep_cell_is_found(oracle_url, duck, writer):
    """500 columns: the row text passes 4,000 bytes on some rows and not on
    others, so both hash paths run in one checksum; they must agree with
    DuckDB, and one changed cell deep in the row must be found."""
    ddl, select = _wide(100, oracle=True)
    writer.table("ORA_WIDE", ddl, select)
    clean = _diff(oracle_url, duck, "ORA_WIDE", "main.wide", threshold=16)
    assert clean.identical and clean.stats.rows_downloaded == 0, _found(clean)[:3]
    writer.run("update ORA_WIDE set c449 = c449 + 1 where id = 50")
    result = _diff(oracle_url, duck, "ORA_WIDE", "main.wide", threshold=16)
    assert _found(result) == [(50, "different", ["c449"])]


def test_differences_past_4000_and_32767_bytes_of_row_text_are_found(
    oracle_url, duck, writer
):
    """The 4,000-byte trap. Three VARCHAR2(4000) columns and a 40,000-character
    CLOB join to 52,003 bytes: no VARCHAR2 can hold the row, STANDARD_HASH
    takes no CLOB, and a truncated text would hide a change past the cut. A
    change at character 1,500 of the third VARCHAR2 (~9,500 of the row) and at
    character 35,000 of the CLOB (past 32,767) must both be found, and the
    clean table must download nothing."""
    import oracledb

    writer.table(
        "ORA_LONGTEXT",
        "id number(18), a varchar2(4000), b varchar2(4000), c varchar2(4000), m clob",
    )
    writer.many(
        "insert into ORA_LONGTEXT values (:1, :2, :3, :4, :5)",
        [(i, "a" * 4000, "b" * 4000, "c" * 4000, "m" * 40000) for i in range(1, 6)],
        sizes=[None, None, None, None, oracledb.DB_TYPE_CLOB],
    )
    # Negative control: the trap is real - joined as VARCHAR2 the row raises.
    a = open_oracle(oracle_url)
    try:
        with pytest.raises(Exception, match="ORA-01489"):
            a.query("select a || b from ORA_LONGTEXT")
    finally:
        a.close()
    clean = _diff(oracle_url, duck, "ORA_LONGTEXT", "main.longtext")
    assert clean.identical and clean.stats.rows_downloaded == 0
    writer.run(
        "update ORA_LONGTEXT set c = substr(c, 1, 1499) || 'X' || substr(c, 1501) where id = 2"
    )
    writer.run(
        "update ORA_LONGTEXT set m = to_clob(rpad('m', 34999, 'm')) || 'X' "
        "|| to_clob(rpad('m', 5000, 'm')) where id = 4"
    )
    result = _diff(oracle_url, duck, "ORA_LONGTEXT", "main.longtext")
    assert _found(result) == [(2, "different", ["c"]), (4, "different", ["m"])]


# --------------------------------------------- false-match traps: buckets


def test_keys_at_both_ends_of_bigint(oracle_url, duck, writer):
    """A NUMBER(19) key spanning 2^64 is hashed (it is wider than 18 digits);
    the walk completes and finds a change on the largest key."""
    writer.table("ORA_EXTREME", "id number(19), v varchar2(20)")
    writer.many(
        "insert into ORA_EXTREME values (:1, :2)",
        [(k, f"x{i}") for i, k in enumerate(EXTREME_KEYS)],
    )
    clean = _diff(
        oracle_url, duck, "ORA_EXTREME", "main.extreme", threshold=1, bisection_factor=2
    )
    assert clean.identical
    writer.run(f"update ORA_EXTREME set v = 'changed' where id = {2**63 - 1}")
    result = _diff(
        oracle_url, duck, "ORA_EXTREME", "main.extreme", threshold=1, bisection_factor=2
    )
    assert [(str(d.key), d.kind) for d in result.diffs] == [(str(2**63 - 1), "different")]


def test_38_digit_integer_keys_are_hashed_and_exact(oracle_url, duck, writer):
    """NUMBER(38) keys up to 10^38 - 1 against DuckDB HUGEINT: too wide to
    bucket natively in NUMBER without rounding, so both sides hash the exact
    text, and a change on the largest key is found under its full digits."""
    writer.table("ORA_BIGKEYS", "id number(38), v varchar2(10)")
    writer.many(
        "insert into ORA_BIGKEYS values (:1, :2)",
        [
            (Decimal(-(10**38) + 1), "lo"),
            (Decimal(0), "zero"),
            (Decimal(12345678901234567890123456789), "mid"),
            (Decimal(10**38 - 1), "hi"),
        ],
    )
    clean = _diff(
        oracle_url, duck, "ORA_BIGKEYS", "main.big_keys", threshold=1, bisection_factor=2
    )
    assert clean.identical, _found(clean)
    writer.run(f"update ORA_BIGKEYS set v = 'HI' where id = {10**38 - 1}")
    result = _diff(oracle_url, duck, "ORA_BIGKEYS", "main.big_keys", threshold=1)
    assert [(str(d.key), d.kind) for d in result.diffs] == [
        (str(10**38 - 1), "different")
    ]


def test_the_sql_bucket_is_exactly_the_python_one_at_every_boundary(oracle_url, writer):
    """NUMBER division rounds its quotient at 38-40 digits, so a key one below
    a bucket boundary could land in the next bucket on this side only. For
    random ranges up to 2^64 wide, every key at and either side of every
    boundary gets the bucket `bucket_bounds` assigns it."""
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
    writer.table("ORA_BKEYS", "k number(19)")
    writer.many("insert into ORA_BKEYS values (:1)", [(k,) for k in keys])
    a = open_oracle(oracle_url)
    try:
        for lo, hi, n in cases:
            offset = f"({a.wide_int('k')} - ({lo}))"
            exact = a.int_div(f"{offset} * {n}", f"({hi - lo})")
            rows = a.query(
                f"select k, {exact} from ORA_BKEYS where k >= {lo} and k <= {hi - 1}"
            )
            for k, got in rows:
                assert int(got) == (int(k) - lo) * n // (hi - lo), (lo, hi, n, k)
    finally:
        a.close()


# ------------------------------------------------- snapshot and read-only


def test_a_live_table_is_read_at_one_snapshot(oracle_url, writer):
    """CLAUDE.md 4.9: a row committed mid-walk must not be seen by later
    levels. A pinned dialect keeps its count; a fresh one sees the new row -
    the negative control proving the insert really landed."""
    writer.table(
        "ORA_LIVE", "id number(18)", "select level from dual connect by level <= 100"
    )
    a = open_oracle(oracle_url)
    try:
        key = a.key_spec("ORA_LIVE", "ID")
        assert a.key_stats("ORA_LIVE", key).rows == 100
        writer.run("insert into ORA_LIVE values (101)")
        writer.run("update ORA_LIVE set id = -id where id = 1")
        stats = a.key_stats("ORA_LIVE", key)
        assert (stats.rows, stats.lo) == (100, 1)
    finally:
        a.close()
    fresh = open_oracle(oracle_url)
    try:
        stats = fresh.key_stats("ORA_LIVE", fresh.key_spec("ORA_LIVE", "ID"))
        assert (stats.rows, stats.lo) == (101, -1)
    finally:
        fresh.close()


def test_a_write_is_refused_by_the_dialect_and_by_the_transaction(oracle_url, writer):
    """Two layers: `query` refuses a non-SELECT before sending it, and the
    session's READ ONLY transaction refuses it at the server - checked by
    going around `query`. The row is still there afterwards."""
    writer.table("ORA_RO", "id number(18)", "select 1 from dual")
    a = open_oracle(oracle_url)
    try:
        with pytest.raises(ValueError, match="single SELECT"):
            a.query("delete from ORA_RO")
        with pytest.raises(ValueError, match="single SELECT"):
            a.query("select * from ORA_RO for update")
        cur = a._conn.cursor()
        with pytest.raises(Exception, match="ORA-01456"):
            cur.execute("delete from ORA_RO")
        cur.close()
    finally:
        a.close()
    assert writer.run("select count(*) from ORA_RO")[0][0] == 1


def test_a_missing_table_names_the_side_and_the_case_near_miss(oracle_url, orders):
    """The error says which side, points at the upper-case table a lower-case
    name missed, and that a missing GRANT looks the same."""
    a = open_oracle(oracle_url, side="B")
    try:
        with pytest.raises(ValueError) as exc:
            a.columns("ora_orders")
        message = str(exc.value)
        assert "side B: oracle" in message and "ORA_ORDERS" in message
        assert "SELECT" in message
    finally:
        a.close()


# ------------------------------------------- second logins: SYSTEM needed


@pytest.fixture(scope="module")
def system(oracle_url):
    """A SYSTEM session on the same service, or skip."""
    import oracledb

    args = parse_oracle_url(oracle_url)
    args.update(user="system", password=SYSTEM_PASSWORD)
    try:
        con = oracledb.connect(**args)
    except Exception as exc:  # pragma: no cover - depends on the server
        pytest.skip(f"no SYSTEM login: {type(exc).__name__}")
    yield con
    con.close()


def _sys(con, *statements: str) -> None:
    """Run statements as SYSTEM, ignoring 'already exists'/'does not exist'."""
    cur = con.cursor()
    for s in statements:
        try:
            cur.execute(s)
        except Exception as exc:
            if not any(
                code in str(exc) for code in ("ORA-01920", "ORA-01918", "ORA-04080")
            ):
                raise
    cur.close()


def test_the_diff_runs_with_only_create_session_and_select(
    oracle_url, system, duck, orders, writer
):
    """The minimum grants the docs promise: CREATE SESSION and SELECT on the
    table - no catalog role, no EXECUTE on DBMS_CRYPTO (which a reader does
    not hold, hence `parity_md5`). Such a login finds a planted change, also
    in a row that takes the CLOB hash path."""
    owner = parse_oracle_url(oracle_url)["user"].upper()
    _sys(
        system,
        "drop user ora_reader cascade",
        "create user ora_reader identified by reader",
        "grant create session to ora_reader",
    )
    import oracledb

    table = _planted(writer, "ORA_ORDERS_RO", "update {t} set amount = 0 where id = 99")
    writer.run(f"grant select on {table} to ora_reader")
    # Rows of 52,003 bytes, which only the PL/SQL MD5 can hash for this login.
    writer.table(
        "ORA_LONG_RO",
        "id number(18), a varchar2(4000), b varchar2(4000), c varchar2(4000), m clob",
    )
    writer.many(
        "insert into ORA_LONG_RO values (:1, :2, :3, :4, :5)",
        [(i, "a" * 4000, "b" * 4000, "c" * 4000, "m" * 40000) for i in range(1, 6)],
        sizes=[None, None, None, None, oracledb.DB_TYPE_CLOB],
    )
    writer.run("grant select on ORA_LONG_RO to ora_reader")
    url = with_login(oracle_url, "ora_reader", "reader")
    result = _diff(url, duck, f"{owner}.{table}")
    assert _found(result) == [(99, "different", ["amount"])]
    long_table = f"{owner}.ORA_LONG_RO"
    assert _diff(url, duck, long_table, "main.longtext").identical
    writer.run(
        "update ORA_LONG_RO set m = to_clob(rpad('m', 34999, 'm')) || 'X' "
        "|| to_clob(rpad('m', 5000, 'm')) where id = 3"
    )
    result = _diff(url, duck, long_table, "main.longtext")
    assert _found(result) == [(3, "different", ["m"])]
    d = open_oracle(url)
    try:
        assert not d.crypto
        with pytest.raises(
            Exception, match=r"ORA-00904|ORA-01031|PLS-00201|must be declared"
        ):
            d._conn.cursor().execute("select dbms_crypto.hash(to_clob('a'), 2) from dual")
    finally:
        d.close()


#: Every session setting a logon trigger, a role default or a client could
#: turn hostile, set to the most hostile value Oracle accepts.
HOSTILE = [
    "alter session set nls_territory = 'GERMANY'",
    "alter session set nls_language = 'GERMAN'",
    "alter session set nls_numeric_characters = ',.'",
    "alter session set nls_calendar = 'Thai Buddha'",
    "alter session set nls_date_format = 'DD.MM.RR'",
    "alter session set nls_timestamp_format = 'DD.MM.RR HH12:MI AM'",
    "alter session set nls_timestamp_tz_format = 'DD.MM.RR HH12:MI AM TZR'",
    "alter session set nls_sort = 'GERMAN_CI'",
    "alter session set nls_comp = 'LINGUISTIC'",
    "alter session set time_zone = 'Asia/Tokyo'",
    "alter session set nls_length_semantics = 'CHAR'",
    "alter session set nls_nchar_conv_excp = 'FALSE'",
    "alter session set cursor_sharing = 'FORCE'",
    "alter session set nls_date_language = 'GERMAN'",
    "alter session set nls_iso_currency = 'GERMANY'",
    "alter session set nls_dual_currency = '?'",
]


def test_hostile_session_settings_are_pinned_away(
    oracle_url, system, duck, orders, writer
):
    """Round G for Oracle: a login whose logon trigger sets every NLS setting,
    the time zone, the calendar, linguistic comparison and cursor sharing to
    hostile values still renders every row byte-identically to DuckDB, and a
    planted change is still found exactly. Negative control: the trigger
    really fires - a raw query on the dialect's own connection, before the
    pins are applied, sees the hostile numeric characters."""
    owner = parse_oracle_url(oracle_url)["user"].upper()
    body = " ".join(f"execute immediate q'[{s}]';" for s in HOSTILE)
    _sys(
        system,
        "drop user ora_hostile cascade",
        "create user ora_hostile identified by hostile",
        "grant create session to ora_hostile",
        "create or replace trigger system.ora_hostile_logon after logon on database "
        f"begin if user = 'ORA_HOSTILE' then {body} end if; end;",
    )
    try:
        writer.run(f"grant select on {orders} to ora_hostile")
        url = with_login(oracle_url, "ora_hostile", "hostile")
        import oracledb

        raw = oracledb.connect(**parse_oracle_url(url))
        cur = raw.cursor()
        cur.execute("select to_char(1.5), sessiontimezone from dual")
        assert cur.fetchone() == ("1,5", "Asia/Tokyo")
        raw.close()
        clean = _diff(url, duck, f"{owner}.{orders}")
        assert clean.identical, _found(clean)[:5]
        table = _planted(
            writer,
            "ORA_ORDERS_HOSTILE",
            "update {t} set created_at = created_at + "
            "numtodsinterval(0.000001, 'SECOND') where id = 4000",
        )
        writer.run(f"grant select on {table} to ora_hostile")
        result = _diff(url, duck, f"{owner}.{table}")
        assert _found(result) == [(4000, "different", ["created_at"])]
    finally:
        _sys(system, "drop trigger system.ora_hostile_logon")


def test_a_logon_trigger_moving_current_schema_cannot_redirect_the_rows(
    oracle_url, system, duck, orders, writer
):
    """An unqualified name is introspected in CURRENT_SCHEMA at connect and
    read there too - always schema-qualified - so a decoy of the same name in
    the login's own schema is never read instead."""
    _sys(
        system,
        "drop user ora_decoy cascade",
        "create user ora_decoy identified by decoy quota unlimited on users",
        "grant create session, create table to ora_decoy",
    )
    owner = parse_oracle_url(oracle_url)["user"].upper()
    writer.run(f"grant select on {orders} to ora_decoy")
    decoy = with_login(oracle_url, "ora_decoy", "decoy")
    import oracledb

    con = oracledb.connect(**parse_oracle_url(decoy))
    cur = con.cursor()
    cur.execute(f"create table {orders} as select * from {owner}.{orders}")
    cur.execute(f"update {orders} set amount = -1 where id = 5")
    con.commit()
    con.close()
    _LAST_WRITE[0] = time.monotonic()
    result = _diff(decoy, duck, orders)
    assert _found(result) == [(5, "different", ["amount"])]  # its own table, as named
    result = _diff(decoy, duck, f"{owner}.{orders}")
    assert result.identical


# ------------------------------------------------------ against PostgreSQL

PG_BUILD = f"""
create table {PG_SCHEMA}_oracle.orders as
select i::bigint                                              as id,
       ((i * 7 % 100000) / 100.0)::numeric(12,2)              as amount,
       (case when i % 3 = 0 then 'paid'
             when i % 3 = 1 then 'open' else 'void' end)::varchar(10) as status,
       (case when i % 3 = 0 then 'ab' when i % 3 = 1 then 'abc' else '' end)::char(10) as fixed,
       (date '1599-12-25' + i::integer)::timestamp            as day,
       (to_char(timestamp '2024-06-15 12:00:00' + (i * 61) * interval '1 second',
                'YYYY-MM-DD HH24:MI:SS')
        || case i % 3 when 0 then '+05:30' when 1 then '-08:00' else '+00:00' end
       )::timestamptz                                         as paid_at,
       case when i % 13 = 0 then null
            else 'note ' || i::text || ' ü✓😀日本' end         as note,
       case when i % 19 = 0 then null when i = 9 then 1e20::float8 / 3
            else i::float8 / 7 end                            as ratio
from generate_series(1, {N}) as s(i)
"""


def test_the_cli_finds_planted_differences_between_oracle_and_postgres(
    oracle_url, pg_url, orders, writer, capsys
):
    """The headline workflow through the CLI with neither side DuckDB: Oracle
    -> PostgreSQL, planted differences (a changed decimal, a deleted row, a
    NULL against a value, a one-ulp double) found exactly, exit code 1. The
    unchanged pair exits 0."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute(f"drop schema if exists {PG_SCHEMA}_oracle cascade")
        con.execute(f"create schema {PG_SCHEMA}_oracle")
        con.execute(PG_BUILD)
    finally:
        con.close()
    cols = "amount,status,fixed,day,paid_at,note,ratio"
    argv = [
        "diff", "--a", oracle_url, "--a-table", orders,
        "--b", pg_url, "--b-table", f"{PG_SCHEMA}_oracle.orders",
        "--key", "id", "--columns", cols, "--json",
    ]  # fmt: skip
    settle()
    assert main(argv) == 0, capsys.readouterr().out
    clean = json.loads(capsys.readouterr().out)
    assert clean["identical"] and clean["stats"]["rows_downloaded"] == 0
    table = _planted(
        writer,
        "ORA_ORDERS_PG",
        "update {t} set amount = amount + 0.01 where id = 1234",
        "delete from {t} where id = 777",
        "update {t} set note = null where id = 27",
        "update {t} set ratio = ratio + 1e-6d where id = 40",
    )
    argv[4] = table
    settle()
    assert main(argv) == 1
    got = json.loads(capsys.readouterr().out)
    assert [
        (d["key"], d["kind"], [c.lower() for c in d["columns"]])
        for d in got["differences"]
    ] == [
        (27, "different", ["note"]),
        (40, "different", ["ratio"]),
        (777, "only_in_b", sorted(cols.split(","))),
        (1234, "different", ["amount"]),
    ]


def test_parity_md5_is_declared_only_where_it_is_called(oracle_url):
    """`query` prepends the inline MD5 only to SQL that calls it, so every
    other statement is exactly the SQL generated - and it compiles under the
    minimal grants (no CREATE PROCEDURE: it is never stored)."""
    a = open_oracle(oracle_url)
    try:
        assert a.query("select 1 from dual") == [(1,)]
        assert a.query("select parity_md5(to_clob('abc')) from dual")[0][0] == (
            hashlib.md5(b"abc").hexdigest().upper()
        )
        assert "parity_md5" in PARITY_MD5
    finally:
        a.close()
