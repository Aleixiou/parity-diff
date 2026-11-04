"""TIME, BINARY and JSON: the rules, and planted differences found exactly.

These three were the commonest types without a rule: they fell to UNKNOWN and
compared through each engine's text cast, unchecked by the sample, usually
differently on two engines, and on Snowflake a TIME lost its fraction (COV-6,
a false match). Each now has an executable spec in `dialects/base.py`
(`canonical_time_text`, `canonical_binary_text`, `canonical_json_text`), the
reference reads it (`parity.reference`), and `tests/conformance` holds every
engine's SQL to it value by value.

This file pins the specs offline and, live, plants one change of each kind
that a lossy rendering would hide - a microsecond, a trailing zero byte, 1
against 1.0, SQL NULL against JSON null, NULL against an empty value - and
requires the walk to find exactly those rows and columns, between engines,
with the sample check covering the columns (CLAUDE.md section 8).
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from conftest import duckdb_write, mysql_rw_cursor, open_duckdb, open_mysql, open_pg

from parity.dialects.base import (
    canonical_binary_text,
    canonical_json_text,
    canonical_time_text,
    json_layout,
    map_type,
)
from parity.dialects.bigquery_dialect import BigQueryDialect, map_type_bigquery
from parity.dialects.mssql_dialect import map_type_mssql
from parity.dialects.mysql_dialect import map_type_mysql
from parity.dialects.snowflake_dialect import SnowflakeDialect, map_type_snowflake
from parity.engine import diff
from parity.reference import UNCHECKED, canonical_text, reference_token
from parity.types import Column, LogicalType

# ------------------------------------------------------------- the specs


@pytest.mark.parametrize(
    ("micros", "text"),
    [
        (0, "00:00:00.000000"),
        (45296_500000, "12:34:56.500000"),
        (45296_050000, "12:34:56.050000"),
        (86399_999999, "23:59:59.999999"),
        (86400_000000, "24:00:00.000000"),  # PostgreSQL/DuckDB/MySQL accept it
        (-500000, "-00:00:00.500000"),  # MySQL durations
        (838 * 3600_000000 + 3599_000000, "838:59:59.000000"),
    ],
)
def test_time_text(micros: int, text: str):
    """Six digits always; hours past 23 and a sign only for durations."""
    assert canonical_time_text(micros) == text


def test_binary_text_is_exact_bytes():
    """Lower-case hex behind `\\\\x`; padding and zero bytes are data."""
    assert canonical_binary_text(b"") == "\\\\x"
    assert canonical_binary_text(b"\x00\xff") == "\\\\x00ff"
    assert canonical_binary_text(b"\x00") != canonical_binary_text(b"\x00\x00")
    assert canonical_binary_text(b"abc") != canonical_binary_text(b"abc ")


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ('{"a":1,"b":[1,2]}', '{"a": 1, "b": [1, 2]}'),
        ('{ "a" :\n 1 }', '{"a": 1}'),
        ("[]", "[]"),
        ("{}", "{}"),
        ("[{}, []]", "[{}, []]"),
        ('"x, y: z"', '"x, y: z"'),  # separators inside a string stay as they are
        ('"\\u00e9\\/\\u007f"', '"é/\x7f"'),  # decoded, re-escaped minimally
        ('"\\t\\u001f\\u0001"', '"\\t\\u001f\\u0001"'),
        ('"\\ud83d\\ude00"', '"\U0001f600"'),
        ("1.50", "1.50"),  # numbers as the engine spelled them
        ("1e2", "1e2"),
        ('{"b": 1, "a": 2}', '{"b": 1, "a": 2}'),  # key order as the engine gave it
        ('{"a": 1, "a": 2}', '{"a": 1, "a": 2}'),
    ],
)
def test_json_text_from_a_normal_form(raw: str, text: str):
    """Layout and string escaping are canonical; everything else is the
    engine's normal form, kept."""
    assert canonical_json_text(raw) == text


def test_json_verbatim_keeps_strings_as_written():
    """DuckDB's JSON is text: only the layout is normalised."""
    assert canonical_json_text('{"a":"\\u00e9"}', verbatim=True) == '{"a": "\\u00e9"}'
    assert canonical_json_text('{"a":"\\u00e9"}') == '{"a": "é"}'
    assert json_layout('[1,{"k":"a, b"}]') == '[1, {"k": "a, b"}]'


