"""SQL Server dialect (SQL Server 2019+, and by extension Azure SQL).

Verified against a live SQL Server 2022 (16.0, Developer edition, Linux
container) on 2026-11-02, in a UTF-8 database, a Latin1 (code page 1252)
database and a case-sensitive one: the hash constant agrees, every row of a
5,000-row mixed-type table renders byte-identically to DuckDB, the FLOAT
expression matches `canonical_float_text` on the whole adversarial sample, and
`tests/test_mssql.py` finds every planted difference exactly. Nothing below is
taken from the documentation alone; several things are not what it suggests.

The SQL Server-specific decisions, each confirmed by that run:

- **UTF-8 bytes for MD5, whatever the database collation.** SQL Server holds
  Unicode as UTF-16, so `HASHBYTES('MD5', nvarchar)` hashes different bytes
  from every other engine, on every row. Row text is built as `nvarchar(max)`
  and converted once, at the hash, through `COLLATE
  Latin1_General_100_BIN2_UTF8` to `varchar(max)` - which *is* UTF-8 under a
  `_UTF8` collation. Equal to Python's `md5(s.encode())` for accents, CJK and
  emoji, in a UTF-8 and a Latin1 database alike. UTF-8 collations exist from
  SQL Server 2019, so older servers are refused at connect.
- **Every value is `nvarchar(max)` before it is joined.** `CONCAT_WS` returns
  at most 4,000 characters (8,000 bytes) unless an argument is a `max` type,
  and truncates silently: three `nvarchar(4000)` values joined came back as
  4,000 characters, not 12,002. A difference past the cut would vanish - a
  false match. So `normalize` casts every column to `nvarchar(max)`.
- **`CONCAT_WS` refuses a single value** ("requires 3 to 254 arguments"), so
  `_concat` returns a lone part as itself - byte-identical to joining it.
- **The hash goes through hex text.** `HASHBYTES` gives `varbinary(16)`;
  style-2 hex, the first 15 digits behind a `0`, read back as `varbinary(8)`
  and then `bigint`, is the 60-bit prefix - `648541476951500027` for `'abc'`.
  One `HASHBYTES` per row; the bit-arithmetic alternative needs two.
- **`int_div` must not use `/`.** Decimal division rounds its result to a
  reduced scale, so a quotient of `q.9999996` becomes `q + 1` and a row lands
  in the next bucket on this side only. `(n - n % d) / d` divides an exact
  multiple, so nothing can round.
- **No `GROUP BY 1` and no 32-bit `COUNT`.** T-SQL accepts neither an ordinal
  nor an alias in GROUP BY, and `COUNT` raises past 2^31 - 1 rows; the base
  class's `group_by` and `count` hooks spell them `group by <expr>` and
  `count_big`.
- **Key uniqueness is counted on SHA-256, not on text.** Text equality is the
  collation's: case-insensitive by default and blind to trailing spaces under
  every collation, so the keys `'a'`, `'A'` and `'a '` count as one and a
  genuinely unique key would be refused as duplicated.
- **FLOAT builds the shortest repr, which SQL Server does not print.** Style 3
  is always 17 digits. But `cast(float as decimal(38, q))` is the exact binary
  value rounded half up (verified on every sample value), and decimal to float
  is correctly rounded, so the shortest round-trip decimal is the first of its
  15-, 16- and 17-digit roundings that converts back to the same double - the
  route the BigQuery dialect takes. See `_float_text`.
- **One snapshot when the database allows it.** With
  `ALLOW_SNAPSHOT_ISOLATION ON` the session runs SNAPSHOT inside one
  transaction, so the walk reads one version of a live table. Otherwise it
  stays READ COMMITTED - never REPEATABLE READ, whose held shared locks would
  block writers on a live legacy system for the whole diff, and never NOLOCK,
  whose dirty reads invent differences - and `caveats` says so in the output.
- **No session time zone to pin.** `datetime2` is naive and `datetimeoffset`
  carries its own offset, which `SWITCHOFFSET` moves to UTC.
- **Read-only by the login, not the session.** SQL Server has no read-only
  session (`ApplicationIntent=ReadOnly` does not enforce one on a primary), so
  the dialect issues SELECT only, `query` refuses anything else, nothing is
  ever committed, and `close` rolls back. Use a login that holds only
  `db_datareader`; verified that such a login runs the diff and is refused a
  write.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from parity.dialects.base import (
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    NULL_SENTINEL,
    Dialect,
    float_range_where,
    float_tiny_sql,
    float_zero_text,
)
from parity.types import Column, KeySpec, LogicalType

#: The collation the row text is converted through to reach UTF-8 bytes. A
#: binary one, so the conversion never depends on linguistic rules; any
#: `_UTF8` collation would give the same bytes.
UTF8_COLLATION = "Latin1_General_100_BIN2_UTF8"

#: Largest float scale the FLOAT expression is exact at. Its tie test adds a
#: `decimal(38, 37)` to itself, which T-SQL evaluates one place shorter; past
#: 18 places a 17-digit candidate for a value below 1e-19 would need that
#: place. Refused rather than rounded - nobody compares doubles at 19 places.
MSSQL_FLOAT_MAX_SCALE = 18

#: Compared columns (key included) from which the checksum query is
#: hash-aggregated - see `SQLServerDialect._segment_sql`. The default plan
#: overflows the 8,060-byte row limit at ~307 int columns; below 300 it has
#: room, and the hint can cost it the expression-services limit.
HASH_GROUP_FROM = 300

#: `SERVERPROPERTY('EngineEdition')` values this dialect is verified for:
#: 1-4 the boxed SQL Server editions (2 Standard, 3 Enterprise/Developer, 4
#: Express), 5 Azure SQL Database, 8 Azure SQL Managed Instance. Synapse (6,
#: 11) runs READ UNCOMMITTED by default or has no transactional snapshot, and
#: Fabric (11, 12) ignores `SET TRANSACTION ISOLATION LEVEL`; both are refused
#: until someone verifies them.
SUPPORTED_EDITIONS = {1, 2, 3, 4, 5, 8}

#: Query parameters `mssql://` understands, and the ODBC keyword each maps
#: to. Anything else is refused rather than ignored: a misspelt
#: `aplication_intent=readonly` silently dropped is a connection that does not
#: do what the user thinks.
_URL_PARAMS = {
    "encrypt": "Encrypt",
    "trust_server_certificate": "TrustServerCertificate",
    "application_intent": "ApplicationIntent",
    # Not an ODBC keyword for mssql-python, which rejects `LoginTimeout`
    # ("Unknown keyword ... is not recognized", RT-F6): it is the driver's
    # `timeout=` argument instead (SQL_ATTR_LOGIN_TIMEOUT). See
    # `mssql_login_timeout`.
    "login_timeout": "",
    "instance": "",  # folded into Server=host\\instance
}

#: Leads the text a FLOAT past 2^116 raises with, so `query` can find it.
_FLOAT_RANGE = re.compile(
    r"'(" + re.escape(FLOAT_OUT_OF_RANGE_MESSAGE) + r".*)' to data type"
)

#: Leads the canonical text of an nvarchar value that is not valid UTF-16 (an
#: unpaired surrogate), followed by its UTF-16LE bytes in hex. See `_utf16_safe`.
INVALID_UTF16_MARKER = "parity: invalid UTF-16 "


#: Words that never appear in the SQL this dialect generates outside a quoted
#: identifier or a string literal, and that any statement able to write,
#: execute or change the session needs. `into` is the one that matters most:
#: `select ... into t` is a SELECT that creates a table.
_WRITE_WORDS = re.compile(
    r"\b(into|insert|update|delete|merge|drop|alter|create|truncate|exec|execute"
    r"|grant|revoke|deny|declare|set|use|backup|restore|dbcc|waitfor|bulk|begin"
    r"|commit|rollback|save|openrowset|opendatasource|openquery|openxml"
    r"|shutdown|kill|reconfigure|xp_\w*|sp_\w*)\b",
    re.IGNORECASE,
)

#: A bracketed identifier (`]]` escapes `]`) or a string literal, N-prefixed
#: or not (`''` escapes `'`) - what `_skeleton` blanks out.
_QUOTED = re.compile(r"\[(?:[^\]]|\]\])*\]|N?'(?:[^']|'')*'", re.IGNORECASE)


def _skeleton(sql: str) -> str | None:
    """`sql` with every bracketed identifier and string literal blanked out,
    or None if one is unterminated.

    Identifiers and literals come from the user's schema and the command line;
    everything else is text this dialect wrote. So whatever remains is what
    the statement *does*, and can be checked word by word.
    """
    rest = _QUOTED.sub(" q ", sql)
    if "[" in rest or "'" in rest:
        return None
    return rest


def is_single_select(sql: str) -> bool:
    """Whether `sql` is one plain SELECT - no INTO, no second statement.

    A prefix check let `select * into copy from t` (creates a table) and
    `select 1; drop table t` through (RT-F12). This dialect only ever runs SQL
    it generated itself, whose shape is known: one SELECT, no `;`, no comment,
    and none of `_WRITE_WORDS` outside identifiers and literals. Anything else
    is refused, even what might be harmless.
    """
    rest = _skeleton(sql)
    if rest is None:
        return False
    if not rest.lstrip().lower().startswith("select"):
        return False
    if ";" in rest or "--" in rest or "/*" in rest or '"' in rest:
        return False
    return _WRITE_WORDS.search(rest) is None


def _odbc_value(value: str) -> str:
    """Brace an ODBC connection-string value, doubling any `}` inside it.

    Unbraced, a `;` in a password would end the value and start a new keyword
    - an injection into the connection string.
    """
    return "{" + value.replace("}", "}}") + "}"


def parse_mssql_url(connection_string: str) -> tuple[str, str]:
    """Turn ``mssql://user:pw@host:port/database?...`` into an ODBC string.

    Returns ``(odbc_connection_string, database)``. Parameters are whitelisted
    (`_URL_PARAMS`) and every value is braced, so neither a typo nor a
    password can smuggle in a keyword. A named instance, which cannot ride in
    a URL host, is ``?instance=NAME``.
    """
    url = urlparse(connection_string)
    if not url.hostname:
        raise ValueError(
            "mssql connection string needs a host: "
            "mssql://user:password@host[:port]/database"
        )
    database = unquote(url.path.lstrip("/"))
    if not database or "/" in database:
        raise ValueError(
            "mssql connection string needs exactly one database: "
            "mssql://user:password@host[:port]/database"
        )
    if not url.username:
        raise ValueError(
            "mssql connection string needs a user: SQL authentication "
            "(user:password@host) is the only kind verified so far"
        )
    params = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
    unknown = sorted(set(params) - set(_URL_PARAMS))
    if unknown:
        raise ValueError(
            f"unknown mssql connection parameter(s) {unknown}; "
            f"supported: {sorted(_URL_PARAMS)}"
        )
    server = url.hostname
    if "instance" in params:
        server += "\\" + params.pop("instance")
        if url.port:
            server += f",{url.port}"
    else:
        server += f",{url.port or 1433}"
    parts = [
        f"Server={_odbc_value(server)}",
        f"Database={_odbc_value(database)}",
        f"UID={_odbc_value(unquote(url.username))}",
        f"PWD={_odbc_value(unquote(url.password or ''))}",
    ]
    params.pop("login_timeout", None)
    parts += [f"{_URL_PARAMS[k]}={_odbc_value(v)}" for k, v in sorted(params.items())]
    return ";".join(parts), database


def mssql_login_timeout(connection_string: str) -> int:
    """`?login_timeout=` in seconds, or 0 (the driver default) when absent.

    Passed to `mssql_python.connect(timeout=...)`, which sets
    SQL_ATTR_LOGIN_TIMEOUT; the driver has no connection-string keyword for it.
    """
    values = parse_qs(urlparse(connection_string).query).get("login_timeout")
    if not values:
        return 0
    raw = values[-1].strip()
    if not raw.isdigit():
        raise ValueError(
            f"mssql login_timeout must be a whole number of seconds, got {raw!r}"
        )
    return int(raw)


class SQLServerDialect(Dialect):
    name = "mssql"
    #: Replaced at connect by the login's own default schema, `SCHEMA_NAME()`.
    default_schema = "dbo"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Set up per-instance state; nothing touches the network until connect."""
        super().__init__(*args, **kwargs)
        #: The mssql-python connection. Typed loosely: the driver is optional.
        self._conn: Any = None
        self.database = ""
        #: Whether the walk reads one snapshot. Unconnected (SQL generation in
        #: tests) is assumed pinned, so `caveats` stays quiet there.
        self.snapshot = True

    # ---------------------------------------------------------------- setup

    def connect(self, connection_string: str) -> None:
        """Open a session reading one snapshot where the server allows it.

        Order matters: the isolation level is set while autocommit is still
        on, then autocommit goes off, so the first query opens the one
        transaction the whole diff runs in - and SNAPSHOT takes its snapshot
        at that transaction's first data access, as PostgreSQL's REPEATABLE
        READ does at its first query.
        """
        try:
            odbc, self.database = parse_mssql_url(connection_string)
            login_timeout = mssql_login_timeout(connection_string)
        except ValueError as exc:
            # `get_dialect` passes a ValueError through untouched, trusting it
            # to name the side already - so it has to.
            raise self._err(str(exc)) from exc

        import mssql_python

        self._conn = mssql_python.connect(odbc, autocommit=True, timeout=login_timeout)
        edition, schema, snapshot_state, utf8 = self._probe()
        if edition not in SUPPORTED_EDITIONS:
            raise self._err(
                f"unsupported SQL Server engine edition {edition}: this dialect "
                "is verified for SQL Server 2019+ and Azure SQL Database / "
                "Managed Instance. Microsoft Fabric and Azure Synapse differ in "
                "isolation and are not supported yet."
            )
        if not utf8:
            raise self._err(
                "this server has no UTF-8 collations (SQL Server 2019 or newer "
                "is required): rows could not be hashed as the UTF-8 bytes "
                "every other engine hashes"
            )
        self.default_schema = schema or "dbo"
        self.snapshot = snapshot_state == 1
        cur = self._conn.cursor()
        try:
            cur.execute(
                "set transaction isolation level "
                + ("snapshot" if self.snapshot else "read committed")
            )
        finally:
            cur.close()
        # Nothing is ever committed: `close` rolls back, so even a bug that
        # somehow issued a write could not persist it.
        self._conn.autocommit = False

    def _probe(self) -> tuple[int, str, int, bool]:
        """Edition, default schema, snapshot state and UTF-8 support, in one query."""
        row = self.query(
            "select cast(serverproperty('EngineEdition') as int), schema_name(), "
            "(select snapshot_isolation_state from sys.databases "
            "where database_id = db_id()), "
            "(select count(*) from sys.fn_helpcollations() "
            f"where name = '{UTF8_COLLATION}')"
        )[0]
        return int(row[0] or 0), str(row[1] or ""), int(row[2] or 0), bool(row[3])

    def close(self) -> None:
        """End the snapshot by rolling back - never committing - and close."""
        try:
            self._conn.rollback()
        finally:
            self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run one SELECT and return every row as a tuple.

        Refuses anything but one plain SELECT before it reaches the server
        (`is_single_select`). SQL Server has no read-only session to fall back
        on, so this guard, the transaction that is never committed, and a
        read-only login are the defences.
        """
        if not is_single_select(sql):
            raise self._err(f"refusing to run anything but a single SELECT: {sql[:60]!r}")
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(row) for row in cur.fetchall()]
        except Exception as exc:
            found = _FLOAT_RANGE.search(str(exc))
            if found is None:
                raise
            raise self._err(found.group(1)) from exc
        finally:
            cur.close()

    # ------------------------------------------------------------ metadata

    def columns(self, table: str) -> list[Column]:
        """Introspect columns, keeping the fractional-second precision.

        `DATA_TYPE` reports bare names (`datetime2`, not `datetime2(7)`), and
        the precision decides whether a timestamp is compared exactly, so it
        is read alongside and folded into `raw_type`.
        """
        schema, name = self.split_table(table, self.default_schema)
        # Upper case, and it has to be: in a database with a case-sensitive
        # collation, catalog names are case-sensitive too, and
        # `information_schema.columns` is "Invalid object name" (verified).
        rows = self.query(
            "select COLUMN_NAME, DATA_TYPE, DATETIME_PRECISION "
            f"from {self._columns_catalog(schema)} "
            f"where TABLE_SCHEMA = {self.string_literal(schema)} "
            f"and TABLE_NAME = {self.string_literal(name)} "
            "order by ORDINAL_POSITION"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [_column(str(r[0]), str(r[1]), r[2]) for r in rows]

    def _columns_catalog(self, schema: str) -> str:
        """Upper case: see `columns`."""
        return "INFORMATION_SCHEMA.COLUMNS"

    def _not_found(self, table: str, schema: str, name: str) -> str:
        """Explain a missing table: near misses by case, and the GRANT hint.

        Its own near-miss query, because the shared one spells the catalog's
        columns in lower case, which a case-sensitive database rejects. And
        `INFORMATION_SCHEMA` lists only what this login can see, so a missing
        permission looks exactly like a missing table.
        """
        message = f"table not found: {table} (looked in schema {schema!r})"
        try:
            near = self.query(
                "select distinct TABLE_SCHEMA, TABLE_NAME "
                f"from {self._columns_catalog(schema)} "
                f"where lower(TABLE_NAME) = lower({self.string_literal(name)})"
            )
        except Exception:  # noqa: BLE001 - diagnosing an error must never replace it
            near = []
        others = sorted(f"{s}.{t}" for s, t in near if (s, t) != (schema, name))
        if others:
            message += (
                ". Names are matched exactly, including case, in a database "
                f"with a case-sensitive collation - did you mean {' or '.join(others)}?"
            )
        return (
            message + ". If it does exist, this login may lack SELECT on it "
            "(db_datareader, or GRANT SELECT)"
        )

    def string_literal(self, value: str) -> str:
        """`N'...'`: a Unicode literal, so a non-Latin table name survives."""
        return "N'" + value.replace("'", "''") + "'"

    def quote(self, identifier: str) -> str:
        """Bracket an identifier, doubling any `]` it contains.

        The injection boundary - names arrive from the command line. Brackets
        work whatever `QUOTED_IDENTIFIER` is set to, unlike double quotes.
        """
        return "[" + identifier.replace("]", "]]") + "]"

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """What makes this side's comparison less than exact, for the output."""
        out: list[str] = []
        if not self.snapshot:
            out.append(
                f"database {self.database!r} has ALLOW_SNAPSHOT_ISOLATION OFF, so "
                "the walk runs at READ COMMITTED and each query sees the table as "
                "of its own start: rows written during the diff can be reported "
                "as differences that never existed at one point in time, or "
                "missed. Turn on ALLOW_SNAPSHOT_ISOLATION, or diff a quiet "
                "replica or snapshot, for a single-snapshot result"
            )
        seven = sorted(
            c.name for c in columns if self.holds_sub_micro(c) and not c.sub_micro
        )
        if seven:
            out.append(
                f"columns {seven} hold 100-nanosecond ticks; they are compared "
                "at microseconds (the seventh digit is truncated), so a "
                "difference below one microsecond is not seen"
            )
        return out

    # --------------------------------------------------- SQL spelling hooks

    def count(self, expr: str = "*") -> str:
        """`count_big`: `count` is a 32-bit int and raises past 2^31 - 1 rows."""
        return f"count_big({expr})"

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count distinct keys by SHA-256 of their UTF-8 text, not by text.

        Text equality follows the collation, which merges `'a'`, `'A'` and
        `'a '` under the default one; a 256-bit digest of the bytes does not.
        An integer key compares as an integer and needs none of this.
        """
        if not key.hashed:
            return f"count_big(distinct {identity})"
        return f"count_big(distinct hashbytes('SHA2_256', {self._utf8(identity)}))"

    def group_by(self, position: int, expr: str) -> str:
        """The expression itself: T-SQL has no GROUP BY ordinal or alias."""
        return expr

    def _segment_sql(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> str:
        """The shared checksum query, hash-aggregated when it is wide (RT-C8).

        Left to choose, the optimizer sorts the rows by bucket and stream-
        aggregates them, and the sort's worktable carries every column's
        `nvarchar(max)` intermediate - about 26 bytes each - so past ~307
        compared columns it overflows the 8,060-byte row limit and the diff
        died, on a table SQL Server itself stores happily (up to 1,024
        columns). A hash aggregate keeps only the group key and the running
        count and sum: verified at 400 and 1,000 int columns and 400
        nvarchar(max) ones, and on 300,000 rows it returned the same checksums
        in the same time (1.5s against 1.65s).

        Only from `HASH_GROUP_FROM` columns, though: the hint also changes how
        the expressions are compiled, and a 130-column mix of ints, strings
        and floats - which the default plan handles - then hit "an expression
        services limit has been reached". Below the threshold the SQL is
        exactly what it always was. Either limit, when it is still reached,
        becomes an error naming the side and table (`segment_checksums`). The
        row text and the hash are untouched, so no checksum moves.
        """
        sql = super()._segment_sql(table, key, columns, lo, hi, n_segments)
        if len(key.columns) + len(columns) >= HASH_GROUP_FROM:
            sql += " option (hash group)"
        return sql

    def segment_checksums(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> dict[int, tuple[int, int]]:
        """The shared checksums, with SQL Server's query-size limits named.

        A raw "Cannot create a row of size ..." or "expression services
        limit" names neither side nor table; say which, and what to do.
        """
        try:
            return super().segment_checksums(table, key, columns, lo, hi, n_segments)
        except Exception as exc:
            text = str(exc)
            if "maximum row size" not in text and "expression services limit" not in text:
                raise
            raise self._err(
                f"the checksum query over {table} ({len(columns)} compared "
                "columns) is too large for SQL Server to compile or run "
                f"({text.rsplit(']', 1)[-1].strip()}). Compare fewer columns "
                "per run with --columns / --exclude"
            ) from exc

    def fetch_text(self, expr: str) -> str:
        """Canonical text as its UTF-8 bytes, `varbinary(max)` (RT-A9).

        mssql-python drops trailing NUL characters from an nvarchar(max)
        result (verified: `N'a' + nchar(0)` fetched as `'a'`, while a NUL in
        the middle survives), so a row whose checksum proved it different was
        downloaded equal to the other side's and the difference discarded.
        Bytes arrive intact (`b'a\\x00'`, verified to 100,001 bytes), and they
        are exactly the bytes `hash_expr` hashes - so the downloaded text is
        now, by construction, the text the checksum saw. Only the small ranges
        `fetch_range` downloads pay for it.
        """
        return f"convert(varbinary(max), {self._utf8(expr)})"

    def fetched(self, value: Any) -> Any:
        """Decode what `fetch_text` selected. Strict UTF-8: the server built
        it from valid UTF-16 (an unpaired surrogate is rendered as an ASCII
        marker first, see `_utf16_safe`), so a decode error would be a bug to
        surface, never text to guess at. An integer key passes through."""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8")
        return value

    # ----------------------------------------------------------- rendering

    def holds_sub_micro(self, column: Column) -> bool:
        """datetime2(7) and datetimeoffset(7) hold 100 ns ticks."""
        return column.raw_type.lower() in ("datetime2(7)", "datetimeoffset(7)")

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check, past three driver limits.

        Text travels as its UTF-8 bytes, because mssql-python drops trailing
        NUL characters from nvarchar results (the reason for `fetch_text`).
        Timestamps travel as ODBC-canonical text (style 121; 127 for
        datetimeoffset, offset included): the driver rounds datetime2(7) to
        microseconds where the rules truncate, and `datetime`'s tick only
        has its documented display as text. The reference reads both forms.
        """
        c = self.quote(column.name)
        if column.logical_type is LogicalType.STRING:
            return f"convert(varbinary(max), {self._utf8(f'cast({c} as nvarchar(max))')})"
        if column.logical_type is LogicalType.TIMESTAMP:
            style = 127 if _base_type(column.raw_type) == "datetimeoffset" else 121
            return f"convert(nvarchar(40), {c}, {style})"
        return super().sample_select(column)

    def sample_value(self, column: Column, value: Any) -> Any:
        """Decode the text bytes; a uniqueidentifier in lower case, as the
        rules render it (SQL Server prints it upper case)."""
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="replace")
        if isinstance(value, str) and _base_type(column.raw_type) == "uniqueidentifier":
            return value.lower()
        return value

    def separator_sql(self) -> str:
        """`nchar(31)`: `char(31)` would be varchar in the database code page,
        and the text stays nvarchar until the one UTF-8 conversion."""
        return "nchar(31)"

    def null_sentinel_sql(self) -> str:
        r"""`N'\N'` - T-SQL does not process backslash escapes in literals
        (verified by hashing)."""
        return f"N'{NULL_SENTINEL}'"

    def _concat(self, parts: list[str]) -> str:
        """Nest `concat_ws` as the base does, but never call it with one value.

        T-SQL's `CONCAT_WS` needs at least two (it raises "requires 3 to 254
        arguments" counting the separator). One part reaches here for a single
        compared column, a single-column hashed key, or the remainder group of
        the nesting tree (65 columns = 64 + 1), and joining one value is that
        value, so returning it is byte-identical.
        """
        if len(parts) == 1:
            return parts[0]
        return super()._concat(parts)

    def float_scale_for(self, column: Column) -> int:
        """The shared limit, tightened to `MSSQL_FLOAT_MAX_SCALE`."""
        s = super().float_scale_for(column)
        if s > MSSQL_FLOAT_MAX_SCALE:
            raise self._err(
                f"FLOAT column {column.name!r} cannot be compared at float_scale "
                f"{s} on SQL Server; the maximum is {MSSQL_FLOAT_MAX_SCALE}"
            )
        return s

    def normalize(self, column: Column) -> str:
        """Render one column as `nvarchar(max)` canonical text, null-safe.

        Every branch is `nvarchar(max)` - see the module docstring for why a
        shorter type would be a false-match risk - and ends inside the
        `coalesce`, so NULL is always the sentinel.
        """
        c = self.quote(column.name)
        raw = column.raw_type.lower().split("(")[0].strip()
        t = column.logical_type
        text = "nvarchar(max)"
        if t is LogicalType.INTEGER:
            expr = f"convert({text}, {c})"
        elif t is LogicalType.DECIMAL:
            # numeric -> numeric at a lower scale rounds half away from zero,
            # as DuckDB's decimal cast and PostgreSQL's round() do (verified
            # on +/-0.0000005). money converts exactly. A value with more than
            # 38 - scale integer digits raises an overflow, loudly.
            if self.float_scale > 38:
                raise self._err(
                    f"DECIMAL column {column.name!r} cannot be compared at "
                    f"float_scale {self.float_scale}; SQL Server's maximum "
                    "decimal scale is 38"
                )
            expr = f"convert({text}, cast({c} as decimal(38,{self.float_scale})))"
        elif t is LogicalType.FLOAT:
            expr = self._float_text(c, self.float_scale_for(column), column)
        elif t is LogicalType.BOOLEAN:
            # `when ... = 0`, never `else`: an `else` sends NULL to 'false' and
            # NULL-versus-FALSE would compare equal (CLAUDE.md section 4.3).
            expr = f"case when {c} = 1 then N'true' when {c} = 0 then N'false' end"
        elif t is LogicalType.DATE:
            # Style 23 is yyyy-mm-dd, year zero-padded (0001, 0999).
            expr = f"convert({text}, {c}, 23)"
        elif t is LogicalType.TIMESTAMP:
            expr = self._timestamp_text(c, raw)
            if column.sub_micro:
                # Both sides hold sub-microsecond digits (COV-2): a seventh
                # digit is kept, as nine digits - what DuckDB TIMESTAMP_NS and
                # Snowflake print for the same instant.
                v = f"switchoffset({c}, '+00:00')" if raw == "datetimeoffset" else c
                s27 = f"convert(nvarchar(max), cast({v} as datetime2(7)), 121)"
                expr = (
                    f"case when right({s27}, 1) = N'0' then {expr} else {s27} + N'00' end"
                )
        elif raw in ("char", "nchar"):
            # A fixed-width column stores 'abc' as 'abc       '. PostgreSQL's
            # bpchar -> text and MySQL's CHAR both strip that padding, and
            # DuckDB has none, so without rtrim every migrated char row
            # differs. Nothing is lost: char(n) cannot tell 'abc' from 'abc '.
            expr = f"rtrim(cast({c} as {text}))"
            if raw == "nchar":
                expr = self._utf16_safe(expr)
            expr = self.escape_text(expr)
        elif raw == "uniqueidentifier":
            # SQL Server prints GUIDs upper-case; PostgreSQL and DuckDB lower.
            expr = f"lower(convert({text}, {c}))"
        elif t is LogicalType.STRING:
            expr = f"cast({c} as {text})"
            if raw in ("nvarchar", "ntext", "sysname"):
                expr = self._utf16_safe(expr)
            expr = self.escape_text(expr)
        elif raw in ("binary", "varbinary", "image", "timestamp", "rowversion"):
            # Bytes as 0x-hex (style 1); a cast to nvarchar would read them as
            # UTF-16. `timestamp` is rowversion, not a point in time.
            expr = f"convert({text}, convert(varbinary(max), {c}), 1)"
        elif raw in ("geography", "geometry", "hierarchyid"):
            expr = self.escape_text(f"cast({c}.ToString() as {text})")
        elif raw == "sql_variant":
            expr = self._variant_text(c)
        else:
            # Any other type through its text: arbitrary, so escaped.
            expr = self.escape_text(f"cast({c} as {text})")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def _variant_text(self, c: str) -> str:
        """A `sql_variant` as `<base type>:<exact value>` (RT-A16, RT-C5).

        A plain cast to nvarchar(max) is lossy twice over: it drops the base
        type, so int 1 and N'1' (or float 1.5 and decimal 1.5) rendered alike,
        and it formats by the base type's *default* style, which keeps ~6
        significant digits of a float and only the minutes of a datetime or
        datetime2 (`Jan  1 2024 10:00AM`) - two different values on one engine
        compared equal. So the base type leads, and each value is rendered by
        an exact style: float/real round-trip (style 3), money to 4 places
        (style 2), date-times to their full precision (style 121), binary as
        hex. No other engine has the type, so this is about exactness on SQL
        Server itself: a variant column against another engine's column
        differs on every row, as the unmapped-type warning says.
        """
        text = "nvarchar(max)"
        base = f"cast(sql_variant_property({c}, 'BaseType') as nvarchar(128))"
        by_type = {
            "float": f"convert({text}, cast({c} as float), 3)",
            "real": f"convert({text}, cast({c} as real), 3)",
            "money": f"convert({text}, cast({c} as money), 2)",
            "smallmoney": f"convert({text}, cast({c} as smallmoney), 2)",
            "datetime": f"convert({text}, cast({c} as datetime), 121)",
            "smalldatetime": f"convert({text}, cast({c} as smalldatetime), 120)",
            "datetime2": f"convert({text}, cast({c} as datetime2(7)), 121)",
            "datetimeoffset": f"convert({text}, cast({c} as datetimeoffset(7)), 121)",
            "date": f"convert({text}, cast({c} as date), 23)",
            "time": f"convert({text}, cast({c} as time(7)))",
            "binary": f"convert({text}, cast({c} as varbinary(8000)), 1)",
            "varbinary": f"convert({text}, cast({c} as varbinary(8000)), 1)",
            "uniqueidentifier": f"lower(convert({text}, {c}))",
            "nvarchar": self._utf16_safe(f"cast({c} as {text})"),
            "nchar": self._utf16_safe(f"cast({c} as {text})"),
        }
        whens = " ".join(f"when N'{t}' then {e}" for t, e in by_type.items())
        # Integers, bit, decimal/numeric and char/varchar convert exactly by
        # default: digits, and the decimal's own scale.
        value = f"case {base} {whens} else convert({text}, {c}) end"
        # The value can be arbitrary text (a varchar/nvarchar base type), so
        # it is escaped like any string: no `\N` or chr(31) can leak through.
        return f"{base} + N':' + {self.escape_text(value)}"

    def backslash_sql(self) -> str:
        """`nchar(92)`: nvarchar like the rest of the row text."""
        return "nchar(92)"

    def concat_sql(self, left: str, right: str) -> str:
        """T-SQL concatenates with `+`."""
        return f"({left} + {right})"

    def escape_text(self, expr: str) -> str:
        """Escape under a binary collation, whatever the column's own.

        T-SQL's `replace()` compares under the input's collation, and the
        Windows collations treat control characters such as chr(31) as
        ignorable, so a linguistic `replace` cannot be trusted to find them.
        Latin1_General_BIN2 compares UTF-16 code units, which is exactly
        what is wanted for one backslash and one chr(31), in any database
        collation; it changes no character, and the hash's own UTF-8
        conversion overrides it.
        """
        return super().escape_text(f"(({expr}) collate Latin1_General_BIN2)")

    def decimal_key_text(self, c: str, column: Column) -> str:
        """decimal's text is exact and plain at the declared scale; drop the
        trailing fractional zeros. T-SQL 2019 has no `rtrim(s, chars)`, so
        the zeros are cut with `reverse`/`patindex`. money and smallmoney
        convert to text at two places by default, so they go through their
        exact decimal(19,4) / decimal(10,4) first."""
        raw = column.raw_type.lower().split("(")[0].strip()
        if raw == "money":
            c = f"cast({c} as decimal(19,4))"
        elif raw == "smallmoney":
            c = f"cast({c} as decimal(10,4))"
        t = f"convert(nvarchar(max), {c})"
        nz = f"left({t}, len({t}) - patindex(N'%[^0]%', reverse({t})) + 1)"
        return (
            f"case when {c} = 0 then N'0' "
            f"when charindex(N'.', {t}) = 0 then {t} "
            f"when right({nz}, 1) = N'.' then left({nz}, len({nz}) - 1) "
            f"else {nz} end"
        )

    def normalize_key(self, column: Column) -> str:
        """Exact key text; a datetime2/datetimeoffset key keeps its 100 ns tick.

        As a value the seventh fractional digit is truncated (`caveats` says
        so), and as a key that merged distinct keys. A key whose seventh digit
        is not zero renders as nanoseconds instead - the seven digits and
        `00`, the nine digits DuckDB prints for the same TIMESTAMP_NS - and
        any other key renders exactly as before, so it still matches a
        microsecond key elsewhere. Six digits against nine: no collisions.
        """
        raw = column.raw_type.lower().split("(")[0].strip()
        if column.logical_type is LogicalType.TIMESTAMP and raw in (
            "datetime2",
            "datetimeoffset",
        ):
            c = self.quote(column.name)
            v = f"switchoffset({c}, '+00:00')" if raw == "datetimeoffset" else c
            s27 = f"convert(nvarchar(max), cast({v} as datetime2(7)), 121)"
            return (
                f"case when {c} is null or right({s27}, 1) = N'0' "
                f"then {self.normalize(column)} else {s27} + N'00' end"
            )
        return super().normalize_key(column)

    def _timestamp_text(self, c: str, raw: str) -> str:
        """`yyyy-mm-dd hh:mi:ss.ffffff`, as the other engines print timestamps.

        datetime2 and datetimeoffset hold 100 ns ticks and the other engines
        microseconds. Style 121 of a datetime2(7) has seven digits, and the
        seventh is *truncated* (`left(..., 26)`), not rounded: that is how a
        microsecond reader - Python's datetime, an ODBC export - carries the
        value, and it never rolls 23:59:59.9999995 into the next day. `caveats`
        reports the lost digit. smalldatetime widens to datetime2 exactly.

        datetime is different: it ticks in 1/300 s, and what every tool shows
        and exports is its rounded display, `.003` / `.007`, not the
        `.0033333` a datetime2 cast yields under compatibility level 130+. So
        it renders its own millisecond display with three zeros appended.

        datetimeoffset is an instant: moved to UTC first, since a cast to
        datetime2 simply drops the offset. SQL Server has no session time zone
        to pin (CLAUDE.md section 4.8) - nothing else depends on one.
        """
        if raw == "datetime":
            return f"convert(nvarchar(max), {c}, 121) + N'000'"
        if raw == "datetimeoffset":
            c = f"switchoffset({c}, '+00:00')"
        return f"left(convert(nvarchar(max), cast({c} as datetime2(7)), 121), 26)"

    def _float_text(self, c: str, s: int, column: Column | None = None) -> str:
        """The canonical text of a double (`canonical_float_text`, base.py).

        Below 2^53 that is the shortest round-trip decimal, rounded half away
        from zero to the scale. SQL Server prints no shortest repr - style 3 is
        always 17 digits - but two conversions are exact, verified on every
        value of the adversarial sample: float -> decimal(38, q) is the exact
        binary value rounded half up, and decimal -> float is correctly
        rounded. So the shortest repr is the first of the 15-, 16- and
        17-significant-digit roundings of the exact value that converts back
        to the same double. Each is the closest decimal of its length, and
        below 2^53 the closest one round-trips whenever any of that length
        does, which makes the first one that does exactly the shortest repr.

        An exact tie at 16 or 17 digits (2^50 + 0.25 sits exactly between
        ...624.2 and ...624.3, and both round-trip) goes to even, as Ryu and
        dtoa break it; ROUND is half away from zero, so a tie - no digits past
        the next place, and the value the midpoint of the two neighbours -
        takes the truncation instead when its last digit is even. At 15 digits
        a tied neighbour can never round-trip (a double's spacing is below a
        15-digit unit), so that candidate needs no tie handling.

        Two bands, because decimal(38) holds 38 digits: below 1 the exact value
        to 37 places; from 1 to 2^53, to 21 places (16 integer digits). The
        decimal exponent e comes from the text of the exact value, never from a
        float log10. Values under 10^-(s+1) are zero at this scale and short-
        circuit. From 2^53 every double is an integer, and decimal(38, 0) holds
        it exactly up to 2^116; past that it is refused, by a conversion error
        whose message `query` turns into one naming the side. SQL Server floats
        cannot be Infinity or NaN, so there are no tokens to emit.
        """
        x = f"cast({c} as float)"
        two = "cast(2 as float)"
        # The column, in the refusal past 2^116 (RT-E10); quotes doubled for
        # the N'...' literal.
        where = float_range_where(column).replace("'", "''") if column else ""

        def shortest(q: int, exponent: str) -> str:
            """The shortest repr, from the exact value to q places; `exponent`
            computes the decimal exponent e from `{d}`."""
            d = f"cast({x} as decimal(38,{q}))"
            e = exponent.format(d=d)

            def candidate(n: int, ties: bool) -> str:
                """The n-significant-digit rounding, ties to even if `ties`."""
                k = f"({n - 1} - {e})"
                r = f"round({d}, {k})"
                if not ties:
                    return r
                t = f"round({d}, {k}, 1)"
                return (
                    f"(case when round({d}, {k} + 1, 1) = {d} "
                    f"and {d} + {d} = {t} + {r} "
                    f"and floor(abs({t}) / nullif(abs({r} - {t}), 0)) % 2 = 0 "
                    f"then {t} else {r} end)"
                )

            c15, c16, c17 = candidate(15, False), candidate(16, True), candidate(17, True)
            return (
                f"(case when cast({c15} as float) = {x} then {c15} "
                f"when cast({c16} as float) = {x} then {c16} else {c17} end)"
            )

        # Below 1: '0.000123...' - e is 2 minus the position of the first
        # non-zero digit. From 1: the length of the integer part, minus one.
        small = shortest(37, "(2 - patindex('%[1-9]%', convert(varchar(41), abs({d}))))")
        large = shortest(
            21, "(len(convert(varchar(41), cast(floor(abs({d})) as decimal(38,0)))) - 1)"
        )
        frac = "." + "0" * s if s else ""
        return (
            f"case when abs({x}) < {float_tiny_sql(s)} then N'{float_zero_text(s)}' "
            f"when abs({x}) < 1 "
            f"then convert(nvarchar(max), cast({small} as decimal(38,{s}))) "
            f"when abs({x}) < power({two}, 53) "
            f"then convert(nvarchar(max), cast({large} as decimal(38,{s}))) "
            f"when abs({x}) < power({two}, 116) "
            f"then convert(nvarchar(max), cast({x} as decimal(38,0))) + N'{frac}' "
            f"when {x} is not null then convert(nvarchar(max), cast("
            f"N'{FLOAT_OUT_OF_RANGE_MESSAGE}' + convert(nvarchar(30), {x}, 3) "
            f"+ N'{where}' as int)) end"
        )

    def _utf16_safe(self, v: str) -> str:
        """`v` (nvarchar(max)) as itself, or a marker if it is not valid UTF-16.

        nvarchar stores UTF-16 code units and accepts an unpaired surrogate
        (`nchar(0xD800)`), which no other engine can hold. `hash_expr` hashes
        UTF-8, and the conversion to UTF-8 turns every unpaired surrogate into
        U+FFFD - so `nchar(0xD800)`, `nchar(0xD801)` and a real U+FFFD all
        hashed alike, a false match (RT-E9), and downloading such a row raised
        UnicodeDecodeError in the driver (RT-E9b).

        A value is valid exactly when the round trip through UTF-8 gives back
        the same UTF-16 bytes - compared with their lengths, since SQL Server
        pads the shorter binary with zeros when comparing. One that is not renders as the marker plus its
        own UTF-16LE bytes in hex: distinct for distinct values, plain ASCII
        for the driver, and different from any text the other engines can
        store unless that text spells the marker itself. Valid text - every
        value any other engine can hold - takes the first branch and renders
        exactly as before, so no checksum moves.

        It is not free. Measured on SQL Server 2022, 300,000 rows with three
        nvarchar columns, one planted difference: 3.2s without the guard,
        about 4.0s with it, and about 5.6s when every row holds an emoji (a
        surrogate pair takes the round trip; text without one stops at the
        LIKE). Only SQL Server's N-types pay it, and a false match is the one
        outcome this tool cannot afford.
        """
        bytes16 = f"convert(varbinary(max), {v})"
        back = f"convert(varbinary(max), cast({self._utf8(v)} as nvarchar(max)))"
        # Almost all text holds no surrogate code unit at all, and is valid
        # without the round trip. A binary, non-supplementary collation sees a
        # surrogate pair as two code units, so this range catches every
        # surrogate, paired or not; only those values pay for the round trip.
        surrogate = "N'%[' + nchar(55296) + N'-' + nchar(57343) + N']%'"
        return (
            f"case when {v} collate Latin1_General_BIN2 not like {surrogate} then {v} "
            f"when {back} = {bytes16} "
            f"and datalength({back}) = datalength({bytes16}) then {v} "
            f"when {v} is not null then N'{INVALID_UTF16_MARKER}' "
            f"+ convert(nvarchar(max), {bytes16}, 1) end"
        )

    def _utf8(self, text_expr: str) -> str:
        """The UTF-8 bytes of a text expression, as `varchar(max)`.

        Through `nvarchar(max)` first, so a `varchar` input in a legacy code
        page is decoded by its own collation before being re-encoded.
        """
        return (
            f"cast(cast(({text_expr}) as nvarchar(max)) "
            f"collate {UTF8_COLLATION} as varchar(max))"
        )

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        MD5 of the UTF-8 bytes; its first 15 hex digits behind a `0` are 8
        bytes whose top nibble is zero, so the bigint is positive and equals
        the other engines' 60-bit prefix - 648541476951500027 for 'abc'.
        """
        digest = f"hashbytes('MD5', {self._utf8(text_expr)})"
        return (
            f"convert(bigint, convert(varbinary(8), '0' + "
            f"left(convert(varchar(32), {digest}, 2), {HASH_HEX_CHARS}), 2))"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """Exact truncating division on decimal operands - never plain `/`.

        T-SQL decimal division rounds to a reduced scale, so `/` alone can turn
        a quotient of q.9999996 into q + 1 and move a row into the next bucket
        on this side only. `n - n % d` is an exact multiple of `d`, so the
        division is exact; the operands are non-negative, so this is floor.
        """
        n, d = f"({numerator})", f"({denominator})"
        return f"cast(({n} - {n} % {d}) / {d} as bigint)"

    def wide_int(self, expr: str) -> str:
        """decimal(38, 0): the key offset times the bucket count cannot
        overflow it, even for a key range as wide as bigint (2^64 * n)."""
        return f"cast(({expr}) as decimal(38,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum in decimal(38, 0) - about 8.6e19 rows of 2^60 - and 0 when empty."""
        return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"


