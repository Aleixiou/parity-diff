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

import dataclasses
import difflib
import random
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, TypeVar

from parity.dialects.base import Dialect, require_matching_scales
from parity.reference import UNCHECKED, canonical_text, column_token, describe
from parity.types import (
    Column,
    DiffResult,
    DiffStats,
    KeySpec,
    LogicalType,
    RowDiff,
    Verification,
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

#: Rows the independent sample check re-compares from raw values, about. A
#: table this size or smaller is compared in full. The cost is one scan per
#: side that hashes the key only, plus this many rows downloaded.
DEFAULT_VERIFY_SAMPLE = 10_000

#: Failures listed in full before the rest are only counted.
_MAX_LISTED = 20

#: Pairs of logical types whose canonical text can coincide even though the
#: types differ, so a column of one type against a column of the other is not
#: predicted to differ on every row. DECIMAL/FLOAT share one rounded encoding;
#: an UNKNOWN type (a PostgreSQL or DuckDB enum, say) renders as its raw text,
#: which is exactly what a STRING column on the other side holds.
_RENDER_ALIKE = (
    _NUMERIC_EQUIVALENT,
    frozenset({LogicalType.STRING, LogicalType.UNKNOWN}),
    # A JSON column against text holding JSON - SQL Server has no JSON type,
    # so its nvarchar documents meet another engine's json - matches wherever
    # the text is already spelled in the canonical layout.
    frozenset({LogicalType.STRING, LogicalType.JSON}),
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


def _near(name: str, cols: dict[str, Column]) -> str:
    """` Did you mean ...?` for a column name not found, or nothing."""
    by_fold = {f: c.name for f, c in cols.items()}
    close = difflib.get_close_matches(name.casefold(), list(by_fold), n=3, cutoff=0.6)
    if not close:
        return ""
    return f" Did you mean {' or '.join(repr(by_fold[f]) for f in close)}?"


def _apply_column_map(
    column_map: Mapping[str, str],
    cols_a: dict[str, Column],
    cols_b: dict[str, Column],
    a_table: str,
    b_table: str,
    exclude: Sequence[str],
    columns: Sequence[str] | None,
) -> tuple[dict[str, Column], dict[str, str]]:
    """Re-key side B's columns so a renamed column matches its side-A name.

    `--map a_name=b_name` says B's `b_name` holds what A calls `a_name`. Every
    later step matches columns by the folded name they are keyed under (see
    `_fold_columns`), so re-keying B's column under A's folded name is all
    the pairing needs: the key, `--columns`, `--exclude`, the compared column
    list and its order all follow from A's names alone, and each side still
    quotes its own stored name. Matching stays case-insensitive.

    Returns side B's re-keyed columns and `{b_fold: a_fold}` for the mapped
    pairs. Anything that would make the pairing ambiguous is refused, never
    resolved by a guess - two A columns onto one B column, a mapped B column
    that a same-named A column would also claim, a B column still matching a
    mapped A column by name - because a column compared against the wrong
    counterpart is a wrong verdict on every row.
    """
    a_to_b: dict[str, str] = {}
    b_to_a: dict[str, str] = {}
    for a_name, b_name in column_map.items():
        a_name, b_name = a_name.strip(), b_name.strip()
        if not a_name or not b_name:
            raise ValueError(f"--map needs a_name=b_name, got {a_name!r}={b_name!r}")
        af, bf = a_name.casefold(), b_name.casefold()
        if af not in cols_a:
            raise ValueError(
                f"[side A] --map names column {a_name!r}, which is not in "
                f"{a_table}.{_near(a_name, cols_a)} Columns are: "
                f"{sorted(c.name for c in cols_a.values())}"
            )
        if bf not in cols_b:
            raise ValueError(
                f"[side B] --map names column {b_name!r}, which is not in "
                f"{b_table}.{_near(b_name, cols_b)} Columns are: "
                f"{sorted(c.name for c in cols_b.values())}"
            )
        if a_to_b.get(af, bf) != bf:
            raise ValueError(
                f"--map pairs side A's {cols_a[af].name!r} with two side B "
                f"columns: {cols_b[a_to_b[af]].name!r} and {cols_b[bf].name!r}"
            )
        if b_to_a.get(bf, af) != af:
            raise ValueError(
                f"--map pairs two side A columns, {cols_a[b_to_a[bf]].name!r} and "
                f"{cols_a[af].name!r}, with one side B column {cols_b[bf].name!r}"
            )
        a_to_b[af], b_to_a[bf] = bf, af

    excluded = {e.casefold() for e in exclude}
    for af, bf in a_to_b.items():
        if af in excluded:
            raise ValueError(
                f"--map and --exclude both name {cols_a[af].name!r}: leave it out "
                f"of one of them"
            )
        # A's column of B's name, left unmapped, would have matched B's
        # column by name - which the map gives to another A column.
        if bf != af and bf in cols_a and bf not in a_to_b and bf not in excluded:
            raise ValueError(
                f"--map pairs side B's {cols_b[bf].name!r} with side A's "
                f"{cols_a[af].name!r}, but side A also has a column "
                f"{cols_a[bf].name!r}, which would otherwise have matched it by "
                f"name. Map side A's {cols_a[bf].name!r} too, or --exclude it"
            )
    # --exclude and --columns take side A's names. A mapped B name that is
    # not also an A name is a slip - say which name to use instead.
    for flag, names in (("--exclude", exclude), ("--columns", columns or ())):
        for n in names:
            f = n.casefold()
            if f in b_to_a and f not in cols_a and f != b_to_a[f]:
                raise ValueError(
                    f"{flag} names {n!r}, side B's name for side A's "
                    f"{cols_a[b_to_a[f]].name!r} (renamed by --map); {flag} "
                    f"takes side A's names"
                )

    rekeyed: dict[str, Column] = {}
    for f, c in cols_b.items():
        k = b_to_a.get(f, f)
        if k in rekeyed:
            # Only reachable when an unmapped B column carries the name of a
            # mapped A column: both would pair with A's column.
            other = rekeyed[k]
            mapped, by_name = (other, c) if f == k else (c, other)
            raise ValueError(
                f"--map pairs side A's {cols_a[k].name!r} with side B's "
                f"{mapped.name!r}, but side B also has a column {by_name.name!r}, "
                f"which matches it by name. Map side B's {by_name.name!r} too "
                f"(to the A column it holds), or compare through a view"
            )
        rekeyed[k] = c
    return rekeyed, b_to_a


def _resolve_key(
    key: str | Sequence[str],
    cols_a: dict[str, Column],
    cols_b: dict[str, Column],
    a_table: str,
    b_table: str,
    warnings: list[str],
    a: Dialect | None = None,
    b: Dialect | None = None,
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
                + (
                    ". If side B renamed it, pair the two with --map A_COL=B_COL"
                    if side == "B"
                    else ""
                )
            )

    a_key = tuple(cols_a[n.casefold()] for n in names)
    b_key = tuple(cols_b[n.casefold()] for n in names)

    # Hash unless it is one integer column on both sides. A single-column key
    # that is integer on one side and text on the other has to be hashed too,
    # or the two sides would bucket by different things entirely.
    # And only if both dialects can bucket it as itself: DuckDB's UHUGEINT is
    # wider than any type the bucket expression can widen it into, so it is
    # hashed like any other key (`Dialect.bisects_natively`).
    single_integer = (
        len(names) == 1
        and a_key[0].logical_type is LogicalType.INTEGER
        and b_key[0].logical_type is LogicalType.INTEGER
        and (a is None or a.bisects_natively(a_key[0]))
        and (b is None or b.bisects_natively(b_key[0]))
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
        # An integer key against a decimal one renders alike: a decimal key
        # is its minimal exact text, `42` where the integer is `42`.
        if (
            not _renders_alike(ta, tb)
            and not ({ta, tb} & _TEXTUAL)
            and {ta, tb} != _INTEGER_DECIMAL
        ):
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


#: An integer on one side and a decimal on the other. Both hold exact numbers,
#: so the pair is compared by value: the integer side renders through the
#: DECIMAL rule (`_integer_as_decimal`).
_INTEGER_DECIMAL = frozenset({LogicalType.INTEGER, LogicalType.DECIMAL})


def _integer_as_decimal(
    a_cols: list[Column], b_cols: list[Column]
) -> tuple[list[Column], list[Column]]:
    """Render an INTEGER column compared against a DECIMAL one as a DECIMAL.

    Engines disagree on whether a scale-0 decimal is an integer: Snowflake
    and Oracle have only NUMBER, and read NUMBER(14,0) as INTEGER, while
    PostgreSQL's numeric(14,0) and DuckDB's DECIMAL(14,0) stay DECIMAL. A
    faithful migration then compared `41886` against `41886.000000` and
    reported every row as different (RT-HX2) - and so did a bigint migrated
    to numeric(18,0) on any engine pair. No mapping fixes that from one side,
    since Snowflake's INT is NUMBER(38,0) and a bigint can land as either.

    So the pair is reconciled here, from both sides' types: the integer
    column takes the DECIMAL rule, which every dialect applies exactly to an
    integer operand (an integer has no places to round), and the pair
    compares by value at the float scale, as DECIMAL against DECIMAL does.
    Only such pairs change; a same-type pair renders exactly as before. Keys
    need nothing: `normalize_key` already renders a decimal key as its
    minimal exact text, `42` like an integer key.
    """
    a_out, b_out = list(a_cols), list(b_cols)
    for i, (ca, cb) in enumerate(zip(a_cols, b_cols, strict=True)):
        if {ca.logical_type, cb.logical_type} == _INTEGER_DECIMAL:
            if ca.logical_type is LogicalType.INTEGER:
                a_out[i] = dataclasses.replace(ca, logical_type=LogicalType.DECIMAL)
            else:
                b_out[i] = dataclasses.replace(cb, logical_type=LogicalType.DECIMAL)
    return a_out, b_out


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
    # different canonical encoding. Rows still compare by that text, so they
    # match wherever the two encodings coincide (BIGINT 1 and VARCHAR '1' both
    # render `1`) and differ wherever they do not (DECIMAL 1.5 renders
    # `1.500000`, VARCHAR '1.5' stays `1.5`). Many differences then look like
    # a catastrophic data change but are a schema change, and a clean run
    # still hides the type change itself - so name it either way.
    for f in shared:
        name = cols_a[f].name
        ta, tb = cols_a[f].logical_type, cols_b[f].logical_type
        if ta is tb or {ta, tb} <= _NUMERIC_EQUIVALENT:
            continue
        if {ta, tb} == _INTEGER_DECIMAL:
            # Compared by value (`_integer_as_decimal`), so no row differs
            # for the type alone - but a decimal with places compared against
            # an integer is a schema change worth naming, and the comparison
            # is at the float scale, not exact.
            warnings.append(
                f"column {name!r} is {_type_label(cols_a[f])} on side A but "
                f"{_type_label(cols_b[f])} on side B; compared by value, both "
                f"rendered as decimals at the float scale"
            )
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
        warnings.append(
            f"{what}: a row matches only where both types render the same "
            f"text (integer 1 and text '1' both render 1 and compare equal; "
            f"decimal 1.5 renders 1.500000 against text '1.5' and differs), so "
            f"expect differences wherever the spellings diverge, and a match "
            f"does not mean the type change is harmless"
        )

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
    verify_sample: int = DEFAULT_VERIFY_SAMPLE,
    where_a: str | None = None,
    where_b: str | None = None,
    column_map: Mapping[str, str] | None = None,
) -> DiffResult:
    """Compare ``a_table`` on side ``a`` with ``b_table`` on side ``b``.

    After the checksum walk, about ``verify_sample`` rows are re-compared from
    the raw values the drivers return, by the documented rules in Python
    (`parity.reference`) - an independent second opinion that a mistake in a
    dialect's SQL cannot fool. ``verify_sample=0`` switches it off.

    ``where_a`` / ``where_b`` filter each side's rows by a SQL predicate in
    that engine's own dialect: only matching rows are compared, and every
    query that reads the table applies it (`Dialect.rows_from`). The verdict
    is about the filtered rows only, and the result says so.

    ``column_map`` pairs renamed columns, ``{a_name: b_name}``: side B's
    ``b_name`` is compared with side A's ``a_name``. ``key``, ``columns`` and
    ``exclude`` take side A's names; differences are reported under them.

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
    if verify_sample < 0:
        raise ValueError(f"verify_sample must be >= 0, got {verify_sample}")
    # Checked before any query: two sides rounding floats differently would
    # report every float row as changed.
    require_matching_scales(a, b)
    # A row filter is checked before any query, on the side that runs it.
    for dialect, where in ((a, where_a), (b, where_b)):
        if where is not None:
            dialect.check_where(where)
    where_a = where_a.strip() if where_a else None
    where_b = where_b.strip() if where_b else None
    # Passed to the four queries that read rows, and only when set, so an
    # unfiltered run makes exactly the calls - and emits exactly the SQL -
    # it always did.
    wa: tuple[str, ...] = (where_a,) if where_a else ()
    wb: tuple[str, ...] = (where_b,) if where_b else ()

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
        # Renamed columns: side B's are re-keyed under side A's folded
        # names, so everything below pairs them exactly as it pairs columns
        # that kept their name - in the same position of the row text.
        b_to_a: dict[str, str] = {}
        if column_map:
            cols_b, b_to_a = _apply_column_map(
                column_map, cols_a, cols_b, a_table, b_table, exclude, columns
            )
        # Introspection is metadata, not a scan; do not inflate the query count
        # users read as "how much work did this cost".
        stats.queries -= 2

        key_a, key_b = _resolve_key(key, cols_a, cols_b, a_table, b_table, warnings, a, b)
        key_folds = {c.name.casefold() for c in key_a.columns}

        shared, schema_differences = _select_columns(
            cols_a, cols_b, key_folds, columns, exclude, warnings
        )
        # A column the login cannot read is missing from `columns` itself
        # (PostgreSQL column-level grants), so the one-sided check above never
        # saw it: never compared, and never a pass (RT-HP1) - unless the user
        # set it aside with --exclude or --columns.
        excluded = {n.casefold() for n in exclude}
        wanted = {n.casefold() for n in columns} if columns else None
        for side, dialect, table in (("A", a, a_table), ("B", b, b_table)):
            hidden = sorted(dialect.unreadable_columns(table))
            unread = [
                h
                for h in hidden
                if h.casefold() not in excluded
                and (wanted is None or h.casefold() in wanted)
            ]
            for names, out in (
                (unread, schema_differences),
                ([h for h in hidden if h not in unread], warnings),
            ):
                if names:
                    out.append(
                        f"not compared, side {side} cannot read {names} of {table}: "
                        "this login lacks SELECT on those columns (column-level "
                        "privileges)"
                    )
        a_cols = [cols_a[f] for f in shared]
        b_cols = [cols_b[f] for f in shared]
        # Sub-microsecond digits are compared exactly when both sides hold
        # them, and truncated - with a caveat from the dialect - when either
        # side holds microseconds only (COV-2: DuckDB kept them, SQL Server
        # and Snowflake truncated, so one stored instant read as two).
        for i, (ca, cb) in enumerate(zip(a_cols, b_cols, strict=True)):
            if a.holds_sub_micro(ca) and b.holds_sub_micro(cb):
                a_cols[i] = dataclasses.replace(ca, sub_micro=True)
                b_cols[i] = dataclasses.replace(cb, sub_micro=True)
        a_cols, b_cols = _integer_as_decimal(a_cols, b_cols)
        # JSON is compared as each engine's normal form, and the engines
        # normalise differently (key order, duplicate keys, number spelling).
        # Equal text is always the same document, so this can only cost false
        # alarms - but a run reporting every JSON row of a correct migration
        # as changed must say why, and the dialects know how they differ.
        json_forms = [
            (ca.name, sa, sb)
            for ca, cb in zip(a_cols, b_cols, strict=True)
            if ca.logical_type is LogicalType.JSON
            and cb.logical_type is LogicalType.JSON
            and (sa := a.json_semantics(ca)) != (sb := b.json_semantics(cb))
        ]
        if json_forms:
            names = [n for n, _, _ in json_forms]
            _, sa, sb = json_forms[0]
            warnings.append(
                f"JSON columns {names} are compared as each engine's normalised "
                f"document, and the two engines normalise differently - side A: "
                f"{sa}; side B: {sb}. Documents that are equal can therefore be "
                f"reported as different (never the reverse): compare a canonical "
                f"text form of them through a view on each side if that is not "
                f"what you want"
            )
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
            # Key columns always render every stored digit (`normalize_key`),
            # so they are passed as `sub_micro` and draw no precision caveat.
            exact_keys = [dataclasses.replace(c, sub_micro=True) for c in key_cols]
            for note in dialect.caveats([*exact_keys, *cols]):
                warnings.append(f"side {side} ({dialect.name}): {note}")
        warnings.extend(_filter_caveats(a, b, where_a, where_b))

        ks_a, ks_b = both("key_stats", key_a, *wa, _args_b=(key_b, *wb))

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
        # Zero rows on a side is legitimate (an empty table is identical to
        # another), but it is also what a mistyped filter or the wrong table
        # looks like, so it is said on every run (RT-HS10).
        for side, table, ks in (("A", a_table, ks_a), ("B", b_table, ks_b)):
            if not ks.rows:
                warnings.append(
                    f"0 rows compared on side {side} ({table}): "
                    + (
                        "no row matches its filter"
                        if (wa, wb)[side == "B"]
                        else "it is empty"
                    )
                )

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
                *wa,
                _args_b=(key_b, b_cols, s_lo, s_hi, n, *wb),
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
                *wa,
                _args_b=(key_b, b_cols, r_lo, r_hi, *wb),
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

        def check_totals(
            cs_a: dict[int, tuple[int, int]], cs_b: dict[int, tuple[int, int]]
        ) -> None:
            """The first level covers the whole key range, so each side's
            bucket counts must add up to the rows it reported holding. A server
            setting that caps result rows (MySQL sql_select_limit, Snowflake
            ROWS_PER_RESULTSET) dropped buckets on both sides alike, and they
            then read as empty - identical (RT-G1, G2). Any shortfall, or a
            table changing between the two queries, is never identical."""
            for side, table, cs, ks in (
                ("A", a_table, cs_a, ks_a),
                ("B", b_table, cs_b, ks_b),
            ):
                total = sum(count for count, _ in cs.values())
                if total != ks.rows:
                    unexplained.append(
                        f"side {side} ({table}) holds {ks.rows:,} row(s), but its "
                        f"checksums covered {total:,}: rows were hidden from the "
                        f"comparison (a server setting limiting result rows) or "
                        f"the table changed during the run, so the result is "
                        f"unverified"
                    )

        if bounds:
            # Half-open [lo, hi): +1 so the largest key is inside the range.
            lo, hi = min(bounds), max(bounds) + 1
            #: Each range to walk, with both sides' (count, checksum) for it
            #: from the level above - None for the whole range.
            queue: list[tuple[int, int, tuple[Any, Any] | None]] = [(lo, hi, None)]

            def check_level(
                s_lo: int,
                s_hi: int,
                cs_a: dict[int, tuple[int, int]],
                cs_b: dict[int, tuple[int, int]],
                parent: tuple[Any, Any] | None,
            ) -> None:
                """A range's buckets must add up, count and checksum alike, to
                what the level above measured for the whole range: the sums
                are exact and the buckets tile the range. On one snapshot
                with a deterministic filter they always do. When they do not,
                the rows changed between the two queries - a row filter with
                random() or clock_timestamp(), or a live table on an engine
                without one snapshot - and a difference seen above could
                vanish below without a trace. Never identical, then."""
                if parent is None:
                    return
                for side, table, cs, want in (
                    ("A", a_table, cs_a, parent[0]),
                    ("B", b_table, cs_b, parent[1]),
                ):
                    got = (
                        sum(c for c, _ in cs.values()),
                        sum(h for _, h in cs.values()),
                    )
                    if got != tuple(want):
                        other = " with another checksum" if got[0] == want[0] else ""
                        unexplained.append(
                            f"{what} [{s_lo}, {s_hi}): side {side} ({table}) "
                            f"counted {want[0]:,} row(s) there at one level of "
                            f"the walk, but its buckets at the next add up to "
                            f"{got[0]:,} row(s){other}: "
                            f"its rows changed between two queries (a row filter "
                            f"that is not stable, or a table written during the "
                            f"run), so this range is unverified"
                        )

            def limit_reached() -> bool:
                """Whether enough differences have been collected to stop."""
                return max_diffs is not None and len(diffs) >= max_diffs

            while queue:
                if limit_reached():
                    unchecked += len(queue)
                    break

                s_lo, s_hi, parent = queue.pop()
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
                    check_totals(cs_a, cs_b)
                    va, vb = cs_a.get(0, EMPTY), cs_b.get(0, EMPTY)
                    if va != vb:
                        compare(s_lo, s_hi, va, vb)
                    continue

                n = min(bisection_factor, span)
                cs_a, cs_b = checksums(s_lo, s_hi, n)
                if (s_lo, s_hi) == (lo, hi):
                    check_totals(cs_a, cs_b)
                check_level(s_lo, s_hi, cs_a, cs_b, parent)

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
                        queue.append((b_lo_i, b_hi_i, (va, vb)))

        verification: Verification | None = None
        complete = not unchecked and (max_diffs is None or len(diffs) <= max_diffs)
        if verify_sample and complete and (ks_a.rows or ks_b.rows):
            rows = max(ks_a.rows, ks_b.rows)
            modulus = max(1, rows // verify_sample)
            # Fresh randomness each run, reported in the result: the sample
            # is not predictable from the data, and a run can be reproduced.
            residue = random.SystemRandom().randrange(modulus)
            verification = Verification(modulus, residue, 0, 0)
            try:
                sample_a, sample_b = both(
                    "fetch_sample",
                    key_a,
                    a_cols,
                    modulus,
                    residue,
                    *wa,
                    _args_b=(key_b, b_cols, modulus, residue, *wb),
                )
            except ValueError as exc:
                verification.error = str(exc)
            else:
                stats.rows_sampled = len(sample_a) + len(sample_b)
                _verify(verification, a, b, a_cols, b_cols, sample_a, sample_b, diffs)
            finally:
                stats.queries -= 2
                stats.sample_queries += 2

    # The pins a dialect set at connect decide the canonical text; a row
    # filter or a view's function that changed one mid-walk changed the text
    # (RT-HP6). Checked once, after everything else, outside the query count.
    for side, dialect in (("A", a), ("B", b)):
        try:
            drift = dialect.session_drift()
        except Exception as exc:  # noqa: BLE001 - the verdict must say so, not crash
            drift = [f"session settings could not be re-checked after the walk: {exc}"]
        unexplained.extend(f"side {side} ({dialect.name}): {d}" for d in drift)

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
        verification=verification,
        where_a=where_a,
        where_b=where_b,
        column_map={
            cols_a[af].name: cols_b[af].name
            for bf, af in b_to_a.items()
            # A pair mapping a name onto itself (`id=ID`) renames nothing.
            if bf != af and (af in shared or af in key_folds)
        },
        verify_sample=verify_sample,
    )


def _filter_caveats(
    a: Dialect, b: Dialect, where_a: str | None, where_b: str | None
) -> list[str]:
    """What a row filter does to the verdict, for the output (CLAUDE.md 8).

    A filtered "identical" is a statement about the filtered rows only, and
    a filter re-evaluated by every query is only as stable as its
    expression - so both are said on every filtered run, not left to the docs.
    """
    out: list[str] = []
    for side, dialect, where in (("A", a, where_a), ("B", b, where_b)):
        if where:
            out.append(
                f"side {side} ({dialect.name}) is filtered by ({where}): only "
                f"the rows it selects are compared, so this result says nothing "
                f"about the rest of the table. Each query evaluates it afresh: "
                f"an expression that can change during the run (random(), "
                f"clock_timestamp(), now(), current_timestamp() or "
                f"current_date, unless the engine evaluates it once per run - "
                f"a snapshot need not pin the clock) can select "
                f"different rows in different queries - prefer fixed literals"
            )
    if bool(where_a) != bool(where_b):
        only, other = ("A", "B") if where_a else ("B", "A")
        out.append(
            f"only side {only} is filtered: side {other}'s whole table is "
            f"compared with side {only}'s filtered rows, so every side {other} "
            f"row outside the filter reads as only in {other}"
        )
    return out


def _verify(
    v: Verification,
    a: Dialect,
    b: Dialect,
    a_cols: list[Column],
    b_cols: list[Column],
    sample_a: dict[int | str, tuple[Any, ...]],
    sample_b: dict[int | str, tuple[Any, ...]],
    diffs: list[RowDiff],
) -> None:
    """Re-compare the sampled rows by value and record any contradiction.

    The walk's verdict on each sampled key - equal, `different` in some
    columns, or present on one side only - must match what the raw values
    say. A row the walk called equal whose values differ, a column it missed,
    or a key it never reported is a failure: the checksum path rendered two
    different values alike, which is how every false match this tool has had
    worked. A difference the walk reports that the values do not show is only
    a note - a false alarm, or something the reference rules cannot see.
    """
    v.rows_a, v.rows_b = len(sample_a), len(sample_b)
    names = [c.name for c in a_cols]
    reported = {d.key: d for d in diffs}
    pairs = list(zip(a_cols, b_cols, strict=True))
    checkable = [
        i
        for i, (ca, cb) in enumerate(pairs)
        if LogicalType.UNKNOWN not in (ca.logical_type, cb.logical_type)
    ]
    v.checked = [names[i] for i in checkable]
    v.unchecked = [names[i] for i in range(len(names)) if i not in checkable]
    skipped: dict[str, int] = {}

    def token(dialect: Dialect, column: Column, value: Any) -> Any:
        """One raw value, reduced by the reference rules for its side."""
        return column_token(dialect, column, value)

    failures: list[str] = []
    for k in sorted(set(sample_a) | set(sample_b), key=_key_order):
        ra, rb, rd = sample_a.get(k), sample_b.get(k), reported.get(k)
        if ra is None or rb is None:
            kind = "only_in_b" if ra is None else "only_in_a"
            if rd is None or rd.kind != kind:
                walk = "nothing" if rd is None else rd.kind
                side = "B" if ra is None else "A"
                failures.append(
                    f"key {k!r}: in the sample of side {side} only, but the "
                    f"checksum walk reported {walk} for it"
                )
            continue
        differ: dict[str, tuple[Any, Any]] = {}
        for i in checkable:
            ta = token(a, pairs[i][0], ra[i])
            tb = token(b, pairs[i][1], rb[i])
            if ta is UNCHECKED or tb is UNCHECKED:
                skipped[names[i]] = skipped.get(names[i], 0) + 1
            elif canonical_text(ta) != canonical_text(tb):
                # By canonical text, not by token: the rules call two rows
                # equal iff their text is (CLAUDE.md 4.2), so a STRING '1.5'
                # against a DECIMAL that renders `1.5` must not count here
                # when it does not count in the walk.
                differ[names[i]] = (ta, tb)
        if rd is not None and rd.kind != "different":
            failures.append(
                f"key {k!r}: present on both sides in the sample, but the "
                f"checksum walk reported {rd.kind}"
            )
            continue
        walk_cols = set(rd.columns) if rd is not None else set()
        missed = [n for n in differ if n not in walk_cols]
        if missed:
            detail = "; ".join(
                f"{n}: A={describe(differ[n][0])} B={describe(differ[n][1])}"
                for n in missed
            )
            said = "equal" if rd is None else f"different only in {sorted(walk_cols)}"
            failures.append(
                f"key {k!r}: the checksum walk found it {said}, but the raw "
                f"values differ - {detail}"
            )
        extra = sorted(n for n in walk_cols if n in v.checked and n not in differ)
        if extra and len(v.notes) < _MAX_LISTED:
            v.notes.append(
                f"key {k!r}: reported different in {extra}, which the raw "
                f"values do not show (a false alarm, or a difference outside "
                f"the reference rules)"
            )
    if len(failures) > _MAX_LISTED:
        rest = len(failures) - _MAX_LISTED
        failures = [*failures[:_MAX_LISTED], f"... and {rest} more"]
    v.failures = failures
    for name, count in sorted(skipped.items()):
        v.notes.append(
            f"{count} value(s) of column {name!r} are outside the reference "
            f"rules and were not independently checked"
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