def test_json_text_refuses_what_is_not_json():
    """Never guess at a document."""
    with pytest.raises(ValueError):
        canonical_json_text("{")
    with pytest.raises(ValueError):
        canonical_json_text("{'a': 1}", verbatim=True)


def test_json_null_is_not_sql_null():
    """The JSON literal `null` is a value; SQL NULL is the sentinel."""
    null_doc = canonical_text(reference_token(LogicalType.JSON, "null", 6))
    assert null_doc == "null"
    assert canonical_text(reference_token(LogicalType.JSON, None, 6)) == "\\N"


def test_reference_reads_every_driver_form():
    """time / timedelta / text for TIME; bytes for BINARY; text for JSON -
    and nothing it would have to guess at."""
    t = LogicalType.TIME
    assert reference_token(t, dt.time(12, 34, 56, 500000), 6) == (
        "time",
        "12:34:56.500000",
    )
    assert reference_token(t, dt.timedelta(days=1), 6) == ("time", "24:00:00.000000")
    assert reference_token(t, dt.timedelta(microseconds=-1), 6) == (
        "time",
        "-00:00:00.000001",
    )
    assert reference_token(t, "23:59:59.9999999", 6) == ("time", "23:59:59.999999")
    assert reference_token(t, "24:00:00", 6) == ("time", "24:00:00.000000")
    tz = dt.time(12, tzinfo=dt.timezone.utc)
    assert reference_token(t, tz, 6) is UNCHECKED
    b = LogicalType.BINARY
    assert reference_token(b, bytearray(b"\x00\x01"), 6) == ("bin", "\\\\x0001")
    assert reference_token(b, "\x00\x01", 6) is UNCHECKED
    j = LogicalType.JSON
    assert reference_token(j, {"a": 1}, 6) is UNCHECKED  # already parsed: lossy
    assert reference_token(j, "{", 6) is UNCHECKED
    # Escaped as text in the canonical text, as every dialect's SQL does.
    assert canonical_text(reference_token(j, '"a\\\\b"', 6)) == '"a\\\\\\\\b"'


@pytest.mark.parametrize(
    ("mapper", "raw", "expected"),
    [
        (map_type, "time without time zone", LogicalType.TIME),
        (map_type, "TIME", LogicalType.TIME),
        (map_type, "time with time zone", LogicalType.TIMETZ),  # its own rule
        (map_type, "TIME_NS", LogicalType.UNKNOWN),
        (map_type, "bytea", LogicalType.BINARY),
        (map_type, "BLOB", LogicalType.BINARY),
        (map_type, "json", LogicalType.JSON),
        (map_type, "jsonb", LogicalType.JSON),
        (map_type, "JSON", LogicalType.JSON),
        (map_type, "money", LogicalType.DECIMAL),
        (map_type, "ENUM('a', 'b')", LogicalType.STRING),
        (map_type, "bit", LogicalType.BITS),  # a bit *string* here
        (map_type_mysql, "time", LogicalType.TIME),
        (map_type_mysql, "varbinary", LogicalType.BINARY),
        (map_type_mysql, "longblob", LogicalType.BINARY),
        (map_type_mysql, "json", LogicalType.JSON),
        (map_type_mssql, "time", LogicalType.TIME),
        (map_type_mssql, "varbinary", LogicalType.BINARY),
        (map_type_mssql, "timestamp", LogicalType.BINARY),  # rowversion
        (map_type_mssql, "image", LogicalType.BINARY),
        (map_type_bigquery, "TIME", LogicalType.TIME),
        (map_type_bigquery, "BYTES(16)", LogicalType.BINARY),
        (map_type_bigquery, "JSON", LogicalType.JSON),
        (map_type_bigquery, "ARRAY<INT64>", LogicalType.LIST),
    ],
)
def test_type_maps(mapper, raw: str, expected: LogicalType):
    """Every engine's spelling of the three types reaches its rule."""
    assert mapper(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TIME", LogicalType.TIME),
        ("BINARY", LogicalType.BINARY),
        ("VARIANT", LogicalType.JSON),
        ("OBJECT", LogicalType.JSON),
        ("ARRAY", LogicalType.JSON),
    ],
)
def test_snowflake_type_map(raw: str, expected: LogicalType):
    """Snowflake's semi-structured types are JSON."""
    assert map_type_snowflake(raw, None) is expected


