"""Per-engine native types and table builders for the conformance suite.

For each engine: the native column types it maps onto a logical type (and a
selection of those it maps to UNKNOWN), which value group each one is loaded
with, which values of that group the type can hold exactly, and how each
value is *bound* by the driver - never written into SQL as a literal, since
SQL Server's string-to-float parse is not correctly rounded and literal
spelling is exactly the kind of thing under test.

Every object created is ours alone: PostgreSQL schema `parity_cov`, MySQL
tables `cov_*` in the test database, SQL Server database `cov_db`, BigQuery
and Snowflake tables `cov_*` / `COV_*`, DuckDB files in a temp directory -
the prefix and schema overridable (`PARITY_COV_PREFIX`,
`PARITY_COV_PG_SCHEMA`) so two sweeps on one server never meet. The
builders drop everything they made in `drop_all`.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import uuid as uuidlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal
from typing import Any
from urllib.parse import urlparse, urlunparse

from cov_values import Val

from parity.dialects.base import Dialect, get_dialect

#: Per-run suffix for cloud tables, so two runs never collide.
RUN = os.environ.get("PARITY_COV_RUN", f"{os.getpid()}")

#: Prefix of every table, type and database the sweep creates, and the
#: PostgreSQL schema it creates them in - overridable so two sweeps on one
#: server (say, a type study beside a red-team run) never touch each
#: other's objects.
PREFIX = os.environ.get("PARITY_COV_PREFIX", "cov")
PG_SCHEMA = os.environ.get("PARITY_COV_PG_SCHEMA", "parity_cov")


# ------------------------------------------------------------- value filters


def int_range(lo: int, hi: int) -> Callable[[Val], bool]:
    """Integers in [lo, hi] (and NULL)."""
    return lambda v: v.value is None or lo <= v.value <= hi


def dec_fits(precision: int, scale: int) -> Callable[[Val], bool]:
    """Decimals a DECIMAL(precision, scale) holds exactly."""

    def ok(v: Val) -> bool:
        """Exact at `scale` places, within `precision - scale` integer digits."""
        if v.value is None:
            return True
        d: Decimal = v.value
        q = d.quantize(Decimal(1).scaleb(-scale), context=Context(prec=200))
        # copy_abs, not abs(): abs() rounds to the context precision (28).
        return q == d and d.copy_abs() < Decimal(f"1e{precision - scale}")

    return ok


def no_nonfinite(v: Val) -> bool:
    """Engines whose floats cannot hold Infinity or NaN."""
    return not (isinstance(v.value, float) and not math.isfinite(v.value))


def no_nul(v: Val) -> bool:
    """Engines whose text cannot hold U+0000."""
    return not (isinstance(v.value, str) and "\x00" in v.value)


def max_len(n: int) -> Callable[[Val], bool]:
    """Strings of at most `n` code points."""
    return lambda v: v.value is None or len(v.value) <= n


def max_utf8(n: int) -> Callable[[Val], bool]:
    """Strings of at most `n` UTF-8 bytes."""
    return lambda v: v.value is None or len(v.value.encode()) <= n


def max_utf16(n: int) -> Callable[[Val], bool]:
    """Strings of at most `n` UTF-16 code units."""
    return lambda v: v.value is None or len(v.value.encode("utf-16-le")) // 2 <= n


def year_range(lo: int, hi: int) -> Callable[[Val], bool]:
    """Dates and timestamps (Python objects) whose year is in [lo, hi]."""

    def ok(v: Val) -> bool:
        """NULL, or a date/datetime in range; never an engine-text value."""
        if v.value is None:
            return True
        if not isinstance(v.value, (dt.date, dt.datetime)):
            return False
        x = v.value
        if isinstance(x, dt.datetime) and x.tzinfo is not None:
            x = x.astimezone(dt.timezone.utc)
        return lo <= x.year <= hi

    return ok


def utc_between(lo: dt.datetime, hi: dt.datetime) -> Callable[[Val], bool]:
    """Instants within [lo, hi] UTC (MySQL TIMESTAMP's 1970..2038)."""

    def ok(v: Val) -> bool:
        """NULL or an aware datetime in range."""
        if v.value is None:
            return True
        x = v.value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return lo <= x <= hi

    return ok


def micros_multiple(n: int) -> Callable[[Val], bool]:
    """Timestamps whose microseconds are a multiple of `n` (TIMESTAMP_MS: 1000)."""
    return lambda v: (
        v.value is None
        or (isinstance(v.value, dt.datetime) and v.value.microsecond % n == 0)
    )


def mssql_datetime_ok(v: Val) -> bool:
    """SQL Server `datetime`: 1753..9999, and milliseconds it displays as given.

    It ticks in 1/300 s and displays .000/.003/.007 endings; a value in whole
    milliseconds ending 0, 3 or 7 (and no sub-millisecond part) shows as typed.
    The last instant of 9999 rounds past the type, so it is left out.
    """
    if v.value is None:
        return True
    x = v.value
    if not isinstance(x, dt.datetime) or not (1753 <= x.year <= 9999):
        return False
    if x.microsecond % 1000 or (x.microsecond // 1000) % 10 not in (0, 3, 7):
        return False
    return x < dt.datetime(9999, 12, 31, 23, 59, 59, 997000)


def smalldatetime_ok(v: Val) -> bool:
    """SQL Server `smalldatetime`: 1900-01-01 .. 2079-06-06 23:59, whole minutes."""
    if v.value is None:
        return True
    x = v.value
    return (
        isinstance(x, dt.datetime)
        and dt.datetime(1900, 1, 1) <= x <= dt.datetime(2079, 6, 6, 23, 59)
        and x.second == 0
        and x.microsecond == 0
    )


def digits_at_most(n: int) -> Callable[[Val], bool]:
    """Sub-microsecond text values with at most `n` fractional digits."""
    return lambda v: v.value is None or len(str(v.value).rpartition(".")[2]) <= n


def whole_seconds(v: Val) -> bool:
    """Times with no fraction (a time(0) rounds one on insert)."""
    return v.value is None or (isinstance(v.value, dt.time) and v.value.microsecond == 0)


def max_bytes(n: int) -> Callable[[Val], bool]:
    """Byte strings of at most `n` bytes."""
    return lambda v: v.value is None or len(v.value) <= n


def width(n: int) -> Callable[[Val], bool]:
    """Bit strings of exactly `n` digits (a bit(n) pads or refuses others)."""
    return lambda v: v.value is None or len(v.value) == n


def nonempty(v: Val) -> bool:
    """Bit strings with at least one digit (DuckDB's BIT cannot be empty)."""
    return v.value is None or len(v.value) > 0


def is_network(v: Val) -> bool:
    """inet values that are networks, which is all a cidr can hold."""
    import ipaddress

    if v.value is None:
        return True
    try:
        ipaddress.ip_network(v.value, strict=True)
    except ValueError:
        return False
    return True


def pg_name_ok(v: Val) -> bool:
    """Text a PostgreSQL `name` holds whole: 63 bytes, no NUL."""
    return v.value is None or (len(v.value.encode()) <= 63 and "\x00" not in v.value)


def finite_interval(v: Val) -> bool:
    """Intervals other than PostgreSQL 17's infinities."""
    return not isinstance(v.value, str)


def interval_input(x: Any) -> Any:
    """An interval (months, days, microseconds) as text both engines parse
    into exactly those components; NULL and infinities as they are."""
    if not isinstance(x, tuple):
        return x
    m, d, us = x
    return f"{m} months {d} days {us} microseconds"


def pg_shaped_literal(value: Any) -> str:
    """A PostgreSQL array literal of a `ShapedList`: its dimensions, then its
    elements nested to them - `[0:1][1:2]={{1,2},{3,4}}`."""
    import re

    from parity.dialects.base import ShapedList

    if not isinstance(value, ShapedList):
        return pg_array_literal(value)
    lengths = [
        int(u) - int(lo) + 1 for lo, u in re.findall(r"\[(-?\d+):(-?\d+)\]", value.dims)
    ]
    flat = pg_array_literal(value.items)[1:-1].split(",")

    def nest(level: int, start: int) -> str:
        """The braces of dimension `level` from element `start` on."""
        if level == len(lengths) - 1:
            return "{" + ",".join(flat[start : start + lengths[level]]) + "}"
        step = 1
        for n in lengths[level + 1 :]:
            step *= n
        return (
            "{"
            + ",".join(nest(level + 1, start + k * step) for k in range(lengths[level]))
            + "}"
        )

    return value.dims + "=" + nest(0, 0)


def pg_array_literal(items: Sequence[Any]) -> str:
    """A one-dimensional PostgreSQL array literal, every element quoted -
    bound as text, which the column's type parses: no element is spliced
    into SQL, and no Python-side type guess is involved."""

    def one(x: Any) -> str:
        """One element, quoted and escaped; NULL bare."""
        if x is None:
            return "NULL"
        if isinstance(x, bool):
            text = "t" if x else "f"
        elif isinstance(x, float):
            if math.isnan(x):
                text = "NaN"
            elif math.isinf(x):
                text = "Infinity" if x > 0 else "-Infinity"
            else:
                text = repr(x)
        elif isinstance(x, tuple):
            text = interval_input(x)
        else:
            text = str(x)  # int, Decimal, date, str
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return "{" + ",".join(one(x) for x in items) + "}"


def json_not(*texts: str) -> Callable[[Val], bool]:
    """JSON values other than these (an engine refuses them on load)."""
    return lambda v: v.value not in texts


def both(*fs: Callable[[Val], bool]) -> Callable[[Val], bool]:
    """All of the filters."""
    return lambda v: all(f(v) for f in fs)


def anything(v: Val) -> bool:
    """Every value of the group."""
    return True


@dataclass(frozen=True)
class Native:
    """One native column type, the value group it is loaded with, and a filter."""

    ddl: str
    group: str
    accepts: Callable[[Val], bool] = anything
    #: Distinct when the same DDL is loaded with two groups.
    tag: str = ""

    @property
    def slug(self) -> str:
        """A column-name-safe identifier for this (type, group)."""
        base = "".join(ch if ch.isalnum() else "_" for ch in self.ddl.lower())
        return f"{base}__{self.group}{self.tag}".strip("_")[:60]


# --------------------------------------------------------------- the engines


class CovEngine:
    """One engine: its natives, how to bind, build, open and drop."""

    name = ""
    #: True: one table per native type (robust per-row inserts). False: one
    #: wide table for every native, loaded in one batch (the billed engines).
    per_native = True

    def natives(self) -> list[Native]:
        """The native types under test on this engine."""
        raise NotImplementedError

    def bind(self, native: Native, v: Val) -> Any:
        """The value as this driver should bind it into this column."""
        return v.value

    def build(
        self,
        table: str,
        columns: Sequence[tuple[str, str]],
        rows: Sequence[Sequence[Any]],
    ) -> dict[int, str]:
        """Create `table` and insert `rows` (first column the integer id).

        Returns {row id: error} for rows the engine refused to store.
        """
        raise NotImplementedError

    def open(self, scale: int) -> Dialect:
        """A read-only parity dialect, as the CLI would open it."""
        raise NotImplementedError

    def ref(self, table: str) -> str:
        """The table name parity is given."""
        return table

    def table_name(self, slug: str) -> str:
        """The physical table name for a native's slug."""
        return f"{PREFIX}_{slug}"[:60]

    def drop_all(self) -> None:
        """Drop every object this engine created."""


def _per_row(execute: Callable[[Sequence[Any]], None], rows) -> dict[int, str]:
    """Insert row by row, collecting the ones the engine refuses."""
    bad: dict[int, str] = {}
    for r in rows:
        try:
            execute(r)
        except Exception as exc:
            bad[int(r[0])] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    return bad


INT64 = int_range(-(2**63), 2**63 - 1)


class PostgresCov(CovEngine):
    """PostgreSQL: tables in schema `parity_cov`, bound through psycopg."""

    name = "postgres"
    schema = PG_SCHEMA

    def __init__(self, url: str) -> None:
        """Connect an autocommit writer and create the schema."""
        import psycopg

        self.url = url
        self.con = psycopg.connect(url, autocommit=True)
        self.con.execute(f"create schema if not exists {self.schema}")
        self.con.execute(
            f"do $$ begin create type {PG_SCHEMA}.{PREFIX}_mood as enum ('sad', 'ok', 'Ok'); "
            "exception when duplicate_object then null; end $$"
        )
        self.tables: list[str] = []

    def natives(self) -> list[Native]:
        """Every PostgreSQL type the shared `map_type` covers, plus UNKNOWN ones."""
        return [
            Native("smallint", "int", int_range(-(2**15), 2**15 - 1)),
            Native("integer", "int", int_range(-(2**31), 2**31 - 1)),
            Native("bigint", "int", INT64),
            Native("numeric(38,9)", "dec"),
            Native("numeric", "dec"),
            Native("numeric", "dec_wide"),
            Native("real", "float4"),
            Native("double precision", "float"),
            Native("double precision", "float_refuse"),
            Native("boolean", "bool"),
            Native("text", "str", no_nul),
            Native("varchar(6000)", "str", no_nul),
            Native("char(20)", "char"),
            Native("uuid", "uuid"),
            Native("date", "date"),
            Native("date", "date_ext"),
            Native("timestamp", "ts"),
            Native("timestamp", "ts_ext"),
            Native("timestamptz", "tstz"),
            # UNKNOWN: compared through ::text, escaped.
            Native("time", "time"),
            Native("time", "time_ext"),
            Native("time(0)", "time", whole_seconds),
            Native("bytea", "bytes"),
            Native("json", "json"),
            Native("json", "json_forms"),
            Native("jsonb", "json"),
            Native("jsonb", "json_forms"),
            Native(
                "money",
                "dec",
                both(
                    dec_fits(17, 2),
                    lambda v: v.value is None or abs(v.value) < 90071992547409,
                ),
            ),
            Native(f"{PG_SCHEMA}.{PREFIX}_mood", "pg_enum"),
            Native("name", "str", pg_name_ok),
            Native("bit(8)", "bits", width(8)),
            Native("bit varying", "bits"),
            Native("interval", "interval"),
            Native("timetz", "timetz"),
            Native("inet", "inet"),
            Native("cidr", "inet", is_network),
            Native("integer[]", "list_int"),
            Native("text[]", "list_text"),
            Native("date[]", "list_date"),
            Native("numeric[]", "list_dec"),
            Native("double precision[]", "list_float"),
            Native("boolean[]", "list_bool"),
            Native("interval[]", "list_interval"),
            Native("integer[]", "list_shaped"),
            # UNKNOWN: compared through ::text, escaped.
            Native("int4range", "pg_range"),
            Native("jsonb[]", "pg_jsonarr"),
        ]

    def bind(self, native: Native, v: Val) -> Any:
        """psycopg binds Python objects; JSON as text."""
        from psycopg.types.json import Json, Jsonb

        if native.group.startswith("json") and v.value is not None:
            # As the text itself: Json(json.loads(...)) re-serialised it in
            # Python first, losing duplicate keys and number spellings.
            return (Jsonb if native.ddl == "jsonb" else Json)(v.value, dumps=lambda x: x)
        if native.group == "list_shaped" and v.value is not None:
            return pg_shaped_literal(v.value)
        if native.group.startswith("list_") and v.value is not None:
            return pg_array_literal(v.value)
        if native.group == "interval":
            return interval_input(v.value)
        return v.value

    def build(self, table, columns, rows):
        """Recreate `parity_cov.<table>` and bind each row."""
        q = f'{self.schema}."{table}"'
        self.con.execute(f"drop table if exists {q}")
        cols = ", ".join(f'"{n}" {ddl}' for n, ddl in columns)
        self.con.execute(f"create table {q} ({cols})")
        self.tables.append(q)
        marks = ", ".join(["%s"] * len(columns))
        return _per_row(
            lambda r: self.con.execute(f"insert into {q} values ({marks})", r), rows
        )

    def ref(self, table):
        """Schema-qualified, as a user would name it."""
        return f"{self.schema}.{table}"

    def open(self, scale):
        """A parity dialect at `scale`."""
        return get_dialect(self.url, side="A", float_scale=scale)

    def drop_all(self):
        """Drop every table, the enum type and the schema."""
        for q in self.tables:
            self.con.execute(f"drop table if exists {q}")
        self.con.execute(f"drop type if exists {PG_SCHEMA}.{PREFIX}_mood")
        self.con.execute(f"drop schema if exists {self.schema}")
        self.con.close()


PG_EXTRA: dict[str, list[Val]] = {
    # Still UNKNOWN: ranges, and arrays of JSON. Each value distinct (`[1,2]`
    # is `[1,3)` and jsonb `[1,2]` is `[1, 2]`: one value, rightly one text).
    "pg_range": [
        Val("rg:<NULL>", None),
        Val("rg:[1,3)", "[1,3)"),
        Val("rg:[1,4)", "[1,4)"),
        Val("rg:empty", "empty"),
        Val("rg:(,)", "(,)"),
        Val("rg:(,1)", "(,1)"),
    ],
    "pg_jsonarr": [
        Val("ja:<NULL>", None),
        Val("ja:{}", "{}"),
        Val('ja:{"{}"}', '{"{}"}'),
        Val('ja:{"[1, 2]"}', '{"[1, 2]"}'),
        Val('ja:{"[2, 1]"}', '{"[2, 1]"}'),
        Val('ja:{"{}","[]"}', '{"{}","[]"}'),
        Val("ja:{NULL}", "{NULL}"),
    ],
    "pg_enum": [
        Val("en:<NULL>", None),
        Val("en:sad", "sad"),
        Val("en:ok", "ok"),
        Val("en:Ok", "Ok"),
    ],
}


class DuckDBCov(CovEngine):
    """DuckDB: every table in one file, written then opened read-only."""

    name = "duckdb"

    def __init__(self, directory: str) -> None:
        """Open the writer on a fresh file in `directory`."""
        import duckdb

        self.path = os.path.join(directory, "cov.duckdb")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.con = duckdb.connect(self.path)
        self.con.execute(f"create type {PREFIX}_mood as enum ('sad', 'ok', 'Ok')")

    def natives(self) -> list[Native]:
        """Every DuckDB type the shared `map_type` covers, plus UNKNOWN ones."""
        ns_range = year_range(1678, 2261)  # TIMESTAMP_NS is int64 nanoseconds
        return [
            Native("TINYINT", "int", int_range(-128, 127)),
            Native("SMALLINT", "int", int_range(-(2**15), 2**15 - 1)),
            Native("INTEGER", "int", int_range(-(2**31), 2**31 - 1)),
            Native("BIGINT", "int", INT64),
            Native("HUGEINT", "int", int_range(-(2**127), 2**127 - 1)),
            Native("UTINYINT", "int", int_range(0, 255)),
            Native("USMALLINT", "int", int_range(0, 65535)),
            Native("UINTEGER", "int", int_range(0, 2**32 - 1)),
            Native("UBIGINT", "int", int_range(0, 2**64 - 1)),
            Native("UHUGEINT", "int", int_range(0, 2**128 - 1)),
            Native("DECIMAL(38,9)", "dec"),
            Native("DECIMAL(18,4)", "dec", dec_fits(18, 4)),
            Native("DECIMAL(4,1)", "dec", dec_fits(4, 1)),
            Native("DECIMAL(38,30)", "dec_wide", dec_fits(38, 30)),
            Native("FLOAT", "float4"),
            Native("DOUBLE", "float"),
            Native("DOUBLE", "float_refuse"),
            Native("BOOLEAN", "bool"),
            Native("VARCHAR", "str"),
            Native("UUID", "uuid"),
            Native("DATE", "date"),
            Native("DATE", "date_ext"),
            Native("TIMESTAMP", "ts"),
            Native("TIMESTAMP", "ts_ext"),
            Native("TIMESTAMP_S", "ts", micros_multiple(1_000_000)),
            Native("TIMESTAMP_MS", "ts", micros_multiple(1000)),
            Native("TIMESTAMP_NS", "ts", ns_range),
            Native("TIMESTAMP_NS", "ts_sub"),
            Native("TIMESTAMPTZ", "tstz"),
            # UNKNOWN: compared through their varchar cast, escaped.
            Native("TIME", "time"),
            Native("TIME", "time_ext"),
            Native("BLOB", "bytes"),
            Native("JSON", "json"),
            Native("JSON", "json_forms"),
            Native(f"{PREFIX}_mood", "pg_enum"),
            Native("BIT", "bits", nonempty),
            Native("INTERVAL", "interval", finite_interval),
            Native("TIME WITH TIME ZONE", "timetz"),
            Native("INTEGER[]", "list_int"),
            Native("VARCHAR[]", "list_text"),
            Native("DATE[]", "list_date"),
            Native("DECIMAL(18,7)[]", "list_dec"),
            Native("DOUBLE[]", "list_float"),
            Native("BOOLEAN[]", "list_bool"),
            Native("INTERVAL[]", "list_interval"),
            # UNKNOWN: compared through their varchar cast, escaped.
            Native("INTEGER[][]", "duck_nested"),
            Native("STRUCT(a INTEGER, b VARCHAR)", "duck_struct"),
        ]

    def bind(self, native: Native, v: Val) -> Any:
        """Instants and engine-text values as strings DuckDB casts on insert."""
        x = v.value
        if x is None:
            return None
        if native.group == "tstz":
            return x.isoformat(" ")
        if native.group in ("date_ext", "ts_ext") and " BC" in x:
            # DuckDB spells BC `(BC)` after the date: '0044-03-15 (BC) 12:30:00'.
            day, _, rest = x.replace(" BC", "").partition(" ")
            return f"{day} (BC)" + (f" {rest}" if rest else "")
        if native.group == "uuid":
            return uuidlib.UUID(x)
        if native.group == "interval":
            return interval_input(x)
        if native.group == "list_interval":
            return [interval_input(i) for i in x]
        return x

    def build(self, table, columns, rows):
        """Create the table in the shared file and bind each row."""
        cols = ", ".join(f'"{n}" {ddl}' for n, ddl in columns)
        self.con.execute(f'create table "{table}" ({cols})')
        marks = ", ".join(["?"] * len(columns))
        return _per_row(
            lambda r: self.con.execute(
                f'insert into "{table}" values ({marks})', list(r)
            ),
            rows,
        )

    def ref(self, table):
        """In `main`."""
        return f"main.{table}"

    def open(self, scale):
        """Close the writer (DuckDB allows one) and open parity read-only."""
        if self.con is not None:
            self.con.close()
            self.con = None
        return get_dialect(f"duckdb:///{self.path}", side="B", float_scale=scale)

    def drop_all(self):
        """Remove the file."""
        if self.con is not None:
            self.con.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


class MySQLCov(CovEngine):
    """MySQL 8: `cov_*` tables in the test database, bound by the connector."""

    name = "mysql"

    def __init__(self, url: str) -> None:
        """Open a dialect and unlock its session for writing fixtures."""
        from conftest import mysql_rw_cursor

        self.url = url
        self.w = get_dialect(url, side="W")
        mysql_rw_cursor(self.w).close()
        self.tables: list[str] = []

    def natives(self) -> list[Native]:
        """Every MySQL type `map_type_mysql` covers, plus UNKNOWN ones."""
        ts_ok = year_range(1000, 9999)
        return [
            Native("tinyint", "int", int_range(-128, 127)),
            Native("smallint", "int", int_range(-(2**15), 2**15 - 1)),
            Native("mediumint", "int", int_range(-(2**23), 2**23 - 1)),
            Native("int", "int", int_range(-(2**31), 2**31 - 1)),
            Native("bigint", "int", INT64),
            Native("bigint unsigned", "int", int_range(0, 2**64 - 1)),
            Native("decimal(38,9)", "dec"),
            Native("decimal(65,30)", "dec_wide", dec_fits(65, 30)),
            Native("float", "float4", no_nonfinite),
            Native("double", "float", no_nonfinite),
            Native("double", "float_refuse", no_nonfinite),
            Native("boolean", "bool"),
            Native("varchar(6000)", "str"),
            Native("longtext", "str"),
            Native("char(20)", "char"),
            Native("date", "date"),
            Native("datetime(6)", "ts", ts_ok),
            Native(
                "timestamp(6)",
                "tstz",
                utc_between(
                    dt.datetime(1970, 1, 1, 0, 0, 1),
                    dt.datetime(2038, 1, 19, 3, 14, 7, 999999),
                ),
            ),
            # UNKNOWN (or text-cast) types.
            Native("time(6)", "time"),
            Native("time(6)", "time_ext"),
            Native("time(6)", "time_dur"),
            Native("time", "time", whole_seconds),
            Native("varbinary(300)", "bytes"),
            Native("blob", "bytes"),
            Native("json", "json"),
            Native("json", "json_forms"),
            Native("bit(8)", "bits", width(8)),
            Native("bit(64)", "bits", width(64)),
            Native("bit(1)", "bits", width(1)),
            Native("enum('sad','ok','Ok') collate utf8mb4_bin", "my_enum"),
            Native("year", "my_year"),
        ]

    def bind(self, native: Native, v: Val) -> Any:
        """The connector drops tzinfo, so instants go as their UTC wall clock
        (the session is pinned to +00:00); BIT as bytes; DECIMAL as text."""
        x = v.value
        if x is None:
            return None
        if native.group == "tstz":
            return x.astimezone(dt.timezone.utc).replace(tzinfo=None)
        if native.group == "bits":
            # As the big-endian bytes of the value: a str would be read as
            # the characters '0' and '1'.
            return int(x, 2).to_bytes((len(x) + 7) // 8, "big")
        if isinstance(x, Decimal):
            # The connector's Decimal binding keeps ~23 significant digits:
            # 0.00000049999999999999999999 was stored as 0.0000005. As text
            # the server converts it exactly.
            return format(x, "f")
        return x

    def _run(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """One fixture statement on the writable session, committed."""
        cur = self.w._conn.cursor()
        try:
            cur.execute(sql, params)
        finally:
            cur.close()
        self.w._conn.commit()

    def build(self, table, columns, rows):
        """Recreate `table` as utf8mb4 and bind each row."""
        self._run(f"drop table if exists `{table}`")
        cols = ", ".join(f"`{n}` {ddl}" for n, ddl in columns)
        self._run(f"create table `{table}` ({cols}) default charset=utf8mb4")
        self.tables.append(table)
        marks = ", ".join(["%s"] * len(columns))
        return _per_row(
            lambda r: self._run(f"insert into `{table}` values ({marks})", r), rows
        )

    def ref(self, table):
        """Unqualified: the URL's database."""
        return table

    def open(self, scale):
        """A fresh read-only dialect."""
        return get_dialect(self.url, side="A", float_scale=scale)

    def drop_all(self):
        """Drop every table, then close the writer."""
        for t in self.tables:
            self._run(f"drop table if exists `{t}`")
        self.w.close()


MYSQL_EXTRA: dict[str, list[Val]] = {
    "my_enum": PG_EXTRA["pg_enum"],
    "my_year": [Val("yr:<NULL>", None), Val("yr:1901", 1901), Val("yr:2155", 2155)],
}


#: Our own SQL Server database: UTF-8 collation, snapshot allowed.
MSSQL_DB = f"{PREFIX}_db"


class MSSQLCov(CovEngine):
    """SQL Server: tables in database `cov_db` (UTF-8), bound by mssql-python."""

    name = "mssql"

    def __init__(self, url: str) -> None:
        """Create `cov_db` and connect a writer to it."""
        import mssql_python

        from parity.dialects.mssql_dialect import parse_mssql_url

        self.base_url = url
        odbc, _ = parse_mssql_url(url)
        master = mssql_python.connect(odbc, autocommit=True)
        cur = master.cursor()
        cur.execute(f"select 1 from sys.databases where name = '{MSSQL_DB}'")
        if not cur.fetchall():
            cur.execute(
                f"create database [{MSSQL_DB}] collate Latin1_General_100_CI_AS_SC_UTF8"
            )
        cur.execute(f"alter database [{MSSQL_DB}] set allow_snapshot_isolation on")
        cur.close()
        master.close()
        u = urlparse(url)
        self.url = urlunparse(u._replace(path="/" + MSSQL_DB))
        odbc, _ = parse_mssql_url(self.url)
        self.con = mssql_python.connect(odbc, autocommit=True)
        self.tables: list[str] = []

    def natives(self) -> list[Native]:
        """Every SQL Server type `map_type_mssql` covers, plus UNKNOWN ones."""
        return [
            Native("tinyint", "int", int_range(0, 255)),
            Native("smallint", "int", int_range(-(2**15), 2**15 - 1)),
            Native("int", "int", int_range(-(2**31), 2**31 - 1)),
            Native("bigint", "int", INT64),
            Native("decimal(38,9)", "dec"),
            Native(
                "money",
                "dec",
                both(
                    dec_fits(19, 4),
                    lambda v: v.value is None or abs(v.value) < 922337203685477,
                ),
            ),
            Native(
                "smallmoney",
                "dec",
                both(dec_fits(10, 4), lambda v: v.value is None or abs(v.value) < 214748),
            ),
            Native("decimal(38,30)", "dec_wide", dec_fits(38, 30)),
            Native("real", "float4", no_nonfinite),
            Native("float", "float", no_nonfinite),
            Native("float", "float_refuse", no_nonfinite),
            Native("bit", "bool"),
            Native("varchar(max)", "str"),
            Native("nvarchar(max)", "str"),
            Native("char(20)", "char", max_utf8(20)),
            Native("nchar(20)", "char", max_utf16(20)),
            Native("uniqueidentifier", "uuid"),
            Native("date", "date"),
            Native("datetime2(6)", "ts"),
            Native("datetime2(7)", "ts"),
            Native("datetime2(7)", "ts_sub", digits_at_most(7)),
            Native("datetime", "ts", mssql_datetime_ok),
            Native("smalldatetime", "ts", smalldatetime_ok),
            Native("datetimeoffset(7)", "tstz"),
            # UNKNOWN types.
            Native("time(7)", "time"),
            Native("time(7)", "time_sub"),
            Native("time(0)", "time", whole_seconds),
            Native("varbinary(max)", "bytes"),
            Native("varbinary(300)", "bytes"),
            Native("image", "bytes"),
            Native("sql_variant", "variant"),
        ]

    def bind(self, native: Native, v: Val) -> Any:
        """Instants as ISO text with their offset; sub-microsecond as text."""
        x = v.value
        if x is None:
            return None
        if native.group == "tstz":
            return x.isoformat(" ")
        if native.group == "uuid":
            return uuidlib.UUID(x)
        if native.group == "date":
            # mssql-python refuses a Python date before 1753 ("Date out of
            # range for SQL Server") though `date` holds 0001..9999; as ISO
            # text it binds and converts exactly.
            return x.isoformat()
        if native.group == "variant":
            return variant_bind(*x)
        return x

    def build(self, table, columns, rows):
        """Recreate `dbo.<table>` and bind each row."""
        q = f"dbo.[{table}]"
        cur = self.con.cursor()
        cur.execute(f"drop table if exists {q}")
        cols = ", ".join(f"[{n}] {ddl}" for n, ddl in columns)
        cur.execute(f"create table {q} ({cols})")
        cur.close()
        self.tables.append(q)

        def one(r: Sequence[Any]) -> None:
            """Insert one row; a `Cast` value through its own cast, so a
            sql_variant column receives the base type it names."""
            marks = ", ".join(x.sql if isinstance(x, Cast) else "?" for x in r)
            params = [x.value if isinstance(x, Cast) else x for x in r]
            c = self.con.cursor()
            try:
                c.execute(f"insert into {q} values ({marks})", params)
            finally:
                c.close()

        return _per_row(one, rows)

    def ref(self, table):
        """In `dbo`."""
        return f"dbo.{table}"

    def open(self, scale):
        """A parity dialect on `cov_db`."""
        return get_dialect(self.url, side="A", float_scale=scale)

    def drop_all(self):
        """Drop every table, then the database itself (ours alone)."""
        import mssql_python

        from parity.dialects.mssql_dialect import parse_mssql_url

        cur = self.con.cursor()
        for q in self.tables:
            cur.execute(f"drop table if exists {q}")
        cur.close()
        self.con.close()
        odbc, _ = parse_mssql_url(self.base_url)
        master = mssql_python.connect(odbc, autocommit=True)
        cur = master.cursor()
        try:
            # Pooled connections of our own may still hold the database.
            cur.execute(
                f"if db_id('{MSSQL_DB}') is not null begin "
                f"alter database [{MSSQL_DB}] set single_user with rollback immediate; "
                f"drop database [{MSSQL_DB}]; end"
            )
        finally:
            cur.close()
            master.close()


MSSQL_EXTRA: dict[str, list[Val]] = {}


@dataclass(frozen=True)
class Cast:
    """A bound value with the SQL that casts it: `cast(? as money)`."""

    sql: str
    value: Any


def variant_bind(base: str, value: Any) -> Cast:
    """A sql_variant value as its base type: the parameter is cast to it in
    the INSERT, from the driver's own type or from exact text."""
    text = "cast(cast(? as nvarchar(4000)) as {})"

    def clock(ns: int, digits: int) -> str:
        """`HH:MM:SS[.f]` of nanoseconds since midnight, `digits` places."""
        seconds, frac = divmod(ns, 1_000_000_000)
        out = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        return out + ("." + f"{frac:09d}"[:digits] if digits else "")

    if base in ("tinyint", "smallint", "int", "bigint", "bit", "float", "real"):
        return Cast(f"cast(? as {base})", value)
    if base in ("decimal", "numeric"):
        t = value.as_tuple()
        scale = max(0, -t.exponent)
        precision = max(len(t.digits), scale, 1)
        return Cast(text.format(f"{base}({precision},{scale})"), format(value, "f"))
    if base in ("money", "smallmoney"):
        return Cast(text.format(base), format(value, "f"))
    if base in ("nvarchar", "varchar"):
        return Cast(f"cast(? as {base}(100))", value)
    if base in ("nchar", "char"):
        return Cast(f"cast(? as {base}({len(value)}))", value)
    if base == "date":
        return Cast(text.format("date"), value.isoformat())
    if base in ("datetime", "smalldatetime", "datetime2"):
        d, ns = value
        digits = {"datetime": 3, "smalldatetime": 0, "datetime2": 7}[base]
        target = "datetime2(7)" if base == "datetime2" else base
        return Cast(text.format(target), f"{d.isoformat()} {clock(ns, digits)}")
    if base == "datetimeoffset":
        d, ns, offset = value
        sign = "-" if offset < 0 else "+"
        hh, mm = divmod(abs(offset), 60)
        return Cast(
            text.format("datetimeoffset(7)"),
            f"{d.isoformat()} {clock(ns, 7)} {sign}{hh:02d}:{mm:02d}",
        )
    if base == "time":
        return Cast(text.format("time(7)"), clock(value, 7))
    if base == "binary":
        return Cast(f"cast(? as binary({len(value)}))", value)
    if base == "varbinary":
        return Cast("cast(? as varbinary(100))", value)
    if base == "uniqueidentifier":
        return Cast(text.format("uniqueidentifier"), value)
    raise ValueError(f"no binder for sql_variant base type {base!r}")


class BigQueryCov(CovEngine):
    """BigQuery: one wide table, loaded by a JSON load job (free, exact)."""

    name = "bigquery"
    per_native = False

    def __init__(self, url: str) -> None:
        """A read-write client on the URL's dataset."""
        from google.cloud import bigquery

        from parity.dialects.bigquery_dialect import parse_bigquery_url

        self.url = url
        self.project, self.dataset, location = parse_bigquery_url(url)
        self.client = bigquery.Client(project=self.project, location=location)
        self.tables: list[str] = []

    def natives(self) -> list[Native]:
        """Every BigQuery type `map_type_bigquery` covers, plus UNKNOWN ones."""
        return [
            Native("INT64", "int", INT64),
            Native("NUMERIC", "dec"),
            Native("BIGNUMERIC", "dec"),
            Native("BIGNUMERIC", "dec_wide", dec_fits(76, 38)),
            Native("FLOAT64", "float"),
            Native("FLOAT64", "float_refuse"),
            Native("BOOL", "bool"),
            Native("STRING", "str"),
            Native("DATE", "date"),
            Native("DATETIME", "ts"),
            Native("TIMESTAMP", "tstz"),
            Native("TIME", "time"),
            Native("BYTES", "bytes"),
            # A JSON null cannot be loaded as anything but a missing field.
            Native("JSON", "json", json_not("null")),
            # BigQuery refuses an integer past 64 bits in JSON on load.
            Native("JSON", "json_forms", json_not("12345678901234567890123")),
        ]

    def table_name(self, slug: str) -> str:
        """Per-run, so concurrent runs never collide."""
        return f"{PREFIX}_{slug}_{RUN}"

    def _json(self, native: Native, x: Any) -> Any:
        """A value in BigQuery's JSON load encoding."""
        if x is None:
            return None
        if isinstance(x, Decimal):
            return format(x, "f")
        if isinstance(x, float):
            if math.isnan(x):
                return "NaN"
            if math.isinf(x):
                return "Infinity" if x > 0 else "-Infinity"
            return x
        if isinstance(x, bytes):
            return base64.b64encode(x).decode()
        if isinstance(x, dt.datetime):
            return x.isoformat(" ")
        if isinstance(x, (dt.date, dt.time)):
            return x.isoformat()
        return x

    def build(self, table, columns, rows):
        """Create the table by a load job with an explicit schema."""
        from google.cloud import bigquery

        schema = [bigquery.SchemaField(n, ddl) for n, ddl in columns]
        names = [n for n, _ in columns]
        kinds = [ddl for _, ddl in columns]
        data = [
            {
                n: json.loads(x) if k == "JSON" else self._json(None, x)
                for n, k, x in zip(names, kinds, r, strict=True)
                if x is not None
            }
            for r in rows
        ]
        ref = f"{self.project}.{self.dataset}.{table}"
        self.tables.append(ref)
        job = self.client.load_table_from_json(
            data,
            ref,
            job_config=bigquery.LoadJobConfig(
                schema=schema, write_disposition="WRITE_TRUNCATE"
            ),
        )
        job.result()
        return {}

    def bind(self, native: Native, v: Val) -> Any:
        """JSON values go through `_json` at load time; JSON type as text."""
        return v.value

    def ref(self, table):
        """Resolved against the URL's dataset."""
        return table

    def open(self, scale):
        """A parity dialect (read-only OAuth scope)."""
        return get_dialect(self.url, side="A", float_scale=scale)

    def drop_all(self):
        """Delete every table - BigQuery storage is billed."""
        for ref in self.tables:
            self.client.delete_table(ref, not_found_ok=True)
        self.client.close()


class SnowflakeCov(CovEngine):
    """Snowflake: one wide table, rows bound server-side (qmark array binding)."""

    name = "snowflake"
    per_native = False

    def __init__(self, url: str) -> None:
        """Open a dialect whose connection doubles as the fixture writer."""
        self.url = url
        self.w = get_dialect(url, side="W")
        self.tables: list[str] = []

    def natives(self) -> list[Native]:
        """Every Snowflake type `map_type_snowflake` covers, plus UNKNOWN ones."""
        return [
            Native("NUMBER(38,0)", "int", int_range(-(10**38) + 1, 10**38 - 1)),
            Native("NUMBER(38,9)", "dec"),
            Native("NUMBER(38,30)", "dec_wide", dec_fits(38, 30)),
            Native("FLOAT", "float"),
            Native("FLOAT", "float_refuse"),
            Native("BOOLEAN", "bool"),
            Native("VARCHAR", "str"),
            Native("DATE", "date"),
            Native("TIMESTAMP_NTZ(9)", "ts"),
            Native("TIMESTAMP_NTZ(9)", "ts_sub"),
            Native("TIMESTAMP_TZ(9)", "tstz"),
            Native("TIMESTAMP_LTZ(9)", "tstz"),
            Native("TIME(9)", "time"),
            Native("TIME(9)", "time_sub"),
            Native("BINARY", "bytes"),
            Native("VARIANT", "json"),
            # PARSE_JSON refuses duplicate keys.
            Native("VARIANT", "json_forms", json_not('{"a": 1, "a": 2}')),
        ]

    def table_name(self, slug: str) -> str:
        """Per-run, upper case as Snowflake stores unquoted names."""
        return f"{PREFIX}_{slug}_{RUN}".upper()

    def bind(self, native: Native, v: Val) -> Any:
        """Typed bindings: temporal values as ISO text, numbers as text with
        their Snowflake bind type; the rest as is."""
        x = v.value
        if x is None:
            return None
        if isinstance(x, (dt.date, dt.time)):
            # As TIMESTAMP_* the connector binds epoch nanoseconds, which
            # cannot carry an offset (refused: "Invalid bind value") nor reach
            # year 1 or 9999 in int64; bound as text, the server converts
            # the ISO form exactly, offset included.
            return (
                "TEXT",
                x.isoformat(" ") if isinstance(x, dt.datetime) else x.isoformat(),
            )
        if isinstance(x, float) and not math.isfinite(x):
            return ("TEXT", "NaN" if math.isnan(x) else ("inf" if x > 0 else "-inf"))
        if isinstance(x, float):
            return ("REAL", repr(x))
        if isinstance(x, Decimal):
            return ("FIXED", format(x, "f"))
        return x

    def build(self, table, columns, rows):
        """Create the table and insert every row in one bound batch."""
        cur = self.w._conn.cursor()
        try:
            # A string bound into a VARIANT is stored as a VARCHAR variant,
            # not parsed; so VARIANT columns are loaded as text and the table
            # is rebuilt with PARSE_JSON over them.
            variant = {n for n, ddl in columns if ddl == "VARIANT"}
            cols = ", ".join(
                f'"{n}" {"VARCHAR" if n in variant else ddl}' for n, ddl in columns
            )
            cur.execute(f'create or replace table "{table}" ({cols})')
            self.tables.append(table)
            marks = ", ".join(["?"] * len(columns))
            sql = f'insert into "{table}" values ({marks})'
            # Server-side binding: the default pyformat style interpolates
            # values into the SQL text client-side - exactly a literal. The
            # connection is switched to qmark outright, because connector 4.x
            # (the supported line) ignores `_force_qmark_paramstyle` in
            # executemany and formats with `%` instead.
            self.w._conn._paramstyle = "qmark"
            cur.executemany(sql, [list(r) for r in rows], _force_qmark_paramstyle=True)
            if variant:
                exprs = ", ".join(
                    f'parse_json("{n}") as "{n}"' if n in variant else f'"{n}"'
                    for n, _ in columns
                )
                cur.execute(
                    f'create or replace table "{table}" as select {exprs} from "{table}"'
                )
        finally:
            cur.close()
        return {}

    def open(self, scale):
        """A parity dialect."""
        return get_dialect(self.url, side="A", float_scale=scale)

    def drop_all(self):
        """Drop every table."""
        cur = self.w._conn.cursor()
        try:
            for t in self.tables:
                cur.execute(f'drop table if exists "{t}"')
        finally:
            cur.close()
            self.w.close()


#: Still UNKNOWN on DuckDB: a nested list and a struct.
DUCK_EXTRA: dict[str, list[Val]] = {
    "duck_nested": [
        Val("dn:<NULL>", None),
        Val("dn:[]", []),
        Val("dn:[[]]", [[]]),
        Val("dn:[[1, 2]]", [[1, 2]]),
        Val("dn:[[1], [2]]", [[1], [2]]),
        Val("dn:[None]", [None]),
        Val("dn:[[None]]", [[None]]),
    ],
    "duck_struct": [
        Val("ds:<NULL>", None),
        Val("ds:{a: 1, b: 'x'}", {"a": 1, "b": "x"}),
        Val("ds:{a: None, b: None}", {"a": None, "b": None}),
        Val("ds:{a: 1, b: 'NULL'}", {"a": 1, "b": "NULL"}),
        Val("ds:{a: 1, b: \"x', b: 'y\"}", {"a": 1, "b": "x', b: 'y"}),
    ],
}

#: Ad hoc values for engine-specific UNKNOWN types, by group.
EXTRA: dict[str, list[Val]] = {**PG_EXTRA, **MYSQL_EXTRA, **MSSQL_EXTRA, **DUCK_EXTRA}
