"""Red team round H: SQL Server, broad and deep (RT-HS<n>).

Every test plants a difference and asserts that exactly it is found, or
asserts a refusal. A confirmed problem is `xfail(strict=True)` with an
`RT-HS<n>` id and asserts what a trustworthy tool must do, so it turns green
the moment it is fixed; everything that was attacked and held is an ordinary
regression test.

Runs against the SQL Server named by `PARITY_TEST_MSSQL` (the login must be
able to create databases and logins - sa on the development container), and
skips without one. Only databases and logins named `rth_mssql_*` are
created, and every one is dropped at the end; the database options changed
are database-scoped and restored. Optional: `PARITY_HS_MSSQL2019` names an
sa URL on a SQL Server 2019 container started with a case-sensitive server
collation (see `test_a_case_sensitive_binary_server_collation_on_2019`).
"""

from __future__ import annotations

import math
import os
import random
import struct
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pytest

from parity.dialects.base import canonical_float_text, get_dialect
from parity.engine import diff

MSSQL_URL = os.environ.get("PARITY_TEST_MSSQL", "")
MSSQL2019_URL = os.environ.get("PARITY_HS_MSSQL2019", "")

pytestmark = pytest.mark.skipif(not MSSQL_URL, reason="PARITY_TEST_MSSQL is not set")

#: The main scratch database: UTF-8, case-insensitive, snapshot allowed.
DB = "rth_mssql_h"
#: A login holding only db_datareader on `DB` (and on the other scratch dbs).
READER = "rth_mssql_h_reader"
READER_PW = "Rth!hReader2026x"  # throwaway, for a disposable local login only


# --------------------------------------------------------------- helpers


def at(db: str, base: str = MSSQL_URL, user: str | None = None, pw: str | None = None) -> str:
    """`base` pointed at database `db`, optionally as another login."""
    u = urlparse(base)._replace(path="/" + db)
    if user:
        u = u._replace(netloc=f"{user}:{pw}@{u.hostname}:{u.port or 1433}")
    return u.geturl()


def ex(db: str, *statements: str, base: str = MSSQL_URL) -> list[list[tuple[Any, ...]]]:
    """Run `statements` with autocommit as the URL's (admin) login in `db`."""
    mssql_python = pytest.importorskip("mssql_python")
    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(at(db, base))
    conn = mssql_python.connect(odbc, autocommit=True, timeout=10)
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


def drop_db(name: str, base: str = MSSQL_URL) -> None:
    """Drop scratch database `name` if it exists, kicking its sessions out."""
    assert name.startswith("rth_mssql_")
    ex(
        "master",
        f"if db_id('{name}') is not null begin alter database [{name}] "
        f"set single_user with rollback immediate; drop database [{name}] end",
        base=base,
    )


def make_db(
    name: str,
    collation: str = "Latin1_General_100_CI_AS_SC_UTF8",
    snapshot: bool = True,
    base: str = MSSQL_URL,
) -> str:
    """A fresh scratch database, the reader login mapped into it; its URL."""
    drop_db(name, base)
    ex("master", f"create database [{name}] collate {collation}", base=base)
    if snapshot:
        ex("master", f"alter database [{name}] set allow_snapshot_isolation on", base=base)
    ex(
        name,
        f"create user [{READER}] for login [{READER}]",
        f"alter role db_datareader add member [{READER}]",
        base=base,
    )
    return at(name, base)


def reader(db: str = DB, base: str = MSSQL_URL) -> str:
    """The db_datareader-only login's URL on `db`."""
    return at(db, base, READER, READER_PW)


def run(ua: str, ub: str, ta: str, tb: str, **kw: Any):
    """`diff` between two SQL Server URLs (or any URLs), always closing both."""
    a = get_dialect(ua, side="A")
    b = get_dialect(ub, side="B")
    kw.setdefault("key", "id")
    try:
        return diff(a, b, ta, tb, **kw)
    finally:
        a.close()
        b.close()


def found(result) -> list[tuple]:
    """(key, kind, sorted columns) of every reported difference, sorted."""
    return sorted(
        ((d.key, d.kind, tuple(sorted(d.columns))) for d in result.diffs),
        key=lambda t: (type(t[0]).__name__, str(t[0])),
    )