def test_bigquery_json_keeps_sql_null_null():
    """TO_JSON_STRING(NULL) is the text 'null' - found by the conformance
    sweep - so both paths must keep a SQL NULL NULL, or it equals a JSON null.
    Pinned offline because a BigQuery load job cannot store a JSON null."""
    d = BigQueryDialect(side="A")
    col = Column("j", LogicalType.JSON, "JSON")
    assert "if(`j` is null, null, to_json_string(`j`))" in d.normalize(col)
    assert d.sample_select(col) == "if(`j` is null, null, to_json_string(`j`))"


def test_snowflake_json_keeps_non_json_types_tagged():
    """A DATE in a VARIANT prints as a JSON string; its TYPEOF keeps it
    apart from the VARCHAR spelling it, and the sample leaves it unchecked."""
    d = SnowflakeDialect(side="A")
    col = Column("J", LogicalType.JSON, "VARIANT")
    assert "typeof(\"J\") || ':'" in d.normalize(col)
    assert d.sample_value(col, 'OBJECT:{"a":1}') == '{"a":1}'
    tagged = d.sample_value(col, 'DATE:"2024-01-01"')
    assert reference_token(LogicalType.JSON, tagged, 6) is UNCHECKED


# ----------------------------------------------------- planted differences

N = 300

#: Keys whose value on side B differs from side A, and in which column.
PLANTED = {
    10: "t",  # one microsecond later
    20: "b",  # a trailing zero byte
    30: "j",  # 1 -> 1.0
    40: "j",  # SQL NULL -> JSON null
    50: "b",  # NULL -> empty bytes
    60: "t",  # NULL -> midnight
    70: "j",  # "\u001f" -> "\\s" (the separator's escape, as text)
    80: "b",  # 0xc3a9 (valid UTF-8) -> 0xc3a8
}


def _rows(side: str) -> list[tuple]:
    """(id, time, bytes, json text) - identical on both sides but for PLANTED.

    The JSON keys are in an order every engine's normal form agrees on (a,
    id, tag: shortest first *and* lowest first), so a correct migration
    between any two engines compares clean.
    """
    rows = []
    for i in range(1, N + 1):
        t: dt.time | None = dt.time(i % 24, i % 60, (i * 7) % 60, (i * 137) % 1_000_000)
        b: bytes | None = bytes([i % 256, (i * 7) % 256]) + (
            b"\x00" if i % 5 == 0 else b""
        )
        j: str | None = f'{{"a": {i % 7}, "id": "x{i}", "tag": ["p", "q", {i}]}}'
        if i % 17 == 0:
            t = b = j = None
        if i in (40, 50, 60):
            t = b = j = None
        if i == 70:
            j = '{"a": "\\u001f"}'
        if i == 80:
            b = b"\xc3\xa9"
        if side == "B":
            if i == 10:
                t = dt.time(t.hour, t.minute, t.second, t.microsecond + 1)
            if i == 20:
                b = b + b"\x00"
            if i == 30:
                j = j.replace('"a": 2', '"a": 2.0')
            if i == 40:
                j = "null"
            if i == 50:
                b = b""
            if i == 60:
                t = dt.time(0, 0)
            if i == 70:
                j = '{"a": "\\\\s"}'
            if i == 80:
                b = b"\xc3\xa8"
        rows.append((i, t, b, j))
    assert '"a": 2' in _plain_json(30), "key 30 must carry the planted 2"
    return rows


def _plain_json(i: int) -> str:
    """Side A's JSON for key `i`."""
    return f'{{"a": {i % 7}, "id": "x{i}", "tag": ["p", "q", {i}]}}'


def _check(result, columns: set[str]) -> None:
    """Exactly the planted rows, in exactly their columns, all checked."""
    expected = {k: c for k, c in PLANTED.items() if c in columns}
    found = {d.key: d.columns for d in result.diffs}
    assert {d.kind for d in result.diffs} <= {"different"}, result.diffs
    assert found == {k: [c] for k, c in expected.items()}, found
    v = result.verification
    assert v is not None and not v.failures and not v.error
    assert set(v.checked) >= columns and not (set(v.unchecked) & columns)


