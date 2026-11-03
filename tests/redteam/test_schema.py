"""Red team: schema and metadata.

The failure hunted here is a column that is silently *not compared* - or
compared through a rendering that cannot see the difference - so the run says
"identical" while the data differs. Every test plants a real difference and
asserts what the tool does with it (CLAUDE.md section 8).

Confirmed bugs are `xfail(strict=True)` with an `RT-C<n>` id; everything else
pins behaviour that was attacked and held. Every object created is prefixed
`rt_sch_` and dropped afterwards.
"""

from __future__ import annotations

import io
import json
import os
from urllib.parse import unquote, urlparse

import pytest
from conftest import MSSQL_URL, PG_URL, duckdb_write

from parity.cli import EXIT_DIFFERENCES, main
from parity.dialects.base import MAX_CONCAT_ARGS, get_dialect
from parity.engine import _is_tz_aware, diff
from parity.types import Column

PG_SCHEMA = "rt_sch_pg"
MSSQL_DB = "parity_utf8"
MSSQL_SCHEMA = "rt_sch_s"


def _found(result) -> list[tuple]:
    """(key, kind, columns) for every reported difference."""
    return [(d.key, d.kind, sorted(d.columns)) for d in result.diffs]


def _duck(tmp_path, name: str, *sql: str) -> str:
    """Build a DuckDB file from statements and return its path."""
    path = str(tmp_path / f"{name}.duckdb")
    con = duckdb_write(path)
    for s in sql:
        con.execute(s)
    con.close()
    return path


def _run(a_url: str, b_url: str, a_table: str, b_table: str, key, **kw):
    """Open both sides, diff, close."""
    a = get_dialect(a_url, side="A")
    b = get_dialect(b_url, side="B")
    try:
        return diff(a, b, a_table, b_table, key, **kw)
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def pg(pg_url):
    """An autocommit admin connection with a fresh `rt_sch_pg` schema."""
    import psycopg

    con = psycopg.connect(pg_url, autocommit=True)
    con.execute(f"drop schema if exists {PG_SCHEMA} cascade")
    con.execute('drop schema if exists "Rt_Sch Odd" cascade')
    con.execute(f"create schema {PG_SCHEMA}")
    yield con
    con.execute(f"drop schema if exists {PG_SCHEMA} cascade")
    con.execute('drop schema if exists "Rt_Sch Odd" cascade')
    con.execute("drop table if exists public.rt_sch_shadow")
    con.close()


@pytest.fixture
def my(mysql_url):
    """An autocommit MySQL admin cursor; drops every rt_sch_ table afterwards."""
    import mysql.connector

    u = urlparse(mysql_url)
    con = mysql.connector.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 3306,
        user=unquote(u.username) if u.username else None,
        password=unquote(u.password) if u.password else None,
        database=u.path.lstrip("/"),
        autocommit=True,
    )
    cur = con.cursor()

    def drop_all() -> None:
        """Drop every `rt_sch_` table, so a failed earlier run leaves nothing."""
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = database() and table_name like 'rt\\_sch\\_%'"
        )
        for (t,) in cur.fetchall():
            cur.execute(f"drop table `{t}`")

    drop_all()
    yield cur
    drop_all()
    cur.close()
    con.close()


@pytest.fixture
def ms(mssql_url):
    """A SQL Server admin with a fresh `rt_sch_s` schema in parity_utf8."""
    from test_mssql import Admin

    adm = Admin(mssql_url)

    def drop_all() -> None:
        """Drop every table in our schema, so a failed earlier run leaves nothing."""
        for (t,) in adm.run(
            MSSQL_DB,
            "select name from sys.tables where schema_name(schema_id) = ?",
            (MSSQL_SCHEMA,),
        ):
            adm.run(MSSQL_DB, f"drop table [{MSSQL_SCHEMA}].[{t}]")

    adm.run(
        MSSQL_DB,
        f"if schema_id('{MSSQL_SCHEMA}') is null exec('create schema {MSSQL_SCHEMA}')",
    )
    drop_all()
    yield lambda sql, params=None: adm.run(MSSQL_DB, sql, params)
    drop_all()
    adm.run(MSSQL_DB, f"drop schema if exists [{MSSQL_SCHEMA}]")
    adm.close()


def _ms_url() -> str:
    """The SQL Server URL pointed at parity_utf8."""
    from test_mssql import db_url

    return db_url(MSSQL_URL, MSSQL_DB)


# =================================================================== RT-C1
# DuckDB TIMESTAMP_NS is mapped to TIMESTAMP by prefix and rendered with
# strftime('%f'), which is microseconds. Two values that differ only in their
# nanoseconds render identically, and - unlike SQL Server's datetime2(7), which
# gets a caveat - nothing in the output says the comparison was truncated.


def test_duckdb_timestamp_ns_difference_is_found_or_caveated(tmp_path):
    """Plant a 998 ns difference; it must be reported or the loss said aloud."""
    paths = []
    for name, v in (("a", "000000001"), ("b", "000000999")):
        p = _duck(
            tmp_path,
            name,
            "create table t(id int, ts timestamp_ns)",
            f"insert into t values (1, '2024-01-01 00:00:00.{v}'::timestamp_ns)",
        )
        paths.append(p)
    # The stored values really differ.
    import duckdb

    got = [
        duckdb.connect(p, read_only=True).execute("select epoch_ns(ts) from t").fetchone()
        for p in paths
    ]
    assert got[0] != got[1]

    result = _run(f"duckdb:///{paths[0]}", f"duckdb:///{paths[1]}", "t", "t", "id")
    sub_us = any("nanosecond" in w or "microsecond" in w for w in result.warnings)
    assert not result.identical or sub_us, result.warnings


# =================================================================== RT-C2
# PostgreSQL: `columns()` looks an unqualified table up in `public` (the
# documented default), but every data query names the table unqualified, so
# PostgreSQL resolves it through the session's `search_path`. With a role or
# URL that puts another schema first, the tool reads one table's columns and
# another table's rows - and can call `public.t` identical to side B when it
# is not.