def pair(db: str, ddl: str, rows_a: list[str], rows_b: list[str], name: str) -> tuple[str, str]:
    """Create `<name>_a` and `<name>_b` with column list `ddl`; insert rows."""
    a, b = f"{name}_a", f"{name}_b"
    stmts = [f"drop table if exists {a}", f"drop table if exists {b}"]
    stmts += [f"create table {a} ({ddl})", f"create table {b} ({ddl})"]
    stmts += [f"insert {a} values {r}" for r in rows_a]
    stmts += [f"insert {b} values {r}" for r in rows_b]
    ex(db, *stmts)
    return f"dbo.{a}", f"dbo.{b}"


@pytest.fixture(scope="module")
def db() -> Iterator[str]:
    """The main scratch database (and the reader login), dropped afterwards."""
    url = make_db(DB)
    try:
        yield url
    finally:
        drop_db(DB)


@pytest.fixture(scope="module", autouse=True)
def _scratch() -> Iterator[None]:
    """Create the reader login; drop it and every scratch database after."""
    if not MSSQL_URL:
        yield
        return
    pytest.importorskip("mssql_python")
    ex(
        "master",
        f"if suser_id('{READER}') is null create login [{READER}] "
        f"with password = '{READER_PW}', check_policy = off",
    )
    yield
    rows = ex("master", "select name from sys.databases where name like 'rth[_]mssql[_]%'")[0]
    for (name,) in rows:
        drop_db(name)
    ex("master", f"if suser_id('{READER}') is not null drop login [{READER}]")


# =================================================== permissions: the reader


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS1: dynamic data masking - a login without UNMASK hashes the "
    "masks, so different masked values compare identical (false match)",
)
def test_rt_hs1_masked_columns_are_not_compared_as_their_masks(db):
    """A masked column read by a login without UNMASK returns the mask, and
    every expression over it is masked too: two tables whose emails, numbers
    and names all differ are reported identical, with no warning. The tool
    must refuse (or at least not call it identical) when a compared column is
    masked for the reading login (`sys.masked_columns`, UNMASK permission)."""
    ta, tb = pair(
        DB,
        "id int primary key, email varchar(50) masked with (function = 'email()'), "
        "n int masked with (function = 'default()'), "
        "s nvarchar(20) masked with (function = 'partial(1,\"xx\",1)')",
        ["(1, 'alice@example.com', 1, N'abcdef')", "(2, 'x@y.z', 5, N'same')"],
        ["(1, 'bob@example.com', 2, N'axxxxf')", "(2, 'x@y.z', 6, N'same')"],
        "ddm",
    )
    # As sa (UNMASK implied) both planted rows are found ...
    assert found(run(db, db, ta, tb)) == [
        (1, "different", ("email", "n", "s")),
        (2, "different", ("n",)),
    ]
    # ... as the datareader they must be found, or the run refused.
    try:
        result = run(reader(), reader(), ta, tb)
    except ValueError as exc:
        assert "mask" in str(exc).lower()
        return
    assert not result.identical


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS2: row-level security silently filters the reading login's "
    "rows and no caveat says so (false match); PostgreSQL warns",
)
def test_rt_hs2_row_level_security_is_reported(db):
    """A security policy hides rows owned by other users from the reader.
    The hidden row differs between the tables; the reader's verdict is
    "identical" with no warning. PostgreSQL's dialect puts an RLS caveat in
    the output (CLAUDE.md 4.11); SQL Server needs the same
    (`sys.security_policies`)."""
    ta, tb = pair(
        DB,
        "id int primary key, v int, owner sysname",
        [f"(1, 1, '{READER}')", "(2, 2, 'other')", f"(3, 3, '{READER}')"],
        [f"(1, 1, '{READER}')", "(2, 99, 'other')", f"(3, 3, '{READER}')"],
        "rls",
    )
    ex(
        DB,
        "drop security policy if exists dbo.rth_pol",
        "drop function if exists dbo.rth_fn",
    )
    ex(
        DB,
        "create function dbo.rth_fn(@o sysname) returns table with schemabinding as "
        "return select 1 as ok where @o = user_name() or user_name() = 'dbo'",
    )
    ex(
        DB,
        "create security policy dbo.rth_pol "
        "add filter predicate dbo.rth_fn(owner) on dbo.rls_a, "
        "add filter predicate dbo.rth_fn(owner) on dbo.rls_b with (state = on)",
    )
    try:
        assert found(run(db, db, ta, tb)) == [(2, "different", ("v",))]
        result = run(reader(), reader(), ta, tb)
    finally:
        ex(DB, "drop security policy if exists dbo.rth_pol")
    assert not result.identical or any(
        "row-level security" in w.lower() or "security polic" in w.lower()
        for w in result.warnings
    )


