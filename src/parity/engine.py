"""Engine-agnostic segmented diff.

This module must not import a concrete dialect. It talks to two ``Dialect``
objects through their contract and knows nothing about SQL - that separation is
what makes adding Snowflake or BigQuery a single new file.

The strategy: split the key range into buckets, ask each side for one checksum
per bucket (one query per side per level), and recurse only into buckets whose
checksums disagree. Rows are downloaded solely from ranges already proven to
differ, and only once those ranges are small.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, TypeVar

from parity.dialects.base import Dialect, require_matching_scales
from parity.types import (
    Column,
    DiffResult,
    DiffStats,
    KeySpec,
    LogicalType,
    RowDiff,
)

#: What a bucket looks like when a side returned no group for it at all.
#: `group by` only emits non-empty groups, so an absent bucket genuinely holds
#: zero rows. Two absent buckets match; absent on one side only does not.
EMPTY = (0, 0)

#: DECIMAL and FLOAT render through the same rounded-text encoding, so a column
#: that is decimal on one side and double on the other still compares correctly.
#: This is the common migration case and must not raise a warning.
_NUMERIC_EQUIVALENT = frozenset({LogicalType.DECIMAL, LogicalType.FLOAT})

#: Differences retained before the walk stops. Each `RowDiff` costs about
#: 715 bytes, measured, and the count is linear - so an unbounded walk over
#: two tables that share nothing needs 8.5 GB at ten million rows, which is
#: an out-of-memory kill rather than an answer. Pointing the tool at the
#: wrong table or the wrong environment is exactly the situation a parity
#: check exists to catch, so it must survive it. Pass `None` for no limit.
DEFAULT_MAX_DIFFS = 10_000

#: Pairs of logical types whose canonical text can coincide even though the
#: types differ, so a column of one type against a column of the other is not
#: predicted to differ on every row. DECIMAL/FLOAT share one rounded encoding;
#: an UNKNOWN type (a PostgreSQL or DuckDB enum, say) renders as its raw text,
#: which is exactly what a STRING column on the other side holds.
_RENDER_ALIKE = (
    _NUMERIC_EQUIVALENT,
    frozenset({LogicalType.STRING, LogicalType.UNKNOWN}),
)

#: For a *key*, text against anything can still line up - a varchar holding
#: '42' matches a bigint 42 - so only a mismatch where neither side is text is
#: certain to break every match.
_TEXTUAL = frozenset({LogicalType.STRING, LogicalType.UNKNOWN})

_A = TypeVar("_A")
_B = TypeVar("_B")


def bucket_bounds(i: int, lo: int, hi: int, n: int) -> tuple[int, int]:
    """Key range of bucket ``i``, inverting the SQL bucket expression.

    SQL computes ``bucket = (key - lo) * n / (hi - lo)`` with *truncating*
    integer division. The inverse of that is ceiling division. Getting this
    wrong makes the walker skip key ranges while still reporting a clean
    match - the worst failure this tool can have - so it is isolated here and
    property-tested against the SQL formula in ``tests/test_engine.py``.
    """
    span = hi - lo
    b_lo = lo + -(-(i * span) // n)
    b_hi = lo + -(-((i + 1) * span) // n)
    return b_lo, b_hi


#: How often the main thread wakes while waiting on the two sides.
_POLL_SECONDS = 0.2


def _gather(fa: Future[_A], fb: Future[_B]) -> tuple[_A, _B]:
    """Wait for both sides, staying interruptible while doing so.

    ``Future.result()`` with no timeout blocks in a lock acquire that Windows
    will not deliver a KeyboardInterrupt through, so Ctrl-C is not noticed
    until the query returns on its own - measured at 17 seconds into a 40
    second diff. Passing a timeout makes the wait a series of short sleeps the
    interrupt can land between, at a cost of one cheap wakeup every fifth of a
    second against queries that run for tens of seconds.
    """
    while True:
        try:
            return fa.result(timeout=_POLL_SECONDS), fb.result(timeout=_POLL_SECONDS)
        except FuturesTimeout:
            continue


@contextmanager
def _cancel_on_interrupt(a: Dialect, b: Dialect) -> Iterator[None]:
    """Turn a Ctrl-C into an actual abort of both in-flight queries.

    Both sides are queried on worker threads, so the interrupt lands on the
    main thread while the workers sit blocked in the database driver. Exiting
    the `ThreadPoolExecutor` context then *waits* for those queries to finish -
    so without this, Ctrl-C on the ten-minute diff someone actually wants to
    abort does nothing for ten minutes.

    Cancelling makes the workers' queries raise, the threads end, and the pool
    shuts down promptly. The original KeyboardInterrupt is re-raised either way.
    """
    try:
        yield
    except BaseException:
        for side in (a, b):
            try:
                side.cancel()
            except Exception:  # noqa: BLE001, S110
                # A failed cancel must never replace the real exception -
                # the KeyboardInterrupt below is what the caller needs.
                pass
        raise


def _renders_alike(ta: LogicalType, tb: LogicalType) -> bool:
    """Whether two logical types can produce byte-identical canonical text."""
    return ta is tb or any({ta, tb} <= pair for pair in _RENDER_ALIKE)


def _type_label(column: Column) -> str:
    """The engine's own type name when it has one, else the logical type."""
    return column.raw_type or column.logical_type.value


