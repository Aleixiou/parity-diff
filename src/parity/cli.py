"""Command line entry point.

The exit codes are the product here: they are what let `parity diff` sit in a
CI pipeline as the gate on a migration cutover. Everything else is reporting.

Output leads with the verdict, then the evidence. The rows-downloaded
percentage is always printed, because it is the proof that the tool pushed the
work into the engines rather than dragging both tables across the network - and
because a number that suddenly reads 100% is how a user finds out something is
wrong with their key column.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from parity import __version__
from parity.dialects.base import decode_canonical_field
from parity.engine import DEFAULT_MAX_DIFFS, DEFAULT_VERIFY_SAMPLE
from parity.types import (
    CHECKSUM_COLLISION_BOUND,
    RISK_CONFIDENCE,
    DiffResult,
    LogicalType,
    RowDiff,
)

#: Exit codes. `1` means "differences found", not "crashed" - a CI job can tell
#: an honest disagreement from a broken connection string.
EXIT_IDENTICAL = 0
EXIT_DIFFERENCES = 1
EXIT_ERROR = 2

KIND_LABELS = {
    "only_in_a": "only in A",
    "only_in_b": "only in B",
    "different": "different",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the whole command line surface.

    Split out from `main` so `--help` can be rendered, and the flags asserted,
    without running anything or importing a database driver.
    """
    parser = argparse.ArgumentParser(
        prog="parity",
        description=(
            "Prove two tables in two different database engines hold the same "
            "data - without moving the data out of either engine."
        ),
        epilog=(
            "exit codes: 0 identical, 1 not identical (row differences, a "
            "column only one side has, or a checksum mismatch the rows could "
            "not explain), 2 error. "
            "Connection strings look like postgres://user:pw@host/db or "
            "duckdb:///path/to.duckdb"
        ),
    )
    parser.add_argument("--version", action="version", version=f"parity {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    d = sub.add_parser(
        "diff",
        help="compare two tables across two engines",
        description="Compare two tables and report exactly which rows differ.",
    )
    d.add_argument("--a", required=True, metavar="CONN", help="side A connection string")
    d.add_argument("--a-table", required=True, metavar="TABLE", help="side A table")
    d.add_argument("--b", required=True, metavar="CONN", help="side B connection string")
    d.add_argument("--b-table", required=True, metavar="TABLE", help="side B table")
    d.add_argument(
        "--key",
        required=True,
        metavar="COL[,COL...]",
        help="the column(s) that identify a row. One integer column is used "
        "directly; a uuid, a text key or several columns together are "
        "hashed so the key space can be bisected, and rows are still "
        "reported by their real key.",
    )
    d.add_argument(
        "--columns",
        metavar="a,b,c",
        help="compare only these columns (default: every column both sides share)",
    )
    d.add_argument("--exclude", metavar="x,y", help="skip these columns")
    d.add_argument(
        "--bisection-factor",
        type=int,
        default=32,
        metavar="N",
        help="key-range buckets per level (default: 32)",
    )
    d.add_argument(
        "--threshold",
        type=int,
        default=10_000,
        metavar="N",
        help="stop bisecting and download once a differing range holds at most "
        "N rows (default: 10000)",
    )
    d.add_argument(
        "--float-scale",
        type=int,
        default=6,
        metavar="N",
        help="decimal places at which floats and decimals are compared "
        "(default: 6). Both sides always use the same value.",
    )
    d.add_argument(
        "--max-diffs",
        type=_non_negative_int,
        default=DEFAULT_MAX_DIFFS,
        metavar="N",
        help=f"stop after N differences (default: {DEFAULT_MAX_DIFFS:,}; 0 for "
        f"no limit). The result is then explicitly marked partial - it "
        f"does not mean the rest matched. The default exists because each "
        f"difference costs memory, so two tables that share nothing would "
        f"otherwise exhaust it rather than answering.",
    )
    d.add_argument(
        "--verify-sample",
        type=_non_negative_int,
        default=DEFAULT_VERIFY_SAMPLE,
        metavar="N",
        help=f"after the comparison, re-compare about N rows (default: "
        f"{DEFAULT_VERIFY_SAMPLE:,}; 0 to skip) from the raw values each "
        f"driver returns, by the documented rules in Python - an independent "
        f"check that a mistake in the tool's own SQL cannot fool. Tables of N "
        f"rows or fewer are re-compared in full. Any contradiction means not "
        f"identical.",
    )
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing; rely on the exit code",
    )
    return parser