def test_a_denied_column_is_refused_loudly_and_can_be_excluded(db):
    """DENY SELECT on one column: the run fails naming the side, and with
    the column excluded the planted difference in another column is found."""
    ta, tb = pair(
        DB,
        "id int primary key, v int, secret int",
        ["(1, 1, 1)", "(2, 2, 2)"],
        ["(1, 1, 1)", "(2, 3, 2)"],
        "deny",
    )
    ex(DB, f"deny select on dbo.deny_a(secret) to [{READER}]")
    ex(DB, f"deny select on dbo.deny_b(secret) to [{READER}]")
    with pytest.raises(ValueError, match=r"side A.*deny_a.*(?i:denied)"):
        run(reader(), reader(), ta, tb)
    assert found(run(reader(), reader(), ta, tb, exclude=["secret"])) == [
        (2, "different", ("v",))
    ]


def test_the_reader_login_cannot_write_and_the_diff_still_finds_the_row(db):
    """The documented deployment: a db_datareader login. The diff runs and
    finds the planted row; the same login is refused a write by the server."""
    ta, tb = pair(DB, "id int primary key, v nvarchar(10)", ["(1, N'a')"], ["(1, N'b')"], "ro")
    assert found(run(reader(), reader(), ta, tb)) == [(1, "different", ("v",))]
    with pytest.raises(Exception, match=r"(?i)permission was denied"):
        ex(DB, "delete from dbo.ro_a", base=reader())
    assert ex(DB, "select count(*) from dbo.ro_a")[0] == [(1,)]


# ====================================================== types and values


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS3: geometry/geography render through ToString(), which drops "
    "the SRID - the same coordinates in two reference systems match (false match)",
)
def test_rt_hs3_spatial_srid_is_compared(db):
    """`ToString()` is WKT and has no SRID: POINT(1 2) in SRID 0 and in 4326,
    or a geography point in WGS 84 (4326) and in NAD 83 (4269), render
    alike. A migration that loses or changes the SRID changes what every
    distance and area query returns."""
    ta, tb = pair(
        DB,
        "id int primary key, g geometry, gg geography",
        ["(1, geometry::Point(1, 2, 0), null)", "(2, null, geography::Point(47.5, 8.5, 4326))"],
        ["(1, geometry::Point(1, 2, 4326), null)", "(2, null, geography::Point(47.5, 8.5, 4269))"],
        "srid",
    )
    assert found(run(db, db, ta, tb)) == [
        (1, "different", ("g",)),
        (2, "different", ("gg",)),
    ]