def test_unqualified_postgres_table_is_read_from_public_not_search_path(pg, tmp_path):
    """public.rt_sch_shadow differs from B; a search_path twin matches B."""
    pg.execute("create table public.rt_sch_shadow(id int, amount int)")
    pg.execute("insert into public.rt_sch_shadow values (1, 999)")
    pg.execute(f"create table {PG_SCHEMA}.rt_sch_shadow(id int, amount int)")
    pg.execute(f"insert into {PG_SCHEMA}.rt_sch_shadow values (1, 10)")
    b = _duck(
        tmp_path,
        "b",
        "create table rt_sch_shadow(id int, amount int)",
        "insert into rt_sch_shadow values (1, 10)",
    )
    url = PG_URL + f"?options=-csearch_path%3D{PG_SCHEMA},public"

    # Qualified, the real difference in public is found ...
    qualified = _run(url, f"duckdb:///{b}", "public.rt_sch_shadow", "rt_sch_shadow", "id")
    assert _found(qualified) == [(1, "different", ["amount"])]
    # ... and unqualified, which the docs say means public, must agree.
    result = _run(url, f"duckdb:///{b}", "rt_sch_shadow", "rt_sch_shadow", "id")
    assert _found(result) == [(1, "different", ["amount"])]


# =================================================================== RT-C3
# A PostgreSQL materialized view is not in information_schema.columns, so it
# cannot be diffed at all - and the error then tells the user they lack a
# GRANT, because pg_class does list it.


def test_postgres_materialized_view_is_diffable_or_named_honestly(pg, tmp_path):
    """Either compare the matview, or say it is one - never blame privileges."""
    pg.execute(f"create table {PG_SCHEMA}.base(id int, amount int)")
    pg.execute(f"insert into {PG_SCHEMA}.base values (1, 10), (2, 20)")
    pg.execute(
        f"create materialized view {PG_SCHEMA}.mv as select * from {PG_SCHEMA}.base"
    )
    b = _duck(
        tmp_path,
        "b",
        "create table mv(id int, amount int)",
        "insert into mv values (1, 10), (2, 21)",
    )
    try:
        result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.mv", "mv", "id")
    except ValueError as exc:
        assert "GRANT" not in str(exc) and "cannot read" not in str(exc), str(exc)
    else:
        assert _found(result) == [(2, "different", ["amount"])]


# =================================================================== RT-C4
# MySQL binary-family types are unmapped, so they render through
# `cast(c as char)`. MySQL returns NULL - with only warning 1300 - for bytes
# that are not valid UTF-8, the coalesce turns that into the NULL sentinel, and
# every such value compares equal to every other such value and to a real
# NULL. BINARY(16) from UUID_TO_BIN is the textbook MySQL uuid column.


@pytest.mark.parametrize(
    ("ddl", "a_val", "b_val"),
    [
        ("varbinary(16)", "0xFF01", "0xFE02"),
        (
            "binary(16)",
            "uuid_to_bin('3f06af63-a93c-11e4-9797-00505690773f')",
            "uuid_to_bin('9f06af63-a93c-11e4-9797-00505690773f')",
        ),
        ("blob", "0xC0", "0xC1"),
        (
            "bit(64)",
            "b'1111111111111111111111111111111111111111111111111111111111111111'",
            "b'1111111111111111111111111111111111111111111111111111111111111110'",
        ),
        ("varbinary(16)", "0xFF01", "null"),
        # Geometry is stored as SRID + WKB, whose doubles are rarely valid
        # UTF-8: two different points render NULL just the same.
        ("geometry", "st_geomfromtext('POINT(1 2)')", "st_geomfromtext('POINT(1 3)')"),
    ],
    ids=["varbinary", "binary16-uuid", "blob", "bit64", "binary-vs-null", "geometry"],
)
def test_mysql_binary_values_that_differ_are_reported(my, mysql_url, ddl, a_val, b_val):
    """Two different byte strings in the same column must not compare equal."""
    for t, v in (("rt_sch_bin_a", a_val), ("rt_sch_bin_b", b_val)):
        my.execute(f"create table {t}(id int primary key, v {ddl})")
        my.execute(f"insert into {t} values (1, {v})")
    result = _run(mysql_url, mysql_url, "rt_sch_bin_a", "rt_sch_bin_b", "id")
    assert _found(result) == [(1, "different", ["v"])]


def test_mysql_utf8_valid_binary_differences_are_still_found(my, mysql_url):
    """The rendering is only blind to *invalid* UTF-8; valid bytes still differ."""
    for t, v in (("rt_sch_ok_a", "0x41"), ("rt_sch_ok_b", "0x42")):
        my.execute(f"create table {t}(id int primary key, v varbinary(4))")
        my.execute(f"insert into {t} values (1, {v})")
    result = _run(mysql_url, mysql_url, "rt_sch_ok_a", "rt_sch_ok_b", "id")
    assert _found(result) == [(1, "different", ["v"])]
    # BINARY has a rule now: the sample check compares it by its bytes.
    assert "v" in result.verification.checked


def test_mysql_invisible_column_is_compared(my, mysql_url):
    """An INVISIBLE column is in information_schema and is selected by name."""
    for t, v in (("rt_sch_inv_a", 1), ("rt_sch_inv_b", 2)):
        my.execute(f"create table {t}(id int primary key, x int, h int invisible)")
        my.execute(f"insert into {t}(id, x, h) values (1, 5, {v})")
    result = _run(mysql_url, mysql_url, "rt_sch_inv_a", "rt_sch_inv_b", "id")
    assert _found(result) == [(1, "different", ["h"])]


# =================================================================== RT-C5
# SQL Server sql_variant is unmapped and renders as cast(... as nvarchar(max)),
# which for a variant holding a float keeps ~6 significant digits, for one
# holding a datetime or datetime2 keeps only minutes ("Jan  1 2024 10:00AM"),
# and for any value drops the base type (int 1 == nvarchar '1').


