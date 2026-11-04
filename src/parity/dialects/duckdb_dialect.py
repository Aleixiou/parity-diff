"""DuckDB dialect.

The other half of a comparison, and the shortest complete example of the
`Dialect` contract - a good place to start when writing a new one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    escape_canonical_text,
    float_range_where,
    float_tiny_sql,
    float_zero_text,
    map_type,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType

#: The accepted spellings, for the error that explains them.
DUCKDB_URL_FORMS = (
    "duckdb:///relative/path.duckdb, duckdb:////absolute/posix/path.duckdb, "
    "duckdb:///C:/windows/path.duckdb or duckdb:///:memory:"
)


def _redact(path: str) -> str:
    """A path for an error message, its query string cut off: a URL's `?...`
    can carry a token (MotherDuck's `motherduck_token=`, RT-HD3)."""
    at = path.find("?")
    return path if at < 0 else path[:at] + "?<redacted>"


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
    head, sep, rest = connection_string.partition("://")
    if not sep:
        # `duckdb:/path` or `duckdb:path` used to die on an IndexError whose
        # text ("list index out of range") explained nothing (RT-HD10).
        raise ValueError(
            f"not a DuckDB connection string: {_redact(connection_string)!r}. "
            f"Expected {DUCKDB_URL_FORMS}"
        )
    return rest.removeprefix("/")


#: Oldest DuckDB whose rendering is verified (RT-G10, G11, G14 below it).
MIN_DUCKDB = (1, 3)


def _version_tuple(version: str) -> tuple[int, ...]:
    """`1.5.5` -> (1, 5, 5); a suffix such as `.dev12` is ignored."""
    out = []
    for part in version.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


#: Types the sample check reads as DuckDB's own text (see `sample_select`):
#: the driver is lossy on their edges - infinities, BC, nanoseconds,
#: `24:00:00`, a month folded into 30 days.
_TEXT_SAMPLED = frozenset(
    {
        LogicalType.DATE,
        LogicalType.TIMESTAMP,
        LogicalType.TIME,  # `24:00:00` has no datetime.time either
        LogicalType.JSON,
        LogicalType.INTERVAL,  # the driver turns `1 month` into 30 days
        # Not TIMETZ: its varchar cast drops an offset's minutes when it has
        # seconds (+05:00:30 prints +05:30, COV-7), and the driver's
        # datetime.time carries the offset whole (24:00:00 arrives as text).
    }
)


@dataclass(frozen=True)
class DuckType:
    """A DuckDB type name, parsed: `kind` is "list" (a `[]` list or `[n]`
    ARRAY, `fixed` telling them apart), "struct", "map", "union" or
    "scalar"; `fields` the (name, type) pairs of a struct or union, the
    element of a list, the key and value of a map."""

    kind: str
    raw: str
    fields: tuple[tuple[str, DuckType], ...] = ()
    fixed: bool = False


def _split_top(text: str) -> list[str]:
    """Split on the commas outside parentheses and quotes."""
    parts: list[str] = []
    depth, quote, start = 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _field(text: str) -> tuple[str, str]:
    """`name TYPE`, or a double-quoted name (a quote in it doubled) and
    its TYPE -> (name, TYPE)."""
    if text.startswith('"'):
        i = 1
        name = ""
        while i < len(text):
            if text[i] == '"':
                if text[i + 1 : i + 2] == '"':
                    name += '"'
                    i += 2
                    continue
                return name, text[i + 1 :].strip()
            name += text[i]
            i += 1
        raise ValueError(f"unterminated field name in {text!r}")
    name, _, typ = text.partition(" ")
    return name, typ.strip()


def parse_duckdb_type(raw: str) -> DuckType:
    """Parse a DuckDB type name as `information_schema` and DESCRIBE spell
    it: `STRUCT("a, b" INTEGER, c VARCHAR[2])`, `MAP(VARCHAR, TIME WITH TIME
    ZONE)`, `UNION(n INTEGER, s VARCHAR)`, `ENUM('a, b', 'c')[2]`."""
    raw = raw.strip()
    suffix = re.search(r"\[(\d*)\]$", raw)
    if suffix:
        inner = parse_duckdb_type(raw[: suffix.start()])
        return DuckType("list", raw, (("", inner),), fixed=bool(suffix.group(1)))
    up = raw.upper()
    for kind in ("struct", "union", "map"):
        if up.startswith(kind.upper() + "(") and raw.endswith(")"):
            body = _split_top(raw[len(kind) + 1 : -1])
            if kind == "map":
                if len(body) != 2:
                    break
                fields: tuple[tuple[str, DuckType], ...] = (
                    ("key", parse_duckdb_type(body[0])),
                    ("value", parse_duckdb_type(body[1])),
                )
            else:
                fields = tuple((n, parse_duckdb_type(t)) for n, t in map(_field, body))
            return DuckType(kind, raw, fields)
    return DuckType("scalar", raw)


#: Scalars whose varchar or JSON cast loses part of the value when nested:
#: TIMETZ drops an offset's seconds before DuckDB 2.0 (COV-7, RT-HD6), and
#: VARIANT its type tag.
_LOSSY_NESTED_SCALARS = frozenset({"TIMETZ", "TIME WITH TIME ZONE", "VARIANT"})


def _needs_nested_rule(t: DuckType, *, top: bool) -> bool:
    """Whether `t` must be rendered part by part (`DuckDBDialect._nested`).

    Nested anywhere: a UNION (its tag), a fixed-size ARRAY (strings unquoted
    before DuckDB 2.0) and the `_LOSSY_NESTED_SCALARS`. A top-level UNION
    already renders as JSON, which keeps its own tag, so only what it holds
    counts; a top-level ARRAY is a LIST (`map_type`) unless its element has
    no rule.
    """
    if t.kind == "scalar":
        return not top and t.raw.upper() in _LOSSY_NESTED_SCALARS
    if not top and (t.kind == "union" or (t.kind == "list" and t.fixed)):
        return True
    if top and t.kind == "list" and t.fixed:
        return True
    if top and t.kind == "union":
        return any(_holds(f, _LOSSY_NESTED_SCALARS) for _, f in t.fields)
    return any(_needs_nested_rule(f, top=False) for _, f in t.fields)


def _holds(t: DuckType, scalars: frozenset[str]) -> bool:
    """Whether `t` holds one of `scalars` anywhere inside."""
    if t.kind == "scalar":
        return t.raw.upper() in scalars
    return any(_holds(f, scalars) for _, f in t.fields)


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

        #: Warnings found while introspecting (a view's stale catalog), for
        #: `caveats`.
        self._notes: list[str] = []
        # Before 1.3 DuckDB rendered lists unquoted (['NULL'] = [NULL], RT-G10),
        # 1.1 lost pre-epoch nanoseconds (RT-G11) and 1.0 truncated DECIMAL
        # where it must round (RT-G14): verified false matches, so refused.
        if _version_tuple(duckdb.__version__) < MIN_DUCKDB:
            raise self._err(
                f"DuckDB {duckdb.__version__} is not supported: parity needs "
                f"DuckDB {'.'.join(map(str, MIN_DUCKDB))} or newer (older "
                "versions render some values differently, which can hide a "
                "difference). Upgrade with: pip install -U duckdb"
            )
        try:
            path = duckdb_path(connection_string)
        except ValueError as exc:
            raise self._err(str(exc)) from None
        if path.lower().startswith(("md:", "motherduck:")):
            # MotherDuck is a network service: duckdb.connect would load its
            # extension and log in (CLAUDE.md 6), and the query string holds
            # the token, which an error must never print (RT-HD3).
            raise self._err(
                "MotherDuck databases (md:, motherduck:) are not supported: "
                "parity reads local DuckDB files only. Export the tables to a "
                "local .duckdb file and compare that"
            )
        if not path or path == ":memory:":
            # An in-memory database holds no user data to protect, and a
            # read-only in-memory database is empty by definition.
            self._conn = duckdb.connect(":memory:")
            self._no_extension_downloads()
            self._pin_utc()
            self._catalog = str(
                self._conn.execute("select current_database()").fetchall()[0][0]
            )
            return
        if not os.path.exists(path):
            # read_only=True on a missing path fails with a driver-level error
            # that does not say which side or which file. Say it ourselves.
            raise self._err(f"database file not found: {_redact(path)}")
        if os.path.isfile(path):
            self._refuse_foreign_file(path)
        # CLAUDE.md section 6: read-only by construction. Enforcing it at the
        # connection means no bug in query building can ever write to a user's
        # database. It also lets several parity runs share one file.
        self._conn = duckdb.connect(path, read_only=True)
        self._no_extension_downloads()
        self._pin_utc()
        self._catalog = str(
            self._conn.execute("select current_database()").fetchall()[0][0]
        )
        self._refuse_shadowing()

    def _refuse_foreign_file(self, path: str) -> None:
        """Refuse a file that is not a DuckDB database before DuckDB opens it.

        `duckdb.connect` sniffs the header itself and opens a SQLite file
        through the sqlite_scanner extension - installing it from
        extensions.duckdb.org first if it is missing, inside the connect,
        before any setting of ours can turn autoinstall off (RT-HD4, a
        network call; CLAUDE.md 6). Config passed to connect would stop it,
        but DuckDB refuses a second connection to a file that another one in
        the process opened with a different config, which a library caller
        holding the file read-only would then hit. So the header is read
        here: every DuckDB database file carries the magic `DUCK` at byte 8.
        """
        try:
            with open(path, "rb") as fh:
                head = fh.read(16)
        except OSError as exc:
            raise self._err(
                f"could not connect: cannot read {_redact(path)}: {exc}"
            ) from exc
        if head[8:12] != b"DUCK":
            what = (
                " (it is a SQLite database)"
                if head.startswith(b"SQLite format 3")
                else ""
            )
            raise self._err(
                f"{_redact(path)} is not a valid DuckDB database file{what}; "
                "parity opens DuckDB files only and loads no extension to read "
                "other formats"
            )

    def _refuse_shadowing(self) -> None:
        """Refuse a file whose macros or types carry a built-in's name.

        DuckDB resolves a macro stored in the database before the built-in
        of the same name - on a read-only connection too - so a file holding
        `create macro md5(x) as 'd41d...'` rewrote every row hash parity
        computes into one constant, and a changed row compared identical
        (RT-HD1); `replace`, `substr` or `concat_ws` did the same to the row
        text, and a user type named `json` to the UNION rendering. Built-ins
        can be qualified (`system.main.md5`), but the type cast cannot be on
        DuckDB < 2.0 and the shared SQL names dozens of functions, so the
        defence is complete only as a refusal: any non-internal function or
        type whose name a built-in has, wherever it is defined.
        """
        funcs = self._conn.execute(
            "select distinct function_name from duckdb_functions() where not internal "
            "and lower(function_name) in (select lower(function_name) "
            "from duckdb_functions() where internal) order by 1"
        ).fetchall()
        types = self._conn.execute(
            "select distinct type_name from duckdb_types() where not internal "
            "and lower(type_name) in (select lower(type_name) "
            "from duckdb_types() where internal) order by 1"
        ).fetchall()
        shadows = [f"macro {r[0]}" for r in funcs] + [f"type {r[0]}" for r in types]
        if shadows:
            self.close()
            raise self._err(
                f"the database defines {', '.join(shadows)}, named like DuckDB "
                "built-ins. DuckDB calls a stored macro or type before the "
                "built-in, so parity's SQL would run it and its checksums "
                "could not be trusted. Compare a copy without them"
            )

    def _no_extension_downloads(self) -> None:
        """Never fetch or load an extension behind the user's back (RT-F13).

        CLAUDE.md section 6: no network calls, ever. With autoinstall on - the
        default - a query touching a feature from an uninstalled extension
        downloads it from extensions.duckdb.org, in C++ below any Python
        socket guard; a view over an `https://` or `s3://` file in the user's
        database is enough. With autoload off as well, an extension installed
        earlier is not loaded on demand either, so such a view fails loudly
        instead of reaching out to the network. Set on the connection rather
        than in `duckdb.connect(config=...)`, which DuckDB refuses for a file
        another connection in the process opened with a different config.
        The extensions the comparison itself uses (ICU for the UTC pin, JSON)
        are built into the Python wheel and need neither.
        """
        self._conn.execute("set autoinstall_known_extensions = false")
        self._conn.execute("set autoload_known_extensions = false")

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
        """Run `sql` and return every row as a list of tuples.

        The FLOAT refusal past 2^116 (`normalize`) arrives as an Invalid Input
        Error carrying its own text; re-raise it naming the side.
        """
        import duckdb

        try:
            return self._conn.execute(sql).fetchall()
        except duckdb.Error as exc:
            text = str(exc)
            at = text.find(FLOAT_OUT_OF_RANGE_MESSAGE)
            if at < 0:
                raise
            raise self._err(text[at:].splitlines()[0]) from exc

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in double quotes, doubling any it contains.

        This is the injection boundary: table and column names arrive from the
        command line and reach SQL through here.
        """
        return '"' + identifier.replace('"', '""') + '"'

    def qualify(self, table: str) -> str:
        """`"catalog"."schema"."table"`, always all three parts.

        `"warehouse"."t"` is ambiguous in a file named warehouse.duckdb that
        also holds a schema `warehouse` - DuckDB cannot tell a catalog from a
        schema there and every query failed (RT-HD12). The catalog is the
        attached database's own name, read at connect.
        """
        schema, name = self.split_table(table, self.default_schema)
        # A dialect built without a connection (SQL generation in tests)
        # has no catalog to name.
        catalog = getattr(self, "_catalog", None)
        parts = (schema, name) if catalog is None else (catalog, schema, name)
        return ".".join(self.quote(p) for p in parts)

    def _columns_catalog(self, schema: str) -> str:
        """The system catalog's `information_schema`: in a file named
        information_schema.duckdb the bare name is ambiguous (RT-HD12)."""
        return "system.information_schema.columns"

    def columns(self, table: str) -> list[Column]:
        r"""The table's columns as the catalog lists them - or, for a view,
        as the view returns them now.

        `information_schema.columns` keeps a view's column list and types as
        they were when it was created: after `alter table t alter v type
        varchar` a `select *` view still reads INTEGER there, so v was
        rendered by the integer rule and '\N' equalled NULL; a column added
        to the base table was never compared at all (RT-HD13, false
        matches). DESCRIBE binds the view afresh. When the two disagree the
        live description is used, and `caveats` says so.
        """
        schema, name = self.split_table(table, self.default_schema)
        where = (
            f"where table_catalog = {self.string_literal(self._catalog)} "
            f"and table_schema = {self.string_literal(schema)} "
            f"and table_name = {self.string_literal(name)}"
        )
        rows = [
            (str(r[0]), str(r[1]))
            for r in self.query(
                f"select column_name, data_type from {self._columns_catalog(schema)} "  # noqa: S608 - names via string_literal
                f"{where} order by ordinal_position"
            )
        ]
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        kind = self.query(
            f"select table_type from system.information_schema.tables {where}"  # noqa: S608 - names via string_literal
        )
        if kind and kind[0][0] == "VIEW":
            listed = rows
            live = [
                (str(r[0]), str(r[1]))
                for r in self.query(f"describe select * from {self.qualify(table)}")  # noqa: S608
            ]
            if live != listed:
                self._notes.append(
                    f"view {table}: the catalog lists its columns as "
                    f"{[f'{n} {t}' for n, t in listed]}, but the view returns "
                    f"{[f'{n} {t}' for n, t in live]} (a base table was altered "
                    "after the view was created); compared as the view returns them"
                )
                rows = live
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        r"""Count distinct key texts by their bytes.

        From DuckDB 2.0 `count(distinct ...)` of the key text follows the key
        column's collation (NOCASE, NOACCENT, NFC, ICU) or a
        `default_collation`, so a unique key holding 'a' and 'A' was refused
        as not unique (RT-HD14; CLAUDE.md 4.11). `encode` is the text's UTF-8
        bytes as a BLOB, which no collation applies to (a cast to BLOB would
        read `\x41` escapes). An integer key carries no collation.
        """
        if not key.hashed:
            return super().count_distinct_key(key, identity)
        return f"count(distinct encode({identity}))"

    # ----------------------------------------------------------- rendering

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        The contract: two rows are equal if and only if this text is
        byte-identical to the other engine's. Every rule ends up inside this
        `coalesce`, so a NULL always becomes the sentinel.
        """
        expr = self._render(column, self.quote(column.name))
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def _render(self, column: Column, c: str) -> str:
        """`column`'s rule applied to the SQL expression `c`; NULL for NULL.

        `normalize` applies it to the column itself, the nested rules
        (`_nested`) to a struct field, a map entry or a union member.
        """
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as varchar)"
        elif t is LogicalType.FLOAT:
            # Widen first: a single-precision column (`real`, FLOAT) is cast to
            # double, which is exact on every engine, so float4 and float8 share
            # one canonical form. Rendered as float4 directly, PostgreSQL and
            # DuckDB print different shortest reprs (-4088288.25 as `-4088288.2`
            # against `-4088288.25`) and MySQL prints only 6 digits. For a
            # double column the cast is a no-op.
            c = f"cast({c} as double)"
            # Infinity and NaN cannot be cast to DECIMAL - DuckDB raises
            # "Could not cast value inf to DECIMAL(38,6)" and the whole diff
            # dies. They are ordinary in float columns (any division by zero
            # produces one), so render them as fixed tokens that PostgreSQL
            # spells the same way.
            #
            # Finite values follow `canonical_float_text` (base.py), and the
            # direct `cast(c as decimal(38,s))` does not: it computes c * 10^s
            # in double precision, so 1e15+0.5 rendered `...0.524288`. Below
            # 2^53 the varchar cast is the shortest round-trip decimal, and the
            # string-to-decimal cast rounds it half away from zero - except in
            # exponent notation with a large negative exponent, which it rounds
            # on the leading digit alone (`'6.4e-10'` -> `0.000001`). Anything
            # under 10^-(s+1) is zero at this scale, so it short-circuits there.
            # From 2^53 every double is an integer and hugeint holds it
            # exactly; past 2^116 the other engines cannot, so refuse it too.
            s = self.float_scale_for(column)
            zero = float_zero_text(s)
            frac = "." + "0" * s if s else ""
            expr = (
                f"case when isinf({c}) then (case when {c} > 0 then 'Infinity' "
                f"else '-Infinity' end) "
                f"when isnan({c}) then 'NaN' "
                f"when abs({c}) < cast('{float_tiny_sql(s)}' as double) then '{zero}' "
                f"when abs({c}) < cast('{FLOAT_EXACT_INT_FROM}' as double) "
                f"then cast(cast(cast({c} as varchar) as decimal(38,{s})) as varchar) "
                f"when abs({c}) < cast('{FLOAT_MAX_MAGNITUDE_SQL}' as double) "
                f"then cast(cast({c} as hugeint) as varchar) || '{frac}' "
                f"when {c} is not null then error('{FLOAT_OUT_OF_RANGE_MESSAGE}' || cast({c} as varchar) "
                f"|| {sql_literal(float_range_where(column))}) end"
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
            src = c
            if column.raw_type.upper() in ("TIMESTAMP_S", "TIMESTAMP_MS"):
                # strftime binds its TIMESTAMP_NS overload for these, which
                # raises outside ~1677-2262 and killed the diff (COV-1). A
                # cast to TIMESTAMP (microseconds) is exact for both.
                src = f"cast({c} as timestamp)"
            expr = f"strftime({src}, '%Y-%m-%d %H:%M:%S.%f')"
            if column.raw_type.upper().startswith("TIMESTAMP_NS") and column.sub_micro:
                # `%f` is microseconds, so .000000001 and .000000999 rendered
                # alike, silently (RT-A7/C1/E7). A whole-microsecond value
                # still renders exactly as before - the six digits every other
                # engine prints, so no checksum moves and it still matches a
                # microsecond column elsewhere. Any other value appends its
                # three nanosecond digits: exact against another TIMESTAMP_NS,
                # and against an engine that holds only microseconds a
                # *reported* difference, which it is - that engine cannot hold
                # the value. `%f` floors (pre-1970 included), so the remainder
                # is taken non-negative to match. Built from epoch_ns rather
                # than strftime's `%n`, which older DuckDB releases lack.
                ns = f"((epoch_ns({c}) % 1000) + 1000) % 1000"
                expr = (
                    f"case when {ns} = 0 then {expr} "
                    f"else {expr} || lpad(cast({ns} as varchar), 3, '0') end"
                )
        elif t is LogicalType.TIME:
            # strftime takes no TIME, and the varchar cast drops trailing
            # zeros (`12:34:56.5`). Built from the fields instead; hour() of
            # `24:00:00`, which TIME accepts, is 24 (verified), so it renders
            # `24:00:00.000000` as PostgreSQL does. microsecond() counts the
            # seconds too, hence the modulo.
            def two(part: str) -> str:
                """One clock field, zero-padded to two digits."""
                return f"lpad(cast({part}({c}) as varchar), 2, '0')"

            expr = (
                f"{two('hour')} || ':' || {two('minute')} || ':' || {two('second')} "
                f"|| '.' || lpad(cast(microsecond({c}) % 1000000 as varchar), 6, '0')"
            )
        elif t is LogicalType.TIMETZ:
            # Local clock fields (hour() of `24:00:00` is 24, as for TIME)
            # and the offset in seconds east of UTC, held to the second -
            # `canonical_timetz_text`. The varchar cast printed `12:00:00+05:45`
            # with the fraction's trailing zeros dropped.
            def pad(value: str, width: int = 2) -> str:
                """An integer expression, zero-padded."""
                return f"lpad(cast({value} as varchar), {width}, '0')"

            def field(name: str) -> str:
                """One clock field of the local time."""
                return f"datepart('{name}', {c})"

            off = field("timezone")
            mag = f"abs({off})"
            expr = (
                f"{pad(field('hour'))} || ':' || {pad(field('minute'))} || ':' "
                f"|| {pad(field('second'))} || '.' "
                f"|| {pad(field('microsecond') + ' % 1000000', 6)} "
                f"|| case when {off} < 0 then '-' else '+' end "
                f"|| {pad(mag + ' // 3600')} || ':' || {pad(mag + ' % 3600 // 60')} "
                f"|| case when {mag} % 60 <> 0 then ':' || {pad(mag + ' % 60')} "
                "else '' end"
            )
        elif t is LogicalType.INTERVAL:
            # The three stored components - `canonical_interval_text`. year
            # and month split the months, hour, minute and microseconds the
            # micros, each truncating so all carry the stored sign; the sum
            # is in HUGEINT because 2562047788 hours of microseconds is not
            # far from INT64's edge. The varchar cast printed `1 day` and
            # `24:00:00` - distinct, but in a layout no other engine shares.
            def iv(field: str) -> str:
                """One field, widened."""
                return f"cast(datepart('{field}', {c}) as hugeint)"

            expr = (
                f"cast({iv('year')} * 12 + {iv('month')} as varchar) || ' mon ' "
                f"|| cast({iv('day')} as varchar) || ' day ' "
                f"|| cast({iv('hour')} * 3600000000 + {iv('minute')} * 60000000 "
                f"+ {iv('microseconds')} as varchar) || ' us'"
            )
        elif t is LogicalType.BITS:
            # BIT prints its digits, 0 and 1 only - the bytes the escaped
            # fallback produced, so no checksum moves.
            expr = f"cast({c} as varchar)"
        elif t is LogicalType.LIST:
            # `canonical_list_text`: each element by its own rule, bound to
            # the comprehension variable (which shadows a column of that
            # name), followed by the terminator, joined. The varchar cast
            # printed `[a, b]` - the same text as the one string 'a, b' in a
            # list, a false match. A fixed-size ARRAY (`VARCHAR[2]`) is a list
            # here too: the comprehension reads it as one.
            element = self.list_element_text(column)
            var = self.quote(self.LIST_ELEMENT_NAME)
            each = f"[{element} || {self.list_terminator_sql()} for {var} in {c}]"
            expr = (
                f"case when {c} is not null then '[' || {self._joined(each)} || ']' end"
            )
        elif t is LogicalType.BINARY:
            # The varchar cast prints printable bytes as themselves and the
            # rest as `\xFF`, so b'abc' rendered exactly like the text 'abc'.
            # BINARY_PREFIX + lower-case hex is exact for every byte string.
            expr = f"{self.binary_prefix_sql()} || lower(hex({c}))"
        elif t is LogicalType.JSON:
            # DuckDB's JSON is text, validated but not normalised: key order,
            # duplicates and spellings stay as written (`json_verbatim`). Only
            # the layout is canonical - whitespace outside strings removed,
            # then one space after each separator (`json_compact_layout`).
            # `json()` would normalise more, but it rewrites every non-integer
            # number through a double, which could make two different stored
            # documents equal; this stays exact.
            strip = self.spell_sql(r'(?s)("(?:\\.|[^"\\])*")|[ ' + "\t\n\r]+")
            stripped = (
                f"regexp_replace(cast({c} as varchar), {strip}, "
                f"{self.spell_sql(chr(92) + '1')}, 'g')"
            )
            expr = self.escape_text(self.json_compact_layout(stripped))
        elif _needs_nested_rule(parse_duckdb_type(column.raw_type), top=True):
            # A STRUCT, MAP, UNION or list holding something the varchar (or
            # JSON) cast renders lossily - a UNION's tag inside a STRUCT
            # (RT-HD7), a TIMETZ offset's seconds before DuckDB 2.0 (RT-HD6),
            # a fixed-size ARRAY's unquoted strings before 2.0 (RT-HD5):
            # rendered part by part, each scalar by its own rule (`_nested`).
            # Every other nested type keeps the varchar cast it always had,
            # so its checksum does not move.
            expr = self._nested(c, parse_duckdb_type(column.raw_type), 0)
        elif column.raw_type.upper().startswith("UNION("):
            # A plain varchar cast prints the member value and drops its tag,
            # so UNION(n int, s varchar) rendered 1::int and '1' alike - a
            # false match (RT-A12/C14). JSON carries both: `{"n":1}` against
            # `{"s":"1"}`, the tag as a quoted key and a string in quotes.
            expr = f"cast(cast({c} as json) as varchar)"
        elif column.raw_type.upper() == "VARIANT":
            expr = self._variant(c)
        else:
            # STRING, and every unmapped type (structs, maps, nested lists,
            # ...) through its varchar cast: arbitrary text, so escaped.
            expr = self.escape_text(f"cast({c} as varchar)")
        return expr

    # ------------------------------------------------------ nested values

    @staticmethod
    def _joined(each: str) -> str:
        """`array_to_string(each, '')`, never NULL for a list.

        DuckDB 1.3 and 1.4 return NULL for an empty list, not '' as 1.5 does,
        so every empty list rendered as the NULL sentinel - `[]` against
        NULL compared identical (RT-HD11). `each` is never NULL where this
        is used (the caller tests the list first), so the coalesce changes
        nothing else, and nothing at all from 1.5.
        """
        return f"coalesce(array_to_string({each}, ''), '')"

    def _text_sql(self, text: str) -> str:
        """`text` as SQL with every backslash from its code point (RT-D1)."""
        return " || '''' || ".join(self.spell_sql(piece) for piece in text.split("'"))

    def _variant(self, c: str) -> str:
        """A VARIANT: its stored type's name, `:`, then its value.

        A VARIANT INTEGER 1 and VARCHAR '1' both cast to `1`; `variant_typeof`
        names the stored type and JSON keeps a string's quotes (a DATE and the
        VARCHAR spelling it print the same JSON, which the type name tells
        apart). A TIMETZ's JSON is its varchar cast, which drops an offset's
        seconds before DuckDB 2.0 (RT-HD6), so it renders by the TIMETZ rule.
        """
        typ = f"variant_typeof({c})"
        timetz = self._render(
            Column("", LogicalType.TIMETZ, "TIMETZ"), f"cast({c} as timetz)"
        )
        return (
            f"case when {c} is not null then {typ} || ':' || "
            f"case when {typ} = 'TIME_MICROS_TZ' then {timetz} "
            f"else cast(cast({c} as json) as varchar) end end"
        )

    def _part(self, expr: str, t: DuckType, depth: int) -> str:
        """`_nested`, null-safe: a NULL part renders as the sentinel."""
        return f"coalesce({self._nested(expr, t, depth)}, {self.null_sentinel_sql()})"

    def _nested(self, expr: str, t: DuckType, depth: int) -> str:
        r"""A nested value's canonical text, part by part; NULL for NULL.

        - list or ARRAY: `canonical_list_text` - `[` + each element by its
          own rule + `\,` ... + `]`;
        - STRUCT: `{` + per field, in declared order, the name escaped as
          text, `\=`, the value by its rule, `\,` + `}` - the layout of
          `canonical_struct_text` (BigQuery's STRUCT rule, RT-HB2);
        - MAP: its entries in stored order (order is part of a DuckDB MAP:
          `map {'a': 1, 'b': 2} = map {'b': 2, 'a': 1}` is false), as the
          list of `{key\=..\,value\=..\,}` structs `map_entries` returns;
        - UNION: `<` + the tag escaped + `\=` + the member by its rule + `>`;
        - a scalar by its own rule, a TIMESTAMP_NS with every digit it holds.

        Every scalar text is escaped or from an alphabet with no backslash,
        names are escaped, so no part can forge a mark, and a NULL part is
        the sentinel while a NULL whole is NULL: `{'a': NULL}` is `{a\=\N\,}`,
        never NULL. Compared only against DuckDB's own rendering of a nested
        type (no other engine's nested type maps to the same text).
        """
        mark = self.list_dims_mark_sql()
        term = self.list_terminator_sql()
        if t.kind == "scalar":
            column = Column(f"__parity_e{depth}", map_type(t.raw), t.raw, sub_micro=True)
            if t.raw.upper() == "VARIANT":
                return self._variant(expr)
            return self._render(column, expr)
        if t.kind == "list":
            var = self.quote(f"__parity_e{depth}")
            each = f"[{self._part(var, t.fields[0][1], depth + 1)} || {term} for {var} in {expr}]"
            return f"case when {expr} is not null then '[' || {self._joined(each)} || ']' end"
        if t.kind == "map":
            entries = DuckType("list", "", (("", DuckType("struct", "", t.fields)),))
            return self._nested(f"map_entries({expr})", entries, depth)
        if t.kind == "struct":
            body = " || ".join(
                f"{self._text_sql(escape_canonical_text(name))} || {mark} || "
                f"{self._part(f'struct_extract({expr}, {sql_literal(name)})', ft, depth + 1)} || {term}"
                for name, ft in t.fields
            )
            return f"case when {expr} is not null then '{{' || {body} || '}}' end"
        # union
        arms = " ".join(
            f"when {sql_literal(name)} then '<' || {self._text_sql(escape_canonical_text(name))} "
            f"|| {mark} || {self._part(f'union_extract({expr}, {sql_literal(name)})', ft, depth + 1)} "
            "|| '>'"
            for name, ft in t.fields
        )
        return f"case cast(union_tag({expr}) as varchar) {arms} end"

    def holds_sub_micro(self, column: Column) -> bool:
        """TIMESTAMP_NS holds nanoseconds."""
        return column.logical_type is LogicalType.TIMESTAMP and (
            column.raw_type.upper().startswith("TIMESTAMP_NS")
        )

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """A TIMESTAMP_NS compared against a microsecond engine is truncated;
        a view whose catalog entry is stale is compared as it returns rows."""
        notes = list(getattr(self, "_notes", []))
        fine = sorted(
            c.name for c in columns if self.holds_sub_micro(c) and not c.sub_micro
        )
        if not fine:
            return notes
        note = (
            f"columns {fine} hold nanoseconds and the other side microseconds "
            "only; they are compared at microseconds (the digits past the sixth "
            "are dropped), so a difference below one microsecond is not seen"
        )
        return [*notes, note]

    def single_precision(self, column: Column) -> bool:
        """DuckDB's FLOAT is float4 (REAL and FLOAT4 are its aliases)."""
        return column.raw_type.lower().split("(")[0].strip() in (
            "float",
            "float4",
            "real",
        )

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check; dates and timestamps as text.

        The driver is lossy on every temporal edge (COV-3..5): it returns
        `infinity` as 9999-12-31, BC values as strings, rounds TIMESTAMP_NS to
        the microsecond where the rules truncate, and returns TIMESTAMPTZ only
        with pytz installed. DuckDB's varchar cast prints the stored value
        whole - `infinity`, `0044-03-15 (BC)`, nine digits, `+00` under the
        pinned UTC - and `parity.reference` reads all of it.
        """
        if column.logical_type in _TEXT_SAMPLED:
            return f"cast({self.quote(column.name)} as varchar)"
        if (
            column.logical_type is LogicalType.LIST
            and self.list_element(column).logical_type in _TEXT_SAMPLED
        ):
            # Each element as the scalar sample would read it.
            return f"cast({self.quote(column.name)} as varchar[])"
        return super().sample_select(column)

    def bisects_natively(self, column: Column) -> bool:
        """UHUGEINT holds 2^128 - 1, past the HUGEINT that `wide_int` widens
        into, and DuckDB turns UHUGEINT arithmetic with a signed operand into
        DOUBLE silently; such a key is hashed instead."""
        return column.raw_type.upper() != "UHUGEINT"

    def json_verbatim(self, column: Column) -> bool:
        """DuckDB's JSON is validated text, never normalised."""
        return True

    def json_semantics(self, column: Column) -> str:
        """Text, compared as written."""
        return (
            "DuckDB JSON is text: object keys in the order written, duplicate keys "
            "kept, numbers and string escapes compared exactly as spelled"
        )

    def json_compact_layout(self, expr: str) -> str:
        """The three steps of the base docstring, in DuckDB's RE2 spelling."""
        pattern = self.spell_sql(r'(?s)("(?:\\.|[^"\\])*"|[^",:]*)([,:]?)')
        marked = (
            f"regexp_replace({expr}, {pattern}, "
            f"{self.spell_sql(chr(92) + '1' + chr(92) + '2' + chr(1))}, 'g')"
        )
        return (
            f"replace(replace(replace({marked}, ',' || chr(1), ', '), "
            f"':' || chr(1), ': '), chr(1), '')"
        )

    def normalize_key(self, column: Column) -> str:
        """Exact key text; a TIMESTAMP_NS key keeps its nanoseconds.

        As a value TIMESTAMP_NS renders at microseconds like every other
        timestamp, and as a key that merged distinct keys (RT-B6). A key with
        a sub-microsecond part renders all nine digits instead; one without
        renders exactly as before, so it still matches a microsecond key on
        another engine. Six digits against nine, so no two keys collide.
        """
        if (
            column.logical_type is LogicalType.TIMESTAMP
            and column.raw_type.lower().startswith("timestamp_ns")
        ):
            c = self.quote(column.name)
            return (
                f"case when {c} is null or epoch_ns({c}) % 1000 = 0 "
                f"then {self.normalize(column)} "
                f"else strftime({c}, '%Y-%m-%d %H:%M:%S.%n') end"
            )
        return super().normalize_key(column)

    def decimal_key_text(self, c: str, column: Column) -> str:
        """DECIMAL's varchar cast is exact and plain, at the declared scale;
        drop the trailing fractional zeros that scale adds."""
        t = f"cast({c} as varchar)"
        return (
            f"case when {c} = 0 then '0' "
            f"when contains({t}, '.') then rtrim(rtrim({t}, '0'), '.') "
            f"else {t} end"
        )

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
        # hugeint is 128-bit, which a 64-bit key offset cannot overflow. A
        # HUGEINT key is the exception, handled by `bucket_expr`.
        return f"cast(({expr}) as hugeint)"

    def bucket_expr(self, key: KeySpec, k: str, lo: int, hi: int, n_segments: int) -> str:
        """The shared bucket expression, unless it would overflow INT128.

        `wide_int` is hugeint, which is no widening for a HUGEINT key: `k - lo`
        passes 2^127 once the key span does, and `(k - lo) * n` once the span
        passes 2^127 / n - both raw OutOfRange crashes naming no side (RT-A19,
        RT-B13). DuckDB has nothing wider (DECIMAL stops at 38 digits, short of
        2^128), so for such a span the same floor is computed without forming
        that product.

        With s = hi - lo = n*D + R (D, R computed here in Python), the bucket
        `floor(off * n / s)` is the largest q with ceil(q * s / n) <= off, and
        ceil(q * s / n) = q*D + ceil(q*R / n). Every term fits UHUGEINT: off
        < s <= 2^128, q*D <= off, q*R < n^2. A span this wide makes D vastly
        larger than n, and then the answer is q0 = min(n - 1, off // D) or one
        less - since q*D <= ceil(q*s/n) <= q*(D + 1), the true q lies between
        off // (D + 1) and off // D, which differ by at most one once off <
        D*(D + 1). So one comparison decides it, and the result is exactly
        what the shared expression would give, and what `bucket_bounds`
        inverts - for every key, which `test_engine.py` style property tests
        check against Python's integers.
        """
        span = hi - lo
        if (
            key.hashed
            or key.columns[0].raw_type.upper() != "HUGEINT"
            or span * n_segments <= 2**127 - 1
        ):
            # Every key's offset is below the span, so neither `k - lo` nor
            # its product with n can pass INT128: the shared SQL, unchanged.
            return super().bucket_expr(key, k, lo, hi, n_segments)
        d, r = divmod(span, n_segments)
        if d <= n_segments:
            # Unreachable for any sane bisection factor (D > 2^127 / n^2), but
            # the one-step correction below relies on it, so never guess.
            raise self._err(
                f"key {key.label}: a key span of {span} cannot be split into "
                f"{n_segments} buckets exactly; lower --bisection-factor"
            )

        def u(value: int) -> str:
            """A UHUGEINT constant, via a string: past 2^127 a bare literal
            would not parse as one."""
            return f"cast('{value}' as uhugeint)"

        # off = k - lo, in UHUGEINT without ever forming an out-of-range value:
        # for k >= 0 add |lo| (or subtract lo) as unsigned; for k < 0 (only
        # possible when lo < 0 too) `k - lo` lies in [0, 2^127) as HUGEINT.
        if lo < 0:
            off = (
                f"(case when {k} >= 0 then cast({k} as uhugeint) + {u(-lo)} "
                f"else cast({k} - ({lo}) as uhugeint) end)"
            )
        else:
            off = f"(cast({k} as uhugeint) - {u(lo)})"
        q0 = f"least({u(n_segments - 1)}, {off} // {u(d)})"
        # ceil(q0*R / n) > off - q0*D  <=>  q0 is one too many.
        too_far = f"({q0} * {u(r)} + {u(n_segments - 1)}) // {u(n_segments)} > {off} - {q0} * {u(d)}"
        return f"cast({q0} - case when {too_far} then 1 else 0 end as hugeint)"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group."""
        # DECIMAL(38,0) holds sums far beyond any realistic row count * 2^60.
        return f"coalesce(sum(cast(({expr}) as decimal(38,0))), 0)"