def test_spatial_and_hierarchyid_coordinates_are_exact(db):
    """What ToString() does carry is exact: a one-ulp coordinate, 1e-300
    against 2e-300, a Z and an M value, and a hierarchyid path all differ."""
    x = 47.123456789012344
    ta, tb = pair(
        DB,
        "id int primary key, g geography, m geometry, h hierarchyid, x xml",
        [
            f"(1, geography::Point({x!r}, 8.1, 4326), null, null, null)",
            f"(2, null, geometry::Point({math.nextafter(1.0, 2)!r}, 1e-300, 0), null, null)",
            "(3, null, geometry::STGeomFromText('POINT(1 2 3 4)', 0), null, null)",
            "(4, null, null, hierarchyid::Parse('/1/2/'), '<a b=\"1\"/>')",
            "(5, null, null, hierarchyid::Parse('/1/'), '<a>x</a>')",
        ],
        [
            f"(1, geography::Point({math.nextafter(x, 100)!r}, 8.1, 4326), null, null, null)",
            "(2, null, geometry::Point(1, 2e-300, 0), null, null)",
            "(3, null, geometry::STGeomFromText('POINT(1 2 3 5)', 0), null, null)",
            "(4, null, null, hierarchyid::Parse('/1/3/'), '<a b=\"2\"/>')",
            "(5, null, null, hierarchyid::Parse('/1/'), '<a>x</a>')",
        ],
        "geo",
    )
    result = run(db, db, ta, tb)
    assert found(result) == [
        (1, "different", ("g",)),
        (2, "different", ("m",)),
        (3, "different", ("m",)),
        (4, "different", ("h", "x")),
    ]
    assert any("unmapped types" in w for w in result.warnings)


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS4: varchar in a DBCS code page (cp932): distinct invalid byte "
    "sequences all decode to U+30FB and hash alike (false match)",
)
def test_rt_hs4_invalid_dbcs_bytes_are_distinct():
    """A Japanese_CI_AS varchar stores bytes; 0x8120 and 0x8121 are both an
    invalid lead/trail pair in code page 932, and the conversion to nvarchar
    turns each into U+30FB (KATAKANA MIDDLE DOT) - so the two rows, and a
    real U+30FB, render alike. The same class as RT-E9 (lone UTF-16
    surrogates): legacy Japanese data with broken encoding is exactly what a
    migration meets. Row 2 (0x41FF vs 0x41FE, valid single-byte katakana)
    is found today."""
    url = make_db("rth_mssql_jp", collation="Japanese_CI_AS")
    ta, tb = pair(
        "rth_mssql_jp",
        "id int primary key, v varchar(20), t text",
        [
            "(1, convert(varchar(20), 0x8120), 'a')",
            "(2, convert(varchar(20), 0x41FF), 'a')",
            "(3, 'x', convert(varchar(20), 0x8120))",
        ],
        [
            "(1, convert(varchar(20), 0x8121), 'a')",
            "(2, convert(varchar(20), 0x41FE), 'a')",
            "(3, 'x', convert(varchar(20), 0x8121))",
        ],
        "jp",
    )
    assert found(run(url, url, ta, tb)) == [
        (1, "different", ("v",)),
        (2, "different", ("v",)),
        (3, "different", ("t",)),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS7: a DECIMAL with more than 38 - scale integer digits kills "
    "the diff with a bare 'Arithmetic overflow', naming no column or value",
)
def test_rt_hs7_a_decimal_too_wide_for_the_scale_names_the_column(db):
    """decimal(38,0) holds 38 integer digits; the rendering cast to
    decimal(38,6) holds 32, so 10^38 - 1 overflows. PostgreSQL renders it,
    MySQL's guard raises naming the column and value (CLAUDE.md 4.2). Here the
    error names the side and table but not the column, the value, or that
    `--float-scale 0` would compare it. Accepts either a comparison that finds
    the row or a refusal that names the column."""
    ta, tb = pair(
        DB,
        "id int primary key, huge_amount decimal(38,0)",
        ["(1, 99999999999999999999999999999999999999)", "(2, 1)"],
        ["(1, 99999999999999999999999999999999999998)", "(2, 1)"],
        "wide",
    )
    try:
        result = run(db, db, ta, tb)
    except ValueError as exc:
        assert "huge_amount" in str(exc)
        return
    assert found(result) == [(1, "different", ("huge_amount",))]


def test_a_wide_decimal_compares_at_float_scale_0(db):
    """The workaround the RT-HS7 message should name: at scale 0 the
    decimal(38,0) cast holds every value, and the one-unit difference shows."""
    ta, tb = pair(
        DB,
        "id int primary key, huge_amount decimal(38,0)",
        ["(1, 99999999999999999999999999999999999999)", "(2, -1)"],
        ["(1, 99999999999999999999999999999999999998)", "(2, -1)"],
        "wide0",
    )
    a = get_dialect(db, side="A", float_scale=0)
    b = get_dialect(db, side="B", float_scale=0)
    try:
        result = diff(a, b, ta, tb, key="id")
    finally:
        a.close()
        b.close()
    assert found(result) == [(1, "different", ("huge_amount",))]


def test_always_encrypted_columns_are_refused_loudly_and_can_be_excluded(db):
    """An Always Encrypted column cannot be converted server-side: the run
    fails naming the side and table (and SQL Server names the encryption);
    excluded, the planted difference elsewhere is found. Never a match over
    ciphertext."""
    ex(
        DB,
        "create column master key rth_cmk with (key_store_provider_name = "
        "'MSSQL_CERTIFICATE_STORE', key_path = "
        "'CurrentUser/My/0123456789ABCDEF0123456789ABCDEF01234567')",
    )
    ex(
        DB,
        "create column encryption key rth_cek with values (column_master_key = rth_cmk, "
        "algorithm = 'RSA_OAEP', encrypted_value = 0x" + "01" * 64 + ")",
    )
    enc = (
        "encrypted with (column_encryption_key = rth_cek, encryption_type = "
        "deterministic, algorithm = 'AEAD_AES_256_CBC_HMAC_SHA_256')"
    )
    ex(
        DB,
        f"create table ae_a (id int primary key, v int, s int {enc})",
        f"create table ae_b (id int primary key, v int, s int {enc})",
        "insert ae_a (id, v) values (1, 1), (2, 2)",
        "insert ae_b (id, v) values (1, 1), (2, 3)",
    )
    with pytest.raises(ValueError, match=r"side A.*ae_a.*(?i:encrypted)"):
        run(db, db, "dbo.ae_a", "dbo.ae_b")
    assert found(run(db, db, "dbo.ae_a", "dbo.ae_b", exclude=["s"])) == [
        (2, "different", ("v",))
    ]