def _on_side(side: str, dialect: Dialect, method: str, table: str, *args: Any) -> Any:
    """Call one dialect method, making any error it raises name side and table.

    CLAUDE.md section 6: "table not found" is useless when two databases are
    in play, and so is a bare `canceling statement due to statement timeout`
    when both are PostgreSQL. The dialects name the side in the errors they
    raise themselves, but a server error from a checksum or fetch query - a
    timeout, a cancelled query, a dropped connection - arrives from the driver
    bare. Wrapping here, at the one boundary every query crosses, covers every
    engine at once without the engine knowing any of them.
    """
    try:
        return getattr(dialect, method)(table, *args)
    except Exception as exc:
        text = str(exc)
        if text.startswith("[side "):
            # Already one of ours. Re-wrapping would print the prefix twice;
            # only add the table when the message does not already name it.
            if table in text:
                raise
            raise ValueError(f"{text} (table {table})") from exc
        name = getattr(dialect, "name", "?")
        raise ValueError(
            f"[side {side}: {name}] query on {table} failed during {method}: "
            f"{type(exc).__name__}: {text}"
        ) from exc


def _is_tz_aware(column: Column) -> bool:
    """Whether a column carries a timezone, judged from the engine's own name.

    Both engines report `timestamp with time zone` (DuckDB in upper case), and
    `map_type` folds it onto TIMESTAMP by prefix - so the logical type cannot
    tell these apart and the raw name is the only signal there is.
    """
    if column.tz_aware is not None:
        # The dialect knew for certain - see `Column.tz_aware`.
        return column.tz_aware
    return "with time zone" in column.raw_type.lower()


def _key_order(key: int | str) -> tuple[int, int | str]:
    """Sort ints numerically and text lexically, never comparing the two.

    A plain `str()` sort would put 4321 before 5, which is a nasty thing to do
    to a report someone is scanning for a row.
    """
    return (1, key) if isinstance(key, str) else (0, key)


def _fold_columns(columns: list[Column], side: str, table: str) -> dict[str, Column]:
    """Index a side's columns by case-folded name, for cross-engine matching.

    Two engines fold unquoted identifiers to different cases, so a column is
    the same column on both sides when its *folded* name matches. Each side
    still holds its own `Column`, whose real stored name is what gets quoted
    into SQL - only the matching is case-insensitive, never the rendering.

    A table with two columns that differ only in case (possible only through
    quoted identifiers) cannot be folded unambiguously, so it is refused with
    a clear message rather than silently dropping one of them.
    """
    out: dict[str, Column] = {}
    for c in columns:
        fold = c.name.casefold()
        if fold in out:
            raise ValueError(
                f"[side {side}] {table} has two columns that differ only in "
                f"case: {out[fold].name!r} and {c.name!r}. parity matches "
                f"columns case-insensitively across engines and cannot tell "
                f"these apart - rename or quote one, or diff a view that does."
            )
        out[fold] = c
    return out