@pytest.mark.parametrize(
    ("a_val", "b_val"),
    [
        ("cast(1.0000001 as float)", "cast(1.0000002 as float)"),
        (
            "cast('2024-01-01 10:00:01' as datetime)",
            "cast('2024-01-01 10:00:59' as datetime)",
        ),
        # The base type is lost too: int 1 and the string '1' both render "1".
        ("cast(1 as int)", "cast(N'1' as nvarchar(10))"),
    ],
    ids=["float", "datetime", "int-vs-string"],
)
def test_mssql_sql_variant_differences_are_reported(ms, a_val, b_val):
    """Two different values inside sql_variant must not compare equal."""
    for t, v in (("va", a_val), ("vb", b_val)):
        ms(f"create table [{MSSQL_SCHEMA}].[{t}](id int primary key, v sql_variant)")
        ms(f"insert into [{MSSQL_SCHEMA}].[{t}] values (1, {v})")
    url = _ms_url()
    result = _run(url, url, f"{MSSQL_SCHEMA}.va", f"{MSSQL_SCHEMA}.vb", "id")
    assert _found(result) == [(1, "different", ["v"])]


def test_mssql_sparse_columns_and_time7_are_compared(ms):
    """Sparse columns, their column set and time(7) ticks all reach the diff."""
    for t, sp, tm in (("sa", 5, "10:00:00.1234561"), ("sb", 6, "10:00:00.1234569")):
        ms(
            f"create table [{MSSQL_SCHEMA}].[{t}](id int primary key, sp int sparse null, "
            "cs xml column_set for all_sparse_columns, tm time(7))"
        )
        ms(f"insert into [{MSSQL_SCHEMA}].[{t}](id, sp, tm) values (1, {sp}, '{tm}')")
    url = _ms_url()
    result = _run(url, url, f"{MSSQL_SCHEMA}.sa", f"{MSSQL_SCHEMA}.sb", "id")
    assert _found(result) == [(1, "different", ["cs", "sp", "tm"])]


# =================================================================== RT-C6/7
# Snowflake reports TIMESTAMP_TZ / TIMESTAMP_LTZ / TIMESTAMP_NTZ, never
# "with time zone", and its dialect does not set `Column.tz_aware`. So the
# engine believes every Snowflake timestamp is naive: a TIMESTAMP_NTZ against
# a PostgreSQL timestamptz raises no warning, and TIMESTAMP_TZ against
# timestamptz raises a false one. Offline - the classification is pure Python.


@pytest.mark.parametrize("raw", ["TIMESTAMP_TZ", "TIMESTAMP_LTZ"])
def test_snowflake_timezone_aware_types_are_recognised(raw):
    """What the Snowflake dialect builds for a TZ column must read as aware.

    RT-C6, fixed: built through the dialect's own `_column` (what `columns()`
    returns), which sets `tz_aware` - a bare Column carries no such knowledge.
    """
    from parity.dialects.snowflake_dialect import _column

    assert _is_tz_aware(_column("TS", raw, None, 9))
    assert not _is_tz_aware(_column("TS", "TIMESTAMP_NTZ", None, 9))


def test_snowflake_nanosecond_timestamps_carry_a_caveat():
    """Snowflake timestamps default to 9 digits; FF6 drops three, silently.

    SQL Server says so for datetime2(7). Snowflake renders every TIMESTAMP with
    FF6 and reports nothing.
    """
    from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake

    d = SnowflakeDialect(side="A")
    col = Column("TS", map_type_snowflake("TIMESTAMP_NTZ", None), "TIMESTAMP_NTZ")
    assert "FF6" in d.normalize(col)
    assert d.caveats([col]), "no caveat for a column compared below its precision"


# ============================================================ one-sided columns
# A column renamed during the migration whose values also changed. This used
# to be a green verdict and exit 0 with only a warning; since RT-F1 an
# uncompared column is a schema difference: not identical, exit 1.


def test_a_renamed_column_with_changed_values_is_not_identical(tmp_path):
    """Pin the user-visible behaviour: no all-clear headline, exit 1, named."""
    a = _duck(
        tmp_path,
        "a",
        "create table t(id int, amount int)",
        "insert into t values (1, 10), (2, 20)",
    )
    b = _duck(
        tmp_path,
        "b",
        "create table t(id int, amount_usd int)",
        "insert into t values (1, 99), (2, 98)",
    )
    base = ["diff", "--a", f"duckdb:///{a}", "--a-table", "t"]
    base += ["--b", f"duckdb:///{b}", "--b-table", "t", "--key", "id"]

    out = io.StringIO()
    assert main(base, out=out) == EXIT_DIFFERENCES
    text = out.getvalue()
    assert "schemas differ" in text.splitlines()[0]
    assert "not compared, present only on side A: ['amount']" in text
    assert "not compared, present only on side B: ['amount_usd']" in text
    assert "no comparable columns" in text

    out = io.StringIO()
    assert main([*base, "--json"], out=out) == EXIT_DIFFERENCES
    doc = json.loads(out.getvalue())
    assert doc["identical"] is False and doc["columns_compared"] == []
    assert any("amount_usd" in w for w in doc["schema_differences"])

    out = io.StringIO()
    assert main([*base, "--quiet"], out=out) == EXIT_DIFFERENCES
    assert out.getvalue() == "", "--quiet prints nothing; the exit code carries it"


def test_postgres_63_byte_truncation_splits_one_column_into_two_one_sided(pg, tmp_path):
    """A 70-character name is cut to 63 bytes on PostgreSQL, not on DuckDB."""
    long = "c" * 70
    pg.execute(f'create table {PG_SCHEMA}.lng(id int, "{long}" int)')
    pg.execute(f"insert into {PG_SCHEMA}.lng values (1, 1)")
    b = _duck(
        tmp_path,
        "b",
        f'create table lng(id int, "{long}" int)',
        "insert into lng values (1, 2)",
    )
    result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.lng", "lng", "id")
    assert not result.identical  # the planted 1-vs-2 is not seen, but ...
    assert result.columns == []
    # ... the split column is a schema difference, and every reason printed.
    joined = "\n".join(result.schema_differences + result.warnings)
    assert "c" * 63 + "'" in joined and long in joined
    assert "no comparable columns" in joined