@pytest.fixture(scope="module")
def duck(tmp_path_factory):
    """Side B on DuckDB: TIME, BLOB, JSON - planted."""
    pytest.importorskip("duckdb")
    path = str(tmp_path_factory.mktemp("typ") / "typ.duckdb")
    con = duckdb_write(path)
    for side in ("A", "B"):
        con.execute(f"create table typ_{side} (id bigint, t time, b blob, j json)")
        con.executemany(f"insert into typ_{side} values (?, ?, ?, ?)", _rows(side))
    con.close()
    d = open_duckdb(path, side="B")
    d.path = path  # type: ignore[attr-defined]
    yield d
    d.close()


def test_duckdb_to_duckdb(duck):
    """Same engine: the planted rows, nothing else. Its own connection for
    side A: the two sides are queried from two threads at once."""
    a = open_duckdb(duck.path, side="A")
    try:
        _check(diff(a, duck, "typ_A", "typ_B", "id"), {"t", "b", "j"})
    finally:
        a.close()


@pytest.fixture(scope="module")
def pg(pg_url):
    """Side A on PostgreSQL: time, bytea, jsonb and json, in `parity_types`."""
    import psycopg
    from psycopg.types.json import Json, Jsonb

    con = psycopg.connect(pg_url, autocommit=True)
    try:
        con.execute("create schema if not exists parity_types")
        con.execute("drop table if exists parity_types.typ_rules")
        con.execute(
            "create table parity_types.typ_rules "
            "(id bigint, t time, b bytea, j jsonb, jt json)"
        )
        with con.cursor() as cur:
            cur.executemany(
                "insert into parity_types.typ_rules values (%s, %s, %s, %s, %s)",
                [
                    (
                        i,
                        t,
                        b,
                        None if j is None else Jsonb(j, dumps=lambda x: x),
                        # The json copy is spelled compact with its keys
                        # reversed: jsonb normalises both away.
                        None
                        if j is None
                        else Json(
                            j.replace('{"a": ', '{"tag":0,"a":').replace(", ", ","),
                            dumps=lambda x: x,
                        ),
                    )
                    for i, t, b, j in _rows("A")
                ],
            )
    finally:
        con.close()
    d = open_pg(pg_url, side="A")
    yield d
    d.close()
    con = psycopg.connect(pg_url, autocommit=True)
    con.execute("drop table if exists parity_types.typ_rules")
    con.close()


@pytest.mark.postgres
def test_postgres_to_duckdb(pg, duck):
    """Across engines: TIME, BINARY and JSON all compare exactly."""
    result = diff(pg, duck, "parity_types.typ_rules", "typ_B", "id", exclude=["jt"])
    _check(result, {"t", "b", "j"})
    # Their JSON normal forms differ (DuckDB keeps text as written), and the
    # run says so even though these documents happen to agree.
    assert any("JSON columns ['j']" in w for w in result.warnings)


@pytest.mark.postgres
def test_postgres_json_is_jsonb_equality(pg):
    """A json column equals the jsonb it normalises to: spacing and key order
    are not data. (Key `tag` repeated: jsonb keeps the last, which is the
    original's.)"""
    j = Column("j", LogicalType.JSON, "jsonb")
    jt = Column("jt", LogicalType.JSON, "json")
    differ = pg.query(
        f"select id from parity_types.typ_rules where "
        f"{pg.normalize(j)} <> {pg.normalize(jt)}"
    )
    # Every jt differs from j by its leading duplicate key only, which jsonb
    # drops in favour of the last - so every row renders identically, except
    # key 70, whose document has no later `tag` to supersede the extra one:
    # there the duplicate is the only `tag`, a real difference, and found.
    assert differ == [(70,)]


@pytest.fixture(scope="module")
def my(mysql_url):
    """Side A on MySQL: time(6), varbinary, json."""
    d = open_mysql(mysql_url, side="A")
    cur = mysql_rw_cursor(d)
    try:
        cur.execute("drop table if exists typ_rules")
        cur.execute(
            "create table typ_rules (id bigint, t time(6), b varbinary(64), j json)"
        )
        cur.executemany("insert into typ_rules values (%s, %s, %s, %s)", _rows("A"))
        d._conn.commit()
    finally:
        cur.close()
    d.close()
    d = open_mysql(mysql_url, side="A")
    yield d
    d.close()
    w = open_mysql(mysql_url, side="W")
    cur = mysql_rw_cursor(w)
    cur.execute("drop table if exists typ_rules")
    w._conn.commit()
    cur.close()
    w.close()


