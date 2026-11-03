"""MySQL dialect.

A third engine, and the first test of the claim that adding one is a single
small file. Most of the contract is standard SQL; the MySQL-specific decisions,
each of which the encoding tests verify, are:

- **Identifiers quote with backticks**, not double quotes. That is MySQL's
  default; double quotes are string literals unless ANSI_QUOTES is set, which
  we must not depend on.
- **The row hash goes through `CONV`.** MySQL has no `bit(n)::bigint` cast and
  no `0x`-string cast, but `CONV(hex, 16, 10)` converts a hex prefix to an
  integer at 64-bit precision, which a 60-bit value fits exactly. It must
  produce the same integer PostgreSQL and DuckDB do — 648541476951500027 for
  `'abc'` — and a test pins that.
- **`table_schema` is the database.** MySQL has no schema layer inside a
  database, so a `schema.table` name means `database.table`, and the default
  "schema" is whatever database the connection opened.
- **Snapshot and UTC** come from InnoDB's default REPEATABLE READ plus an
  explicit session time zone, the same guarantees the other two dialects set.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_range_where,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType

#: MySQL's widest DECIMAL: 65 significant digits, at most 30 after the point.
#: A DECIMAL column can never hold more, which is what makes the out-of-range
#: guard in `_decimal_text` exact.
MYSQL_MAX_DECIMAL_DIGITS = 65
MYSQL_MAX_DECIMAL_SCALE = 30

#: Leads the text a too-wide DECIMAL raises with, so `query` can recognise it.
DECIMAL_RANGE_MARKER = "parity: DECIMAL value "
_DECIMAL_RANGE_ERROR = re.compile(r"'(" + re.escape(DECIMAL_RANGE_MARKER) + r".*)' for")

#: Leads the text a FLOAT past 2^116 raises with. Short, because MySQL cuts
#: the quoted UUID_TO_BIN argument at 128 characters and the value and column
#: must survive; `query` expands it to `FLOAT_OUT_OF_RANGE_MESSAGE`.
FLOAT_RANGE_MARKER = "parity: FLOAT value "
_FLOAT_RANGE_ERROR = re.compile(r"'" + re.escape(FLOAT_RANGE_MARKER) + r"(.*)' for")


class MySQLDialect(Dialect):
    name = "mysql"
    #: MySQL has no schema inside a database, so the connection's database is
    #: the default namespace and `information_schema` calls it table_schema.
    default_schema = ""

    def connect(self, connection_string: str) -> None:
        """Open a read-only, UTC-pinned connection.

        InnoDB's default isolation is REPEATABLE READ, so a transaction already
        sees one snapshot from its first read - the guarantee PostgreSQL had to
        be told to give. Autocommit is left off so that snapshot spans the diff.
        """
        import mysql.connector

        url = urlparse(connection_string)
        self._database = url.path.lstrip("/") or None
        if self.default_schema == "" and self._database:
            self.default_schema = self._database
        # Stored so `cancel` can open a second connection to KILL this one.
        self._host = url.hostname or "127.0.0.1"
        self._port = url.port or 3306
        self._user = unquote(url.username) if url.username else None
        self._password = unquote(url.password) if url.password else None
        self._conn = mysql.connector.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            autocommit=False,
            connection_timeout=10,
        )
        cur = self._conn.cursor()
        # A timestamp renders through the session time zone; pin it so two
        # servers in different zones cannot report the same instant as different.
        cur.execute("set session time_zone = '+00:00'")
        # Belt and braces on top of the InnoDB default.
        cur.execute("set session transaction isolation level repeatable read")
        cur.close()

    def cancel(self) -> None:
        """Abort the running query from another thread.

        A separate short-lived admin connection issues KILL QUERY against this
        connection's id - mysql-connector cannot interrupt a blocked cursor in
        place, the way psycopg and duckdb can.
        """
        import mysql.connector

        try:
            thread_id = int(self._conn.connection_id or 0)
            admin = mysql.connector.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                connection_timeout=5,
            )
            try:
                c = admin.cursor()
                c.execute(f"kill query {thread_id}")
                c.close()
            finally:
                admin.close()
        except Exception:  # noqa: BLE001, S110 - cancel is best effort;
            # a failure to KILL must never mask the real error.
            pass

    def close(self) -> None:
        """Close the connection, ending the snapshot."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a list of tuples."""
        import mysql.connector

        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            return list(cur.fetchall())
        except mysql.connector.Error as exc:
            huge = _FLOAT_RANGE_ERROR.search(str(exc))
            if huge is not None:
                raise self._err(FLOAT_OUT_OF_RANGE_MESSAGE + huge.group(1)) from exc
            # The out-of-range DECIMAL guard (`_decimal_text`) raises through
            # UUID_TO_BIN; say what actually happened, and on which side.
            found = _DECIMAL_RANGE_ERROR.search(str(exc))
            if found is None:
                raise
            s = self.float_scale
            raise self._err(
                f"{found.group(1)} does not fit DECIMAL({MYSQL_MAX_DECIMAL_DIGITS},"
                f"{s}), the widest type MySQL can render it in at float_scale {s}. "
                "MySQL would clamp it to that type's maximum without an error, "
                "so two different values could compare equal; the comparison is "
                "refused instead. A smaller --float-scale leaves more integer "
                "digits."
            ) from exc
        finally:
            cur.close()

    def columns(self, table: str) -> list[Column]:
        """Introspect columns. `table_schema` is the database in MySQL."""
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = {sql_literal(schema)} "
            f"and table_name = {sql_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [Column(str(r[0]), map_type_mysql(str(r[1])), str(r[1])) for r in rows]

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in backticks, doubling any it contains.

        MySQL's default identifier quote is the backtick; a double quote is a
        string literal unless ANSI_QUOTES is set, which we must not rely on.
        This is the injection boundary - names arrive from the command line.
        """
        return "`" + identifier.replace("`", "``") + "`"

    # ----------------------------------------------------------- rendering

    def separator_sql(self) -> str:
        """`char(31 using utf8mb4)` - MySQL has no `chr`, and a bare `char(31)`
        is a binary string that would coerce the join to bytes."""
        return "char(31 using utf8mb4)"

    def null_sentinel_sql(self) -> str:
        r"""Build the sentinel from CHAR(92) rather than a backslash literal.

        MySQL processes `\` as an escape inside a string literal, and whether
        it does is governed by `sql_mode` (NO_BACKSLASH_ESCAPES), so `'\N'` is
        not portable across servers. CHAR(92) is a backslash unconditionally.
        Verified to hash to the same value DuckDB and PostgreSQL produce for the
        sentinel.
        """
        return "convert(0x5c4e using utf8mb4)"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL casts to DECIMAL(65, scale) then to text, so `1.5` becomes
        `'1.500000'`, and raises rather than clamp a value that does not fit
        (see `_decimal_text`); FLOAT follows the shared canonical float
        form (see the branch). Every branch ends inside the coalesce, so NULL
        always becomes the sentinel.
        """
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as char)"
        elif t is LogicalType.DECIMAL:
            expr = self._decimal_text(column, c)
        elif t is LogicalType.FLOAT:
            # Widen first: a single-precision column (`real`, FLOAT) is cast to
            # double, which is exact on every engine, so float4 and float8 share
            # one canonical form. Rendered as float4 directly, PostgreSQL and
            # DuckDB print different shortest reprs (-4088288.25 as `-4088288.2`
            # against `-4088288.25`) and MySQL prints only 6 digits. For a
            # double column the cast is a no-op.
            c = f"cast({c} as double)"
            # Finite values follow `canonical_float_text` (base.py). Below 2^53
            # MySQL's double -> DECIMAL cast already is that: it converts
            # through the shortest round-trip decimal (dtoa) and rounds half
            # away from zero - verified identical to the Python spec on every
            # value of the adversarial sample, so these checksums do not move.
            # From 2^53 every double is an integer, rebuilt exactly from two
            # BIGINTs, because PostgreSQL cannot match the shortest repr there.
            # Past 2^116 the high half overflows BIGINT, and MySQL's CAST would
            # *clamp* it silently - two different huge values comparing equal.
            # MySQL has no error() and a SELECT turns most errors into
            # warnings, so that branch raises through UUID_TO_BIN, exactly as
            # the DECIMAL guard does (see `_decimal_text`), and `query` names
            # the side, the column and the value. It used to be exp() overflow
            # (1690), which said only "DOUBLE value is out of range in
            # 'exp(...)'" - nothing a user could act on (RT-E10).
            # MySQL DOUBLE cannot hold Infinity or NaN, so no tokens here.
            s = self.float_scale_for(column)
            hi = f"truncate({c} / {FLOAT_EXACT_INT_FROM}, 0)"
            expr = (
                f"case when abs({c}) < {FLOAT_EXACT_INT_FROM} "
                f"then cast(cast({c} as decimal(38,{s})) as char) "
                f"when abs({c}) < cast('{FLOAT_MAX_MAGNITUDE_SQL}' as double) "
                f"then cast(cast(cast({hi} as signed) as decimal(65,0)) "
                f"* {FLOAT_EXACT_INT_FROM} "
                f"+ cast({c} - {hi} * {FLOAT_EXACT_INT_FROM} as signed) "
                f"as decimal(65,{s})) "
                f"when {c} is not null then cast(uuid_to_bin(concat("
                f"'{FLOAT_RANGE_MARKER}', cast({c} as char), "
                f"convert(0x{float_range_where(column).encode().hex()} using utf8mb4)"
                f")) as char) end"
            )
        elif t is LogicalType.BOOLEAN:
            # MySQL BOOLEAN is an alias for TINYINT(1): 0 or 1, never a real
            # boolean. `when not c` still routes NULL to the sentinel.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"date_format({c}, '%Y-%m-%d')"
        elif t is LogicalType.TIMESTAMP:
            # %f is microseconds, six digits, matching the other two engines.
            expr = f"date_format({c}, '%Y-%m-%d %H:%i:%s.%f')"
        else:
            # STRING, and every unmapped type (json, time, ...) through its
            # text cast: arbitrary text, so escaped.
            expr = self.escape_text(f"cast({c} as char)")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def backslash_sql(self) -> str:
        r"""`char(92 using utf8mb4)`: a backslash whatever sql_mode says about
        `\` in literals (NO_BACKSLASH_ESCAPES), and text, not a binary string."""
        return "char(92 using utf8mb4)"

    def concat_sql(self, left: str, right: str) -> str:
        """`concat()`: in MySQL `||` is logical OR unless PIPES_AS_CONCAT."""
        return f"concat({left}, {right})"

    def decimal_key_text(self, c: str, column: Column) -> str:
        """DECIMAL's char cast is exact and plain, at the declared scale; drop
        the trailing fractional zeros that scale adds. Zero is tested first:
        MySQL can hold a negative zero, which would render `-0`."""
        t = f"cast({c} as char)"
        return (
            f"case when {c} = 0 then '0' "
            f"when locate('.', {t}) > 0 "
            f"then trim(trailing '.' from trim(trailing '0' from {t})) "
            f"else {t} end"
        )

    def _decimal_text(self, column: Column, c: str) -> str:
        """Render a DECIMAL column at the float scale, exactly or not at all.

        MySQL's `CAST(x AS DECIMAL(p,s))` does not raise when `x` does not fit:
        inside a SELECT, whatever the sql_mode, it *clamps* to the type's
        largest value and leaves only warning 1264. The original
        `decimal(38,6)` therefore turned every DECIMAL(65,0) value of 1e32 or
        more into `99999999999999999999999999999999.999999` (and the negative
        twin), so 1e32 and 5e40 compared equal - to each other, and to a
        PostgreSQL or DuckDB row really holding that maximum. Verified as a
        false match end to end against all three, and again at float_scale 20,
        where decimal(38,20) clamps from 1e18.

        Two changes close it. The cast widens to `decimal(65,s)`, MySQL's
        widest, so values up to 65 - s integer digits now render exactly;
        rendered text does not depend on the cast's precision, so every value
        the old cast handled renders byte-identically and no checksum moves.
        And a value that still does not fit raises instead of clamping. The
        guard compares against the cast's own maximum, 10^(65-s) - 10^-s,
        which is exact: a value under it could only round past it with 66
        significant digits, and a value over it could only round down into it
        with 66, and no MySQL DECIMAL holds more than 65.

        MySQL has no `error()` and a SELECT downgrades most errors to warnings,
        but UUID_TO_BIN on a non-UUID is a hard error (1411) that quotes its
        argument. MySQL cuts that quote at 128 characters, so the argument
        leads with the value (at most 67) and then the column; `query` turns
        the error into one naming the side and explaining the refusal. The
        column name goes in as a hex literal, like the NULL sentinel, so no
        name can break out of it whatever sql_mode does to backslashes.
        """
        s = self.float_scale
        if s > MYSQL_MAX_DECIMAL_SCALE:
            # MySQL itself refuses with error 1425; say so before any query,
            # naming the side and column rather than a generated expression.
            raise self._err(
                f"DECIMAL column {column.name!r} cannot be compared at "
                f"float_scale {s}; MySQL's maximum DECIMAL scale is "
                f"{MYSQL_MAX_DECIMAL_SCALE}"
            )
        widest = "9" * (MYSQL_MAX_DECIMAL_DIGITS - s) + ("." + "9" * s if s else "")
        where = f" in column {column.name!r}".encode()
        return (
            f"case when abs({c}) > {widest} "
            f"then cast(uuid_to_bin(concat('{DECIMAL_RANGE_MARKER}', {c}, "
            f"convert(0x{where.hex()} using utf8mb4))) as char) "
            f"else cast(cast({c} as decimal({MYSQL_MAX_DECIMAL_DIGITS},{s})) "
            f"as char) end"
        )

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count distinct keys by their bytes, never under a collation.

        A hashed key's identity is `concat_ws(char(31), ...)` text in the
        connection collation, utf8mb4_0900_ai_ci by default. That collation is
        case- and accent-blind ('a' = 'A', 'e' = 'é') and treats control
        characters as ignorable, so the separator vanishes and the integer keys
        (5, 944) and (59, 44) both compare as '5944'. Counted under it, unique
        keys were refused as duplicates (RT-E5, RT-E8). The rows themselves are
        matched in Python by exact text, so the count is the only place that
        equality leaked in. A binary string compares byte for byte, with no
        padding. A single integer key is not text and stays as it is.
        """
        if not key.hashed:
            return super().count_distinct_key(key, identity)
        return f"count(distinct cast({identity} as binary))"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `CONV(hex, 16, 10)` works at 64-bit precision, so 15 hex digits (60
        bits) convert exactly, and the result equals PostgreSQL's bit-cast and
        DuckDB's hex-cast for the same input.
        """
        return (
            f"cast(conv(substr(md5({text_expr}), 1, {HASH_HEX_CHARS}), 16, 10) "
            f"as unsigned)"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. `DIV` is MySQL's, and stays exact on
        the DECIMAL operands `wide_int` produces, unlike `/` which returns a
        scaled result."""
        return f"(({numerator}) div ({denominator}))"

    def wide_int(self, expr: str) -> str:
        """Widen past 64 bits before arithmetic. DECIMAL holds up to 65 digits,
        which the key offset cannot overflow."""
        return f"cast(({expr}) as decimal(65,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group.

        DECIMAL(65,0) is MySQL's widest, far beyond any row count times 2^60.
        """
        return f"coalesce(sum(cast(({expr}) as decimal(65,0))), 0)"


def map_type_mysql(raw: str) -> LogicalType:
    """Map a MySQL type name onto a logical category.

    MySQL's `information_schema` reports lower-case bare names (`int`, `bigint`,
    `decimal`, `double`, `varchar`, `datetime`, ...). Note that BOOLEAN is an
    alias for `tinyint`, so a boolean column reads as an integer here - correct
    for MySQL-to-MySQL, and a cross-engine trap worth knowing when comparing a
    MySQL tinyint against a real boolean on another engine.
    """
    t = raw.lower().split("(")[0].strip()
    if t in {
        "int",
        "integer",
        "bigint",
        "smallint",
        "mediumint",
        "tinyint",
        "int unsigned",
        "bigint unsigned",
        "year",
    }:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric", "dec", "fixed"}:
        return LogicalType.DECIMAL
    if t in {"float", "double", "double precision", "real"}:
        return LogicalType.FLOAT
    if t in {"bool", "boolean"}:  # rarely seen; MySQL stores these as tinyint
        return LogicalType.BOOLEAN
    if t in {"date"}:
        return LogicalType.DATE
    if t in {"datetime", "timestamp"}:
        return LogicalType.TIMESTAMP
    if t in {
        "char",
        "varchar",
        "text",
        "tinytext",
        "mediumtext",
        "longtext",
        "enum",
        "set",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