def _non_negative_int(value: str) -> int:
    """An integer of at least zero, for `--max-diffs`.

    A negative limit counts as "already reached" before the first bucket, so
    `--max-diffs -1` turned two identical tables into "at least 0 differences",
    exit 1 - a verdict about data never looked at. argparse refuses it, exit 2.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(
            f"must be 0 (no limit) or a positive number, got {n}"
        )
    return n


def _split(value: str | None) -> list[str]:
    """Turn a comma-separated flag value into a list, ignoring blanks.

    So `--exclude "a, b,"` gives ["a", "b"] rather than an empty column name
    that would later fail to match anything.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    """Format a count with its noun, pluralised and thousands-separated."""
    return f"{n:,} {word}{'' if n == 1 else 's'}"


#: Cap on rows printed in human output. Without it a badly mismatched pair of
#: tables buries the terminal in a million lines. This is a *display* limit and
#: is reported as such - it is not `--max-diffs`, which stops the walk and makes
#: the result genuinely partial. Conflating the two would be exactly the kind of
#: "looks complete but isn't" this tool exists to avoid.
DISPLAY_LIMIT = 100

#: Longer than this and A/B go on separate lines instead of side by side.
_SIDE_BY_SIDE_MAX = 44


def _symbols(out: TextIO) -> dict[str, str]:
    """Prefer the nicer glyphs, but never crash on a cp1252 console.

    Windows terminals and redirected output still default to a legacy codepage
    in plenty of setups, and a UnicodeEncodeError instead of a diff report is a
    miserable first impression.
    """
    encoding = getattr(out, "encoding", None) or "ascii"
    try:
        "✗·✓".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return {"bad": "x", "ok": "=", "dot": "-", "partial": "?"}
    return {"bad": "✗", "ok": "✓", "dot": "·", "partial": "!"}


class _Writer:
    """Write lines that the stream can always encode.

    The glyphs fall back to ASCII via `_symbols`, but data values cannot: a
    changed value holding CJK or an emoji, written to a cp1252 stream (the
    Windows default for redirected or piped stdout before Python 3.15), raised
    UnicodeEncodeError half-way through the report - after the headline was
    printed - and the run exited 2, "the tool broke", instead of 1 with the
    evidence. Each line is therefore encoded with `backslashreplace` first,
    so an unencodable character prints as its `\\u` escape: still exact, and
    still distinct from any other value. A stream without an encoding (a
    StringIO) takes text as it is.
    """

    def __init__(self, out: TextIO) -> None:
        """Wrap `out`, remembering its encoding (None for a StringIO)."""
        self.out = out
        self.encoding = getattr(out, "encoding", None)

    def line(self, text: str = "") -> None:
        """Write one line, made encodable for this stream."""
        if self.encoding:
            try:
                text.encode(self.encoding)
            except UnicodeEncodeError:
                text = text.encode(self.encoding, "backslashreplace").decode(
                    self.encoding
                )
            except LookupError:
                text = text.encode("ascii", "backslashreplace").decode("ascii")
        print(text, file=self.out)


def _escape(text: str) -> str:
    """Neutralise characters that would act on the terminal instead of printing.

    Column names, keys and warnings come from the databases, and a newline or
    an ANSI escape in one can forge a whole line of the report - a fake
    "no differences" headline, or a cursor move that overwrites the real one.
    Every non-printable character is written as its escape instead.
    """
    if text.isprintable():
        return text
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


