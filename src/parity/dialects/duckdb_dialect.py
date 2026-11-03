"""DuckDB dialect.

The other half of a comparison, and the shortest complete example of the
`Dialect` contract - a good place to start when writing a new one.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_range_where,
    float_tiny_sql,
    float_zero_text,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType


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
            self._no_extension_downloads()
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
        self._no_extension_downloads()
        self._pin_utc()

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
        elif column.raw_type.upper().startswith("UNION("):
            # A plain varchar cast prints the member value and drops its tag,
            # so UNION(n int, s varchar) rendered 1::int and '1' alike - a
            # false match (RT-A12/C14). JSON carries both: `{"n":1}` against
            # `{"s":"1"}`, the tag as a quoted key and a string in quotes.
            expr = f"cast(cast({c} as json) as varchar)"
        elif column.raw_type.upper() == "VARIANT":
            # The same tag loss: a VARIANT INTEGER 1 and VARCHAR '1' both cast
            # to `1`. `variant_typeof` names the stored type and JSON keeps a
            # string's quotes (a DATE and the VARCHAR spelling it print the
            # same JSON, which the type name tells apart).
            expr = (
                f"case when {c} is not null then "
                f"variant_typeof({c}) || ':' || cast(cast({c} as json) as varchar) end"
            )
        else:
            # STRING, and every unmapped type (lists, structs, json, blobs,
            # intervals, ...) through its varchar cast: arbitrary text, so
            # escaped.
            expr = self.escape_text(f"cast({c} as varchar)")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def holds_sub_micro(self, column: Column) -> bool:
        """TIMESTAMP_NS holds nanoseconds."""
        return column.logical_type is LogicalType.TIMESTAMP and (
            column.raw_type.upper().startswith("TIMESTAMP_NS")
        )

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """A TIMESTAMP_NS compared against a microsecond engine is truncated."""
        fine = sorted(
            c.name for c in columns if self.holds_sub_micro(c) and not c.sub_micro
        )
        if not fine:
            return []
        note = (
            f"columns {fine} hold nanoseconds and the other side microseconds "
            "only; they are compared at microseconds (the digits past the sixth "
            "are dropped), so a difference below one microsecond is not seen"
        )
        return [note]

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
        if column.logical_type in (
            LogicalType.DATE,
            LogicalType.TIMESTAMP,
            LogicalType.TIME,  # `24:00:00` has no datetime.time either
            LogicalType.JSON,
        ):
            return f"cast({self.quote(column.name)} as varchar)"
        return super().sample_select(column)

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