# =========================================================== awkward identifiers

#: Column names that need quoting somewhere: reserved words, every engine's
#: quote character, spaces, a dot, a leading digit, non-ASCII.
ODD = [
    "select",
    "group",
    "key",
    'dq"x',
    "bt`x",
    "br]x",
    "with space",
    "dot.x",
    "1lead",
    "ñandú",
    "数据",
]


def _odd_duck(tmp_path, changed: str | None) -> str:
    """DuckDB side: key `order` plus every ODD column, one row changed."""
    q = lambda n: '"' + n.replace('"', '""') + '"'  # noqa: E731
    cols = ", ".join(f"{q(n)} varchar" for n in ODD)
    vals = ", ".join("'B'" if n == changed else f"'v{i}'" for i, n in enumerate(ODD))
    return _duck(
        tmp_path,
        f"odd_{abs(hash(changed))}",
        f'create table odd("order" int, {cols})',
        f"insert into odd values (1, {vals}), (2, {', '.join(repr(f'v{i}') for i in range(len(ODD)))})",
    )


@pytest.mark.parametrize("changed", ODD)
def test_awkward_column_names_are_compared_against_postgres(pg, tmp_path, changed):
    """Each odd name is quoted correctly on both sides and its difference found."""
    q = lambda n: '"' + n.replace('"', '""') + '"'  # noqa: E731
    cols = ", ".join(f"{q(n)} text" for n in ODD)
    vals = ", ".join(repr(f"v{i}") for i in range(len(ODD)))
    pg.execute(f'create table {PG_SCHEMA}.odd("order" int, {cols})')
    pg.execute(f"insert into {PG_SCHEMA}.odd values (1, {vals}), (2, {vals})")
    b = _odd_duck(tmp_path, changed)
    result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.odd", "odd", "ORDER")
    assert _found(result) == [(1, "different", [changed])]
    assert len(result.columns) == len(ODD)


@pytest.mark.parametrize("changed", ODD)
def test_awkward_column_names_are_compared_against_mysql(
    my, mysql_url, tmp_path, changed
):
    """MySQL quotes with backticks; a backtick inside a name must be doubled."""
    q = lambda n: "`" + n.replace("`", "``") + "`"  # noqa: E731
    cols = ", ".join(f"{q(n)} varchar(10)" for n in ODD)
    vals = ", ".join(repr(f"v{i}") for i in range(len(ODD)))
    my.execute(
        f"create table rt_sch_odd(`order` int primary key, {cols}) charset utf8mb4"
    )
    my.execute(f"insert into rt_sch_odd values (1, {vals}), (2, {vals})")
    b = _odd_duck(tmp_path, changed)
    result = _run(mysql_url, f"duckdb:///{b}", "rt_sch_odd", "odd", "order")
    assert _found(result) == [(1, "different", [changed])]


@pytest.mark.parametrize("changed", ODD)
def test_awkward_column_names_are_compared_against_mssql(ms, tmp_path, changed):
    """SQL Server brackets; a `]` inside a name must be doubled."""
    q = lambda n: "[" + n.replace("]", "]]") + "]"  # noqa: E731
    cols = ", ".join(f"{q(n)} nvarchar(10)" for n in ODD)
    vals = ", ".join("N" + repr(f"v{i}") for i in range(len(ODD)))
    ms(f"create table [{MSSQL_SCHEMA}].[odd]([order] int primary key, {cols})")
    ms(f"insert into [{MSSQL_SCHEMA}].[odd] values (1, {vals}), (2, {vals})")
    b = _odd_duck(tmp_path, changed)
    result = _run(_ms_url(), f"duckdb:///{b}", f"{MSSQL_SCHEMA}.odd", "odd", "order")
    assert _found(result) == [(1, "different", [changed])]


def test_a_schema_name_needing_quotes_resolves(pg, tmp_path):
    """Mixed case and a space in the schema name survive `qualify`."""
    pg.execute('create schema "Rt_Sch Odd"')
    pg.execute('create table "Rt_Sch Odd"."T"(id int, v int)')
    pg.execute('insert into "Rt_Sch Odd"."T" values (1, 1), (2, 2)')
    b = _duck(
        tmp_path,
        "b",
        "create table t(id int, v int)",
        "insert into t values (1, 1), (2, 3)",
    )
    result = _run(PG_URL, f"duckdb:///{b}", "Rt_Sch Odd.T", "t", "id")
    assert _found(result) == [(2, "different", ["v"])]


def test_a_view_with_a_computed_column_compares_like_a_table(pg, tmp_path):
    """A view's computed column has a real type in information_schema."""
    pg.execute(f"create table {PG_SCHEMA}.base(id int, a int, b numeric(10,2))")
    pg.execute(f"insert into {PG_SCHEMA}.base values (1, 2, 1.25), (2, 3, 2.5)")
    pg.execute(
        f"create view {PG_SCHEMA}.v as select id, a * 2 as dbl, b * 3 as tri "
        f"from {PG_SCHEMA}.base"
    )
    b = _duck(
        tmp_path,
        "b",
        "create table v(id int, dbl int, tri decimal(12,2))",
        "insert into v values (1, 4, 3.75), (2, 6, 7.51)",
    )
    result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.v", "v", "id")
    assert _found(result) == [(2, "different", ["tri"])]
    assert not [w for w in result.warnings if "dbl" in w or "tri" in w]


# =============================================================== wide tables

#: Around every concat-tree boundary: one call holds 64; PostgreSQL's hard
#: limit is 100 arguments including the separator; 129 nests twice.
WIDTHS = [MAX_CONCAT_ARGS - 1, MAX_CONCAT_ARGS, MAX_CONCAT_ARGS + 1, 99, 100, 101, 129]


def _wide_duck(tmp_path, n: int, changed: int) -> str:
    """DuckDB side: n int columns c0..c{n-1}, column `changed` differs on id 2."""
    cols = ", ".join(f"c{i} int" for i in range(n))
    row1 = ", ".join(str(i) for i in range(n))
    row2 = ", ".join(str(i + (1 if i == changed else 0)) for i in range(n))
    return _duck(
        tmp_path,
        f"w{n}_{changed}",
        f"create table w(id int, {cols})",
        f"insert into w values (1, {row1}), (2, {row2})",
    )