@pytest.mark.mysql
def test_mysql_to_duckdb(my, duck):
    """MySQL TIME (a duration type) and JSON against DuckDB."""
    result = diff(my, duck, "typ_rules", "typ_B", "id")
    _check(result, {"t", "b", "j"})


@pytest.mark.mysql
@pytest.mark.postgres
def test_mysql_to_postgres(my, pg_url):
    """The two engines with the same JSON key order: the planted rows only."""
    import psycopg
    from psycopg.types.json import Jsonb

    # Side B on PostgreSQL holds the planted rows, in its own table.
    url = pg_url
    con = psycopg.connect(url, autocommit=True)
    try:
        con.execute("create schema if not exists parity_types")
        con.execute("drop table if exists parity_types.typ_rules_b")
        con.execute(
            "create table parity_types.typ_rules_b (id bigint, t time, b bytea, j jsonb)"
        )
        with con.cursor() as cur:
            cur.executemany(
                "insert into parity_types.typ_rules_b values (%s, %s, %s, %s)",
                [
                    (i, t, b, None if j is None else Jsonb(j, dumps=lambda x: x))
                    for i, t, b, j in _rows("B")
                ],
            )
        b_side = open_pg(url, side="B")
        try:
            result = diff(my, b_side, "typ_rules", "parity_types.typ_rules_b", "id")
        finally:
            b_side.close()
    finally:
        con.execute("drop table if exists parity_types.typ_rules_b")
        con.close()
    _check(result, {"t", "b", "j"})


@pytest.fixture(scope="module")
def ms(mssql_url):
    """Side A on SQL Server: time(7) and varbinary, in database `typ_rules_db`."""
    from urllib.parse import urlparse, urlunparse

    import mssql_python

    from parity.dialects.base import get_dialect
    from parity.dialects.mssql_dialect import parse_mssql_url

    odbc, _ = parse_mssql_url(mssql_url)
    master = mssql_python.connect(odbc, autocommit=True)
    cur = master.cursor()
    cur.execute("if db_id('typ_rules_db') is null create database typ_rules_db")
    cur.execute("alter database typ_rules_db set allow_snapshot_isolation on")
    cur.close()
    url = urlunparse(urlparse(mssql_url)._replace(path="/typ_rules_db"))
    con = mssql_python.connect(parse_mssql_url(url)[0], autocommit=True)
    cur = con.cursor()
    cur.execute("drop table if exists dbo.typ_rules")
    cur.execute("create table dbo.typ_rules (id bigint, t time(7), b varbinary(64))")
    for i, t, b, _ in _rows("A"):
        cur.execute("insert into dbo.typ_rules values (?, ?, ?)", [i, t, b])
    cur.close()
    con.close()
    d = get_dialect(url, side="A")
    yield d
    d.close()
    cur = master.cursor()
    cur.execute(
        "alter database typ_rules_db set single_user with rollback immediate; "
        "drop database typ_rules_db"
    )
    cur.close()
    master.close()


@pytest.mark.snowflake
def test_snowflake_to_duckdb(snowflake_url, duck):
    """Snowflake TIME(9), BINARY and VARIANT against DuckDB (billed: one small
    table, dropped). VARIANT is loaded as text and parsed, since a bound
    string would be stored as a VARCHAR variant."""
    from parity.dialects.base import get_dialect

    table = f"TYP_RULES_{os.getpid()}"
    w = get_dialect(snowflake_url, side="W")
    cur = w._conn.cursor()
    try:
        cur.execute(
            f'create or replace table "{table}" '
            '("id" number(38,0), "t" time(9), "b" binary, "j" varchar)'
        )
        rows = [
            [i, None if t is None else ("TEXT", t.isoformat()), b, j]
            for i, t, b, j in _rows("A")
        ]
        # Connector 4.x (the supported line) ignores `_force_qmark_paramstyle`
        # in executemany and formats with `%`: switch the connection itself.
        w._conn._paramstyle = "qmark"
        cur.executemany(
            f'insert into "{table}" values (?, ?, ?, ?)',
            rows,
            _force_qmark_paramstyle=True,
        )
        cur.execute(
            f'create or replace table "{table}" as select "id", "t", "b", '
            f'parse_json("j") as "j" from "{table}"'
        )
        a = get_dialect(snowflake_url, side="A")
        try:
            result = diff(a, duck, table, "typ_B", "id")
        finally:
            a.close()
    finally:
        cur.execute(f'drop table if exists "{table}"')
        cur.close()
        w.close()
    _check(result, {"t", "b", "j"})
    assert any("['t'] can hold nanoseconds" in w for w in result.warnings)