def _column(name: str, data_type: str, precision: Any) -> Column:
    """Build a Column, marking datetimeoffset as an instant and folding the
    fractional-second precision into `raw_type`."""
    t = data_type.lower()
    raw = data_type
    if t in ("datetime2", "datetimeoffset", "time") and precision is not None:
        raw = f"{data_type}({int(precision)})"
    tz: bool | None = None
    if t == "datetimeoffset":
        tz = True
    elif t in ("datetime", "datetime2", "smalldatetime"):
        tz = False
    return Column(name, map_type_mssql(data_type), raw, tz_aware=tz)


def map_type_mssql(raw: str) -> LogicalType:
    """Map a SQL Server type name onto a logical category.

    `INFORMATION_SCHEMA.COLUMNS.DATA_TYPE` reports bare lower-case names. Its
    own map rather than the shared `map_type`, for two traps: `bit` is a
    boolean here but a bit *string* on PostgreSQL, and `timestamp` is
    rowversion - eight opaque bytes - which the shared prefix match would read
    as a point in time.
    """
    t = raw.lower().split("(")[0].strip()
    if t in {"tinyint", "smallint", "int", "bigint"}:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric", "money", "smallmoney"}:
        return LogicalType.DECIMAL
    if t in {"float", "real"}:
        return LogicalType.FLOAT
    if t == "bit":
        return LogicalType.BOOLEAN
    if t == "date":
        return LogicalType.DATE
    if t in {"datetime", "datetime2", "smalldatetime", "datetimeoffset"}:
        return LogicalType.TIMESTAMP
    if t in {
        "char",
        "varchar",
        "nchar",
        "nvarchar",
        "text",
        "ntext",
        "sysname",
        "uniqueidentifier",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN


def _base_type(raw: str) -> str:
    """The type name without parameters, lower case: `datetime2(7)` -> `datetime2`."""
    return raw.lower().split("(")[0].strip()