def test_alias_types_render_as_their_base_type(db):
    """`create type ... from bit/decimal`: INFORMATION_SCHEMA reports the
    base type, so a NULL against FALSE in an alias of bit, and a cent in an
    alias of decimal(12,2), are found."""
    ex(
        DB,
        "drop type if exists dbo.rth_flag",
        "drop type if exists dbo.rth_amt",
        "create type dbo.rth_flag from bit",
        "create type dbo.rth_amt from decimal(12,2)",
    )
    ta, tb = pair(
        DB,
        "id int primary key, f dbo.rth_flag, m dbo.rth_amt",
        ["(1, null, 1.50)", "(2, 1, 2.00)", "(3, 0, 3)"],
        ["(1, 0, 1.50)", "(2, 1, 2.01)", "(3, 0, 3)"],
        "alias",
    )
    assert found(run(db, db, ta, tb)) == [
        (1, "different", ("f",)),
        (2, "different", ("m",)),
    ]


def test_date_and_time_types_at_their_range_ends(db):
    """datetimeoffset at 9999-12-31 and 0001-01-01 with +-14:00 offsets (the
    same instant in two offsets is equal; a 100 ns tick apart is not, both
    sides holding ticks), time(3), datetime2(1), smalldatetime minutes and
    datetime's 1/300 s display at both ends of their ranges."""
    cols = (
        "id int primary key, t datetimeoffset(7), t0 datetimeoffset(0), tm time(3), "
        "d2 datetime2(1), sd smalldatetime, dt datetime"
    )
    ta, tb = pair(
        DB,
        cols,
        [
            "(1, '9999-12-31 23:59:59.9999999 +00:00', '2024-01-01 00:00:00 +14:00', "
            "'23:59:59.999', '9999-12-31 23:59:59.9', '2079-06-06 23:59', '9999-12-31 23:59:59.997')",
            "(2, '0001-01-01 00:00:00 -14:00', '0001-01-01 00:00:00 -00:01', '00:00:00', "
            "'0001-01-01', '1900-01-01', '1753-01-01')",
            "(3, '2024-06-01 12:00:00 +05:45', '2024-06-01 12:00:00 -09:30', '12:00:00.5', "
            "'2024-01-01 00:00:00.5', '2024-01-01 00:00', '2024-01-01 00:00:00.003')",
        ],
        [
            "(1, '9999-12-31 23:59:59.9999998 +00:00', '2023-12-31 10:00:00 +00:00', "
            "'23:59:59.998', '9999-12-31 23:59:59.8', '2079-06-06 23:58', '9999-12-31 23:59:59.993')",
            "(2, '0001-01-01 14:00:00 +00:00', '0001-01-01 00:01:00 +00:00', '00:00:00.001', "
            "'0001-01-01 00:00:00.1', '1900-01-01 00:01', '1753-01-01 00:00:00.003')",
            "(3, '2024-06-01 06:15:00 +00:00', '2024-06-01 21:30:00 +00:00', '12:00:00.5', "
            "'2024-01-01 00:00:00.5', '2024-01-01 00:00', '2024-01-01 00:00:00.003')",
        ],
        "dtedge",
    )
    assert found(run(db, db, ta, tb)) == [
        (1, "different", ("d2", "dt", "sd", "t", "tm")),
        (2, "different", ("d2", "dt", "sd", "tm")),
    ]