@pytest.mark.snowflake
def test_snowflake_time_nanoseconds_are_compared_between_two_snowflakes(snowflake_url):
    """Both sides hold TIME(9), so digits past the microsecond are compared
    (COV-2, as for timestamps) - as a value and as a key - and no caveat."""
    from parity.dialects.base import get_dialect

    tables = [f"TYP_TNS_{s}_{os.getpid()}" for s in "AB"]
    vals = {"A": "12:00:00.123456789", "B": "12:00:00.123456780"}
    w = get_dialect(snowflake_url, side="W")
    cur = w._conn.cursor()
    try:
        for side, table in zip("AB", tables, strict=True):
            cur.execute(f'create or replace table "{table}" ("k" time(9), "t" time(9))')
            cur.execute(
                f'insert into "{table}" values (?, ?), (?, ?)',
                [
                    ("TEXT", "01:00:00.000000001"),
                    ("TEXT", vals[side]),
                    ("TEXT", "01:00:00.000000002"),
                    ("TEXT", "12:00:00"),
                ],
                _force_qmark_paramstyle=True,
            )
        a = get_dialect(snowflake_url, side="A")
        b = get_dialect(snowflake_url, side="B")
        try:
            result = diff(a, b, tables[0], tables[1], "k")
        finally:
            a.close()
            b.close()
    finally:
        for table in tables:
            cur.execute(f'drop table if exists "{table}"')
        cur.close()
        w.close()
    # Two distinct nanosecond keys, not merged; one value differing at 1 ns.
    assert [(d.key, d.kind, d.columns) for d in result.diffs] == [
        ("01:00:00.000000001", "different", ["t"])
    ]
    # The value column is compared in full, so no caveat names it. (The key
    # column is still listed - keys never get `sub_micro`, though
    # `normalize_key` keeps their digits; as for timestamp keys.)
    assert not any("nanoseconds" in w and "'t'" in w for w in result.warnings)
    v = result.verification
    assert v is not None and not v.failures and "t" in v.checked


@pytest.mark.bigquery
def test_bigquery_to_duckdb(bigquery_url, duck):
    """BigQuery TIME, BYTES and JSON against DuckDB, loaded by a (free) load
    job into a table of our own, deleted afterwards."""
    import base64
    import json

    from google.cloud import bigquery

    from parity.dialects.base import get_dialect
    from parity.dialects.bigquery_dialect import parse_bigquery_url

    project, dataset, location = parse_bigquery_url(bigquery_url)
    client = bigquery.Client(project=project, location=location)
    ref = f"{project}.{dataset}.typ_rules_{os.getpid()}"
    data = [
        {
            "id": i,
            **({} if t is None else {"t": t.isoformat()}),
            **({} if b is None else {"b": base64.b64encode(b).decode()}),
            **({} if j is None else {"j": json.loads(j)}),
        }
        for i, t, b, j in _rows("A")
    ]
    schema = [
        bigquery.SchemaField("id", "INT64"),
        bigquery.SchemaField("t", "TIME"),
        bigquery.SchemaField("b", "BYTES"),
        bigquery.SchemaField("j", "JSON"),
    ]
    try:
        client.load_table_from_json(
            data, ref, job_config=bigquery.LoadJobConfig(schema=schema)
        ).result()
        a = get_dialect(bigquery_url, side="A")
        try:
            result = diff(a, duck, ref.split(".", 1)[1], "typ_B", "id")
        finally:
            a.close()
    finally:
        client.delete_table(ref, not_found_ok=True)
        client.close()
    _check(result, {"t", "b", "j"})


@pytest.mark.mssql
def test_mssql_to_duckdb(ms, duck):
    """SQL Server time(7) and varbinary against DuckDB; its time(7) caveat."""
    result = diff(ms, duck, "dbo.typ_rules", "typ_B", "id", exclude=["j"])
    _check(result, {"t", "b"})
    assert any("['t'] hold 100-nanosecond ticks" in w for w in result.warnings)