def _wide_rows(n: int) -> tuple[str, str]:
    """Column DDL and one row of values for side A."""
    return ", ".join(f"c{i} int" for i in range(n)), ", ".join(str(i) for i in range(n))


@pytest.mark.parametrize("n", [*WIDTHS, 1000])
def test_wide_tables_find_a_difference_in_the_last_column_on_postgres(pg, tmp_path, n):
    """The last column sits in the last (possibly single-element) group."""
    ddl, row = _wide_rows(n)
    pg.execute(f"create table {PG_SCHEMA}.w(id int, {ddl})")
    pg.execute(f"insert into {PG_SCHEMA}.w values (1, {row}), (2, {row})")
    for changed in (0, n - 1):
        b = _wide_duck(tmp_path, n, changed)
        result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.w", "w", "id")
        assert _found(result) == [(2, "different", [f"c{changed}"])]


@pytest.mark.parametrize("n", [*WIDTHS, 1000])
def test_wide_tables_find_a_difference_in_the_last_column_on_mysql(
    my, mysql_url, tmp_path, n
):
    """MySQL has no small argument limit, but nests identically regardless."""
    ddl, row = _wide_rows(n)
    my.execute(f"create table rt_sch_w(id int primary key, {ddl})")
    my.execute(f"insert into rt_sch_w values (1, {row}), (2, {row})")
    for changed in (0, n - 1):
        b = _wide_duck(tmp_path, n, changed)
        result = _run(mysql_url, f"duckdb:///{b}", "rt_sch_w", "w", "id")
        assert _found(result) == [(2, "different", [f"c{changed}"])]


@pytest.mark.parametrize("n", [*WIDTHS, 300])
def test_wide_tables_find_a_difference_in_the_last_column_on_mssql(ms, tmp_path, n):
    """SQL Server's CONCAT_WS takes 254 arguments and refuses a single one."""
    ddl, row = _wide_rows(n)
    ms(f"create table [{MSSQL_SCHEMA}].[w](id int primary key, {ddl})")
    ms(f"insert into [{MSSQL_SCHEMA}].[w] values (1, {row}), (2, {row})")
    for changed in (0, n - 1):
        b = _wide_duck(tmp_path, n, changed)
        result = _run(_ms_url(), f"duckdb:///{b}", f"{MSSQL_SCHEMA}.w", "w", "id")
        assert _found(result) == [(2, "different", [f"c{changed}"])]


# =================================================================== RT-C8
# SQL Server: every compared value is cast to nvarchar(max) (deliberately - see
# the dialect docstring), and the checksum query's GROUP BY worktable then
# holds ~26 bytes per column. Past about 307 compared columns it exceeds the
# 8,060-byte row limit and the diff dies with a raw driver error that names
# neither side nor table - on a table SQL Server itself stores without
# complaint (up to 1,024 columns). Loud, not a false match.


def test_a_400_column_sql_server_table_can_be_diffed(ms, tmp_path):
    """400 int columns - a quarter under SQL Server's own 1,024 limit."""
    n = 400
    ddl, row = _wide_rows(n)
    ms(f"create table [{MSSQL_SCHEMA}].[w](id int primary key, {ddl})")
    ms(f"insert into [{MSSQL_SCHEMA}].[w] values (1, {row}), (2, {row})")
    b = _wide_duck(tmp_path, n, n - 1)
    try:
        result = _run(_ms_url(), f"duckdb:///{b}", f"{MSSQL_SCHEMA}.w", "w", "id")
    except Exception as exc:
        assert "[side A" in str(exc), f"error names no side: {exc}"
        raise
    assert _found(result) == [(2, "different", [f"c{n - 1}"])]


# ========================================================= type pairs (PG/Duck)

#: (PostgreSQL type, value A, DuckDB type, value B that really differs,
#: whether a type-mismatch warning is expected). A real difference is planted
#: on row 1 in every pair - the property under attack is "no false match", so
#: each must report row 1, and must *warn* whenever the types disagree.
PAIRS = [
    ("int", "1", "varchar", "'2'", True),
    ("text", "'1'", "int", "2", True),
    ("date", "'2024-01-01'", "timestamp", "'2024-01-02 00:00:00'", True),
    ("timestamp", "'2024-01-01 00:00:00'", "date", "'2024-01-02'", True),
    ("boolean", "true", "smallint", "0", True),
    ("smallint", "1", "boolean", "false", True),
    ("bit(1)", "B'1'", "boolean", "false", True),
    ("char(1)", "'Y'", "boolean", "false", True),
    ("numeric(10,2)", "1.25", "double", "1.26", False),
    ("double precision", "1.25", "decimal(10,2)", "1.26", False),
    ("varchar", "'{\"a\":1}'", "json", "'{\"a\":2}'", True),
    ("jsonb", "'{\"a\":1}'", "json", "'{\"a\":2}'", False),
    (
        "uuid",
        "'00000000-0000-0000-0000-000000000001'",
        "uuid",
        "'00000000-0000-0000-0000-000000000002'",
        False,
    ),
    ("interval", "'1 day'", "interval", "'2 days'", False),
    ("int[]", "'{1,2}'", "int[]", "[1,3]", False),
    ("inet", "'10.0.0.1'", "varchar", "'10.0.0.2'", True),
    ("time", "'10:00:00.000001'", "time", "'10:00:00.000002'", False),
]


@pytest.mark.parametrize(("pg_t", "va", "dk_t", "vb", "warn"), PAIRS)
def test_type_pairs_never_hide_a_real_difference(pg, tmp_path, pg_t, va, dk_t, vb, warn):
    """Every same-named, differently-typed pair reports its planted difference."""
    pg.execute(f"create table {PG_SCHEMA}.tp(id int, v {pg_t})")
    pg.execute(f"insert into {PG_SCHEMA}.tp values (1, {va})")
    b = _duck(
        tmp_path,
        "b",
        f"create table tp(id int, v {dk_t})",
        f"insert into tp values (1, {vb})",
    )
    result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.tp", "tp", "id")
    assert _found(result) == [(1, "different", ["v"])]
    mismatch = any("on side A but" in w for w in result.warnings)
    assert mismatch is warn, result.warnings