def _resolve_key(
    key: str | Sequence[str],
    cols_a: dict[str, Column],
    cols_b: dict[str, Column],
    a_table: str,
    b_table: str,
    warnings: list[str],
) -> tuple[KeySpec, KeySpec]:
    """Work out how to match rows up, and whether the key needs hashing.

    A single integer column on both sides is used as-is: the SQL is exactly
    what it has always been and costs nothing extra. Anything else - a uuid, a
    natural string key, several columns together - gets hashed to a 60-bit
    integer so the bisection has something to divide.

    Returns one spec per side. They agree on shape but hold each side's own
    `Column` objects, because a column can be `text` on one side and
    `varchar` on the other and each dialect renders its own - and the key can
    be `ID` on one side and `id` on the other, since `--key` is one name that
    has to resolve against whatever case each engine stored.

    `cols_a` and `cols_b` are keyed by case-folded name (see `_fold_columns`),
    so the same `--key id` finds `id` on PostgreSQL and `ID` on Snowflake.
    """
    # De-duplicate the way matching works: case-insensitively. `--key ID,id`
    # names one column twice, and de-duplicating case-sensitively would turn
    # it into a two-part composite of the same column - hashed, and reported
    # under a doubled key that no reader can look up.
    names: list[str] = []
    seen: set[str] = set()
    for n in [key] if isinstance(key, str) else key:
        if n.casefold() not in seen:
            seen.add(n.casefold())
            names.append(n)
    if not names:
        raise ValueError("--key needs at least one column")

    for side, table, cols in (("A", a_table, cols_a), ("B", b_table, cols_b)):
        missing = [n for n in names if n.casefold() not in cols]
        if missing:
            raise ValueError(
                f"[side {side}] key column(s) {missing} not in {table}. "
                f"Columns are: {sorted(c.name for c in cols.values())}"
            )

    a_key = tuple(cols_a[n.casefold()] for n in names)
    b_key = tuple(cols_b[n.casefold()] for n in names)

    # Hash unless it is one integer column on both sides. A single-column key
    # that is integer on one side and text on the other has to be hashed too,
    # or the two sides would bucket by different things entirely.
    single_integer = (
        len(names) == 1
        and a_key[0].logical_type is LogicalType.INTEGER
        and b_key[0].logical_type is LogicalType.INTEGER
    )
    hashed = not single_integer

    # A key whose type differs between the sides is matched on its canonical
    # text, so a bigint 42 against a decimal(18,0) 42 is '42' against
    # '42.000000': no row can match, and every row is reported missing from
    # one side and present on the other. That reads as total data loss, so it
    # is named as the schema difference it really is. A compared column with
    # the same mismatch is already named in `_select_columns`.
    for ca, cb in zip(a_key, b_key, strict=True):
        ta, tb = ca.logical_type, cb.logical_type
        if not _renders_alike(ta, tb) and not ({ta, tb} & _TEXTUAL):
            warnings.append(
                f"key column {ca.name!r} is {_type_label(ca)} on side A but "
                f"{_type_label(cb)} on side B; the two types render the same "
                f"key value as different text, so rows cannot match on their "
                f"key and every row will report as only in A and only in B. "
                f"Compare through a view that casts one side's key to the "
                f"other's type."
            )
        elif _is_tz_aware(ca) is not _is_tz_aware(cb):
            aware, naive = ("A", "B") if _is_tz_aware(ca) else ("B", "A")
            warnings.append(
                f"key column {ca.name!r} is timezone-aware on side {aware} but "
                f"not on side {naive}; both are read in UTC, so a migration "
                f"that stored local wall-clock time rather than UTC matches no "
                f"row on its key and reports every row as only in A and only "
                f"in B"
            )

    if hashed:
        if len(names) > 1:
            what = "composite key"
        elif _type_label(a_key[0]) == _type_label(b_key[0]):
            what = f"non-integer key ({_type_label(a_key[0])})"
        else:
            what = (
                f"non-integer key ({_type_label(a_key[0])} on side A, "
                f"{_type_label(b_key[0])} on side B)"
            )
        warnings.append(
            f"{what}: bucketed by a 60-bit hash of {names}. Rows are still "
            f"matched and reported by their real key, so a hash collision "
            f"cannot merge two rows - it only puts them in the same bucket."
        )

    return KeySpec(a_key, hashed), KeySpec(b_key, hashed)


