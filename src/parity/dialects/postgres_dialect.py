"""PostgreSQL dialect.

One half of a comparison. Everything here is either an engine-specific spelling
of something in the `Dialect` contract, or a workaround for a place where
PostgreSQL and DuckDB disagree - and every workaround says which.
"""

from __future__ import annotations

from typing import Any

from parity.dialects.base import (
    HASH_HEX_CHARS,
    Dialect,
    sql_literal,
)
from parity.types import Column, LogicalType


class PostgresDialect(Dialect):
    name = "postgres"
    default_schema = "public"

    def connect(self, connection_string: str) -> None:
        """Open a read-only, UTC-pinned, single-snapshot session.

        Four settings, in this order, and the order matters: the timezone is
        set while autocommit is still on so it sticks to the session rather
        than to a transaction that never commits.
        """
        import psycopg

        # psycopg understands postgres:// and postgresql:// URLs directly.
        self._conn = psycopg.connect(connection_string, autocommit=True)
        # Pin the session to UTC before anything else. `timestamptz` renders
        # through the *session* timezone, so two sides whose sessions differ
        # render the same instant as different text and every row with a
        # timestamptz reports as changed - a false positive that looks exactly
        # like catastrophic data loss. A timestamptz is an instant; comparing
        # instants in UTC is both correct and deterministic. Naive `timestamp`
        # columns carry no zone and are unaffected.
        #
        # Done while autocommit is still on, so it applies to the session
        # rather than to a transaction that later rolls back.
        self._conn.execute("set time zone 'UTC'")
        self._conn.autocommit = False
        # Read-only by construction (CLAUDE.md section 6). Enforced by the
        # server, so no bug in query building can write to a user's database.
        self._conn.read_only = True
        # REPEATABLE READ gives the whole diff one snapshot. Under the default
        # READ COMMITTED every statement sees a fresh snapshot, so a table
        # written to during the walk is a different table at each bisection
        # level - and the tool can then report a difference that never existed
        # at any single point in time, or descend into a range that has since
        # changed. The source side of a migration is live by definition, which
        # makes this the normal case rather than an edge case.
        #
        # The cost is one held snapshot for the duration of the diff, which
        # delays vacuuming dead tuples. At tens of seconds that is the same
        # cost as any analytical query, and far cheaper than an untrustworthy
        # verdict.
        self._conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    def cancel(self) -> None:
        """Abort the query currently running on this connection.

        Called from the main thread while a worker is blocked in the driver,
        so it has to be safe across threads.
        """
        # Safe to call from another thread; psycopg opens its own
        # connection to the server to deliver the cancel request.
        self._conn.cancel()

    def close(self) -> None:
        """Close the connection, ending the snapshot the diff was reading."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row. psycopg hands back tuples already."""
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def _exists_but_unreadable(self, schema: str, name: str) -> bool:
        """Is the table really there, with this role simply unable to see it?

        Distinguishes a missing GRANT from a missing table, which arrive as the
        same empty result from `information_schema`.
        """
        # pg_catalog is world-readable, unlike information_schema, which is
        # filtered to what the current role holds privileges on.
        try:
            rows = self.query(
                "select 1 from pg_catalog.pg_class c "
                "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
                f"where n.nspname = {sql_literal(schema)} "
                f"and c.relname = {sql_literal(name)} limit 1"
            )
        except Exception:  # noqa: BLE001 - diagnosing an error must never replace it
            return False
        return bool(rows)

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
            expr = f"({c})::text"
        elif t is LogicalType.FLOAT:
            # PostgreSQL renders these as 'Infinity' / '-Infinity' / 'NaN' on
            # its own, but spelling them explicitly keeps the two engines
            # agreeing by construction rather than by coincidence - DuckDB
            # cannot cast them to DECIMAL at all and needs the same tokens.
            # Note NaN must be found by equality, not `c <> c`: PostgreSQL
            # deliberately treats NaN as equal to itself, unlike IEEE 754.
            expr = (
                f"case when {c} = 'Infinity'::float8 then 'Infinity' "
                f"when {c} = '-Infinity'::float8 then '-Infinity' "
                f"when {c} = 'NaN'::float8 then 'NaN' "
                f"else cast(round(({c})::numeric, {self.float_scale}) as text) end"
            )
        elif t is LogicalType.DECIMAL:
            expr = f"cast(round(({c})::numeric, {self.float_scale}) as text)"
        elif t is LogicalType.BOOLEAN:
            # `else` must not swallow NULL. With `case when c then 'true' else
            # 'false' end` a NULL boolean renders as 'false' - identical to a
            # real FALSE - so the coalesce below never fires and NULL-vs-FALSE
            # reports as a match. Both engines agreed on the wrong answer,
            # which is exactly why the encoding tests plant differences.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"to_char({c}, 'YYYY-MM-DD')"
        elif t is LogicalType.TIMESTAMP:
            expr = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US')"
        else:
            expr = f"({c})::text"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        60 bits, not 64: at 64 PostgreSQL's bit-to-bigint cast wraps negative
        and the two engines stop agreeing.
        """
        return (
            f"(('x' || substr(md5({text_expr}), 1, {HASH_HEX_CHARS}))::bit(60)::bigint)"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """Exact integer division, truncating toward zero."""
        # `div()` is PostgreSQL's exact integer quotient and truncates toward
        # zero, matching Python's `//` for the non-negative operands used here.
        # Plain `/` is right for bigints but silently yields a scaled, rounded
        # result once `wide_int` has promoted an operand to numeric - which is
        # precisely when the bucket boundary has to be exact.
        return f"div(({numerator})::numeric, ({denominator})::numeric)"

    def wide_int(self, expr: str) -> str:
        """Widen an integer past 64 bits, before any arithmetic touches it."""
        # numeric is arbitrary precision: the key offset cannot overflow it.
        return f"(({expr})::numeric)"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group."""
        # numeric is arbitrary precision: cannot overflow no matter the row count.
        return f"coalesce(sum(({expr})::numeric), 0)"