# ===================================================== --columns / --exclude


def test_excluding_the_key_still_matches_rows_by_it(tmp_path):
    """--exclude on the key is a no-op for matching, not a way to lose rows."""
    a = _duck(
        tmp_path,
        "a",
        "create table t(id int, v int)",
        "insert into t values (1, 1), (2, 2)",
    )
    b = _duck(
        tmp_path,
        "b",
        "create table t(ID int, V int)",
        "insert into t values (1, 1), (3, 2)",
    )
    result = _run(f"duckdb:///{a}", f"duckdb:///{b}", "t", "t", "id", exclude=["ID"])
    assert _found(result) == [(2, "only_in_a", ["v"]), (3, "only_in_b", ["v"])]


def test_columns_in_another_case_than_either_side_selects_the_column(tmp_path):
    """--columns AmOuNt against `amount` / `AMOUNT` compares that column."""
    a = _duck(
        tmp_path,
        "a",
        "create table t(id int, amount int, x int)",
        "insert into t values (1, 1, 1)",
    )
    b = _duck(
        tmp_path,
        "b",
        'create table t(id int, "AMOUNT" int, x int)',
        "insert into t values (1, 2, 9)",
    )
    result = _run(f"duckdb:///{a}", f"duckdb:///{b}", "t", "t", "id", columns=["AmOuNt"])
    assert _found(result) == [(1, "different", ["amount"])]


def test_a_composite_key_naming_one_column_twice_in_two_cases_still_finds_rows(
    tmp_path,
):
    """`--key id,ID` resolves both tokens to one column; nothing is lost."""
    a = _duck(
        tmp_path,
        "a",
        "create table t(id int, v int)",
        "insert into t values (1, 1), (2, 2)",
    )
    b = _duck(
        tmp_path,
        "b",
        "create table t(id int, v int)",
        "insert into t values (1, 1), (2, 3)",
    )
    result = _run(f"duckdb:///{a}", f"duckdb:///{b}", "t", "t", ["id", "ID"])
    assert [(d.kind, d.columns) for d in result.diffs] == [("different", ["v"])]


# ================================================================== BigQuery


def test_bigquery_composite_types_render_losslessly(bigquery_url):
    """ARRAY, STRUCT and GEOGRAPHY go through format('%t'), which keeps 17 digits.

    Row 2's point is far from row 1's on purpose: BigQuery snaps nearby
    vertices *within one CTAS*, so `st_geogpoint(1, 2)` next to
    `st_geogpoint(1.00000000000001, 2)` is stored as the latter - a real
    stored difference, not a rendering one.
    """
    from google.cloud import bigquery

    from parity.dialects.bigquery_dialect import parse_bigquery_url

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    ds = f"`{project}`.`{dataset}`"
    run = os.urandom(4).hex()
    ta, tb = f"rt_sch_cx_a_{run}", f"rt_sch_cx_b_{run}"
    try:
        client.query_and_wait(
            f"create table {ds}.{ta} as select 1 as id, [1.0000000000000002] as arr, "
            "st_geogpoint(1.00000000000001, 2) as g, struct(1.0 as f) as s "
            "union all select 2, [1.0], st_geogpoint(30, 40), struct(2.0000000000000004)"
        )
        client.query_and_wait(
            f"create table {ds}.{tb} as select 1 as id, [1.0] as arr, "
            "st_geogpoint(1.00000000000002, 2) as g, struct(1.0 as f) as s "
            "union all select 2, [1.0], st_geogpoint(30, 40), struct(2.0)"
        )
        result = _run(bigquery_url, bigquery_url, ta, tb, "id")
        assert _found(result) == [(1, "different", ["arr", "g"]), (2, "different", ["s"])]
        assert any("unmapped" in w for w in result.warnings)
    finally:
        for t in (ta, tb):
            client.delete_table(f"{project}.{dataset}.{t}", not_found_ok=True)
        client.close()


# =================================================================== RT-C9
# PostgreSQL date/timestamp/timestamptz accept 'infinity' and '-infinity', and
# `to_char` returns SQL NULL for both. The coalesce turns that into the NULL
# sentinel, so +infinity, -infinity and a real NULL all render `\N` and compare
# equal to one another - and to a NULL on DuckDB, whose strftime would have
# printed 'infinity'. 'infinity' is the conventional "valid until forever" end
# date in slowly-changing-dimension tables, so this is ordinary data.

#: (other side's engine, other side's value). The PostgreSQL side holds +infinity.
INF_OTHER = [("pg", "null"), ("pg", "'-infinity'"), ("duck", "null")]


@pytest.mark.parametrize("pg_t", ["date", "timestamp", "timestamptz"])
@pytest.mark.parametrize(
    ("engine", "other"), INF_OTHER, ids=["pg-null", "pg-neginf", "duck-null"]
)
def test_postgres_infinite_dates_are_not_equal_to_null(pg, tmp_path, pg_t, engine, other):
    """'infinity' on side A must not compare equal to NULL or -infinity on B."""
    pg.execute(f"create table {PG_SCHEMA}.inf_a(id int, v {pg_t})")
    pg.execute(f"insert into {PG_SCHEMA}.inf_a values (1, 'infinity'), (2, '2024-01-01')")
    # The stored value really is infinite, not NULL.
    row = pg.execute(f"select isfinite(v) from {PG_SCHEMA}.inf_a where id = 1")
    assert row.fetchone() == (False,)
    if engine == "pg":
        pg.execute(f"create table {PG_SCHEMA}.inf_b(id int, v {pg_t})")
        pg.execute(
            f"insert into {PG_SCHEMA}.inf_b values (1, {other}), (2, '2024-01-01')"
        )
        b_url, b_table = PG_URL, f"{PG_SCHEMA}.inf_b"
    else:
        path = _duck(
            tmp_path,
            "b",
            f"create table inf_b(id int, v {pg_t})",
            f"insert into inf_b values (1, {other}), (2, '2024-01-01')",
        )
        b_url, b_table = f"duckdb:///{path}", "inf_b"
    result = _run(PG_URL, b_url, f"{PG_SCHEMA}.inf_a", b_table, "id")
    assert _found(result) == [(1, "different", ["v"])]