#: Logical types whose canonical text is a fixed, engine-produced format -
#: digits, dates, `true`/`false`, `Infinity` - and so can be printed bare.
#: Anything else holds arbitrary text, and is quoted so it cannot be confused
#: with NULL, with an empty string, or with the report's own layout.
_BARE_TYPES = frozenset(
    {
        LogicalType.INTEGER,
        LogicalType.DECIMAL,
        LogicalType.FLOAT,
        LogicalType.BOOLEAN,
        LogicalType.DATE,
        LogicalType.TIMESTAMP,
        LogicalType.TIME,
        LogicalType.BINARY,  # `\x` + hex, printed as PostgreSQL prints bytea
        LogicalType.INTERVAL,  # `0 mon 1 day 0 us`
        LogicalType.TIMETZ,
        LogicalType.INET,
        # Not BITS: an empty bit string would print as nothing at all.
    }
)


def _display(value: str, quoted: bool = True) -> str:
    """Render one canonical value for a human, unambiguously.

    A real SQL NULL prints as a bare `NULL`. Text prints quoted, Python-repr
    style, so the four letters `'NULL'`, an empty `''`, the two characters
    `"''"` and a value with a trailing space all look different from each
    other and from a NULL - a difference whose two sides print alike is a
    report nobody can act on. Quoting also escapes newlines and terminal
    control sequences (see `_escape`).

    The value is the canonical text the engines compared, in which text is
    escaped (`\\` doubled, chr(31) as `\\s`) so that no value can spell the
    NULL sentinel. It is decoded back first, so `C:\\x` prints as the user
    stored it and a real `'\\N'` string prints quoted, never as NULL (RT-A15).
    JSON output keeps the raw canonical text.
    """
    decoded = decode_canonical_field(value)
    if decoded is None:
        return "NULL"
    if quoted:
        return repr(decoded)
    return _escape(decoded)


def _display_key(key: int | str) -> str:
    """Render a row key for a human.

    A composite key's canonical text is joined by ASCII Unit Separator, which
    is exactly right for hashing and unreadable on a terminal. Show the parts
    separated visibly instead, each quoted like any other text so that a
    hostile key cannot carry a newline or an escape sequence into the report.
    JSON keeps the raw text, so a machine still sees what was actually compared.
    """
    if isinstance(key, str):
        return " | ".join(_display(part) for part in key.split("\x1f"))
    return str(key)