def test_random_doubles_render_the_canonical_float_text(db):
    """3,000 random doubles (random bit patterns, wide magnitudes, short
    decimals, exact halves at the scales) bound as parameters render exactly
    `canonical_float_text` at scales 0, 6 and 18 - and one planted one-ulp
    difference is found by the diff. (30,000 per seed passed at 0, 2, 6, 9
    and 18 during the campaign.)"""
    from parity.types import Column, LogicalType

    rnd = random.Random(20261104)
    vals: set[float] = set()
    while len(vals) < 3000:
        r = rnd.random()
        if r < 0.35:
            x = struct.unpack("<d", rnd.getrandbits(64).to_bytes(8, "little"))[0]
        elif r < 0.6:
            x = rnd.uniform(-1, 1) * 10 ** rnd.randint(-12, 17)
        elif r < 0.8:
            x = float(f"{rnd.randint(-(10**9), 10**9)}e{rnd.randint(-15, 8)}")
        else:
            s = rnd.choice([0, 2, 6, 9, 18])
            x = (rnd.randint(-(10**8), 10**8) + 0.5) / 10**s
        if math.isfinite(x) and abs(x) < 2.0**116:
            vals.add(x)
    ordered = sorted(vals)
    mssql_python = pytest.importorskip("mssql_python")
    from parity.dialects.mssql_dialect import parse_mssql_url

    conn = mssql_python.connect(parse_mssql_url(db)[0], autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute("drop table if exists ff_a")
        cur.execute("drop table if exists ff_b")
        cur.execute("create table ff_a (id int primary key, f float)")
        cur.execute("create table ff_b (id int primary key, f float)")
        rows = [(i, v) for i, v in enumerate(ordered)]
        cur.executemany("insert ff_a values (?, ?)", rows)
        planted = 1234
        rows[planted] = (planted, math.nextafter(ordered[planted], math.inf))
        cur.executemany("insert ff_b values (?, ?)", rows)
    finally:
        conn.close()
    a = get_dialect(db, side="A")
    try:
        for scale in (0, 6, 18):
            a.float_scale = scale
            expr = a.normalize(Column("f", LogicalType.FLOAT, "float"))
            got = dict(a.query(f"select id, {expr} from dbo.ff_a"))
            bad = [
                (v, got[i]) for i, v in enumerate(ordered) if got[i] != canonical_float_text(v, scale)
            ]
            assert not bad, (scale, bad[:5])
    finally:
        a.close()
    a = get_dialect(db, side="A", float_scale=18)
    b = get_dialect(db, side="B", float_scale=18)
    try:
        result = diff(a, b, "dbo.ff_a", "dbo.ff_b", key="id")
    finally:
        a.close()
        b.close()
    v = ordered[planted]
    if canonical_float_text(v, 18) != canonical_float_text(math.nextafter(v, math.inf), 18):
        assert found(result) == [(planted, "different", ("f",))]
    else:  # pragma: no cover - the seed keeps a visible one-ulp step
        assert result.identical


# ================================================================== schema


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS6: memory-optimized tables cannot be read in the dialect's "
    "SNAPSHOT transaction - the diff dies on error 41332",
)
def test_rt_hs6_memory_optimized_tables_can_be_diffed():
    """In-Memory OLTP tables refuse a session whose isolation level is
    SNAPSHOT (41332), and refuse READ COMMITTED inside an explicit or
    implicit transaction (41368) unless the database elevates to snapshot.
    Both are the dialect's own settings, so a database that allows snapshot
    isolation - the one the docs recommend - cannot diff a memory-optimized
    table at all. The table hint `WITH (SNAPSHOT)` reads it transactionally
    consistent within the one transaction."""
    url = make_db("rth_mssql_mo")
    ex(
        "master",
        "alter database [rth_mssql_mo] add filegroup rth_mo contains memory_optimized_data",
        "alter database [rth_mssql_mo] add file (name = 'rth_mo_data1', filename = "
        "'/var/opt/mssql/data/rth_mssql_mo1') to filegroup rth_mo",
    )
    ex(
        "rth_mssql_mo",
        "create table mo_a (id int not null primary key nonclustered, v int) "
        "with (memory_optimized = on)",
        "create table mo_b (id int not null primary key nonclustered, v int) "
        "with (memory_optimized = on)",
        "insert mo_a values (1, 1), (2, 2)",
        "insert mo_b values (1, 1), (2, 3)",
    )
    assert found(run(url, url, "dbo.mo_a", "dbo.mo_b")) == [(2, "different", ("v",))]


