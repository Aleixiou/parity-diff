"""Dialect contract.

A dialect knows three things about one database engine:

1. how to read a column's type and map it onto a :class:`LogicalType`
2. how to render a value as *canonical text* - the same bytes any other
   engine would produce for the same logical value
3. how to fold canonical text into a 60-bit integer and aggregate it

Everything else (segmentation, recursion, reporting) is engine-independent
and lives in :mod:`parity.engine`.

The 60-bit width is deliberate: it is the widest prefix of an MD5 hex digest
that both PostgreSQL's ``bit(n)::bigint`` cast and DuckDB's hex-string cast
render as the same *positive* signed 64-bit integer. At 64 bits PostgreSQL
wraps to negative and the two engines disagree.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, TypeVar

from parity.types import Column, KeySpec, KeyStats, LogicalType

_D = TypeVar("_D", bound="Dialect")

# Field separator inside a row's canonical text. Unit Separator (0x1f) is
# chosen because it effectively never appears in warehouse string data;
# `chr(31)` is spelled the same way in every engine we support.
SEPARATOR_SQL = (
    "chr(31)"  # default; a dialect overrides `separator_sql` if its spelling differs
)
NULL_SENTINEL = "\\N"

# Number of MD5 hex characters folded into the row hash. 15 nibbles = 60 bits.
HASH_HEX_CHARS = 15

#: Most values a single `concat_ws` call may take. PostgreSQL's
#: `max_function_args` is 100 and fixed at compile time; DuckDB allows more.
#: 64 leaves clear headroom on the strictest engine and keeps both sides
#: nesting identically, which is what guarantees identical canonical text.
MAX_CONCAT_ARGS = 64

#: Decimal places at which DECIMAL/FLOAT columns are compared. A deliberate,
#: documented limitation - see CLAUDE.md section 4.2.
DEFAULT_FLOAT_SCALE = 6

# ------------------------------------------------------------------ FLOAT
#
# The canonical text of a finite double. Every engine must compute exactly
# `canonical_float_text` below, and the obvious per-engine spellings do not:
# PostgreSQL's `float8::numeric` keeps only 15 significant digits, and DuckDB's
# `cast(double as decimal)` multiplies by 10^scale in double precision, so the
# two disagreed on a fifth of an adversarial sample of doubles - rounding-only
# false alarms (1e15+0.5 rendered `...0.500000` against `...0.524288`). The
# definition below is a function of the IEEE value alone, so two equal doubles
# always render identically, and every engine can reach it:
#
# - |x| < 2^53: take the *shortest round-trip* decimal (what Python's repr,
#   PostgreSQL 12+'s float8out, DuckDB's and MySQL's double-to-text all print)
#   and round it half away from zero to the float scale. It is also the value
#   a user typed, so a DECIMAL 2.0000005 and a DOUBLE 2.0000005 still agree.
# - 2^53 <= |x| < 2^116: every such double is an integer; render it exactly.
#   Not the shortest repr, because PostgreSQL's shortest-repr algorithm (Ryu)
#   excludes the rounding-interval boundaries where MySQL's and DuckDB's (dtoa)
#   include them, and above 2^54 those boundaries are short integers - so
#   3.8809635010537424e16 prints with 17 digits on one and 16 on the others.
#   Every engine splits x into trunc(x / 2^53) and the exact remainder, both
#   held exactly in a 64-bit integer, and recombines them in a decimal type.
# - |x| >= 2^116: the high half no longer fits 64 bits. Refused loudly, with
#   an error naming the value, on every engine - never rendered approximately.

#: Below this magnitude a double's shortest round-trip decimal is the same on
#: every engine; at and above it every double is an integer and is rendered
#: exactly. 2^53, exactly representable.
FLOAT_EXACT_INT_FROM = 2**53

#: Doubles at or above this magnitude are refused: trunc(x / 2^53) no longer
#: fits a signed 64-bit integer. 2^116 ~= 8.3e34, exactly representable.
FLOAT_MAX_MAGNITUDE = 2**116

#: `FLOAT_MAX_MAGNITUDE` as a decimal string that parses to exactly 2^116.
FLOAT_MAX_MAGNITUDE_SQL = "83076749736557242056487941267521536"

#: The text every engine raises with for a double past `FLOAT_MAX_MAGNITUDE`.
#: The refusing branch must be `when c is not null then ...`, never `else`: a
#: NULL fails every magnitude test and would land in it - BigQuery then raised
#: "Input to ERROR function must not be NULL" on an ordinary NULL double. The
#: boolean `else` trap again (CLAUDE.md section 4.3), in a new place.
FLOAT_OUT_OF_RANGE_MESSAGE = (
    "parity: FLOAT value out of the comparable range (|x| >= 2^116, about "
    "8.3e34) - refusing to round it: "
)


def float_range_where(column: Column) -> str:
    """What follows the value in a FLOAT refusal: the column it is in.

    Each dialect puts it in the SQL text it raises with, as a literal of that
    engine's own quoting, so the error names the column as well as the value
    - and the dialect's `query` adds the side (CLAUDE.md section 6). Without
    it the refusal on DuckDB and PostgreSQL was a bare driver error naming
    neither (RT-E10).
    """
    return f" in column {column.name!r}"


#: Largest --float-scale a FLOAT column can be compared at. Every double below
#: 2^53 needs 16 integer digits, and BigQuery's BIGNUMERIC holds 38 decimal
#: places, which a 17-significant-digit rounding of a value just above
#: 10^-(scale+1) needs scale + 18 of. Past it MySQL's DECIMAL cast would clamp
#: silently (2e15 at 23 places became 999999999999999.999...) and BigQuery
#: would round from a truncated value, so it is refused instead.
FLOAT_MAX_SCALE = 20


def float_tiny_sql(scale: int) -> str:
    """Magnitude below which a double renders as zero at `scale`, as a literal.

    10^-(scale+1) is far below the half-unit 5 * 10^-(scale+1), so anything
    smaller rounds to zero under any reading of the value. Engines use it to
    short-circuit values whose text would be in exponent notation, which
    DuckDB's string-to-decimal cast rounds wrongly (`'6.4e-10'` became
    `0.000001`).
    """
    return f"1e-{scale + 1}"


def float_zero_text(scale: int) -> str:
    """Zero at `scale` places, unsigned: `0.000000`, or `0` at scale 0."""
    return "0." + "0" * scale if scale else "0"


def canonical_float_text(x: float, scale: int = DEFAULT_FLOAT_SCALE) -> str:
    """The canonical text of a double, in Python: the executable spec.

    Every dialect's FLOAT expression must agree with this byte for byte; the
    cross-engine tests and `demo/float_agreement.py` check them against it.
    Raises ValueError for non-finite values (the dialects emit the tokens
    `Infinity`, `-Infinity`, `NaN`) and past `FLOAT_MAX_MAGNITUDE`.
    """
    import math
    from decimal import ROUND_HALF_UP, Context, Decimal

    if not math.isfinite(x):
        raise ValueError(f"not finite: {x!r}")
    if abs(x) >= FLOAT_MAX_MAGNITUDE:
        raise ValueError(FLOAT_OUT_OF_RANGE_MESSAGE + repr(x))
    exact_int = abs(x) >= FLOAT_EXACT_INT_FROM
    d = Decimal(int(x)) if exact_int else Decimal(repr(x))
    ctx = Context(prec=200)
    q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=ctx)
    if q == 0:
        return float_zero_text(scale)
    return format(q, "f")


def sql_literal(value: str) -> str:
    """Quote a string literal. Only ever used for schema and table names read
    back from `information_schema`, never for user data."""
    return "'" + value.replace("'", "''") + "'"


class Dialect(ABC):
    """One database engine's half of a comparison."""

    name: str

    def __init__(
        self,
        float_scale: int = DEFAULT_FLOAT_SCALE,
        side: str = "?",
    ) -> None:
        """Configure one side of a comparison.

        Both settings are per-instance rather than per-class, which matters:
        see `float_scale` below for what a shared class attribute would do.
        """
        if float_scale < 0:
            raise ValueError(f"float_scale must be >= 0, got {float_scale}")
        #: Decimal places at which DECIMAL/FLOAT columns are compared. This is
        #: per-instance, not per-class: as a class attribute a change on one
        #: side would leak to the other, or worse, not leak - and two sides
        #: rounding differently reports *every* float row as different. Use
        #: `require_matching_scales` before comparing.
        self.float_scale = float_scale
        #: "A" or "B". Carried only so errors can name which side failed;
        #: "table not found" is useless when two databases are in play.
        self.side = side

    def _err(self, message: str) -> ValueError:
        """Build an error that names which side and which engine it came from.

        "table not found" is useless when two databases are in play.
        """
        return ValueError(f"[side {self.side}: {self.name}] {message}")

    # ---------------------------------------------------------------- setup

    @abstractmethod
    def connect(self, connection_string: str) -> None:
        """Open a connection. Must be read-only, and should pin UTC."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""

    def __enter__(self: _D) -> _D:
        """`with get_dialect(...) as a:` closes the connection however it ends.

        `diff` never closes the dialects it is handed - the caller owns them.
        A PostgreSQL side left open after a diff raises sits idle in its
        REPEATABLE READ snapshot, holding back vacuum, so library callers
        should close in a `finally` or use this.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the connection on leaving the `with` block."""
        self.close()

    @abstractmethod
    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a list of tuples."""

    def cancel(self) -> None:  # noqa: B027 - optional by design, see below
        """Abort whatever query is in flight, from another thread.

        Both sides are queried on worker threads, so a Ctrl-C reaches the main
        thread while the workers sit blocked on the database. Without this the
        interrupt is not acted on until the queries finish on their own - which
        on the ten-minute diff someone actually wants to abort is the whole
        problem. Optional: a dialect that cannot do it inherits a no-op and
        simply behaves as before.
        """

    # ------------------------------------------------------------ metadata

    #: Where an unqualified table name is looked up. `public` on PostgreSQL,
    #: `main` on DuckDB.
    default_schema: str = "public"

    def columns(self, table: str) -> list[Column]:
        """Introspect a table's columns.

        Both supported engines expose `information_schema.columns`, so this is
        shared. Override it in a dialect whose engine does not.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            f"select column_name, data_type from {self._columns_catalog(schema)} "
            f"where table_schema = {self.string_literal(schema)} "
            f"and table_name = {self.string_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

    def _columns_catalog(self, schema: str) -> str:
        """The relation that lists columns for tables in `schema`.

        One global `information_schema.columns` on PostgreSQL, DuckDB, MySQL
        and Snowflake. BigQuery has no global one: its `INFORMATION_SCHEMA`
        lives inside each dataset (`` `project.dataset`.INFORMATION_SCHEMA ``),
        and the region-wide view needs project-level permissions a narrowly
        granted service account does not hold. A hook rather than a second
        copy of `_not_found`, so the near-miss hint keeps working there too.
        """
        return "information_schema.columns"

    def string_literal(self, value: str) -> str:
        """Quote a schema or table name as a string literal for this engine.

        Standard SQL doubles an embedded quote, which is `sql_literal`.
        BigQuery does not accept that - it escapes with a backslash, and treats
        a bare backslash as the start of an escape sequence - so it overrides
        this. Only ever used for names, never for user data.
        """
        return sql_literal(value)

    def int_literal(self, value: int) -> str:
        """Render an integer the bucket expression needs as a SQL literal.

        Plain digits everywhere but BigQuery, which refuses to *parse* an
        integer literal outside INT64 ("Invalid integer literal") - and the
        precomputed key span of a range as wide as bigint is up to 2^64. Every
        other engine reads such a literal as numeric/hugeint on its own.
        """
        return str(value)

    def _not_found(self, table: str, schema: str, name: str) -> str:
        """Explain a missing table, and point at a case mismatch if that is it.

        Identifiers are always quoted, so lookup is exact and case-sensitive.
        That is correct, but unquoted SQL gets folded to lower case by the
        server, so `--a-table Orders` against a table the server stored as
        `orders` is a very easy mistake with a very unhelpful default message.
        """
        if self._exists_but_unreadable(schema, name):
            return (
                f"table {table} exists but this role cannot read it. "
                f"`information_schema` only lists tables you hold privileges "
                f"on, so a missing GRANT looks exactly like a missing table. "
                f"Ask for SELECT on {schema}.{name}."
            )

        message = f"table not found: {table} (looked in schema {schema!r})"
        try:
            near = self.query(
                "select distinct table_schema, table_name "
                f"from {self._columns_catalog(schema)} "
                f"where lower(table_name) = lower({self.string_literal(name)})"
            )
        except Exception:  # noqa: BLE001 - diagnosing an error must never replace it
            return message
        others = [f"{s}.{t}" for s, t in near if (s, t) != (schema, name)]
        if others:
            message += (
                f". Names are matched exactly, including case - did you mean "
                f"{' or '.join(sorted(others))}?"
            )
        return message

    def _exists_but_unreadable(self, schema: str, name: str) -> bool:
        """Whether the table is really there and this role simply cannot see it.

        Engines with a privilege model filter `information_schema` by what the
        current role may access, so "not found" and "not granted" arrive as the
        same empty result - and telling someone their table does not exist when
        it does sends them hunting for a typo instead of asking for a GRANT.
        Default False for engines with no privilege model.
        """
        return False

    @abstractmethod
    def quote(self, identifier: str) -> str:
        """Quote one identifier. The injection boundary - see the dialects."""

    def qualify(self, table: str) -> str:
        """Quote a possibly schema-qualified table name."""
        return ".".join(self.quote(part) for part in table.split("."))

    def split_table(self, table: str, default_schema: str) -> tuple[str, str]:
        """Split ``schema.table``, refusing anything it cannot honour.

        Without the length check a three-part name like ``db.schema.table``
        silently fell through to the unqualified branch and was looked up as a
        *table* called ``db`` in the default schema - so the eventual "table
        not found" named a schema the user never mentioned.
        """
        parts = table.split(".")
        if len(parts) == 1:
            return default_schema, parts[0]
        if len(parts) == 2:
            return parts[0], parts[1]
        raise self._err(
            f"table name {table!r} has {len(parts)} dot-separated parts; "
            f"expected 'table' or 'schema.table'"
        )

    # ----------------------------------------------------------- rendering

    @abstractmethod
    def normalize(self, column: Column) -> str:
        """SQL expression rendering ``column`` as canonical text.

        Implementations MUST be null-safe: a NULL value renders as
        :data:`NULL_SENTINEL`, never as SQL NULL.
        """

    @abstractmethod
    def hash_expr(self, text_expr: str) -> str:
        """SQL expression folding canonical text into a 60-bit integer."""

    @abstractmethod
    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. ``/`` is *not* portable: PostgreSQL
        truncates on integers, DuckDB promotes to double."""

    @abstractmethod
    def sum_wide(self, expr: str) -> str:
        """Sum ``expr`` in a type wide enough not to overflow.

        Row hashes are up to 2^60; summing millions of them overflows a
        64-bit accumulator, so both sides must aggregate in a 128-bit or
        arbitrary-precision type.
        """

    @abstractmethod
    def wide_int(self, expr: str) -> str:
        """Widen an integer expression beyond 64 bits before arithmetic.

        The bucket expression multiplies the key offset by the bucket count,
        and ``span * n_segments`` exceeds a signed 64-bit integer as soon as
        the key range is wider than about 2.9e17 - which is ordinary for
        sparse bigint keys, and guaranteed once keys are hashed into the full
        bigint range. Both engines raise rather than wrap, so this shows up as
        a crash rather than a wrong answer, but it is still a hole.
        """

    # ------------------------------------------------------------ building

    def separator_sql(self) -> str:
        """SQL for the field separator, ASCII Unit Separator (0x1f).

        `chr(31)` on PostgreSQL and DuckDB. MySQL spells it `char(31)`, and
        must force a character result (`using utf8mb4`) or the join returns a
        binary string, which would push the composite-key identity back to
        bytes when fetched.
        """
        return SEPARATOR_SQL

    def float_scale_for(self, column: Column) -> int:
        """The float scale, refused if a FLOAT column cannot be rendered at it.

        See `FLOAT_MAX_SCALE`: past it at least one engine would round from a
        clamped or truncated value without saying so.
        """
        if self.float_scale > FLOAT_MAX_SCALE:
            raise self._err(
                f"FLOAT column {column.name!r} cannot be compared at "
                f"float_scale {self.float_scale}; the maximum is {FLOAT_MAX_SCALE}"
            )
        return self.float_scale

    def float_caveat(self) -> str | None:
        """A known gap in this engine's FLOAT rendering, or None.

        None means the dialect renders finite doubles exactly as
        `canonical_float_text` does, verified against a live engine. A dialect
        that cannot claim that returns the caveat, and the engine prints it as
        a warning whenever a FLOAT column is compared, so the limitation
        surfaces in the output instead of hiding in a docstring.
        """
        return None

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """Known limitations of this side's comparison, for the output.

        CLAUDE.md section 8: an approximation the docs admit to must also reach
        the output, or a reader takes the verdict as exact. The engine calls
        this once per side with that side's compared columns, after
        `connect`, and prints every line as a warning naming the side. A
        dialect returns what holds for this connection - SQL Server, say,
        whether the walk is pinned to one snapshot, which only the server can
        tell it. Default: nothing to report.
        """
        return []

    # --------------------------------------------------- SQL spelling hooks
    #
    # The shared queries below are standard SQL that every engine before
    # SQL Server accepted. Each hook returns exactly the text the queries
    # always had, so overriding is the only way any engine's SQL changes.

    def count(self, expr: str = "*") -> str:
        """`count(expr)`. SQL Server overrides it with `count_big`, because its
        `count` returns a 32-bit int and raises past 2,147,483,647 rows."""
        return f"count({expr})"

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count the distinct key identities - the uniqueness check.

        Equality here is the engine's, and on SQL Server that is the column's
        collation: case-insensitive by default, and blind to trailing spaces
        under every collation, so two genuinely distinct text keys would merge
        and raise a false "not unique". A dialect with such equality counts
        something exact instead. Default `count(distinct identity)`.
        """
        return f"count(distinct {identity})"

    def group_by(self, position: int, expr: str) -> str:
        """How the checksum query names its grouping column.

        By position (`1`) everywhere but SQL Server, whose GROUP BY accepts
        neither an ordinal nor a select-list alias, so it repeats the
        expression - `expr` is exactly the text the select list holds.
        """
        return str(position)

    def null_sentinel_sql(self) -> str:
        r"""SQL producing the NULL sentinel as a string literal.

        PostgreSQL and DuckDB take `\N` verbatim - neither processes
        backslash escapes in a plain string literal. MySQL does, and its
        setting is toggled by `sql_mode`, so a literal there is doubly unsafe;
        it overrides this to build the two bytes from `CHAR(92)` instead.
        """
        return f"'{NULL_SENTINEL}'"

    def row_text(self, columns: Sequence[Column]) -> str:
        """Join every column's canonical text into one string per row.

        Fields are separated by ASCII Unit Separator, which effectively never
        occurs in warehouse string data - so ("ab", "c") cannot collide with
        ("a", "bc").
        """
        if not columns:
            # Two tables can legitimately share only their key - after
            # `--columns`/`--exclude`, or when the schemas have diverged
            # entirely. `concat_ws(chr(31), )` is a syntax error, so render a
            # constant instead. The checksum query never passes an empty column
            # set here: `segment_checksums` always folds the key in, so in that
            # mode the key itself is what gets hashed. This branch stays as a
            # defensive fallback for any other caller.
            return "''"
        return self._concat([self.normalize(c) for c in columns])

    def _concat(self, parts: list[str]) -> str:
        """Join rendered columns, nesting to stay under the argument limit.

        PostgreSQL's `max_function_args` is 100 and is fixed at compile time,
        so a flat `concat_ws(sep, c1, ..., cN)` raises "cannot pass more than
        100 arguments to a function" the moment a table has ~99 comparable
        columns - which a denormalised warehouse fact table routinely does.
        DuckDB happily accepts 150, so the failure was asymmetric: the same
        table worked on one side and not the other.

        Nesting is **exact, not an approximation**. `concat_ws` joins its
        arguments with the separator and skips only NULLs, and every argument
        here has already been through `coalesce`, so none is ever NULL.
        Therefore `concat_ws(s, concat_ws(s, a, b), c)` is byte-identical to
        `concat_ws(s, a, b, c)`, and a table narrow enough to fit in one call
        renders exactly the SQL it always did - no checksum moves.
        """
        if len(parts) <= MAX_CONCAT_ARGS:
            return f"concat_ws({self.separator_sql()}, {', '.join(parts)})"
        groups = [
            self._concat(parts[i : i + MAX_CONCAT_ARGS])
            for i in range(0, len(parts), MAX_CONCAT_ARGS)
        ]
        return self._concat(groups)

    def row_hash(self, columns: Sequence[Column]) -> str:
        """Fold a whole row down to one 60-bit integer, inside the engine.

        This is what makes the tool cheap: summed per bucket, it means a few
        integers cross the network instead of the table.
        """
        return self.hash_expr(self.row_text(columns))

    def key_spec(self, table: str, *names: str) -> KeySpec:
        """Build a `KeySpec` for these columns of this table.

        Introspects the table, then decides on hashing the same way the engine
        does: a single integer column is used directly, anything else - a uuid,
        a natural string key, several columns together - is hashed.

        The engine builds its own specs so it can compare the two sides' types
        before deciding. This is for calling a dialect on its own.
        """
        if not names:
            raise self._err("a key needs at least one column")
        found = {c.name: c for c in self.columns(table)}
        missing = [n for n in names if n not in found]
        if missing:
            raise self._err(
                f"key column(s) {missing} not in {table}. Columns are: {sorted(found)}"
            )
        cols = tuple(found[n] for n in names)
        hashed = not (len(cols) == 1 and cols[0].logical_type is LogicalType.INTEGER)
        return KeySpec(cols, hashed)

    def key_bucket(self, key: KeySpec) -> str:
        """The integer the bisection divides.

        A single integer column is itself; anything else is hashed down to 60
        bits. Collisions here are harmless - two rows sharing a bucket are
        still compared individually by `key_identity`.
        """
        if not key.hashed:
            return self.quote(key.columns[0].name)
        return self.hash_expr(self.row_text(key.columns))

    def key_identity(self, key: KeySpec) -> str:
        """The key as the user would recognise it, and as rows are matched by.

        Never the hash. Identity has to be exact, or two unrelated rows that
        happened to collide would be compared against each other - a false
        difference at best and a masked one at worst.
        """
        if not key.hashed:
            return self.quote(key.columns[0].name)
        return self.row_text(key.columns)

    # ------------------------------------------------------------- queries

    def key_stats(self, table: str, key: KeySpec) -> KeyStats:
        """Key range plus row and distinct-key counts, in one scan.

        The range is over the *bucketing* value, which is the column itself for
        an integer key and its hash otherwise. The counts are over the
        *identity*, because that is what decides whether two rows are the same
        row - counting distinct hashes would let a collision hide a duplicate.

        Uniqueness rides along with min/max deliberately: the query already has
        to visit the key, and without it a non-unique key goes undetected,
        collapsing rows in `fetch_range` so their differences disappear.
        """
        bucket = self.key_bucket(key)
        identity = self.key_identity(key)
        # `count(identity)` counts non-NULL keys only, while `count(*)` counts
        # every row. Carrying both is what lets a NULL key be diagnosed as a
        # NULL key rather than misreported as a duplicate - `count(distinct)`
        # also ignores NULLs. For a hashed key the identity is coalesced text
        # and so never NULL, which means a NULL component shows up as a
        # duplicate instead; that is the honest limit of doing this in one pass.
        sql = (
            f"select min({bucket}), max({bucket}), {self.count()}, "
            f"{self.count(identity)}, {self.count_distinct_key(key, identity)} "
            f"from {self.qualify(table)}"
        )
        lo, hi, rows, non_null, distinct = self.query(sql)[0]
        if lo is None:
            return KeyStats(None, None, int(rows), 0, int(non_null))
        try:
            return KeyStats(int(lo), int(hi), int(rows), int(distinct), int(non_null))
        except (TypeError, ValueError) as exc:  # pragma: no cover - see below
            # Only reachable if a dialect's `key_bucket` returned something
            # non-integer; the engine hashes any non-integer key before we get
            # here, so this is a guard against a broken dialect rather than
            # against user data.
            raise self._err(
                f"key {key.label} in {table} did not produce an integer to "
                f"bisect on (min value {lo!r})"
            ) from exc

    def segment_checksums(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> dict[int, tuple[int, int]]:
        """Return ``{segment_index: (row_count, checksum)}`` for ``[lo, hi)``.

        This is the whole point of the tool: one query per side per level,
        with the hashing pushed into the engine. Nothing but a handful of
        integers crosses the network.
        """
        return {
            int(seg): (int(count), int(checksum or 0))
            for seg, count, checksum in self.query(
                self._segment_sql(table, key, columns, lo, hi, n_segments)
            )
        }

    def _segment_sql(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
    ) -> str:
        """The checksum query. Split out so its shape can be tested directly."""
        k = self.key_bucket(key)
        # Every part of the bucket expression has to survive a key range as wide
        # as bigint itself, which is what a hashed key produces.
        #
        # 1. Widen the key *before* subtracting, not after. `wide_int(k - lo)`
        #    still computes `k - lo` in the column's own type first, and that
        #    overflows outright when lo is near the bottom of the range and the
        #    key is near the top.
        # 2. Emit the span as one precomputed literal. Letting SQL evaluate
        #    `hi - lo` puts the same overflow back.
        # 3. Bound the range inclusively. `hi` is `max_key + 1`, so for a table
        #    holding the largest bigint it is one past what the type can hold;
        #    `<= hi - 1` keeps every literal inside the column's own range.
        #
        # Widening is unconditional rather than only for wide ranges. The
        # conditional version was measured at 10M rows and saved nothing - 39.3s
        # against 38.4s, inside run-to-run noise, because the cost here is
        # dominated by MD5 over every row, not by integer arithmetic. Paying a
        # couple of percent to delete a second code path is the right trade in
        # the one function whose off-by-one would make the walker skip rows.
        offset = f"({self.wide_int(k)} - ({self.int_literal(lo)}))"
        bucket = self.int_div(
            f"{offset} * {n_segments}", f"({self.int_literal(hi - lo)})"
        )
        # Fold the *key* into every row's hash, not just the comparable columns.
        # A count plus a content-only sum cannot see a same-content insert and
        # delete in one bucket: the counts balance (one in, one out) and equal
        # content sums to the same value, so the bucket reads clean - a false
        # "identical", on data as ordinary as two rows sharing a status or an
        # empty string. Including the key makes an inserted key and a deleted
        # key hash to different values, so the bucket sum changes and the walker
        # recurses in. This only ever *adds* sensitivity: a checksum that
        # differs is always re-checked by downloading and comparing the real
        # rows, so an incidental mismatch costs a query, never a wrong verdict.
        # It also subsumes the no-comparable-columns mode, where the key becomes
        # the only thing hashed. `columns` never contains the key, so the key is
        # hashed exactly once.
        content = self.row_hash([*key.columns, *columns])
        return (
            f"select {bucket} as seg, {self.count()}, "
            f"{self.sum_wide(content)} "
            f"from {self.qualify(table)} "
            f"where {k} >= {self.int_literal(lo)} and {k} <= {self.int_literal(hi - 1)} "
            f"group by {self.group_by(1, bucket)}"
        )

    def fetch_range(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
    ) -> dict[int | str, tuple[str, ...]]:
        """Download canonical text for every row in ``[lo, hi)``.

        Only ever called on ranges the checksums already proved to differ,
        and only once they are small enough to be cheap.
        """
        bucket = self.key_bucket(key)
        identity = self.key_identity(key)
        # With no comparable columns the row tuple is empty and only key
        # presence distinguishes the sides - see `row_text`.
        exprs = "".join(", " + self.normalize(c) for c in columns)
        # Selected by identity, filtered and ordered by bucket. Those differ
        # for a hashed key, and the distinction is the whole safety property:
        # two rows may share a bucket, but they are still returned and compared
        # under their own keys.
        #
        # Inclusive upper bound, for the same reason as the checksum query:
        # `hi` is `max_key + 1`, which for a table holding the largest bigint is
        # one past what the column type can represent.
        sql = (
            f"select {identity}{exprs} from {self.qualify(table)} "
            f"where {bucket} >= {lo} and {bucket} <= {hi - 1} order by {bucket}"
        )
        out: dict[int | str, tuple[str, ...]] = {}
        for row in self.query(sql):
            rk: int | str = row[0] if key.hashed else int(row[0])
            if rk in out:
                # Second line of defence behind the up-front uniqueness check
                # in `key_stats`: a key that duplicates only inside a fetched
                # range would otherwise overwrite the earlier row and silently
                # drop a real difference.
                raise self._err(
                    f"duplicate key {rk!r} in {table}: key {key.label} is not "
                    f"unique, so rows cannot be compared one-to-one"
                )
            out[rk] = tuple(row[1:])
        return out


def get_dialect(
    connection_string: str,
    side: str = "?",
    float_scale: int = DEFAULT_FLOAT_SCALE,
) -> Dialect:
    """Open a connection and return the dialect for it.

    Drivers are imported lazily so a DuckDB-only user never needs a PostgreSQL
    driver installed, and vice versa.
    """
    scheme = connection_string.split(":", 1)[0].lower()
    if scheme in ("duckdb",):
        from parity.dialects.duckdb_dialect import DuckDBDialect

        dialect: Dialect = DuckDBDialect(float_scale=float_scale, side=side)
    elif scheme in ("postgres", "postgresql"):
        from parity.dialects.postgres_dialect import PostgresDialect

        dialect = PostgresDialect(float_scale=float_scale, side=side)
    elif scheme in ("mysql",):
        from parity.dialects.mysql_dialect import MySQLDialect

        dialect = MySQLDialect(float_scale=float_scale, side=side)
    elif scheme in ("snowflake",):
        from parity.dialects.snowflake_dialect import SnowflakeDialect

        dialect = SnowflakeDialect(float_scale=float_scale, side=side)
    elif scheme in ("bigquery",):
        from parity.dialects.bigquery_dialect import BigQueryDialect

        dialect = BigQueryDialect(float_scale=float_scale, side=side)
    elif scheme in ("mssql", "sqlserver"):
        from parity.dialects.mssql_dialect import SQLServerDialect

        dialect = SQLServerDialect(float_scale=float_scale, side=side)
    else:
        raise ValueError(
            f"[side {side}] no dialect for scheme {scheme!r}. "
            f"Supported: duckdb, postgres, mysql, snowflake, bigquery, mssql."
        )
    try:
        dialect.connect(connection_string)
    except ValueError:
        # Already one of ours, and already says which side. Re-wrapping would
        # print the prefix twice.
        raise
    except Exception as exc:
        # A bare driver error says nothing about which of the two endpoints
        # failed, which is the first thing anyone needs to know.
        raise ValueError(
            f"[side {side}: {dialect.name}] could not connect: {exc}"
        ) from exc
    return dialect


def require_matching_scales(a: Dialect, b: Dialect) -> None:
    """Refuse to compare two sides that round floats differently.

    If the scales disagree, every DECIMAL and FLOAT row renders to different
    canonical text and the tool reports the whole table as changed. That looks
    like a catastrophic migration bug rather than a configuration mistake, so
    fail before any query runs.
    """
    if a.float_scale != b.float_scale:
        raise ValueError(
            f"float_scale differs between sides: A={a.float_scale} "
            f"B={b.float_scale}. Both sides must round identically or every "
            f"float and decimal row reports as different."
        )


def map_type(raw: str) -> LogicalType:
    """Map an engine's type name onto a logical category."""
    t = raw.lower().split("(")[0].strip()
    if t in {
        "int",
        "int2",
        "int4",
        "int8",
        "integer",
        "bigint",
        "smallint",
        "tinyint",
        "hugeint",
        "utinyint",
        "usmallint",
        "uinteger",
        "ubigint",
        "serial",
        "bigserial",
    }:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric"}:
        return LogicalType.DECIMAL
    if t in {"float", "float4", "float8", "double", "real", "double precision"}:
        return LogicalType.FLOAT
    if t in {"bool", "boolean"}:
        return LogicalType.BOOLEAN
    if t in {"date"}:
        return LogicalType.DATE
    if t.startswith(("timestamp", "datetime")):
        return LogicalType.TIMESTAMP
    if t in {
        "text",
        "varchar",
        "char",
        "bpchar",
        "character varying",
        "character",
        "string",
        "uuid",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
