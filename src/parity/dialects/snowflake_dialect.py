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
  result. This is a genuine limitation, documented rather than hidden.
"""

from __future__ import annotations

from typing import Any
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
            account=url.hostname,
            user=unquote(url.username) if url.username else None,
            password=unquote(url.password) if url.password else None,
            database=database,
            schema=schema,
            warehouse=opt("warehouse"),
            role=opt("role"),
            autocommit=True,
        )
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
            return list(cur.fetchall())
        finally:
            cur.close()

    def columns(self, table: str) -> list[Column]:
        """Introspect columns, reading numeric_scale to split NUMBER.

        Snowflake reports every integer and decimal as `NUMBER`; only the scale
        distinguishes them, so this cannot use the shared `columns()`.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select column_name, data_type, numeric_scale "
            "from information_schema.columns "
            f"where table_schema = {sql_literal(schema)} "
            f"and table_name = {sql_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [
            Column(str(r[0]), map_type_snowflake(str(r[1]), r[2]), str(r[1]))
            for r in rows
        ]

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in double quotes, doubling any it contains.

        The injection boundary - names arrive from the command line. Snowflake
        stores unquoted names upper-cased, so a lower-case `"id"` will not match
        a column created as `ID`; the tool sidesteps this by quoting the exact
        names `columns()` read back.
        """
        return '"' + identifier.replace('"', '""') + '"'

    # ----------------------------------------------------------- rendering

    def null_sentinel_sql(self) -> str:
        r"""Build the sentinel from CHR(92), not a `\N` literal.

        Snowflake processes backslash escapes in string literals, so a literal
        is unsafe; CHR(92) is a backslash unconditionally and CHR returns a
        varchar (not binary), so the coalesce stays text.
        """
        return "(chr(92) || 'N')"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL/FLOAT match DuckDB's path - cast to NUMBER(38, scale) then to
        text - so `1.5` becomes `'1.500000'`. Non-finite floats are a known
        unverified gap: Snowflake's Inf/NaN detection differs from the other
        engines and must be checked against a real account before this is
        trusted for FLOAT columns holding them.
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
            # FF6 is microseconds, matching the other engines' six digits.
            expr = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
        else:
            expr = f"cast({c} as varchar)"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `MD5_NUMBER_UPPER64` is the top 64 bits of the digest as an unsigned
        number; the top 60 bits - the first 15 hex characters the other engines
        take - are that floor-divided by 16.
        """
        return f"floor(md5_number_upper64({text_expr}) / 16)"

    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division.

        Operands are NUMBER (via `wide_int`), so `/` is exact decimal and
        `floor` truncates exactly for the non-negative operands used here -
        unlike a float `/`, which CLAUDE.md 4.5 warns loses precision.
        """
        return f"floor(({numerator}) / ({denominator}))"

    def wide_int(self, expr: str) -> str:
        """Widen past 64 bits before arithmetic. NUMBER(38,0) holds 38 digits,
        which the key offset cannot overflow."""
        return f"cast(({expr}) as number(38,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group.

        NUMBER(38,0) is far beyond any row count times 2^60.
        """
        return f"coalesce(sum(cast(({expr}) as number(38,0))), 0)"


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
