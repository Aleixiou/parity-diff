"""Snowflake dialect.

Verified against a live Snowflake account (AWS eu-central-2, 2026-09-07): the
hash constant agrees, a full-table checksum matches DuckDB byte-for-byte over
5,000 mixed-type rows, and `tests/test_snowflake.py` passes end to end - the
identical check, a changed decimal, a deleted row, and the NULL-versus-empty
trap. It was first written against Snowflake's documentation; the live run then
turned up the one thing docs could not: Snowflake upper-cases unquoted
identifiers, so the engine had to match keys and columns case-insensitively
(see `_fold_columns` in engine.py) for a Snowflake table to diff against a
lower-casing engine at all.

The Snowflake-specific decisions, each confirmed by that run:

- **No `CONV` and no bit-cast for the hash.** Snowflake has neither, but
  `MD5_NUMBER_UPPER64(x)` returns the top 64 bits of the digest as an unsigned
  number, and the top 60 bits - the first 15 hex characters the other engines
  fold - are `FLOOR(that / 16)`. That should equal 648541476951500027 for
  `'abc'`; a test must pin it.
- **Integer and decimal both report as `NUMBER`.** `information_schema` tells
  them apart only by `numeric_scale` (0 = integer), so `columns()` reads the
  scale rather than trusting `data_type` - otherwise an integer key would be
  rendered as `42.000000` and never match another engine's `42`.
- **The NULL sentinel is built from `CHR(92)`.** Snowflake interprets
  backslash escapes in string literals, so a literal `'\\N'` is unsafe the same
  way it was on MySQL.
- **One-snapshot isolation is not available.** Snowflake offers only READ
  COMMITTED, so unlike PostgreSQL the walk cannot be pinned to a single
  snapshot; a source table mutating mid-diff can produce an inconsistent
  result. This is a genuine limitation, documented rather than hidden, and
  `caveats` prints it on every run.

Changed after that run, from Snowflake's documentation alone and **not yet
verified live** (no account was available; each needs one query to confirm):

- **Client telemetry is switched off** at login (the
  `CLIENT_TELEMETRY_ENABLED` session parameter, plus the connector's own
  switches): CLAUDE.md section 6.
- **TIMESTAMP_TZ / TIMESTAMP_LTZ are instants** (`Column.tz_aware`), and a
  TIMESTAMP_TZ is moved to UTC before it is printed: `TO_CHAR` prints it in
  its *stored* offset, which the session TIMEZONE pin does not touch, so two
  instants hours apart could print the same text.
- **Timestamps finer than microseconds get a caveat**, since FF6 is what
  every other engine can compare.
- **VARIANT / OBJECT / ARRAY render as `TYPEOF` plus `TO_JSON`**, which keeps
  a string's quotes: a plain varchar cast prints the number 1 and the string
  "1" alike.
- **`int_div` divides an exact multiple**, as SQL Server's does: NUMBER
  division rounds its quotient to 6 places, so `floor(n / d)` could put a key
  one bucket high.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from parity.dialects.base import Dialect, sql_literal
from parity.types import Column, LogicalType


class SnowflakeDialect(Dialect):
    name = "snowflake"
    #: Set from the connection's schema segment. Snowflake folds unquoted names
    #: to upper case, so an unqualified table is looked up in the connected
    #: schema as stored.
    default_schema = "PUBLIC"

    def connect(self, connection_string: str) -> None:  # pragma: no cover
        """Open a connection, pinned to UTC.

        URL grammar mirrors SQLAlchemy's Snowflake dialect:
        ``snowflake://user:password@account/database/schema?warehouse=wh&role=r``

        Note the honest gap: Snowflake supports only READ COMMITTED, so there
        is no equivalent of PostgreSQL's REPEATABLE READ - the diff cannot hold
        one snapshot across the walk. UTC is still pinned so timestamps render
        deterministically.
        """
        import snowflake.connector

        url = urlparse(connection_string)
        parts = [p for p in url.path.split("/") if p]
        database = parts[0] if parts else None
        schema = parts[1] if len(parts) > 1 else None
        params = parse_qs(url.query)

        def opt(key: str) -> str | None:
            """First value of a query parameter, or None."""
            values = params.get(key)
            return values[0] if values else None

        if schema:
            self.default_schema = schema

        self._conn = snowflake.connector.connect(
            # CLAUDE.md section 6: no telemetry, ever. The connector reports
            # driver usage (imported packages, connection shape) to Snowflake
            # unless told not to, and does so *during* connect - so the session
            # parameter has to ride in the login request; setting it afterwards
            # would be too late. Not verified live.
            session_parameters={"CLIENT_TELEMETRY_ENABLED": False},
            log_imported_packages_in_telemetry=False,
            account=url.hostname,
            user=unquote(url.username) if url.username else None,
            password=unquote(url.password) if url.password else None,
            database=database,
            schema=schema,
            warehouse=opt("warehouse"),
            role=opt("role"),
            autocommit=True,
        )
        # Belt and braces: the client-side switch, for anything logged later.
        self._conn.telemetry_enabled = False
        cur = self._conn.cursor()
        try:
            # Timestamps render through this; pin it so two accounts in
            # different regions cannot disagree on the same instant.
            cur.execute("alter session set timezone = 'UTC'")
        finally:
            cur.close()

    def close(self) -> None:  # pragma: no cover
        """Close the connection."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:  # pragma: no cover
        """Run `sql` and return every row as a list of tuples."""
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            # The connector's stubs (5.x) type fetchall() as tuples *or* dicts,
            # because a DictCursor returns dicts. We always use the default
            # cursor, which returns tuples, so this is a type-only narrowing.
            return cast("list[tuple[Any, ...]]", list(cur.fetchall()))
        finally:
            cur.close()

    def qualify(self, table: str) -> str:
        """Quote a table name, always schema-qualified.

        `columns()` introspects an unqualified name in `default_schema` (the
        URL's schema, else PUBLIC). Left unqualified in the data queries,
        Snowflake resolved it in the session's current schema - the user's
        DEFAULT_NAMESPACE - so the columns of one table and the rows of
        another could be compared, and a real difference read identical
        (RT-D2, the Snowflake form of PostgreSQL's search_path gap). Naming
        the schema makes the rows come from the table that was introspected.
        Unverified live: no Snowflake account in the test setup.
        """
        schema, name = self.split_table(table, self.default_schema)
        return f"{self.quote(schema)}.{self.quote(name)}"

    def columns(self, table: str) -> list[Column]:
        """Introspect columns, reading numeric_scale to split NUMBER.

        Snowflake reports every integer and decimal as `NUMBER`; only the scale
        distinguishes them, so this cannot use the shared `columns()`.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select column_name, data_type, numeric_scale, datetime_precision "
            "from information_schema.columns "
            f"where table_schema = {sql_literal(schema)} "
            f"and table_name = {sql_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [_column(str(r[0]), str(r[1]), r[2], r[3]) for r in rows]

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in double quotes, doubling any it contains.

        The injection boundary - names arrive from the command line. Snowflake
        stores unquoted names upper-cased, so a lower-case `"id"` will not match
        a column created as `ID`; the tool sidesteps this by quoting the exact
        names `columns()` read back.
        """
        return '"' + identifier.replace('"', '""') + '"'

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """What makes this side's comparison less than exact, for the output.

        CLAUDE.md section 8: the gaps this module's docstring admits to must
        reach the output too, as SQL Server's dialect does for the same two.
        """
        out = [
            (
                "Snowflake offers only READ COMMITTED, so each query of the walk "
                "sees the table as of its own start, not one snapshot: rows written "
                "during the diff can be reported as differences that never existed "
                "at one point in time, or missed. Diff a quiet table or a clone for "
                "a single-snapshot result"
            )
        ]
        fine = sorted(
            c.name
            for c in columns
            if c.logical_type is LogicalType.TIMESTAMP and _sub_micro(c.raw_type)
        )
        if fine:
            out.append(
                f"columns {fine} can hold nanoseconds; they are compared at "
                "microseconds (FF6: the digits past the sixth are dropped), so a "
                "difference below one microsecond is not seen"
            )
        return out

    # ----------------------------------------------------------- rendering

    def float_caveat(self) -> str | None:
        """FLOAT here predates the shared canonical float form, unverified."""
        return (
            "Snowflake's FLOAT rendering (cast to NUMBER(38, scale)) has not "
            "been verified against the canonical float form the other engines "
            "share; doubles with more than ~15 significant digits or within a "
            "hair of a rounding boundary may report as different, or a change "
            "at the last compared decimal place may be missed"
        )

    def null_sentinel_sql(self) -> str:
        r"""Build the sentinel from CHR(92), not a `\N` literal.

        Snowflake processes backslash escapes in string literals, so a literal
        is unsafe; CHR(92) is a backslash unconditionally and CHR returns a
        varchar (not binary), so the coalesce stays text.
        """
        return "(chr(92) || 'N')"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL/FLOAT cast to NUMBER(38, scale) then to text - so `1.5`
        becomes `'1.500000'`. FLOAT is *not* verified against the canonical
        float form the other engines now share (`canonical_float_text`: the
        shortest round-trip decimal, rounded half away from zero): it was
        defined before that form was, and no Snowflake account was available
        to check it, so `float_caveat` warns on every FLOAT column. Non-finite
        floats are a further unverified gap: Snowflake's Inf/NaN detection
        differs from the other engines.
        """
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as varchar)"
        elif t in (LogicalType.DECIMAL, LogicalType.FLOAT):
            expr = f"cast(cast({c} as number(38,{self.float_scale})) as varchar)"
        elif t is LogicalType.BOOLEAN:
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"to_char({c}, 'YYYY-MM-DD')"
        elif t is LogicalType.TIMESTAMP:
            # FF6 is microseconds, matching the other engines' six digits;
            # `caveats` says so when the column holds more.
            if _base(column.raw_type) == "TIMESTAMP_TZ":
                # A TIMESTAMP_TZ prints in its own stored offset, which the
                # session TIMEZONE pin does not move (it governs LTZ only), so
                # 12:00 +00:00 and 12:00 +05:00 - instants five hours apart -
                # would print alike. Move it to UTC first; the two-argument
                # form converts an instant. From the docs, not verified live.
                c = f"convert_timezone('UTC', {c})"
            expr = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
        elif _base(column.raw_type) in ("VARIANT", "OBJECT", "ARRAY"):
            # A plain varchar cast of a VARIANT prints a string without its
            # JSON quotes, so the number 1 and the string "1" render alike - a
            # false match on the very type semi-structured data lands in.
            # TO_JSON keeps the quotes, and TYPEOF keeps what JSON cannot say
            # (a TIMESTAMP and the VARCHAR spelling it print the same JSON).
            # From the docs, not verified live.
            expr = f"typeof({c}) || ':' || to_json({c})"
        else:
            # STRING, and every unmapped type through its varchar cast:
            # arbitrary text, so escaped. Not verified live (no account).
            expr = self.escape_text(f"cast({c} as varchar)")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def decimal_key_text(self, c: str, column: Column) -> str:
        """NUMBER's varchar cast is exact at the declared scale; drop the
        trailing fractional zeros it adds. Not verified live (no account)."""
        t = f"cast({c} as varchar)"
        return (
            f"case when {c} = 0 then '0' "
            f"when contains({t}, '.') then rtrim(rtrim({t}, '0'), '.') "
            f"else {t} end"
        )

    def normalize_key(self, column: Column) -> str:
        """Exact key text; a TIMESTAMP key keeps any sub-microsecond digits.

        Snowflake timestamps hold nanoseconds and render at FF6, which as a
        key merges distinct keys. One with a sub-microsecond part renders FF9
        instead (the nine digits DuckDB prints for a TIMESTAMP_NS); any other
        renders exactly as before. Not verified live (no account).
        """
        if column.logical_type is LogicalType.TIMESTAMP:
            c = self.quote(column.name)
            return (
                f"case when {c} is null or date_part(nanosecond, {c}) % 1000 = 0 "
                f"then {self.normalize(column)} "
                f"else to_char({c}, 'YYYY-MM-DD HH24:MI:SS.FF9') end"
            )
        return super().normalize_key(column)

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `MD5_NUMBER_UPPER64` is the top 64 bits of the digest as an unsigned
        number; the top 60 bits - the first 15 hex characters the other engines
        take - are that floor-divided by 16.
        """
        return f"floor(md5_number_upper64({text_expr}) / 16)"

    def int_div(self, numerator: str, denominator: str) -> str:
        """Exact truncating division on NUMBER operands - never a bare `/`.

        Snowflake gives a NUMBER(38,0) / NUMBER(38,0) quotient six decimal
        places and *rounds* it (`2/3` is 0.666667), so `floor(n / d)` turns a
        quotient of q.9999996 into q + 1 and the row lands in the next bucket
        on this side only - the flaw SQL Server's `int_div` had. `n - mod(n,
        d)` is an exact multiple of `d`, so its quotient is an integer and
        nothing can round; the operands are non-negative, so this is floor.
        From the docs, not verified live.
        """
        n, d = f"({numerator})", f"({denominator})"
        return f"cast(({n} - mod({n}, {d})) / {d} as number(38,0))"

    def wide_int(self, expr: str) -> str:
        """Widen past 64 bits before arithmetic. NUMBER(38,0) holds 38 digits,
        which the key offset cannot overflow."""
        return f"cast(({expr}) as number(38,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group.

        NUMBER(38,0) is far beyond any row count times 2^60.
        """
        return f"coalesce(sum(cast(({expr}) as number(38,0))), 0)"


def _base(raw: str) -> str:
    """The type name without parameters, upper case: `TIMESTAMP_TZ(9)` ->
    `TIMESTAMP_TZ`."""
    return raw.upper().split("(")[0].strip()


def _sub_micro(raw: str) -> bool:
    """Whether a timestamp type can hold digits past microseconds.

    `_column` folds the precision in (`TIMESTAMP_NTZ(9)`). Without one the
    answer is yes: Snowflake's default fractional precision is 9.
    """
    inside = raw.partition("(")[2].rstrip(")").strip()
    return not inside.isdigit() or int(inside) > 6


def _column(name: str, raw: str, numeric_scale: Any, precision: Any) -> Column:
    """Build a Column from one information_schema row.

    Marks TIMESTAMP_TZ and TIMESTAMP_LTZ as instants and TIMESTAMP_NTZ as
    naive. None of the three says `with time zone`, which is all the engine
    can otherwise go on, so every Snowflake timestamp read as naive: no warning
    for NTZ against a PostgreSQL timestamptz, a false one for TZ against it.
    A timestamp's fractional precision is folded into `raw_type`, so `caveats`
    can tell a TIMESTAMP(9) from a TIMESTAMP(6).
    """
    base = _base(raw)
    tz: bool | None = None
    if base in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        tz = True
    elif base in ("TIMESTAMP_NTZ", "DATETIME"):
        tz = False
    if base.startswith("TIMESTAMP") and "(" not in raw and precision is not None:
        try:
            raw = f"{raw}({int(precision)})"
        except (TypeError, ValueError):
            pass
    return Column(name, map_type_snowflake(raw, numeric_scale), raw, tz_aware=tz)


def map_type_snowflake(raw: str, numeric_scale: Any) -> LogicalType:
    """Map a Snowflake type onto a logical category.

    Snowflake reports both integers and decimals as `NUMBER`; only the scale
    tells them apart, so it is passed in. A NULL scale (non-numeric type) is
    treated as not-an-integer.
    """
    t = raw.upper().split("(")[0].strip()
    if t in {"NUMBER", "DECIMAL", "NUMERIC"}:
        try:
            scale = int(numeric_scale) if numeric_scale is not None else 6
        except (TypeError, ValueError):
            scale = 6
        return LogicalType.INTEGER if scale == 0 else LogicalType.DECIMAL
    if t in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}:
        return LogicalType.INTEGER  # aliases that may appear via DATA_TYPE_ALIAS
    if t in {"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}:
        return LogicalType.FLOAT
    if t == "BOOLEAN":
        return LogicalType.BOOLEAN
    if t == "DATE":
        return LogicalType.DATE
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        return LogicalType.TIMESTAMP
    if t in {"TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING"}:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
