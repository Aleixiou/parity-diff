"""PostgreSQL dialect.

One half of a comparison. Everything here is either an engine-specific spelling
of something in the `Dialect` contract, or a workaround for a place where
PostgreSQL and DuckDB disagree - and every workaround says which.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    INTERVAL_INFINITY,
    INTERVAL_NEG_INFINITY,
    Dialect,
    ShapedList,
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
    # On, `format_type` quotes type names (`"date"`, `"text"`), which then map
    # to no logical type: bogus type warnings, BC dates rendered two ways, and
    # a sample check that died on an infinite date (RT-G7). A role or database
    # default can carry it.
    ("quote_all_identifiers", "off"),
)

#: Oldest server the session pins exist on: 9.6 added
#: idle_in_transaction_session_timeout, and 9.5 refused to connect with an
#: error naming no version (RT-G9).
MIN_SERVER_VERSION = 90600

#: PostgreSQL 17 added `transaction_timeout`, which kills the held snapshot
#: transaction just as idle_in_transaction_session_timeout does (RT-D13).
#: Older servers reject the name, so it is pinned only where it exists.
TRANSACTION_TIMEOUT_FROM = 170000

#: Seconds libpq may spend establishing the connection, unless the URL sets
#: `connect_timeout` itself (RT-HP7).
DEFAULT_CONNECT_TIMEOUT = 30

#: Extension types whose text output is exact - two different values never
#: print alike - so an unmapped column of one compares by its text alone, as
#: before (RT-HP9): citext's is the text; hstore quotes every key and value;
#: ltree's labels are text; the isn types print every digit; cube prints its
#: doubles through float8out (shortest round-trip on 12+, 17 digits under the
#: pin before); PostGIS prints hex EWKB; pgvector prints the shortest
#: round-trip of each float4. Any other base type outside pg_catalog is
#: compared by its binary send function as well, or refused
#: (`PostgresDialect._exact_unmapped`). Matched by type name.
TRUSTED_EXTENSION_TYPES = frozenset(
    {
        "citext",
        "hstore",
        "ltree",
        "lquery",
        "ltxtquery",
        "ean13",
        "isbn",
        "isbn13",
        "ismn",
        "ismn13",
        "issn",
        "issn13",
        "upc",
        "cube",
        "geometry",
        "geography",
        "vector",
        "halfvec",
        "sparsevec",
    }
)

#: The text an infinite date, timestamp or timestamptz renders as. `to_char`
#: returns SQL NULL for both infinities, which the coalesce turned into the
#: NULL sentinel: +infinity, -infinity and NULL all compared equal - and
#: `infinity` is the idiomatic "valid until forever" end of an SCD row (RT-A5,
#: B11, C9, E3). Lower case because that is exactly what DuckDB's `strftime`
#: prints for its own infinite DATE / TIMESTAMP / TIMESTAMPTZ, so the two agree
#: without the DuckDB rendering, or any DuckDB checksum, moving. FLOAT's
#: `Infinity` is capitalised, but the two never meet: a DATE column is never
#: compared with a FLOAT one. No other supported engine can store either.
#: The last date a timestamptz can hold, so the last one `to_char` can render.
DATE_TIMESTAMP_MAX = "294276-12-31"

DATE_INFINITY = "infinity"
DATE_NEG_INFINITY = "-infinity"

#: Functions a row filter may not call (`check_where`): they change a session
#: setting (set_config - RT-HP6), take locks or sleep (a hang mid-walk),
#: notify, touch sequences or large objects, signal other backends, reach
#: another server (dblink), or run SQL given as a string (query_to_xml and
#: friends, which could call any of the others out of sight).
_FILTER_FORBIDDEN = re.compile(
    r"\b(set_config|pg_(?:try_)?advisory\w*|pg_sleep\w*|pg_notify|nextval|setval|"
    r"lo_\w+|dblink\w*|pg_terminate_backend|pg_cancel_backend|"
    r"pg_reload_conf|pg_rotate_logfile\w*|pg_switch_wal|pg_create_restore_point|"
    r"pg_promote|pg_log_backend_memory_contexts|"
    r"query_to_xml\w*|cursor_to_xml\w*|ts_stat|txid_current|"
    r"pg_current_xact_id\w*|pg_export_snapshot)\s*\(",
    re.IGNORECASE,
)

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

        # libpq waits forever by default, and on Windows psycopg retries a
        # refused connect - an unreachable port hung the CLI with no output at
        # all (RT-HP7). A `connect_timeout` in the URL wins.
        extra: dict[str, Any] = {}
        if "connect_timeout" not in connection_string.lower():
            extra["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT
        try:
            # psycopg understands postgres:// and postgresql:// URLs directly,
            # but only in lower case - see `_SCHEME`.
            self._conn = psycopg.connect(
                _SCHEME.sub(lambda m: m.group(1).lower() + "://", connection_string),
                autocommit=True,
                **extra,
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
        # PostgreSQL 11 and older print 17 significant digits instead of the
        # shortest repr, so FLOAT columns cannot be rendered canonically there;
        # `normalize` refuses them rather than report rounding-only diffs.
        row = self._conn.execute("show server_version_num").fetchone()
        self._server_version_num = int(row[0]) if row else 0
        if self._server_version_num < MIN_SERVER_VERSION:
            shown = self._conn.execute("show server_version").fetchone()
            self._conn.close()
            raise self._err(
                f"PostgreSQL {shown[0] if shown else self._server_version_num} is "
                "not supported: parity needs PostgreSQL 9.6 or newer (12 or newer "
                "to compare FLOAT columns)"
            )
        # money stores an integer count of the currency's smallest unit; how
        # many of its digits are decimals is the lc_monetary locale's
        # frac_digits - 2 under the pinned C, 0 for yen. Read under the
        # login's own default, before the pin: rendered under C, 12345 yen
        # read 123.45 and matched a migrated 123.45 (RT-HP2).
        row = self._conn.execute(
            "select pg_catalog.scale('0'::money::numeric), "
            "pg_catalog.current_setting('lc_monetary')"
        ).fetchone()
        self._money_digits = int(row[0]) if row else 2
        self._monetary_locale = str(row[1]) if row else "C"
        for setting, value in SESSION_PINS:
            if setting == "extra_float_digits" and self._server_version_num < 120000:
                # Before 12, 1 means 16 significant digits for a double and 7
                # for a real - not enough to tell 0.1 from the next double, so
                # floats inside unmapped types (point, circle, a composite)
                # that differed past them read identical (RT-HP5). 3 prints
                # 17 and 9, which round-trip. FLOAT columns stay refused there.
                value = "3"
            self._conn.execute(f"set {setting} = {value}")
        if self._server_version_num >= TRANSACTION_TIMEOUT_FROM:
            self._conn.execute("set transaction_timeout = 0")
        # What every pin reads back as, to check after the walk that nothing
        # - a `--where` or a view calling set_config() - moved one (RT-HP6).
        self._pinned = self._read_pins()
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

    def check_where(self, where: str) -> None:
        """Refuse, besides the shared checks, a filter calling a function that
        changes the session or acts outside the read (RT-HP6).

        The filter is pasted into every query, so `set_config()` in it reset a
        pin mid-walk - after `connect` had set it - and a planted FLOAT
        difference read identical. Names are matched outside string literals
        (a quoted identifier is a name too: `"set_config"(...)` calls it), and
        a Unicode-escaped identifier (`U&"..."`), which could spell any of
        them, is refused outright. A user function that calls one is caught
        after the walk instead (`session_drift`).
        """
        super().check_where(where)
        code = re.sub(r"'(?:[^']|'')*'", "''", where)
        if re.search(r'\bu&"', code, re.IGNORECASE):
            raise self._err(
                f"row filter {where!r} contains a Unicode-escaped identifier "
                '(U&"..."); spell the name plainly'
            )
        found = _FILTER_FORBIDDEN.search(code.replace('"', " "))
        if found:
            raise self._err(
                f"row filter {where!r} calls {found.group(1)}(), which changes the "
                "session or acts outside the read - a filter may only select rows"
            )

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

    def _pin_names(self) -> list[str]:
        """Every setting `connect` pinned, transaction_timeout where it exists."""
        names = [s for s, _ in SESSION_PINS]
        if self._server_version_num >= TRANSACTION_TIMEOUT_FROM:
            names.append("transaction_timeout")
        return names

    def _read_pins(self) -> dict[str, str]:
        """The current value of every pinned setting, by name."""
        names = self._pin_names()
        cols = ", ".join(f"pg_catalog.current_setting({sql_literal(n)})" for n in names)
        row = self._conn.execute(f"select {cols}").fetchone()
        return dict(zip(names, (str(v) for v in row or ()), strict=False))

    def session_drift(self) -> list[str]:
        """Pinned settings that no longer hold their pinned value.

        Every query the walk runs pastes the user's row filter, and reads
        views that may call functions; `set_config()` in either rewrote a pin
        mid-walk - extra_float_digits to -15, and two different doubles
        rendered alike (RT-HP6). `check_where` refuses the known culprits by
        name; this is the defence that does not depend on a list. One query,
        after the walk, outside the query count.
        """
        pinned = getattr(self, "_pinned", None)
        if not pinned:
            return []
        now = self._read_pins()
        return [
            f"session setting {name} was {pinned[name]!r} at connect but "
            f"{now.get(name)!r} after the walk: something the queries ran (the "
            "row filter, or a function a view calls) changed it, so values may "
            "have rendered differently from the canonical form"
            for name in pinned
            if now.get(name) != pinned[name]
        ]

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
            cols = self._with_arrays(
                table, self._with_enums(table, super().columns(table))
            )
            self._note_row_security(table)
        except ValueError as exc:
            try:
                schema, name = self.split_table(table, self.default_schema)
            except ValueError:
                raise exc from None
            found = self._matview_columns(table, schema, name)
            if found is None:
                raise
            cols = self._with_arrays(table, found)
        self._note_relation(table, cols)
        return cols

    def _note_relation(self, table: str, cols: list[Column]) -> None:
        """Catalog facts about `table` that the comparison must not ignore.

        - **Unreadable columns** (RT-HP1): under column-level grants
          information_schema lists only the columns the role may read, so a
          differing column it may not was silently left out - a false match.
          pg_attribute lists them all; `unreadable_columns` hands them to the
          engine, which never calls such a run identical.
        - **Inheritance children**: a parent table's rows include its
          children's, as any `select` from it reads them - a caveat, since the
          other side may hold the parent's own rows only. Partitions are the
          point of a partitioned table and draw none.
        - **Unmapped types whose text is not known exact** (RT-HP9): see
          `_untrusted_types`.
        """
        schema, name = self.split_table(table, self.default_schema)
        where = (
            "from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)}"
        )
        rows = self.query(
            "select a.attname "
            f"{where.replace('from ', 'from pg_catalog.pg_attribute a, ', 1)} "
            "and a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped "
            "and not pg_catalog.has_column_privilege(c.oid, a.attnum, 'SELECT') "
            "order by a.attnum"
        )
        self._unreadable = {table: [str(r[0]) for r in rows]}
        rows = self.query(
            "select c.relkind, (select pg_catalog.string_agg(pg_catalog.quote_ident("
            "cn.nspname) || '.' || pg_catalog.quote_ident(ch.relname), ', ' "
            "order by cn.nspname, ch.relname) from pg_catalog.pg_inherits i "
            "join pg_catalog.pg_class ch on ch.oid = i.inhrelid "
            "join pg_catalog.pg_namespace cn on cn.oid = ch.relnamespace "
            f"where i.inhparent = c.oid) {where}"
        )
        self._children = (
            [(table, str(rows[0][1]))]
            if rows and rows[0][0] == "r" and rows[0][1]
            else []
        )
        self._untrusted = self._untrusted_types(schema, name, cols)

    def _untrusted_types(
        self, schema: str, name: str, cols: list[Column]
    ) -> dict[str, tuple[str, str | None]]:
        """Unmapped columns whose type's text may not be exact (RT-HP9).

        Walks each such column's type through domains, array elements,
        composite fields and range subtypes, and finds any base type that is
        neither PostgreSQL's own (pg_catalog) nor in `TRUSTED_EXTENSION_TYPES`.
        Maps the column to (that type, its send function) when the column *is*
        that type, possibly through domains; to (type, None) - refused - when
        it is nested, or has no send function. One catalog query, and only
        when an unmapped column exists.
        """
        unknown = [c.name for c in cols if c.logical_type is LogicalType.UNKNOWN]
        if not unknown:
            return {}
        multirange = (
            "union all select r.rngtypid, true from pg_catalog.pg_range r "
            "where ty.typtype = 'm' and r.rngmultitypid = ty.oid "
            if getattr(self, "_server_version_num", 120000) >= 140000
            else ""
        )
        trusted = ", ".join(sql_literal(t) for t in sorted(TRUSTED_EXTENSION_TYPES))
        rows = self.query(
            "with recursive node(attname, typ, nested) as ("
            "select a.attname, a.atttypid, false from pg_catalog.pg_attribute a "
            "join pg_catalog.pg_class c on c.oid = a.attrelid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)} "
            "and a.attnum > 0 and not a.attisdropped "
            "union select node.attname, ch.typ, node.nested or ch.nested from node "
            "join pg_catalog.pg_type ty on ty.oid = node.typ "
            "cross join lateral ("
            "select ty.typbasetype as typ, false as nested where ty.typtype = 'd' "
            "union all select ty.typelem, true where ty.typtype <> 'd' "
            "and ty.typelem <> 0 and ty.typlen = -1 "
            "union all select ca.atttypid, true from pg_catalog.pg_attribute ca "
            "where ty.typtype = 'c' and ca.attrelid = ty.typrelid "
            "and ca.attnum > 0 and not ca.attisdropped "
            "union all select r.rngsubtype, true from pg_catalog.pg_range r "
            f"where ty.typtype = 'r' and r.rngtypid = ty.oid {multirange}"
            ") ch) "
            "select node.attname, bool_or(node.nested), "
            "min(pg_catalog.format_type(ty.oid, null)), "
            "min(case when not node.nested and ty.typsend <> 0 then "
            "pg_catalog.quote_ident(pn.nspname) || '.' || pg_catalog.quote_ident(p.proname) end) "
            "from node join pg_catalog.pg_type ty on ty.oid = node.typ "
            "join pg_catalog.pg_namespace tn on tn.oid = ty.typnamespace "
            "left join pg_catalog.pg_proc p on p.oid = ty.typsend "
            "left join pg_catalog.pg_namespace pn on pn.oid = p.pronamespace "
            "where ty.typtype = 'b' and tn.nspname <> 'pg_catalog' "
            "and not (ty.typelem <> 0 and ty.typlen = -1) "
            f"and ty.typname not in ({trusted}) "
            "group by node.attname"
        )
        out: dict[str, tuple[str, str | None]] = {}
        for attname, nested, typ, send in rows:
            if str(attname) in unknown:
                out[str(attname)] = (str(typ), None if nested or not send else str(send))
        return out

    def unreadable_columns(self, table: str) -> list[str]:
        """Columns of `table` this login lacks SELECT on (`_note_relation`)."""
        return list(getattr(self, "_unreadable", {}).get(table, []))

    def _note_row_security(self, table: str) -> None:
        """Remember a table whose rows a row-level security policy filters.

        RLS silently hides rows from the reading role: two tables differing
        only in hidden rows read identical over the rows that remain (RT-G6).
        It applies when it is enabled and the role neither bypasses it
        (superuser, BYPASSRLS) nor owns the table without FORCE.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select c.relrowsecurity and not (r.rolsuper or r.rolbypassrls) "
            "and (c.relforcerowsecurity or c.relowner <> r.oid), c.relkind "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "join pg_roles r on r.rolname = current_user "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)}"
        )
        if rows and rows[0][0]:
            self._row_security = [*getattr(self, "_row_security", []), table]
        elif rows and rows[0][1] == "v":
            self._note_view_row_security(table, schema, name)

    def _note_view_row_security(self, table: str, schema: str, name: str) -> None:
        """Remember a view that reads, at any depth, a table RLS may filter.

        A view's rows are its tables' rows as *some* role sees them - the
        reader's for a `security_invoker` view, the view owner's otherwise -
        and a view over an RLS table hid rows with no caveat (RT-HP4). Every
        relation the view's rewrite rule depends on is followed, through
        nested views; the caveat fires when a reached table has RLS enabled
        and it applies to the reader or to any owner of a view on the way.
        That is a superset of the roles PostgreSQL actually checks, so it can
        warn needlessly, never miss.
        """
        rows = self.query(
            "with recursive dep(oid) as ("
            "select c.oid from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)} "
            "union select d.refobjid from dep "
            "join pg_catalog.pg_rewrite w on w.ev_class = dep.oid "
            "join pg_catalog.pg_depend d on d.classid = 'pg_catalog.pg_rewrite'::pg_catalog.regclass "
            "and d.objid = w.oid and d.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass "
            "and d.refobjid <> dep.oid), "
            "who(oid) as (select r.oid from pg_catalog.pg_roles r where r.rolname = current_user "
            "union select c.relowner from dep join pg_catalog.pg_class c on c.oid = dep.oid "
            "where c.relkind = 'v') "
            "select pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(c.relname) "
            "from dep join pg_catalog.pg_class c on c.oid = dep.oid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "where c.relrowsecurity and exists (select 1 from who "
            "join pg_catalog.pg_roles r on r.oid = who.oid "
            "where not (r.rolsuper or r.rolbypassrls) "
            "and (c.relforcerowsecurity or c.relowner <> r.oid)) "
            "order by 1"
        )
        if rows:
            under = ", ".join(str(r[0]) for r in rows)
            self._row_security = [
                *getattr(self, "_row_security", []),
                f"{table} (a view over {under})",
            ]

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """Row-level security that filters what this side can read."""
        out = list(super().caveats(columns))
        for table in getattr(self, "_row_security", []):
            verb = "may filter" if "(a view over" in table else "filters"
            out.append(
                f"row-level security {verb} {table} for this role: rows its "
                "policies hide are not compared at all, so a difference there "
                "cannot be seen. Diff as a role that bypasses RLS (BYPASSRLS) "
                "for a complete result"
            )
        for table, children in getattr(self, "_children", []):
            out.append(
                f"{table} has inheritance children ({children}) and its rows "
                "include theirs, as any query on it reads them; compare against a "
                "side that holds the same rows, or diff each table on its own"
            )
        money = [c.name for c in columns if self._is_money(c)]
        if money and getattr(self, "_money_digits", 2) != 2:
            out.append(
                f"money columns {money} are read with {self._money_digits} decimal "
                f"place(s), the frac_digits of this login's lc_monetary "
                f"{self._monetary_locale!r}; money stores whole minor units, so a "
                "value written under a locale with other decimals reads scaled"
            )
        return out

    @staticmethod
    def _is_money(column: Column) -> bool:
        """A money column, or a list of money (whose elements render as one)."""
        raw = column.raw_type.lower()
        return raw == "money" or raw == "money[]"

    def _money_factor(self) -> Decimal | None:
        """What a money value read as numeric under the pinned C locale (two
        decimals) must be multiplied by to carry the login's own frac_digits,
        or None when they are two - the SQL then is exactly what it was."""
        digits = getattr(self, "_money_digits", 2)
        return None if digits == 2 else Decimal(10) ** (2 - digits)

    def money_numeric(self, c: str) -> str:
        """A money expression `c` as exact numeric, at the login's frac_digits
        (RT-HP2). `(c)::numeric` - the old SQL, byte for byte - at two."""
        factor = self._money_factor()
        if factor is None:
            return f"({c})::numeric"
        return f"(({c})::numeric * {format(factor, 'f')})"

    def _with_enums(self, table: str, columns: list[Column]) -> list[Column]:
        """Map enum columns to STRING.

        information_schema reports every user-defined type as `USER-DEFINED`
        - an enum, a composite, an extension's type - so `map_type` cannot
        tell an enum from the rest. An enum's `::text` is its label, exactly
        what a varchar holding it renders, so it is compared as text (the
        rendering, and its checksum, is what the unmapped fallback already
        produced); the other user-defined types stay unmapped. One catalog
        query, and only when a column is user-defined at all.
        """
        if not any(c.raw_type == "USER-DEFINED" for c in columns):
            return columns
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select a.attname from pg_catalog.pg_attribute a "
            "join pg_catalog.pg_class c on c.oid = a.attrelid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "join pg_catalog.pg_type t on t.oid = a.atttypid "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)} "
            "and a.attnum > 0 and not a.attisdropped and t.typtype = 'e'"
        )
        enums = {str(r[0]) for r in rows}
        return [
            Column(c.name, LogicalType.STRING, "enum", c.tz_aware)
            if c.raw_type == "USER-DEFINED" and c.name in enums
            else c
            for c in columns
        ]

    def _with_arrays(self, table: str, columns: list[Column]) -> list[Column]:
        """Give every array column its element type: `integer[]`, not `ARRAY`.

        information_schema reports every array as `ARRAY`, so the element - and
        with it the LIST rule each element is rendered by - comes from the
        catalog: `format_type` of the element type with the column's typmod
        (`character varying(10)[]`), through a domain to its base array. With
        search_path pinned to pg_catalog, a user-defined element type prints
        schema-qualified and maps to UNKNOWN, so its array stays unmapped and
        is compared as before. One catalog query, only when an array exists.
        """
        if not any(c.raw_type == "ARRAY" for c in columns):
            return columns
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select a.attname, pg_catalog.format_type(e.oid, case when t.typtype = 'd' "
            "then t.typtypmod else a.atttypmod end) "
            "from pg_catalog.pg_attribute a "
            "join pg_catalog.pg_class c on c.oid = a.attrelid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "join pg_catalog.pg_type t on t.oid = a.atttypid "
            "left join pg_catalog.pg_type bt on t.typtype = 'd' and bt.oid = t.typbasetype "
            "join pg_catalog.pg_type e on e.oid = coalesce(bt.typelem, t.typelem) "
            f"where n.nspname = {sql_literal(schema)} and c.relname = {sql_literal(name)} "
            "and a.attnum > 0 and not a.attisdropped and e.oid <> 0"
        )
        elements = {str(r[0]): str(r[1]) for r in rows}
        out = []
        for c in columns:
            if c.raw_type == "ARRAY" and c.name in elements:
                raw = elements[c.name] + "[]"
                c = Column(c.name, map_type(raw), raw, c.tz_aware)
            out.append(c)
        return out

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
            num = self.money_numeric(c) if self._is_money(column) else f"({c})::numeric"
            expr = f"cast(round({num}, {self.float_scale}) as text)"
        elif t is LogicalType.BOOLEAN:
            # `else` must not swallow NULL. With `case when c then 'true' else
            # 'false' end` a NULL boolean renders as 'false' - identical to a
            # real FALSE - so the coalesce below never fires and NULL-vs-FALSE
            # reports as a match. Both engines agreed on the wrong answer,
            # which is exactly why the encoding tests plant differences.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            # `to_char` has no date overload: a date goes through timestamptz,
            # whose range ends at 294276-12-31, and a later date - valid up to
            # 5874897 AD in PostgreSQL and DuckDB alike - killed the whole diff
            # (RT-HP3). Past that, the date's own text, which under the pinned
            # DateStyle ISO is `YYYYYY-MM-DD`, what to_char prints for the
            # dates it can reach. Earlier dates keep the old SQL's text.
            expr = (
                f"case when {c} > '{DATE_TIMESTAMP_MAX}'::date and isfinite({c}) "
                f"then ({c})::text else {self._datetime_text(c, '-MM-DD')} end"
            )
        elif t is LogicalType.TIMESTAMP:
            expr = self._datetime_text(c, "-MM-DD HH24:MI:SS.US")
        elif t is LogicalType.TIME:
            # `to_char` has no time overload; time casts implicitly to
            # interval, whose HH24 runs past 23, so `24:00:00` - which time
            # accepts - renders `24:00:00.000000`, not the next midnight.
            # Six digits always, as TIMESTAMP (`::text` drops them: `12:34:56.5`).
            expr = f"to_char({c}, 'HH24:MI:SS.US')"
        elif t is LogicalType.BINARY:
            # BINARY_PREFIX + lower-case hex, whatever bytea_output says. Byte
            # for byte what the old `::text` fallback produced (escaped, with
            # bytea_output pinned to hex), so no bytea checksum moves.
            expr = self.concat_sql(self.binary_prefix_sql(), f"encode({c}, 'hex')")
        elif t is LogicalType.BITS:
            # bit(n) and varbit print their digits at full width, from 0 and
            # 1 only - the bytes the escaped `::text` fallback produced.
            expr = f"({c})::text"
        elif t is LogicalType.INET:
            # `::text` is `text(inet)`, which always prints the prefix
            # (`10.0.0.1/32`; the output function drops a full one), with
            # IPv6 compressed by BIND's rules - `canonical_inet_text`. The
            # fallback's bytes exactly, so no checksum moves.
            expr = f"({c})::text"
        elif t is LogicalType.INTERVAL:
            expr = self._interval_text(c)
        elif t is LogicalType.TIMETZ:
            expr = self._timetz_text(c)
        elif t is LogicalType.LIST:
            expr = self._list_text(column, c)
        elif t is LogicalType.JSON:
            # jsonb is PostgreSQL's normal form - insignificant whitespace
            # gone, keys ordered by length then bytes, the last duplicate kept
            # - and its text is exactly the canonical layout and string
            # escaping (verified against `canonical_json_text`). A `json`
            # column is cast to it, so two json values that differ only in
            # spacing or key order are equal, as two jsonb values always
            # were. A json holding the escape for U+0000, which jsonb cannot
            # store, makes the cast raise - loudly, naming side and table.
            expr = self.escape_text(f"({c})::jsonb::text")
        else:
            # STRING, and every unmapped type (xml, ranges, composites, ...)
            # through its text output: arbitrary text, so escaped.
            expr = self.escape_text(f"({c})::text")
            untrusted = getattr(self, "_untrusted", {}).get(column.name)
            if t is LogicalType.UNKNOWN and untrusted:
                expr = self._exact_unmapped(column, c, expr, untrusted)
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def _exact_unmapped(
        self, column: Column, c: str, text: str, untrusted: tuple[str, str | None]
    ) -> str:
        r"""An unmapped column whose type's text is not known to be exact.

        `::text` is exact for PostgreSQL's own types under the pins, and for a
        short list of extensions (`TRUSTED_EXTENSION_TYPES`), but an
        extension's output function may round: contrib `seg` prints
        123456789 and 123456788 both as `1.23457e+08`, which PostgreSQL's own
        `=` calls different - a false match between two PostgreSQL tables
        (RT-HP9). The type's binary send function is its exact value, so
        `\b` and its hex follow the text (escaped text never holds a lone
        backslash, so the pair cannot be forged). Such a type without a send
        function, or one nested in an array, composite or range - where the
        container's send would embed type OIDs that differ between databases
        - is refused instead.
        """
        typ, send = untrusted
        if send is None:
            raise self._err(
                f"column {column.name!r} holds {typ}, whose text output is not "
                "known to be exact (two different values can print alike) and "
                "which this server cannot compare any other way (no binary send "
                "function, or nested in an array, composite or range); leave it "
                "out with --exclude, or compare an exact cast of it through a view"
            )
        return self.concat_sql(
            self.concat_sql(text, self.concat_sql(self.backslash_sql(), "'b'")),
            f"encode({send}({c}), 'hex')",
        )

    def key_not_null_sql(self, column: Column) -> str:
        """SQL NULL, not "every field NULL": `num_nulls`.

        For a composite, PostgreSQL's `c is not null` is true only when every
        field is non-NULL, so the key row(1, NULL) was refused as a NULL key
        (RT-HP8). `num_nulls` asks whether the value itself is NULL, for any
        type; other types keep the shared SQL.
        """
        if column.logical_type is LogicalType.UNKNOWN:
            return f"pg_catalog.num_nulls({self.quote(column.name)}) = 0"
        return super().key_not_null_sql(column)

    @staticmethod
    def _interval_text(c: str) -> str:
        """An interval's months, days and microseconds - `canonical_interval_text`.

        PostgreSQL stores exactly those three and `extract` returns each
        exactly: year and month split the months (truncating, so both carry
        its sign), and hour, minute and microseconds split the time part the
        same way, so their weighted sum is it. `::numeric` before the
        arithmetic, because before 14 `extract` returns double precision -
        each part is a small exact integer there, but 2562047788 hours in
        microseconds is not. The `::text` fallback printed `1 day` and
        `24:00:00`; that text depends on IntervalStyle, this does not.
        PostgreSQL 17's infinite intervals render as the date infinities.
        """

        def part(field: str) -> str:
            """One extracted field, exact."""
            return f"extract({field} from {c})::numeric"

        months = f"({part('year')} * 12 + {part('month')})::bigint::text"
        days = f"({part('day')})::bigint::text"
        micros = (
            f"({part('hour')} * 3600000000 + {part('minute')} * 60000000 "
            f"+ {part('microseconds')})::bigint::text"
        )
        return (
            f"case when not isfinite({c}) then case when {c} > interval '0' "
            f"then '{INTERVAL_INFINITY}' else '{INTERVAL_NEG_INFINITY}' end "
            f"else {months} || ' mon ' || {days} || ' day ' || {micros} || ' us' end"
        )

    @staticmethod
    def _timetz_text(c: str) -> str:
        """Local time and offset - `canonical_timetz_text`.

        `::time` keeps the local clock (`24:00:00` included) and renders as
        the TIME rule does; `extract(timezone)` is the offset in seconds east
        of UTC, which PostgreSQL holds to the second. The fallback's text
        (`12:00:00+05:45`) dropped the fraction's trailing zeros and the
        offset's zero minutes.
        """
        off = f"extract(timezone from {c})::integer"
        mag = f"abs({off})"
        return (
            f"to_char(({c})::time, 'HH24:MI:SS.US') "
            f"|| case when {off} < 0 then '-' else '+' end "
            f"|| lpad(({mag} / 3600)::text, 2, '0') || ':' "
            f"|| lpad(({mag} % 3600 / 60)::text, 2, '0') "
            f"|| case when {mag} % 60 <> 0 then ':' || lpad(({mag} % 60)::text, 2, '0') "
            "else '' end"
        )

    def _list_text(self, column: Column, c: str) -> str:
        """An array as `canonical_list_text`: each element by its own rule.

        `unnest ... with ordinality` walks the elements in storage order, each
        is rendered by its element type's `normalize` (the alias stands in for
        the column) and followed by the terminator, and `string_agg` joins
        them; an empty array aggregates to NULL, which is `[]`. An array with
        more than one dimension, or whose lower bound is not 1, holds its
        shape as part of its value - `'{{1,2},{3,4}}'` is not `'{1,2,3,4}'`,
        nor `'[0:1]={1,2}'` `'{1,2}'` - and `unnest` flattens it, so its
        `array_dims` come first, between `LIST_DIMS_MARK`s. The fallback
        printed `{a,b}` and `{"a,b"}`, whose quoting no other engine shares.
        """
        element = self.list_element_text(column)
        ordinal = self.quote("__parity_ordinal")
        agg = (
            f"coalesce((select string_agg("
            f"{self.concat_sql(element, self.list_terminator_sql())}, '' order by {ordinal}) "
            f"from unnest({c}) with ordinality "
            f"as {self.quote('__parity_list')}({self.quote(self.LIST_ELEMENT_NAME)}, {ordinal})), '')"
        )
        mark = self.list_dims_mark_sql()
        return (
            f"case when {c} is null then null "
            f"when array_ndims({c}) > 1 or array_lower({c}, 1) <> 1 "
            f"then '[' || {mark} || array_dims({c}) || {mark} || {agg} || ']' "
            f"else '[' || {agg} || ']' end"
        )

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

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check; dates and timestamps as JSON text.

        psycopg cannot load `infinity` or a BC date into a Python date (it
        raises, which would end the whole sample), and the text output of
        `::text` follows the session DateStyle. JSON output is ISO 8601
        whatever DateStyle says and spells infinities and BC years plainly
        (`infinity`, `0044-03-15 BC`, `2024-06-15T06:15:00+00:00` under the
        pinned UTC), which `parity.reference` reads. Verified live.
        """
        c = self.quote(column.name)
        if column.logical_type in (LogicalType.DATE, LogicalType.TIMESTAMP):
            return f"to_json({c}) #>> '{{}}'"
        if column.logical_type is LogicalType.TIME:
            # psycopg cannot load `24:00:00` into a datetime.time (verified:
            # "hour must be in 0..23"), which would end the whole sample.
            return f"({c})::text"
        if column.logical_type is LogicalType.JSON:
            # psycopg parses json into Python objects, turning 1.50 into the
            # float 1.5; jsonb's own text keeps every number as stored, and
            # the reference lays it out and escapes it itself.
            return f"({c})::jsonb::text"
        if column.logical_type in (LogicalType.INTERVAL, LogicalType.TIMETZ):
            # psycopg folds a month into 30 days (`1 mon` and `30 days` both
            # load as timedelta(30)) and cannot load a timetz of 24:00:00;
            # the text, under the pinned IntervalStyle, carries both whole.
            return f"({c})::text"
        if column.logical_type is LogicalType.LIST:
            # As the array's text, which `sample_value` parses: psycopg loads
            # a multi-dimensional array as nested lists and drops its lower
            # bounds (`'[0:1]={1,2}'` arrives as [1, 2]), and fails on the
            # same element values its scalar loaders fail on (infinity, BC,
            # 24:00:00, a month). money elements go through numeric, whose
            # text is not locale-formatted.
            if self.list_element(column).raw_type.lower() == "money":
                return f"({c})::numeric[]::text"
            return f"({c})::text"
        if column.raw_type.lower() == "money":
            # psycopg returns money as its locale text (`$1,234.56`).
            return self.money_numeric(c)
        return super().sample_select(column)

    def sample_value(self, column: Column, value: Any) -> Any:
        """An array's text (`sample_select`) as its elements, for the
        reference: a list, or a `ShapedList` when it has more than one
        dimension or a lower bound other than 1. Elements stay text - the
        reference reads every element type's text - except booleans (`t`,
        `f`) and bytea (`\\x00ff`), whose text form the reference does not
        guess at."""
        if column.logical_type is not LogicalType.LIST or not isinstance(value, str):
            return value
        dims, texts = parse_pg_array(value)
        kind = self.list_element(column).logical_type
        items: list[Any] = list(texts)
        if kind is LogicalType.BOOLEAN:
            items = [None if x is None else x == "t" for x in texts]
        elif kind is LogicalType.BINARY:
            items = [None if x is None else bytes.fromhex(x[2:]) for x in texts]
        elif self._is_money(column) and (factor := self._money_factor()) is not None:
            # As `money_numeric` scales each element of the walk (RT-HP2).
            items = [None if x is None else str(Decimal(x) * factor) for x in texts]
        return items if dims is None else ShapedList(dims, tuple(items))

    def json_semantics(self, column: Column) -> str:
        """jsonb's normal form, which a json column is cast to."""
        return (
            "PostgreSQL jsonb: object keys ordered by length then bytes, the last "
            "duplicate key kept, numbers as the exact decimals written (1.50 stays "
            "1.50, 1e2 becomes 100)"
        )

    def escape_text(self, expr: str) -> str:
        """Escape under the C collation, whatever the column's own.

        `replace()` raises "nondeterministic collations are not supported for
        substring searches" on a column with a case-insensitive ICU collation;
        under "C" it compares code points and the bytes are unchanged.
        """
        return super().escape_text(f'(({expr}) collate "C")')

    def decimal_key_text(self, c: str, column: Column) -> str:
        """numeric's own text is exact and plain (never exponent notation);
        drop the trailing fractional zeros its scale adds. Through `::numeric`
        first, a no-op for numeric, because a `money` key's own text is its
        locale format (`$1,234.56`)."""
        num = self.money_numeric(c) if self._is_money(column) else f"({c})::numeric"
        t = f"{num}::text"
        return (
            f"case when {num} = 0 then '0' "
            f"when strpos({t}, '.') > 0 then rtrim(rtrim({t}, '0'), '.') "
            f"else {t} end"
        )

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        60 bits, not 64: at 64 PostgreSQL's bit-to-bigint cast wraps negative
        and the two engines stop agreeing.
        """
        # md5(text) hashes the *server encoding's* bytes. In a LATIN1 or
        # WIN1252 database that made the mojibake 'Ã©' hash like UTF-8 'é'
        # elsewhere (a false match) and a correct 'é' never match (RT-G4).
        # convert_to(..., 'UTF8') hashes UTF-8 bytes everywhere - the identity
        # in a UTF-8 database, so no checksum moves there.
        utf8 = f"convert_to({text_expr}, 'UTF8')"
        return f"(('x' || substr(md5({utf8}), 1, {HASH_HEX_CHARS}))::bit(60)::bigint)"

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


def parse_pg_array(text: str) -> tuple[str | None, list[str | None]]:
    """(dimensions, elements) of a PostgreSQL array's text output.

    `{a,"b c",NULL,"NULL"}` is four elements, the third SQL NULL; a quoted
    element undoes `\\` and `\"`. Nested braces are dimensions, read in
    storage order; a `[l:u]...=` prefix (printed only when a lower bound is
    not 1) is the dimensions as `array_dims` spells them. Returns None for
    the dimensions of a one-dimensional array from 1 - the plain list - and
    computes them from the nesting for a multi-dimensional one whose bounds
    PostgreSQL left out.
    """
    dims: str | None = None
    if text.startswith("["):
        dims, text = text.split("=", 1)
    items: list[str | None] = []
    lengths: dict[int, int] = {}
    i = 0

    def group(depth: int) -> None:
        """One `{...}`, from `text[i]`; elements appended to `items`."""
        nonlocal i
        i += 1  # the opening brace
        count = 0
        while text[i] != "}":
            if text[i] == "{":
                group(depth + 1)
            elif text[i] == '"':
                i += 1
                out = []
                while text[i] != '"':
                    if text[i] == "\\":
                        i += 1
                    out.append(text[i])
                    i += 1
                i += 1
                items.append("".join(out))
            else:
                end = i
                while text[end] not in ",}":
                    end += 1
                raw = text[i:end]
                items.append(None if raw.upper() == "NULL" else raw)
                i = end
            count += 1
            if text[i] == ",":
                i += 1
        i += 1  # the closing brace
        lengths[depth] = count

    group(0)
    if dims is None and len(lengths) > 1 and items:
        dims = "".join(f"[1:{lengths[d]}]" for d in sorted(lengths))
    return dims, items