def render_human(result: DiffResult, out: TextIO) -> None:
    """Write the report a person reads: verdict first, then the evidence.

    Anything that qualifies the verdict - a schema difference, an unexplained
    checksum mismatch, the float scale, every warning - comes straight after
    the headline, before the row listing. Printed at the bottom, under a
    hundred listed rows, the one line explaining that every row is "missing"
    because the key changed type is the line nobody reads.

    The rows-downloaded percentage is always printed - it is the proof that
    the tool pushed the work into the engines, and a figure that suddenly
    reads 100% is how someone finds out their key column is wrong.
    """
    w = _Writer(out)
    sym = _symbols(out)
    stats = result.stats
    total = max(stats.rows_compared_a, stats.rows_compared_b)
    # Both sides' rows count toward `rows_downloaded`, so the denominator must
    # be both sides too. Against a single side the figure can read 200%, which
    # undermines the one number that proves the tool did the clever thing.
    moveable = stats.rows_compared_a + stats.rows_compared_b
    pct = (100 * stats.rows_downloaded / moveable) if moveable else 0.0

    if result.truncated:
        # Never let a capped run read as a verdict on the whole table.
        headline = (
            f"{sym['partial']} at least {_plural(len(result.diffs), 'difference')} "
            f"in {total:,} rows - stopped early, the rest was not checked"
        )
    elif result.diffs:
        headline = (
            f"{sym['bad']} {_plural(len(result.diffs), 'difference')} in {total:,} rows"
        )
    elif result.verification and result.verification.failures:
        # The walk found nothing, but raw values contradict it: the tool's own
        # rendering hid a difference for these data. Never the all-clear.
        headline = (
            f"{sym['bad']} not verified: the independent sample check found "
            f"{_plural(len(result.verification.failures), 'row')} the checksum "
            f"comparison called equal"
        )
    elif result.unexplained:
        headline = (
            f"{sym['bad']} not verified: checksums differ in "
            f"{_plural(len(result.unexplained), 'key range')} that the downloaded "
            f"rows cannot explain"
        )
    elif result.schema_differences:
        # The shared columns matched, but not every column was compared, so
        # this must not read as the all-clear (RT-F1).
        headline = (
            f"{sym['bad']} schemas differ: no row differences in {total:,} rows, "
            f"but not every column could be compared"
        )
    else:
        headline = f"{sym['ok']} no differences in {total:,} rows"
    w.line(headline)

    dot = f" {sym['dot']} "
    queries = f"{stats.queries:,} quer{'y' if stats.queries == 1 else 'ies'}"
    w.line(
        f"  {queries}{dot}{stats.rows_downloaded:,} rows downloaded "
        f"({pct:.2f}% of both tables){dot}{stats.seconds:.1f}s"
    )

    if result.diffs:
        counts = [
            f"{len(result.by_kind(k)):,} {KIND_LABELS[k]}"
            for k in ("only_in_a", "only_in_b", "different")
            if result.by_kind(k)
        ]
        if len(counts) > 1:
            w.line(f"  {dot.join(counts).strip()}")

    w.line()
    for schema in result.schema_differences:
        w.line(f"  {sym['bad']} schema: {_escape(schema)}")
    for note in result.unexplained:
        w.line(f"  {sym['bad']} unverified {_escape(note)}")
    _render_verification(result, w, sym)
    w.line(f"  comparing floats and decimals at {result.float_scale} decimal places")
    for warning in result.warnings:
        w.line(f"  ! {_escape(warning)}")

    if result.diffs:
        w.line()
        quoted = {c.name: c.logical_type not in _BARE_TYPES for c in result.columns}
        shown = 0
        for kind in ("only_in_a", "only_in_b", "different"):
            for d in result.by_kind(kind):
                if shown >= DISPLAY_LIMIT:
                    break
                _render_diff(d, w, quoted)
                shown += 1
        if len(result.diffs) > DISPLAY_LIMIT:
            w.line(
                f"  ... {len(result.diffs) - DISPLAY_LIMIT:,} more difference(s) "
                f"found but not shown here; use --json for the full list"
            )


def _risk_line(result: DiffResult) -> str:
    """The run's two measurable risk figures, in words (see `risk_figures`)."""
    r = risk_figures(result)
    if r["max_hidden_fraction"] is None:
        return "not computed"
    conf = f"{r['confidence']:.0%}"
    collision = "a checksum collision is below 1 in 10^18"
    if r["max_hidden_fraction"] == 0.0:
        return (
            f"every row was compared by both methods, so no row can hide a "
            f"rendering error; {collision}"
        )
    return (
        f"with {conf} confidence fewer than {r['max_hidden_fraction']:.3%} of rows "
        f"(at most {r['max_hidden_rows']:,} of {r['rows']:,}) could differ in a way "
        f"the checksums missed; {collision}"
    )


def risk_figures(result: DiffResult) -> dict[str, Any]:
    """What a run can state about its own residual risk, as numbers.

    - `checksum_collision_bound`: the chance that a bucket holding a real
      difference still matches, at most 2^-60 for data not crafted to collide.
    - `max_hidden_fraction` / `max_hidden_rows`: from the independent sample,
      the most rows that could carry a difference both the checksums *and*
      the second method missed in the sample, at `confidence` (the exact
      binomial bound; 0 when every row was compared). Valid for the columns
      the check covers (`verification.checked`); `unchecked` ones carry no
      such bound.
    """
    v = result.verification
    rows = max(result.stats.rows_compared_a, result.stats.rows_compared_b)
    frac = v.max_hidden_fraction() if v is not None else None
    hidden = None
    if frac is not None:
        hidden = 0 if frac == 0.0 else max(1, math.ceil(frac * rows))
    return {
        "confidence": RISK_CONFIDENCE,
        "checksum_collision_bound": CHECKSUM_COLLISION_BOUND,
        "max_hidden_fraction": frac,
        "max_hidden_rows": hidden,
        "rows": rows,
        "covered_columns": [] if v is None else v.checked,
        "uncovered_columns": [] if v is None else v.unchecked,
    }