def _select_columns(
    cols_a: dict[str, Column],
    cols_b: dict[str, Column],
    key_folds: set[str],
    columns: Sequence[str] | None,
    exclude: Sequence[str],
    warnings: list[str],
) -> tuple[list[str], list[str]]:
    """Decide which columns to compare, explaining anything dropped.

    Returns the folded names to compare, and the schema differences: columns
    present on one side only that the user did not set aside. Those make the
    result non-identical (see `DiffResult.schema_differences`), because their
    contents were never compared.

    `cols_a`/`cols_b` are keyed by case-folded name and the returned list is
    folded names too, so matching is case-insensitive across engines (see
    `_fold_columns`); the caller maps each folded name back to that side's real
    `Column`. Messages echo the user's own tokens, or side A's stored name, so
    a folded lookup never leaks a lower-cased identifier back at the reader.

    Key columns are never compared: they are how rows are matched up, not
    something compared between them.
    """
    both = (set(cols_a) & set(cols_b)) - key_folds
    excluded = {e.casefold() for e in exclude}

    unknown_exclude = [
        e
        for e in dict.fromkeys(exclude)
        if e.casefold() not in cols_a and e.casefold() not in cols_b
    ]
    if unknown_exclude:
        warnings.append(
            f"--exclude named columns that exist on neither side: "
            f"{sorted(unknown_exclude)}"
        )

    shared = sorted(both - excluded)
    if columns:
        # Fold for matching, but keep the first spelling the user gave each
        # column so error messages read back their own words, not a fold.
        token: dict[str, str] = {}
        for c in columns:
            token.setdefault(c.casefold(), c)
        req = list(token)  # folded, de-duplicated, order kept
        nowhere_folds = {f for f in req if f not in cols_a and f not in cols_b}
        nowhere = [token[f] for f in req if f in nowhere_folds]
        one_side = [token[f] for f in req if f not in nowhere_folds and f not in both]
        dropped = [token[f] for f in req if f in excluded]
        # Order matters: key columns are present on both sides but excluded
        # from `both`, so they would otherwise be misreported as one-sided.
        if named_keys := [token[f] for f in req if f in key_folds]:
            raise ValueError(
                f"--columns named the key column(s) {named_keys}; the key is "
                f"how rows are matched up, not something compared between them"
            )
        if nowhere:
            raise ValueError(f"--columns named unknown columns: {nowhere}")
        if one_side:
            raise ValueError(
                f"--columns named columns present on only one side: {one_side}"
            )
        if dropped:
            raise ValueError(f"--columns and --exclude both name: {dropped}")
        shared = [f for f in shared if f in set(req)]

    schema_differences: list[str] = []
    for side, cols, only in (
        ("A", cols_a, sorted(set(cols_a) - set(cols_b) - key_folds)),
        ("B", cols_b, sorted(set(cols_b) - set(cols_a) - key_folds)),
    ):
        # A one-sided column the user set aside - by `--exclude`, or by
        # narrowing with `--columns` - was deliberately left out, so it only
        # earns a note. Any other one is a column whose contents nobody
        # compared, and "identical" would be a claim about data never looked
        # at: the dropped-column migration passing its CI gate.
        set_aside = [f for f in only if columns or f in excluded]
        unexamined = [f for f in only if f not in set_aside]
        if unexamined:
            names = sorted(cols[f].name for f in unexamined)
            schema_differences.append(
                f"not compared, present only on side {side}: {names}"
            )
        if set_aside:
            names = sorted(cols[f].name for f in set_aside)
            warnings.append(f"not compared, present only on side {side}: {names}")

    # A column whose logical type differs between sides renders through a
    # different canonical encoding, so every row would report as changed. That
    # looks like a catastrophic data difference but is really a schema
    # difference, so name it explicitly.
    for f in shared:
        name = cols_a[f].name
        ta, tb = cols_a[f].logical_type, cols_b[f].logical_type
        if ta is tb or {ta, tb} <= _NUMERIC_EQUIVALENT:
            continue
        what = (
            f"column {name!r} is {_type_label(cols_a[f])} on side A but "
            f"{_type_label(cols_b[f])} on side B; values are compared as text"
        )
        if _renders_alike(ta, tb):
            # Text against an unmapped type (an enum, json, inet): the raw
            # text often matches exactly - an enum's labels always do - so
            # predicting that every row differs would be wrong (RT-C13). The
            # mismatch is still a schema fact worth naming.
            warnings.append(
                f"{what}, which matches only where both engines spell the "
                f"value identically"
            )
            continue
        warnings.append(f"{what} and will very likely all differ")

    # Timezone-awareness is invisible to the logical type - both sides map to
    # TIMESTAMP - but it is a real semantic difference and `timestamptz` to
    # `timestamp` is one of the commonest migration changes there is. Sessions
    # are pinned to UTC, so a migration that stored UTC compares clean. One
    # that stored local wall-clock reports *every* row as different, and
    # without this line there is nothing pointing at which axis to look along.
    for f in shared:
        aware_a = _is_tz_aware(cols_a[f])
        if aware_a is _is_tz_aware(cols_b[f]):
            continue
        aware, naive = ("A", "B") if aware_a else ("B", "A")
        warnings.append(
            f"column {cols_a[f].name!r} is timezone-aware on side {aware} but "
            f"not on side {naive}; both are read in UTC, so a migration that "
            f"stored local wall-clock time rather than UTC will show every row "
            f"as different"
        )

    unknown = sorted(
        {cols_a[f].name for f in shared if cols_a[f].logical_type is LogicalType.UNKNOWN}
        | {
            cols_a[f].name
            for f in shared
            if cols_b[f].logical_type is LogicalType.UNKNOWN
        }
    )
    if unknown:
        warnings.append(
            f"unmapped types, compared as raw text (may differ across engines "
            f"for reasons other than the data): {unknown}"
        )

    if not shared:
        warnings.append(
            "no comparable columns: only the presence of each key is checked, "
            "not row contents"
        )
    return shared, schema_differences