def test_memory_optimized_tables_with_elevate_to_snapshot_and_snapshot_off():
    """The one configuration that works today: snapshot isolation off and
    MEMORY_OPTIMIZED_ELEVATE_TO_SNAPSHOT on. The planted row is found."""
    url = make_db("rth_mssql_mo2", snapshot=False)
    ex(
        "master",
        "alter database [rth_mssql_mo2] add filegroup rth_mo contains memory_optimized_data",
        "alter database [rth_mssql_mo2] add file (name = 'rth_mo_data2', filename = "
        "'/var/opt/mssql/data/rth_mssql_mo2') to filegroup rth_mo",
        "alter database [rth_mssql_mo2] set memory_optimized_elevate_to_snapshot = on",
    )
    ex(
        "rth_mssql_mo2",
        "create table mo_a (id int not null primary key nonclustered, v int) "
        "with (memory_optimized = on)",
        "create table mo_b (id int not null primary key nonclustered, v int) "
        "with (memory_optimized = on)",
        "insert mo_a values (1, 1), (2, 2)",
        "insert mo_b values (1, 1), (2, 3)",
    )
    assert found(run(url, url, "dbo.mo_a", "dbo.mo_b")) == [(2, "different", ("v",))]


@pytest.mark.xfail(
    strict=True,
    reason="RT-HS9: a synonym is reported as 'table not found ... may lack "
    "SELECT', though it exists and is readable",
)
def test_rt_hs9_a_synonym_is_diffed_or_named(db):
    """INFORMATION_SCHEMA.COLUMNS has no synonyms, so `dbo.syn` is "not found"
    and the hint blames permissions. Either resolve it (`sys.synonyms`,
    `base_object_name`) or say it is a synonym and name its target."""
    ta, tb = pair(DB, "id int primary key, v int", ["(1, 1)", "(2, 2)"], ["(1, 1)", "(2, 3)"], "syn")
    ex(DB, "drop synonym if exists dbo.rth_syn", "create synonym dbo.rth_syn for dbo.syn_a")
    try:
        result = run(db, db, "dbo.rth_syn", tb)
    except ValueError as exc:
        assert "synonym" in str(exc).lower()
        return
    assert found(result) == [(2, "different", ("v",))]


def test_computed_persisted_sparse_and_column_set_columns(db):
    """Computed (virtual and persisted), sparse and a column set: a sparse
    value change is found (twice over: the column and the column set's XML),
    and a base change moves the computed columns with it."""
    ddl = (
        "id int primary key, a int, b as a * 2, p as a + 1 persisted, "
        "s int sparse null, cs xml column_set for all_sparse_columns"
    )
    ex(DB, "drop table if exists cc_a", "drop table if exists cc_b")
    ex(DB, f"create table cc_a ({ddl})", f"create table cc_b ({ddl})")
    ex(
        DB,
        "insert cc_a (id, a, s) values (1, 1, 5), (2, 2, null), (3, 3, null)",
        "insert cc_b (id, a, s) values (1, 1, 6), (2, 2, null), (3, 4, null)",
    )
    assert found(run(db, db, "dbo.cc_a", "dbo.cc_b")) == [
        (1, "different", ("cs", "s")),
        (3, "different", ("a", "b", "p")),
    ]


