"""DuckDB dialect.

The other half of a comparison, and the shortest complete example of the
`Dialect` contract - a good place to start when writing a new one.
"""

from __future__ import annotations

import os
from typing import Any

from parity.dialects.base import HASH_HEX_CHARS, Dialect
from parity.types import Column, LogicalType


def duckdb_path(connection_string: str) -> str:
    """Extract the database path from a ``duckdb://`` connection string.

    Following the sqlite/SQLAlchemy convention, the slashes carry meaning:

        duckdb:///relative/path.db   ->  relative/path.db
        duckdb:////var/lib/w.db      ->  /var/lib/w.db     (absolute, POSIX)
        duckdb:///C:/data/w.db       ->  C:/data/w.db      (absolute, Windows)
        duckdb:///:memory:           ->  :memory:

    So exactly *one* leading slash comes off - the one separating the empty
    authority from the path. Stripping them all (``lstrip("/")``) quietly turned
    every absolute POSIX path into a relative one, and the tool then reported
    "database file not found" for a file that was plainly there. Windows hid it
    completely, because its paths start with a drive letter and so carry only
    one leading slash to begin with; it took a Linux CI run to surface.
    """
    rest = connection_string.split("://", 1)[1]
    return rest.removeprefix("/")


class DuckDBDialect(Dialect):
    name = "duckdb"
    default_schema = "main"

    def connect(self, connection_string: str) -> None:
        """Open the database file read-only, pinned to UTC.

        Read-only is enforced at the connection, so no bug in query building
        can write to a user's data. An in-memory database is the exception:
        it holds nothing to protect and read-only would make it permanently
        empty.
        """
        import duckdb

        path = duckdb_path(connection_string)
        if not path or path == ":memory:":
            # An in-memory database holds no user data to protect, and a
            # read-only in-memory database is empty by definition.
            self._conn = duckdb.connect(":memory:")
            self._pin_utc()
            return
        if not os.path.exists(path):
            # read_only=True on a missing path fails with a driver-level error
            # that does not say which side or which file. Say it ourselves.
            raise self._err(f"database file not found: {path}")
        # CLAUDE.md section 6: read-only by construction. Enforcing it at the
        # connection means no bug in query building can ever write to a user's
        # database. It also lets several parity runs share one file.
        self._conn = duckdb.connect(path, read_only=True)
        self._pin_utc()

    def _pin_utc(self) -> None:
        """Render `timestamptz` in UTC regardless of the machine's timezone.

        A timestamptz renders through the session timezone, so two sides whose
        sessions differ turn the same instant into different text and every row
        holding one reports as changed - a false positive indistinguishable
        from catastrophic data loss. A timestamptz is an instant; comparing
        instants in UTC is correct and deterministic. Naive `timestamp` columns
        carry no zone and are unaffected.
        """
        self._conn.execute("set TimeZone='UTC'")

    def cancel(self) -> None:
        """Abort the query currently running, from another thread."""
        self._conn.interrupt()

    def close(self) -> None:
        """Close the connection and release the file."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a list of tuples."""
        return self._conn.execute(sql).fetchall()

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in double quotes, doubling any it contains.

        This is the injection boundary: table and column names arrive from the
        command line and reach SQL through here.
        """
        return '"' + identifier.replace('"', '""') + '"'

    # ----------------------------------------------------------- rendering

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        The contract: two rows are equal if and only if this text is
        byte-identical to the other engine's. Every branch ends up inside the
        `coalesce` at the bottom, so a NULL always becomes the sentinel.
        """
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as varchar)"
        elif t is LogicalType.FLOAT:
            # Infinity and NaN cannot be cast to DECIMAL - DuckDB raises
            # "Could not cast value inf to DECIMAL(38,6)" and the whole diff
            # dies. They are ordinary in float columns (any division by zero
            # produces one), so render them as fixed tokens that PostgreSQL
            # spells the same way.
            expr = (
                f"case when isinf({c}) then (case when {c} > 0 then 'Infinity' "
                f"else '-Infinity' end) "
                f"when isnan({c}) then 'NaN' "
                f"else cast(cast({c} as decimal(38,{self.float_scale})) as varchar) end"
            )
        elif t is LogicalType.DECIMAL:
            expr = f"cast(cast({c} as decimal(38,{self.float_scale})) as varchar)"
        elif t is LogicalType.BOOLEAN:
            # `else` must not swallow NULL. With `case when c then 'true' else
            # 'false' end` a NULL boolean renders as 'false' - identical to a
            # real FALSE - so the coalesce below never fires and NULL-vs-FALSE
            # reports as a match. Both engines agreed on the wrong answer,
            # which is exactly why the encoding tests plant differences.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"strftime({c}, '%Y-%m-%d')"
        elif t is LogicalType.TIMESTAMP:
            expr = f"strftime({c}, '%Y-%m-%d %H:%M:%S.%f')"
        else:
            expr = f"cast({c} as varchar)"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        Must produce the same number PostgreSQL does for the same input -
        648541476951500027 for 'abc', which a test pins.
        """
        return f"cast(('0x' || substr(md5({text_expr}), 1, {HASH_HEX_CHARS})) as bigint)"

    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division."""
        # `//` is DuckDB's truncating integer division. Plain `/` would promote
        # to DOUBLE and silently lose precision on large key ranges.
        return f"(({numerator}) // ({denominator}))"

    def wide_int(self, expr: str) -> str:
        """Widen an integer past 64 bits, before any arithmetic touches it."""
        # hugeint is 128-bit, which the key offset cannot overflow.
        return f"cast(({expr}) as hugeint)"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group."""
        # DECIMAL(38,0) holds sums far beyond any realistic row count * 2^60.
        return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"