def diff(
    a: Dialect,
    b: Dialect,
    a_table: str,
    b_table: str,
    key: str | Sequence[str],
    columns: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    bisection_factor: int = 32,
    threshold: int = 10_000,
    max_diffs: int | None = DEFAULT_MAX_DIFFS,
) -> DiffResult:
    """Compare ``a_table`` on side ``a`` with ``b_table`` on side ``b``.

    The caller owns both dialects and must close them, whether this returns
    or raises: `diff` never closes what it was handed, so a dialect can be
    reused for a second comparison. Close them in a ``finally``, or open them
    with ``with get_dialect(...) as a:`` - a PostgreSQL side left open after
    an error holds its REPEATABLE READ snapshot until the process exits.

    Any error a dialect raises during the walk is re-raised as a
    ``ValueError`` naming the side and table (see `_on_side`), chained to
    the driver's original exception.
    """
    started = time.perf_counter()
    stats = DiffStats()
    warnings: list[str] = []

    if bisection_factor < 2:
        raise ValueError(f"bisection_factor must be >= 2, got {bisection_factor}")
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    if max_diffs is not None and max_diffs < 0:
        # A negative limit is "already reached" before the first bucket, so
        # identical tables would come back "partial". `None` means no limit.
        raise ValueError(f"max_diffs must be >= 0 or None, got {max_diffs}")
    # Checked before any query: two sides rounding floats differently would
    # report every float row as changed.
    require_matching_scales(a, b)

    # Both sides in parallel from here on. One pool for the whole walk - the
    # comparison is almost entirely IO-wait on two independent engines.
    # Order matters: context managers exit in reverse, so `_cancel_on_interrupt`
    # must be the *inner* one. The pool's own exit blocks waiting for its
    # threads, so cancelling has to happen before that, not after.
    with (
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="parity") as pool,
        _cancel_on_interrupt(a, b),
    ):

        def both(
            fn_name: str,
            *args_a: Any,
            _args_b: tuple[Any, ...] | None = None,
        ) -> tuple[Any, Any]:
            """Call the same method on both sides at once and wait for both.

            Every query of the walk goes through here, so `_on_side` makes
            each error, whichever engine raised it, name its side and table.
            """
            fa = pool.submit(_on_side, "A", a, fn_name, a_table, *args_a)
            fb = pool.submit(_on_side, "B", b, fn_name, b_table, *(_args_b or args_a))
            stats.queries += 2
            return _gather(fa, fb)

        cols_a_list, cols_b_list = both("columns")
        # Match identifiers case-insensitively across the two sides. Engines
        # fold unquoted names differently - Snowflake upper-cases, PostgreSQL
        # and DuckDB lower-case - so a Postgres `amount` and its Snowflake
        # `AMOUNT` are the same column and must line up, or the headline use
        # case (diffing a table against its migration) finds no shared columns
        # and no usable key. The map is keyed by the folded name; each side
        # keeps its own Column, with its real stored name, for quoting in SQL.
        cols_a = _fold_columns(cols_a_list, "A", a_table)
        cols_b = _fold_columns(cols_b_list, "B", b_table)
        # Introspection is metadata, not a scan; do not inflate the query count
        # users read as "how much work did this cost".
        stats.queries -= 2

        key_a, key_b = _resolve_key(key, cols_a, cols_b, a_table, b_table, warnings)
        key_folds = {c.name.casefold() for c in key_a.columns}

        shared, schema_differences = _select_columns(
            cols_a, cols_b, key_folds, columns, exclude, warnings
        )
        a_cols = [cols_a[f] for f in shared]
        b_cols = [cols_b[f] for f in shared]
        # A dialect whose FLOAT rendering is not verified against the shared
        # canonical form says so, and the caveat reaches the output rather
        # than only the docs (CLAUDE.md section 8).
        for side, dialect, cols, key_cols in (
            ("A", a, a_cols, key_a.columns),
            ("B", b, b_cols, key_b.columns),
        ):
            floats = [c.name for c in cols if c.logical_type is LogicalType.FLOAT]
            caveat = dialect.float_caveat() if floats else None
            if caveat:
                warnings.append(
                    f"side {side} ({dialect.name}) FLOAT columns {floats}: {caveat}"
                )
            # Anything else the dialect knows makes this comparison less than
            # exact - SQL Server without snapshot isolation, say - reaches the
            # output the same way, naming the side it applies to.
            for note in dialect.caveats([*key_cols, *cols]):
                warnings.append(f"side {side} ({dialect.name}): {note}")

        ks_a, ks_b = both("key_stats", key_a, _args_b=(key_b,))

        # A non-unique key is fatal, not a warning: `fetch_range` maps key to
        # row, so duplicates collapse and their differences disappear.
        # Reporting "identical" for a table we could not actually compare is
        # the one outcome this tool must never produce.
        for side, table, ks in (("A", a_table, ks_a), ("B", b_table, ks_b)):
            # NULL keys first: `count(distinct)` ignores NULLs, so checking
            # uniqueness alone would report a NULL key as a duplicate and send
            # the reader hunting for duplicates that do not exist.
            if ks.has_null_keys:
                raise ValueError(
                    f"[side {side}] key column {key!r} in {table} contains "
                    f"{ks.null_keys:,} NULL value(s). A row with no key cannot "
                    f"be matched to anything on the other side."
                )
            if ks.has_duplicate_keys:
                raise ValueError(
                    f"[side {side}] key column {key!r} in {table} is not "
                    f"unique: {ks.rows:,} rows but only {ks.distinct:,} "
                    f"distinct keys. Rows cannot be compared one-to-one."
                )
        stats.rows_compared_a, stats.rows_compared_b = ks_a.rows, ks_b.rows

        diffs: list[RowDiff] = []
        truncated = False
        #: Checksum mismatches the downloaded rows could not account for.
        unexplained: list[str] = []
        #: Differences or key ranges the walk knowingly did not look at. Any
        #: non-zero value means the answer is partial.
        unchecked = 0
        bounds = [v for v in (ks_a.lo, ks_a.hi, ks_b.lo, ks_b.hi) if v is not None]
        what = "key-hash range" if key_a.hashed else "key range"

        def checksums(
            s_lo: int, s_hi: int, n: int
        ) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
            """One checksum per bucket of `[s_lo, s_hi)`, from both sides."""
            return both(
                "segment_checksums",
                key_a,
                a_cols,
                s_lo,
                s_hi,
                n,
                _args_b=(key_b, b_cols, s_lo, s_hi, n),
            )

        def compare(
            r_lo: int, r_hi: int, va: tuple[int, int], vb: tuple[int, int]
        ) -> None:
            """Download a range whose checksums differ and diff it locally.

            `va`/`vb` are the two sides' (count, sum) for exactly this range.
            The engines have *proved* the range differs, so the download has
            to show why. If it cannot - the rows compare equal, or a side
            hands back a different number of rows than its own checksum
            counted - something between the server's hash and the local
            comparison lost data: a driver dropping trailing bytes, a lossy
            decode, a table changing between two queries. Silently reporting
            "identical" there would be a false match built on evidence to
            the contrary, so the range is recorded as unexplained instead.
            """
            rows_a, rows_b = both(
                "fetch_range",
                key_a,
                a_cols,
                r_lo,
                r_hi,
                _args_b=(key_b, b_cols, r_lo, r_hi),
            )
            stats.rows_downloaded += len(rows_a) + len(rows_b)
            found = _compare_rows(rows_a, rows_b, [c.name for c in a_cols], diffs)
            span = f"{what} [{r_lo}, {r_hi})"
            miscounted = [
                f"side {side} ({table}) counted {want:,} row(s) in its checksum "
                f"but returned {len(got):,}"
                for side, table, want, got in (
                    ("A", a_table, va[0], rows_a),
                    ("B", b_table, vb[0], rows_b),
                )
                if want != len(got)
            ]
            if miscounted:
                unexplained.append(
                    f"{span}: {'; '.join(miscounted)}, so rows were lost between "
                    f"the server and the comparison and this range is unverified"
                )
            elif not found:
                unexplained.append(
                    f"{span}: the checksums of side A ({a_table}) and side B "
                    f"({b_table}) disagree, but the {len(rows_a) + len(rows_b):,} "
                    f"downloaded row(s) compare equal - the difference was lost "
                    f"between the server and the comparison (a driver or decode "
                    f"step altering values, or a table changing mid-run), so this "
                    f"range is unverified"
                )

        if bounds:
            # Half-open [lo, hi): +1 so the largest key is inside the range.
            lo, hi = min(bounds), max(bounds) + 1
            queue: list[tuple[int, int]] = [(lo, hi)]

            def limit_reached() -> bool:
                """Whether enough differences have been collected to stop."""
                return max_diffs is not None and len(diffs) >= max_diffs

            while queue:
                if limit_reached():
                    unchecked += len(queue)
                    break

                s_lo, s_hi = queue.pop()
                span = s_hi - s_lo
                if span <= 0:
                    continue
                stats.segments_checked += 1

                if span <= 1:
                    # Only the *initial* range is ever this small: queued
                    # sub-ranges always have span > 1 (a single-key bucket is
                    # downloaded straight from the loop below, never re-queued).
                    # So this is a one-key table, and it must still be checksum-
                    # qualified rather than downloaded outright, or an identical
                    # one-row table moves rows and breaks the zero-download
                    # promise every other identical table keeps.
                    cs_a, cs_b = checksums(s_lo, s_hi, 1)
                    va, vb = cs_a.get(0, EMPTY), cs_b.get(0, EMPTY)
                    if va != vb:
                        compare(s_lo, s_hi, va, vb)
                    continue

                n = min(bisection_factor, span)
                cs_a, cs_b = checksums(s_lo, s_hi, n)

                differing = [
                    i for i in range(n) if cs_a.get(i, EMPTY) != cs_b.get(i, EMPTY)
                ]
                for position, i in enumerate(differing):
                    # The limit has to be honoured inside the level too. A
                    # single bucket can yield thousands of differences, so
                    # checking only between queue pops would blow past
                    # max_diffs and still call the walk complete.
                    if limit_reached():
                        unchecked += len(differing) - position + len(queue)
                        break
                    va, vb = cs_a.get(i, EMPTY), cs_b.get(i, EMPTY)
                    b_lo_i, b_hi_i = bucket_bounds(i, s_lo, s_hi, n)
                    if max(va[0], vb[0]) <= threshold or b_hi_i - b_lo_i <= 1:
                        compare(b_lo_i, b_hi_i, va, vb)
                    else:
                        queue.append((b_lo_i, b_hi_i))

    diffs.sort(key=lambda d: _key_order(d.key))
    if max_diffs is not None and len(diffs) > max_diffs:
        # A single bucket download can overshoot the limit by a lot. Report the
        # first `max_diffs` in key order and say the rest were not listed.
        unchecked += len(diffs) - max_diffs
        del diffs[max_diffs:]
    if unchecked:
        # Partial answers get a flag on the result, not merely a warning
        # string, so no caller can mistake one for a clean comparison.
        truncated = True
        warnings.append(
            f"stopped at the --max-diffs limit of {max_diffs}; "
            f"{unchecked} further difference(s) or key range(s) were not "
            f"reported, so this is a partial answer"
        )

    stats.seconds = time.perf_counter() - started
    return DiffResult(
        diffs,
        stats,
        a_cols,
        warnings,
        truncated=truncated,
        float_scale=a.float_scale,
        schema_differences=schema_differences,
        unexplained=unexplained,
    )


