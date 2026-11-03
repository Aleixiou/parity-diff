"""PostgreSQL dialect.

One half of a comparison. Everything here is either an engine-specific spelling
of something in the `Dialect` contract, or a workaround for a place where
PostgreSQL and DuckDB disagree - and every workaround says which.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_range_where,
    map_type,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType

#: Session settings pinned at connect, each to the server's stock default.
#: A role, database, `PGOPTIONS` or URL `options=-c ...` default can change any
#: of them, and each changes either the canonical text or whether the diff
#: survives at all - so none may be left to whoever configured the login. All
#: are user-settable and set with plain session-level `SET` (never `ALTER`),
#: while autocommit is still on, so they stick to the session rather than to a
#: transaction that never commits (CLAUDE.md section 4.8).
SESSION_PINS: tuple[tuple[str, str], ...] = (
    # `timestamptz` renders through the *session* timezone, so two sides whose
    # sessions differ render the same instant as different text and every row
    # with a timestamptz reports as changed - a false positive that looks
    # exactly like catastrophic data loss. A timestamptz is an instant;
    # comparing instants in UTC is both correct and deterministic. Naive
    # `timestamp` columns carry no zone and are unaffected.
    ("TimeZone", "'UTC'"),
    # FLOAT canonical text casts a double to text and relies on that being the
    # shortest round-trip decimal. PostgreSQL 12+ prints exactly that whenever
    # extra_float_digits > 0 (the default is 1), but a session or role default
    # of 0 or less falls back to 15 significant digits and a value like
    # 1e15+0.5 would render as 1e15.
    ("extra_float_digits", "1"),
    # Off - a role, database or PGOPTIONS default, and the norm before 9.1 -
    # PostgreSQL processes backslash escapes in plain literals, so a literal
    # `'\N'` reads `'N'` and a NULL compared equal to the one-letter string N
    # on the other side (RT-D1). No literal anywhere may mean two things.
    ("standard_conforming_strings", "on"),
    # Unmapped types (interval, money, bytea, arrays of timestamptz, ...)
    # render through `::text`, whose output follows these four. One table read
    # by two sessions with different styles rendered two ways (RT-D5).
    ("IntervalStyle", "'postgres'"),
    ("DateStyle", "'ISO, MDY'"),
    ("bytea_output", "'hex'"),
    ("lc_monetary", "'C'"),
    # psycopg decodes text in the client encoding. SQL_ASCII (a role default
    # or PGCLIENTENCODING) makes it hand back bytes, and the walk died in a
    # bare TypeError naming neither side nor table (RT-D8). UTF8 is what every
    # other engine's text is compared in.
    ("client_encoding", "'UTF8'"),
    # The REPEATABLE READ snapshot is held for the whole diff and sits idle in
    # transaction whenever the *other* side is the slow one - every level,
    # against a cloud warehouse. A role default such as a one-minute
    # idle_in_transaction_session_timeout (common: it protects vacuum) killed
    # the session mid-walk (RT-D13). 0 disables it for this session only.
    ("idle_in_transaction_session_timeout", "0"),
    # Every built-in the canonical text calls - to_char, md5, concat_ws,
    # round, the casts, the `||` operator - is resolved by name through the
    # search_path. pg_catalog is searched first only when the path does not
    # name it, and even then a user function with a *more exact* signature
    # wins: pg_catalog has no `to_char(date, text)` (DATE goes through an
    # implicit cast to timestamptz), so any such function anywhere on the path
    # replaced it and changed the canonical text - a false match between two
    # PostgreSQL tables (RT-D14). Pinned to pg_catalog alone, nothing a user
    # created can be called; schema-qualifying each call instead could not
    # reach operators or the shared row-text SQL. Every table is
    # schema-qualified (`qualify`), so no relation lookup depends on it.
    ("search_path", "pg_catalog"),
)

#: PostgreSQL 17 added `transaction_timeout`, which kills the held snapshot
#: transaction just as idle_in_transaction_session_timeout does (RT-D13).
#: Older servers reject the name, so it is pinned only where it exists.
TRANSACTION_TIMEOUT_FROM = 170000

#: The text an infinite date, timestamp or timestamptz renders as. `to_char`
#: returns SQL NULL for both infinities, which the coalesce turned into the
#: NULL sentinel: +infinity, -infinity and NULL all compared equal - and
#: `infinity` is the idiomatic "valid until forever" end of an SCD row (RT-A5,
#: B11, C9, E3). Lower case because that is exactly what DuckDB's `strftime`
#: prints for its own infinite DATE / TIMESTAMP / TIMESTAMPTZ, so the two agree
#: without the DuckDB rendering, or any DuckDB checksum, moving. FLOAT's
#: `Infinity` is capitalised, but the two never meet: a DATE column is never
#: compared with a FLOAT one. No other supported engine can store either.
DATE_INFINITY = "infinity"
DATE_NEG_INFINITY = "-infinity"

#: Matches the scheme of a PostgreSQL URL in any case. The dialect is chosen
#: case-insensitively (`get_dialect`), but libpq accepts only lower case and
#: otherwise reads the URL as a key/value string - and echoes it, password and
#: all, in its "missing =" error (RT-F2).
_SCHEME = re.compile(r"^(postgres|postgresql)://", re.IGNORECASE)


def connection_secrets(connection_string: str) -> list[str]:
    """Every string in `connection_string` that might be a password.

    Best effort and deliberately generous: it runs on strings libpq has just
    *refused*, so it cannot assume they are well formed, and redacting a
    little too much from an error is harmless where printing a password into
    a CI log is not. Covers the URI's `user:password@`, a `password=` query
    parameter and a key/value `password=` (quoted or not), each raw and
    percent-decoded. A piece of one (libpq may quote only the part before a
    space) is caught by `redact`'s quoted-token rule, not listed here: a
    one-letter piece replaced everywhere would mangle the rest of the error.
    """
    found: list[str] = []
    rest = connection_string.split("://", 1)[1] if "://" in connection_string else ""
    if "@" in rest:
        # The *last* `@` ends the userinfo: an unencoded password may itself
        # hold `@`, `/` or a space, all of which libpq then refuses.
        userinfo = rest[: rest.rfind("@")]
        if ":" in userinfo:
            found.append(userinfo.split(":", 1)[1])
    # A URI query value runs to the next `&` (spaces included: libpq refuses
    # them, and quotes the whole value); a key/value one to the next space,
    # unless it is single-quoted.
    unquoted = r"[^&]*" if rest else r"\S*"
    pattern = r"password\s*=\s*('(?:[^'\\]|\\.)*'?|" + unquoted + ")"
    for m in re.finditer(pattern, connection_string):
        found.append(m.group(1).strip("'"))
    out: list[str] = []
    for value in found:
        for v in (value, unquote(value)):
            v = v.strip()
            if v and v not in out:
                out.append(v)
    # Longest first, so the raw form is replaced before a shorter decoded one.
    return sorted(out, key=len, reverse=True)


def redact(message: str, secrets: list[str]) -> str:
    """`message` with every secret, and every quoted token holding one, removed.

    libpq quotes the token it failed on - sometimes the whole URL, sometimes
    one piece of the password - so a quoted token that contains a secret, or
    is itself a run of three or more characters out of one, goes as well.
    """
    for secret in secrets:
        message = message.replace(secret, "***")

    def token(m: re.Match[str]) -> str:
        """Replace one quoted token if it holds, or is part of, a secret."""
        inner = m.group(1)
        if "***" in inner or (len(inner) >= 3 and any(inner in s for s in secrets)):
            return '"***"'
        return m.group(0)

    return re.sub(r'"([^"]*)"', token, message)


#: information_schema's own rule for `data_type`, re-applied to pg_attribute
#: for relations information_schema does not list (materialized views): a
#: domain reports its base type, an array `ARRAY`, a non-catalog type
#: `USER-DEFINED`, anything else its unmodified `format_type`. Spelling it the
#: same way is what lets `map_type` and the tz-aware check read both alike.
_DATA_TYPE_SQL = (
    "case when t.typtype = 'd' then "
    "case when bt.typelem <> 0 and bt.typlen = -1 then 'ARRAY' "
    "when nbt.nspname = 'pg_catalog' then pg_catalog.format_type(t.typbasetype, null) "
    "else 'USER-DEFINED' end "
    "else case when t.typelem <> 0 and t.typlen = -1 then 'ARRAY' "
    "when nt.nspname = 'pg_catalog' then pg_catalog.format_type(a.atttypid, null) "
    "else 'USER-DEFINED' end end"
)


class PostgresDialect(Dialect):
    name = "postgres"
    default_schema = "public"

    def connect(self, connection_string: str) -> None:
        """Open a read-only, UTC-pinned, single-snapshot session.

        The session pins (`SESSION_PINS`) are set while autocommit is still on
        so they stick to the session rather than to a transaction that never
        commits; only then do read-only and REPEATABLE READ go on.
        """
        import psycopg

        try:
            # psycopg understands postgres:// and postgresql:// URLs directly,
            # but only in lower case - see `_SCHEME`.
            self._conn = psycopg.connect(
                _SCHEME.sub(lambda m: m.group(1).lower() + "://", connection_string),
                autocommit=True,
            )
        except psycopg.Error as exc:
            # libpq quotes the token it could not parse, which for a mistyped
            # URL is the password or the whole URL (RT-F2). This tool runs in
            # CI, where errors land in logs other people read.
            text = str(exc).strip()
            clean = redact(text, connection_secrets(connection_string))
            # The chained driver exception carries the same text, so drop it
            # whenever redaction had to remove something.
            cause = exc if clean == text else None
            raise self._err(f"could not connect: {clean}") from cause
        for setting, value in SESSION_PINS:
            self._conn.execute(f"set {setting} = {value}")
        # PostgreSQL 11 and older print 17 significant digits instead of the
        # shortest repr, so FLOAT columns cannot be rendered canonically there;
        # `normalize` refuses them rather than report rounding-only diffs.
        row = self._conn.execute("show server_version_num").fetchone()
        self._server_version_num = int(row[0]) if row else 0
        if self._server_version_num >= TRANSACTION_TIMEOUT_FROM:
            self._conn.execute("set transaction_timeout = 0")
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
        """Run `sql` and return every row. psycopg hands back tuples already.

        The FLOAT refusal past 2^116 (`normalize`) surfaces as "invalid input
        syntax for type double precision" quoting its own text; re-raise that
        text naming the side, rather than a cast error naming nothing.
        """
        import psycopg

        with self._conn.cursor() as cur:
            try:
                cur.execute(sql)
                return cur.fetchall()
            except psycopg.Error as exc:
                text = str(exc)
                at = text.find(FLOAT_OUT_OF_RANGE_MESSAGE)
                if at < 0:
                    raise
                line = text[at:].splitlines()[0]
                raise self._err(line[:-1] if line.endswith('"') else line) from exc
            except UnicodeDecodeError as exc:
                # client_encoding is pinned to UTF8, but a SQL_ASCII *database*
                # stores bytes unchecked and the server passes them through
                # unconverted; text that is not UTF-8 cannot be compared as
                # text with any other engine.
                raise self._err(
                    f"the server returned text that is not valid UTF-8 ({exc}); "
                    f"is the database encoding SQL_ASCII?"
                ) from exc

    # ------------------------------------------------------------ metadata

    def qualify(self, table: str) -> str:
        """Quote a table name, always schema-qualified.

        An unqualified name is documented to mean `public.<name>`, and that is
        where `columns()` introspects it. Left unqualified in the data queries,
        PostgreSQL resolved it through the session's search_path - `"$user",
        public` by default, so a login with a sandbox schema named after
        itself, or a role default putting another schema first, read one
        table's columns and *another* table's rows, and could call
        `public.orders` identical when it was not (RT-C2, RT-D2). The
        search_path is now pinned to pg_catalog besides (`SESSION_PINS`), so
        an unqualified name would not resolve at all.
        """
        schema, name = self.split_table(table, self.default_schema)
        return f"{self.quote(schema)}.{self.quote(name)}"

    def columns(self, table: str) -> list[Column]:
        """Introspect a table, view, or materialized view.

        `information_schema.columns` - the shared path - does not list
        materialized views at all (they are PostgreSQL-specific, outside the
        SQL standard), so a materialized view used to fall through to "exists
        but this role cannot read it", blaming a GRANT that was there (RT-C3).
        They are ordinary readable relations, so they are introspected from
        pg_attribute instead. Only on that fallback path: a table's
        introspection runs exactly the SQL it always did.
        """
        try:
            return super().columns(table)
        except ValueError as exc:
            try:
                schema, name = self.split_table(table, self.default_schema)
            except ValueError:
                raise exc from None
            found = self._matview_columns(table, schema, name)
            if found is None:
                raise
            return found

    def _matview_columns(self, table: str, schema: str, name: str) -> list[Column] | None:
        """The columns of materialized view `schema.name`, or None if it is not one."""
        rows = self.query(
            f"select a.attname, {_DATA_TYPE_SQL}, "
            "pg_catalog.has_table_privilege(c.oid, 'SELECT') "
            "from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "join pg_catalog.pg_attribute a on a.attrelid = c.oid "
            "join pg_catalog.pg_type t on t.oid = a.atttypid "
            "join pg_catalog.pg_namespace nt on nt.oid = t.typnamespace "
            "left join pg_catalog.pg_type bt on t.typtype = 'd' and bt.oid = t.typbasetype "
            "left join pg_catalog.pg_namespace nbt on nbt.oid = bt.typnamespace "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)} "
            "and c.relkind = 'm' and a.attnum > 0 and not a.attisdropped "
            "order by a.attnum"
        )
        if not rows:
            return None
        if not rows[0][2]:
            raise self._err(
                f"materialized view {table} exists but this role cannot read it. "
                f"Ask for SELECT on {schema}.{name}."
            )
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

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

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count distinct key identities by their bytes, not their collation.

        A hashed key's identity is text, and `::text` keeps the column's
        collation. Under a nondeterministic ICU collation - PostgreSQL's
        standard answer to case-insensitive emails and user names - `count
        (distinct)` merged 'abc' and 'ABC' and refused two distinct rows as a
        duplicate key (RT-B10, RT-D11), though md5 and `fetch_range` keep them
        apart. `"C"` compares bytes, which is what identity means here. An
        integer key has no collation, so it keeps the shared SQL.
        """
        if not key.hashed:
            return super().count_distinct_key(key, identity)
        return self.count(f'distinct ({identity}) collate pg_catalog."C"')

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
            # Widen first: a single-precision column (`real`, FLOAT) is cast to
            # double, which is exact on every engine, so float4 and float8 share
            # one canonical form. Rendered as float4 directly, PostgreSQL and
            # DuckDB print different shortest reprs (-4088288.25 as `-4088288.2`
            # against `-4088288.25`) and MySQL prints only 6 digits. For a
            # double column the cast is a no-op.
            c = f"({c})::float8"
            # PostgreSQL renders these as 'Infinity' / '-Infinity' / 'NaN' on
            # its own, but spelling them explicitly keeps the two engines
            # agreeing by construction rather than by coincidence - DuckDB
            # cannot cast them to DECIMAL at all and needs the same tokens.
            # Note NaN must be found by equality, not `c <> c`: PostgreSQL
            # deliberately treats NaN as equal to itself, unlike IEEE 754.
            #
            # Finite values follow `canonical_float_text` (base.py), not
            # `round(c::numeric, s)`: float8 -> numeric keeps only 15
            # significant digits, so 1e15+0.5 became `...0.000000` while
            # DuckDB and MySQL rendered `...0.500000`. Below 2^53 the text
            # cast is the shortest round-trip decimal - pinned by
            # `extra_float_digits = 1` at connect - which numeric parses
            # exactly (exponent notation included) and `round` rounds half
            # away from zero. From 2^53 every double is an integer, rebuilt
            # exactly from two bigints, because PostgreSQL's shortest-repr
            # differs from the other engines' at rounding boundaries there.
            # Past 2^116 the bigint would overflow: the cast of a non-numeric
            # string raises, naming the value, instead of rounding it.
            # Only a connected dialect knows its server; an unconnected one
            # (SQL generation in tests) is assumed current.
            version = getattr(self, "_server_version_num", 120000)
            if version < 120000:
                raise self._err(
                    f"FLOAT column {column.name!r} needs PostgreSQL 12 or newer "
                    f"(server_version_num {version}): older servers do not "
                    f"print doubles as their shortest round-trip decimal, so "
                    f"they cannot be rendered canonically"
                )
            s = self.float_scale_for(column)
            two53 = f"{FLOAT_EXACT_INT_FROM}::float8"
            hi = f"trunc({c} / {two53})"
            expr = (
                f"case when {c} = 'Infinity'::float8 then 'Infinity' "
                f"when {c} = '-Infinity'::float8 then '-Infinity' "
                f"when {c} = 'NaN'::float8 then 'NaN' "
                f"when abs({c}) < {two53} "
                f"then cast(round(cast(cast({c} as text) as numeric), {s}) as text) "
                f"when abs({c}) < '{FLOAT_MAX_MAGNITUDE_SQL}'::float8 "
                f"then cast(round(cast({hi} as bigint)::numeric * {FLOAT_EXACT_INT_FROM} "
                f"+ cast({c} - {hi} * {two53} as bigint), {s}) as text) "
                f"when {c} is not null then cast(cast('{FLOAT_OUT_OF_RANGE_MESSAGE}' || cast({c} as text) "
                f"|| {sql_literal(float_range_where(column))} as float8) as text) end"
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
            expr = self._datetime_text(c, "-MM-DD")
        elif t is LogicalType.TIMESTAMP:
            expr = self._datetime_text(c, "-MM-DD HH24:MI:SS.US")
        else:
            expr = f"({c})::text"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    @staticmethod
    def _datetime_text(c: str, after_year: str) -> str:
        """Render a date, timestamp or timestamptz, infinities and BC included.

        `to_char(c, 'YYYY' || after_year)` is the AD case, unchanged, so no
        ordinary checksum moves. Two cases it got wrong:

        - **infinity / -infinity**: `to_char` returns SQL NULL, so both
          equalled each other and a real NULL. They render `DATE_INFINITY` /
          `DATE_NEG_INFINITY`, as DuckDB does.
        - **BC**: `YYYY` drops the era, so 44 BC rendered exactly like AD 44
          (RT-A6, C10, E4). The canonical form is DuckDB's astronomical year
          numbering, where 1 BC is year 0 and 44 BC is year -43: `0000` for
          year 0, and for earlier years a minus sign and the *unpadded*
          magnitude (`-43-03-15`, `-4712-01-01`) - exactly what DuckDB's
          `strftime('%Y')` prints, verified against DuckDB 1.5.5 from 4713 BC
          to 1 BC. `to_char`'s BC `YYYY` is the BC year, so the astronomical
          magnitude is that minus one.

        `c < '0001-01-01'` (true for -infinity too, hence the order) is one
        cheap comparison per value; the literal takes the column's type, and
        for a timestamptz it reads as UTC because the session is pinned there.
        """
        year = f"to_char({c}, 'YYYY')"
        return (
            f"case when {c} = 'infinity' then '{DATE_INFINITY}' "
            f"when {c} = '-infinity' then '{DATE_NEG_INFINITY}' "
            f"when {c} < '0001-01-01' then "
            f"case when {year} = '0001' then '0000' "
            f"else '-' || (cast({year} as integer) - 1)::text end "
            f"|| to_char({c}, '{after_year}') "
            f"else to_char({c}, 'YYYY{after_year}') end"
        )

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