def _render_verification(result: DiffResult, w: _Writer, sym: dict[str, str]) -> None:
    """The independent sample check, in a line or two - every failure listed."""
    v = result.verification
    if v is None:
        return
    if v.error:
        w.line(f"  ! independent sample check could not run: {_escape(v.error)}")
        return
    rows = max(v.rows_a, v.rows_b)
    scope = (
        "every row" if v.modulus == 1 else f"{rows:,} sampled rows (1 in {v.modulus:,})"
    )
    if v.failures:
        w.line(
            f"  {sym['bad']} independent sample check: {scope} re-compared from raw values"
        )
        for failure in v.failures:
            w.line(f"  {sym['bad']} sample {_escape(failure)}")
        w.line(
            "    the checksum comparison is not trustworthy for these data; "
            "please report this"
        )
    else:
        w.line(
            f"  {sym['ok']} independent sample check: {scope} re-compared from raw "
            f"values - all agree"
        )
        w.line(f"    risk: {_risk_line(result)}")
    if v.unchecked:
        w.line(
            f"  ! not covered by the sample check (type outside its rules): {v.unchecked}"
        )
    for note in v.notes:
        w.line(f"  ! sample: {_escape(note)}")


def _render_diff(d: RowDiff, w: _Writer, quoted: dict[str, bool]) -> None:
    """Write one difference.

    A changed row shows both values side by side, which is far easier to scan
    for the character that moved - until the values are too long to share a
    line, at which point they stack.
    """
    label = KIND_LABELS[d.kind]
    shown_key = _display_key(d.key)
    if d.kind != "different":
        w.line(f"  {label:<11} key {shown_key}")
        return

    cols = {c: _escape(c) for c in d.columns}
    w.line(f"  {label:<11} key {shown_key:<14} columns: {', '.join(cols.values())}")
    name_w = max((len(c) for c in cols.values()), default=0)

    def show(values: dict[str, str], col: str) -> str:
        """One side's value of `col`, printed so it cannot pass for another.

        A column absent from a side's values is a bug upstream, not an empty
        string; say so rather than print something that looks like data.
        """
        if col not in values:
            return "<missing>"
        return _display(values[col], quoted.get(col, True))

    a_vals = {c: show(d.values_a, c) for c in d.columns}
    b_vals = {c: show(d.values_b, c) for c in d.columns}

    # Side by side is far easier to scan for the character that moved, but only
    # while the values are short enough to stay on one line.
    a_w = max((len(v) for v in a_vals.values()), default=0)
    if a_w <= _SIDE_BY_SIDE_MAX:
        for col in d.columns:
            w.line(
                f"      {cols[col]:<{name_w}}   A {a_vals[col]:<{a_w}}   B {b_vals[col]}"
            )
    else:
        for col in d.columns:
            w.line(f"      {cols[col]:<{name_w}}   A {a_vals[col]}")
            w.line(f"      {'':<{name_w}}   B {b_vals[col]}")