# ================================================================== RT-C10
# PostgreSQL `to_char(..., 'YYYY-MM-DD')` prints the year without its era, so
# 15 March 44 BC and 15 March AD 44 both render `0044-03-15`. A BC value on
# PostgreSQL against the AD value on PostgreSQL or DuckDB is a false match.
# (DuckDB prints the BC date as `-43-03-15`, so BC on both sides is a false
# alarm instead.)


@pytest.mark.parametrize(
    ("pg_t", "bc", "ad"),
    [
        ("date", "'0044-03-15 BC'", "'0044-03-15'"),
        ("timestamp", "'0044-03-15 10:00:00 BC'", "'0044-03-15 10:00:00'"),
    ],
    ids=["date", "timestamp"],
)
@pytest.mark.parametrize("engine", ["pg", "duck"])
def test_postgres_bc_dates_do_not_match_ad_dates(pg, tmp_path, pg_t, bc, ad, engine):
    """44 BC on side A and AD 44 on side B are 87 years apart."""
    pg.execute(f"create table {PG_SCHEMA}.era_a(id int, v {pg_t})")
    pg.execute(f"insert into {PG_SCHEMA}.era_a values (1, {bc})")
    if engine == "pg":
        pg.execute(f"create table {PG_SCHEMA}.era_b(id int, v {pg_t})")
        pg.execute(f"insert into {PG_SCHEMA}.era_b values (1, {ad})")
        b_url, b_table = PG_URL, f"{PG_SCHEMA}.era_b"
    else:
        path = _duck(
            tmp_path,
            "b",
            f"create table era_b(id int, v {pg_t})",
            f"insert into era_b values (1, {ad})",
        )
        b_url, b_table = f"duckdb:///{path}", "era_b"
    result = _run(PG_URL, b_url, f"{PG_SCHEMA}.era_a", b_table, "id")
    assert _found(result) == [(1, "different", ["v"])]


# ================================================================== RT-C11
# Inferred from the Snowflake documentation and the generated SQL, not run live
# (no account): a TIMESTAMP_TZ value carries its own offset, and TO_CHAR prints
# its wall-clock time *in that offset* - the session TIMEZONE the dialect pins
# to UTC governs TIMESTAMP_LTZ only. The dialect renders every timestamp with
# one offset-free format, so `2024-01-01 12:00 +00:00` and `2024-01-01 12:00
# +05:00` - instants five hours apart - would both render
# `2024-01-01 12:00:00.000000` (false match), and a TIMESTAMP_TZ stored at
# +02:00 would differ from the same instant read in UTC from a PostgreSQL
# timestamptz (false alarm). Any correct rendering has to treat TIMESTAMP_TZ
# differently from TIMESTAMP_NTZ (e.g. convert_timezone('UTC', c) first).


def test_snowflake_timestamp_tz_is_rendered_as_an_instant():
    """TIMESTAMP_TZ needs a UTC conversion that TIMESTAMP_NTZ must not get."""
    from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake

    d = SnowflakeDialect(side="A")
    tz = Column("TS", map_type_snowflake("TIMESTAMP_TZ", None), "TIMESTAMP_TZ")
    ntz = Column("TS", map_type_snowflake("TIMESTAMP_NTZ", None), "TIMESTAMP_NTZ")
    assert d.normalize(tz) != d.normalize(ntz), d.normalize(tz)


# ================================================================== RT-C12
# MySQL TIMESTAMP is an instant - stored as UTC, converted through the session
# time zone the dialect pins to +00:00 - but it is classified timezone-naive
# (no "with time zone" in its name, and the dialect sets no `tz_aware`).
# Against a PostgreSQL timestamptz holding the same instants the verdict is
# right, yet the output says a migration that stored local wall-clock time
# "will show every row as different" - a false alarm in the warnings. MySQL
# DATETIME, which *is* naive, warns rightly (control test below).


def test_mysql_timestamp_against_postgres_timestamptz_raises_no_tz_warning(
    my, mysql_url, pg
):
    """Same instants on both sides, one planted difference: found, no tz warning."""
    my.execute("set session time_zone = '+00:00'")
    my.execute("create table rt_sch_tsz(id int primary key, ts timestamp(6))")
    my.execute(
        "insert into rt_sch_tsz values (1, '2024-06-15 12:00:00.123456'), "
        "(2, '2024-01-15 12:00:00')"
    )
    pg.execute(f"create table {PG_SCHEMA}.tsz(id int, ts timestamptz)")
    pg.execute(
        f"insert into {PG_SCHEMA}.tsz values (1, '2024-06-15 12:00:00.123456+00'), "
        "(2, '2024-01-15 12:00:01+00')"
    )
    result = _run(mysql_url, PG_URL, "rt_sch_tsz", f"{PG_SCHEMA}.tsz", "id")
    assert _found(result) == [(2, "different", ["ts"])]
    assert not [w for w in result.warnings if "timezone-aware" in w], result.warnings


def test_mysql_datetime_against_postgres_timestamptz_warns(my, mysql_url, pg):
    """The control for RT-C12: DATETIME really is naive, and the warning fires."""
    my.execute("create table rt_sch_dtz(id int primary key, ts datetime(6))")
    my.execute("insert into rt_sch_dtz values (1, '2024-06-15 12:00:00')")
    pg.execute(f"create table {PG_SCHEMA}.dtz(id int, ts timestamptz)")
    pg.execute(f"insert into {PG_SCHEMA}.dtz values (1, '2024-06-15 12:00:01+00')")
    result = _run(mysql_url, PG_URL, "rt_sch_dtz", f"{PG_SCHEMA}.dtz", "id")
    assert _found(result) == [(1, "different", ["ts"])]
    assert any("timezone-aware on side B" in w for w in result.warnings)