def _compare_rows(
    rows_a: dict[int | str, tuple[str, ...]],
    rows_b: dict[int | str, tuple[str, ...]],
    names: list[str],
    diffs: list[RowDiff],
) -> int:
    """Diff two downloaded ranges locally; return how many differences it added."""
    before = len(diffs)
    # `strict=True` on every zip below is load-bearing, not tidiness. Both sides
    # render the same column list in the same order, so the tuples must be the
    # same length as `names`. If that invariant ever broke, a plain zip would
    # silently truncate and simply not report the trailing columns - a
    # difference the tool found and then dropped, which is the one outcome it
    # must never produce. Better to raise.
    # Keys are ints or text depending on how the key resolved. Sorting by
    # str() would put 4321 before 5, so order by type first and value
    # second - within one result every key has the same type, so the
    # values are never compared across types.
    for k in sorted(set(rows_a) | set(rows_b), key=_key_order):
        ra, rb = rows_a.get(k), rows_b.get(k)
        if ra is None:
            diffs.append(
                RowDiff(
                    k, "only_in_b", names, {}, dict(zip(names, rb or (), strict=True))
                )
            )
        elif rb is None:
            diffs.append(
                RowDiff(k, "only_in_a", names, dict(zip(names, ra, strict=True)), {})
            )
        elif ra != rb:
            changed = [n for n, x, y in zip(names, ra, rb, strict=True) if x != y]
            diffs.append(
                RowDiff(
                    k,
                    "different",
                    changed,
                    {n: v for n, v in zip(names, ra, strict=True) if n in changed},
                    {n: v for n, v in zip(names, rb, strict=True) if n in changed},
                )
            )
    return len(diffs) - before