def to_dict(result: DiffResult) -> dict[str, Any]:
    """Shape the result for `--json`.

    Carries the raw canonical text rather than the human-friendly rendering,
    so a machine sees exactly what was compared. `identical` is false whenever
    the walk was cut short, a column was left uncompared, or a checksum
    mismatch went unexplained, so a consumer reading only that field cannot
    be misled by any of them.
    """
    stats = result.stats
    moveable = stats.rows_compared_a + stats.rows_compared_b
    return {
        # `identical` is false whenever the walk was cut short, so a consumer
        # that reads only this field can never be misled by a partial run.
        "identical": result.identical,
        "truncated": result.truncated,
        # Columns one side lacks that were not set aside with --exclude or
        # --columns. Non-empty means not identical: they were never compared.
        "schema_differences": result.schema_differences,
        # Key ranges whose checksums differ but whose downloaded rows could
        # not account for it. Non-empty means not identical.
        "unexplained": result.unexplained,
        # The independent sample check; null when skipped. Non-empty
        # `failures` means not identical.
        "verification": None
        if result.verification is None
        else {
            "modulus": result.verification.modulus,
            "residue": result.verification.residue,
            "rows_a": result.verification.rows_a,
            "rows_b": result.verification.rows_b,
            "checked": result.verification.checked,
            "unchecked": result.verification.unchecked,
            "failures": result.verification.failures,
            "notes": result.verification.notes,
            "error": result.verification.error,
        },
        # The run's measurable residual risk; see `risk_figures`.
        "risk": risk_figures(result),
        "difference_count": len(result.diffs),
        "float_scale": result.float_scale,
        "columns_compared": [c.name for c in result.columns],
        "warnings": result.warnings,
        "stats": {
            "queries": stats.queries,
            "segments_checked": stats.segments_checked,
            "rows_downloaded": stats.rows_downloaded,
            "rows_a": stats.rows_compared_a,
            "rows_b": stats.rows_compared_b,
            # Denominator is both sides, matching `rows_downloaded`, so this
            # can never exceed 100.
            "percent_downloaded": round(
                (100 * stats.rows_downloaded / moveable) if moveable else 0.0, 4
            ),
            "seconds": round(stats.seconds, 3),
            "sample_queries": stats.sample_queries,
            "rows_sampled": stats.rows_sampled,
        },
        "differences": [
            {
                "key": d.key,
                "kind": d.kind,
                "columns": d.columns,
                "a": d.values_a,
                "b": d.values_b,
            }
            for d in result.diffs
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run_diff(args: argparse.Namespace, out: TextIO) -> int:
    """Open both sides, run the comparison, render it, and return the exit code.

    Both connections are closed even when the diff raises, and each is opened
    separately so a failure can name which side it was.
    """
    # Imported here, not at module scope, so `parity --help` works with no
    # database driver installed at all.
    from parity.dialects.base import get_dialect
    from parity.engine import diff

    a = b = None
    try:
        # Each side is opened separately so a failure can name which one.
        a = get_dialect(args.a, side="A", float_scale=args.float_scale)
        b = get_dialect(args.b, side="B", float_scale=args.float_scale)
        result = diff(
            a,
            b,
            a_table=args.a_table,
            b_table=args.b_table,
            # Comma-separated for composite keys; a single name is just a
            # one-element list.
            key=_split(args.key) or args.key,
            columns=_split(args.columns) or None,
            exclude=_split(args.exclude),
            bisection_factor=args.bisection_factor,
            threshold=args.threshold,
            # 0 is the documented way to ask for no limit at all.
            max_diffs=args.max_diffs or None,
            verify_sample=args.verify_sample,
        )
    finally:
        for side in (a, b):
            if side is not None:
                try:
                    side.close()
                except Exception:  # noqa: BLE001, S110
                    # Closing is best effort: a failure here must not mask
                    # whatever the diff itself raised.
                    pass

    if not args.quiet:
        if args.json:
            json.dump(to_dict(result), out, indent=2)
            print(file=out)
        else:
            render_human(result, out)

    return EXIT_IDENTICAL if result.identical else EXIT_DIFFERENCES


def main(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Entry point. Returns the exit code rather than calling sys.exit.

    `out` and `err` are injectable so the tests can drive the real CLI and
    read what it wrote. Anything that is not a clean verdict returns 2, never
    1 - a CI job has to be able to tell "the tables differ" from "the tool
    broke".
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(out)
        return EXIT_ERROR

    try:
        return _run_diff(args, out)
    except KeyboardInterrupt:  # pragma: no cover
        print("parity: interrupted", file=err)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - the exit-2 contract, see below
        # Anything that is not a clean verdict is exit 2, never exit 1: a CI
        # job must be able to tell "the tables differ" from "the tool broke".
        # Escaped and made encodable like the report: an error can quote a
        # value or a key straight from the data.
        _Writer(err).line(f"parity: {_escape(str(exc))}")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