# ================================================================== RT-C13
# An enum is a common way a text column changes type in a migration
# (PostgreSQL CREATE TYPE ... AS ENUM, DuckDB ENUM). Both render their labels,
# byte-identical to the text on the other side, and the verdict is right - but
# the warnings say the values "will very likely all differ", because an enum
# maps to UNKNOWN. Cosmetic: a misleading warning, not a wrong verdict.


@pytest.mark.parametrize("enum_side", ["duck", "pg"])
def test_enum_against_text_does_not_predict_every_row_differs(pg, tmp_path, enum_side):
    """Labels equal on row 1, a planted difference on row 2."""
    if enum_side == "pg":
        pg.execute(f"create type {PG_SCHEMA}.mood as enum ('happy', 'sad')")
        pg.execute(f"create table {PG_SCHEMA}.en(id int, v {PG_SCHEMA}.mood)")
        dk = "create table en(id int, v varchar)"
    else:
        pg.execute(f"create table {PG_SCHEMA}.en(id int, v text)")
        dk = "create table en(id int, v enum('happy', 'sad'))"
    pg.execute(f"insert into {PG_SCHEMA}.en values (1, 'happy'), (2, 'happy')")
    b = _duck(tmp_path, "b", dk, "insert into en values (1, 'happy'), (2, 'sad')")
    result = _run(PG_URL, f"duckdb:///{b}", f"{PG_SCHEMA}.en", "en", "id")
    assert _found(result) == [(2, "different", ["v"])]
    assert not [w for w in result.warnings if "very likely all differ" in w], (
        result.warnings
    )


# ===================================================== generated columns, held


def test_duckdb_virtual_generated_column_with_a_changed_expression_is_found(tmp_path):
    """A VIRTUAL column is listed in information_schema and selected by name.

    The data columns are identical; only the generating expression changed in
    the migration - so the generated column is the only place it shows.
    """
    paths = []
    for name, mult in (("a", 2), ("b", 3)):
        paths.append(
            _duck(
                tmp_path,
                name,
                f"create table g(id int, x int, "
                f"t int generated always as (x * {mult}) virtual)",
                "insert into g(id, x) values (1, 0), (2, 5)",
            )
        )
    result = _run(f"duckdb:///{paths[0]}", f"duckdb:///{paths[1]}", "g", "g", "id")
    assert _found(result) == [(2, "different", ["t"])]


def test_postgres_stored_generated_column_with_a_changed_expression_is_found(pg):
    """PostgreSQL STORED generated columns are ordinary columns to a reader."""
    for t, mult in (("ga", 2), ("gb", 3)):
        pg.execute(
            f"create table {PG_SCHEMA}.{t}(id int, x int, "
            f"t int generated always as (x * {mult}) stored)"
        )
        pg.execute(f"insert into {PG_SCHEMA}.{t}(id, x) values (1, 0), (2, 5)")
    result = _run(PG_URL, PG_URL, f"{PG_SCHEMA}.ga", f"{PG_SCHEMA}.gb", "id")
    assert _found(result) == [(2, "different", ["t"])]


# ================================================================== RT-C14
# DuckDB UNION and VARIANT columns are unmapped and render through
# `cast(v as varchar)`, which prints the member value and drops its tag: an
# INTEGER 1 and a VARCHAR '1' both render `1`. A migration that turned a numeric
# member into a string member - the kind of change a type-unsafe pipeline makes
# - compares identical. The only warning is the generic "unmapped types" line,
# which talks about cross-engine false alarms, not missed differences.


@pytest.mark.parametrize(
    ("ddl", "a_val", "b_val"),
    [
        ("union(i int, s varchar)", "union_value(i := 1)", "union_value(s := '1')"),
        ("variant", "1::variant", "'1'::variant"),
    ],
    ids=["union", "variant"],
)
def test_duckdb_tagged_values_of_different_member_types_differ(
    tmp_path, ddl, a_val, b_val
):
    """Same text, different member type: a real difference in the stored value."""
    import duckdb

    paths, tags = [], []
    for n, v in (("a", a_val), ("b", b_val)):
        # VARIANT needs storage version 1.5, which a plain connect does not pick.
        path = str(tmp_path / f"{n}.duckdb")
        con = duckdb.connect(":memory:")
        con.execute(f"attach '{path}' as f (storage_version 'v1.5.0')")
        con.execute(f"create table f.t(id int, v {ddl})")
        con.execute(f"insert into f.t values (1, {v})")
        # The stored values really differ - in their tag.
        tag = "union_tag(v)" if ddl.startswith("union") else "variant_typeof(v)"
        tags.append(con.execute(f"select {tag} from f.t").fetchone())
        con.close()
        paths.append(path)
    assert tags[0] != tags[1], tags
    result = _run(f"duckdb:///{paths[0]}", f"duckdb:///{paths[1]}", "t", "t", "id")
    assert _found(result) == [(1, "different", ["v"])]


# ================================================================== RT-C15
# Inferred from the Snowflake documentation and the generated SQL, not run live
# (no account). VARIANT / OBJECT / ARRAY are unmapped and render through
# `cast(c as varchar)`. For a VARIANT holding a string that cast returns the
# string *without* its JSON quotes, so the VARIANT number 1 and the VARIANT
# string "1" both render `1` - the RT-C14 false match, on the type a Snowflake
# migration lands semi-structured data in. A rendering that keeps the type
# (to_json, say) must not be the plain varchar cast.


def test_snowflake_variant_rendering_keeps_the_json_type():
    """The VARIANT expression must not be the bare varchar cast."""
    from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake

    d = SnowflakeDialect(side="A")
    col = Column("V", map_type_snowflake("VARIANT", None), "VARIANT")
    # The bare cast, as escaped like all text since RT-A1/A2 - escaping alone
    # does not restore the lost JSON type.
    bare = d.escape_text('cast("V" as varchar)')
    assert d.normalize(col) != f"coalesce({bare}, {d.null_sentinel_sql()})"
