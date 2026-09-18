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

from typing import Any
from urllib.parse import unquote, urlparse

from parity.dialects.base import HASH_HEX_CHARS, Dialect, sql_literal
from parity.types import Column, LogicalType


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
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            return list(cur.fetchall())
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

        The DECIMAL/FLOAT path matches DuckDB's exactly - cast to
        DECIMAL(38, scale) then to text - so `1.5` becomes `'1.500000'` on all
        three engines. Every branch ends inside the coalesce, so NULL always
        becomes the sentinel.
        """
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as char)"
        elif t in (LogicalType.DECIMAL, LogicalType.FLOAT):
            expr = f"cast(cast({c} as decimal(38,{self.float_scale})) as char)"
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
            expr = f"cast({c} as char)"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

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