def test_temporal_columnstore_partitioned_and_indexed_view_sources(db):
    """A system-versioned table (its hidden period columns are a schema
    difference against a plain table), a clustered columnstore table, a
    partitioned table and an indexed view each find the one planted row."""
    ex(
        DB,
        "drop view if exists dbo.iv",
        "drop table if exists t_sys",
        "drop table if exists t_sys_hist",
        "drop table if exists t_plain",
        "drop table if exists t_cs",
        "drop table if exists t_part",
        "if exists (select * from sys.partition_schemes where name = 'rth_ps') drop partition scheme rth_ps",
        "if exists (select * from sys.partition_functions where name = 'rth_pf') drop partition function rth_pf",
        "create partition function rth_pf (int) as range right for values (100, 200)",
        "create partition scheme rth_ps as partition rth_pf all to ([primary])",
        "create table t_sys (id int primary key, v int, vf datetime2 generated always as row start "
        "hidden not null, vt datetime2 generated always as row end hidden not null, "
        "period for system_time (vf, vt)) with (system_versioning = on (history_table = dbo.t_sys_hist))",
        "create table t_plain (id int primary key, v int)",
        "create table t_cs (id int not null, v int, index cci clustered columnstore)",
        "create table t_part (id int not null primary key, v int) on rth_ps(id)",
    )
    ex(
        DB,
        "insert t_sys (id, v) select value, value from generate_series(1, 300)",
        "insert t_plain select value, value from generate_series(1, 300)",
        "insert t_cs select value, value from generate_series(1, 300)",
        "insert t_part select value, value from generate_series(1, 300)",
        "update t_sys set v = -1 where id = 7",
        "update t_cs set v = -1 where id = 150",
        "update t_part set v = -1 where id = 250",
    )
    ex(
        DB,
        "create view dbo.iv with schemabinding as select id, v from dbo.t_part",
    )
    ex(DB, "create unique clustered index iv_ix on dbo.iv (id)")
    r = run(db, db, "dbo.t_sys", "dbo.t_plain")
    assert found(r) == [(7, "different", ("v",))]
    assert any("vf" in s and "vt" in s for s in r.schema_differences)
    assert found(run(db, db, "dbo.t_cs", "dbo.t_plain")) == [(150, "different", ("v",))]
    assert found(run(db, db, "dbo.t_part", "dbo.t_plain")) == [(250, "different", ("v",))]
    assert found(run(db, db, "dbo.iv", "dbo.t_plain")) == [(250, "different", ("v",))]


def test_a_view_into_a_database_without_snapshot_is_refused_by_name():
    """Under SNAPSHOT a view reading another database that does not allow it
    fails with SQL Server's own explanation (3952), naming the side and the
    view - loud, never a READ COMMITTED read passed off as one snapshot."""
    other = make_db("rth_mssql_other", snapshot=False)
    main = make_db("rth_mssql_xdb")
    ex("rth_mssql_other", "create table t (id int primary key, v int)", "insert t values (1, 1)")
    ex(
        "rth_mssql_xdb",
        "create view xv as select id, v from rth_mssql_other.dbo.t",
        "create table t (id int primary key, v int)",
        "insert t values (1, 2)",
    )
    with pytest.raises(ValueError, match=r"side A.*xv.*(?i:snapshot isolation)"):
        run(main, main, "dbo.xv", "dbo.t")
    assert found(run(other, main, "dbo.t", "dbo.t")) == [(1, "different", ("v",))]


def test_a_read_only_database_is_diffed_at_one_snapshot():
    """A database set READ_ONLY (a restored legacy backup) with snapshot
    allowed: the diff runs and finds the planted row."""
    url = make_db("rth_mssql_rodb")
    ex(
        "rth_mssql_rodb",
        "create table t (id int primary key, v int)",
        "create table u (id int primary key, v int)",
        "insert t values (1, 1), (2, 2)",
        "insert u values (1, 1), (2, 3)",
    )
    ex("master", "alter database [rth_mssql_rodb] set read_only with rollback immediate")
    r = run(url, url, "dbo.t", "dbo.u")
    assert found(r) == [(2, "different", ("v",))]
    assert not any("SNAPSHOT" in w for w in r.warnings)


def test_database_ansi_options_off_do_not_move_the_verdict():
    """CONCAT_NULL_YIELDS_NULL, ANSI_WARNINGS, ARITHABORT, ANSI_NULLS,
    QUOTED_IDENTIFIER and ANSI_PADDING all OFF at database level. With
    CONCAT_NULL_YIELDS_NULL off a NULL binary would render `\\x` like an
    empty one, and with ANSI_WARNINGS off an overflow would be NULL; the
    driver's session SETs override the database defaults, so NULL against
    0x and a NULL decimal against 5 are both found."""
    url = make_db("rth_mssql_ansi")
    opts = (
        "concat_null_yields_null",
        "ansi_warnings",
        "arithabort",
        "ansi_nulls",
        "quoted_identifier",
        "ansi_padding",
    )
    ex("master", *(f"alter database [rth_mssql_ansi] set {o} off" for o in opts))
    ta, tb = pair(
        "rth_mssql_ansi",
        "id int primary key, b varbinary(10), v sql_variant, d decimal(38,0)",
        ["(1, null, null, 1)", "(2, 0x01, 1, 5)"],
        ["(1, 0x, null, 1)", "(2, 0x01, 1, null)"],
        "ansi",
    )
    assert found(run(url, url, ta, tb)) == [
        (1, "different", ("b",)),
        (2, "different", ("d",)),
    ]
