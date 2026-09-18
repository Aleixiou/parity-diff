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
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from parity import __version__
from parity.dialects.base import NULL_SENTINEL
from parity.engine import DEFAULT_MAX_DIFFS
from parity.types import DiffResult, RowDiff

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
            "exit codes: 0 identical, 1 differences found, 2 error. "
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
        type=int,
        default=DEFAULT_MAX_DIFFS,
        metavar="N",
        help=f"stop after N differences (default: {DEFAULT_MAX_DIFFS:,}; 0 for "
        f"no limit). The result is then explicitly marked partial - it "
        f"does not mean the rest matched. The default exists because each "
        f"difference costs memory, so two tables that share nothing would "
        f"otherwise exhaust it rather than answering.",
    )
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing; rely on the exit code",
    )
    return parser


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


def _display_key(key: int | str) -> str:
    """Render a row key for a human.

    A composite key's canonical text is joined by ASCII Unit Separator, which
    is exactly right for hashing and unreadable on a terminal. Show the parts
    separated visibly instead. JSON keeps the raw text, so a machine still sees
    what was actually compared.
    """
    if isinstance(key, str) and "" in key:
        return " | ".join(_display(part) for part in key.split(""))
    return _display(key) if isinstance(key, str) else str(key)


def _display(value: str) -> str:
    """Make canonical text readable without misrepresenting it.

    `\\N` is the NULL sentinel and `''` is a genuinely empty string - the
    distinction is the whole point of the NULL-versus-empty-string trap, and
    printing the empty one as nothing at all makes the report look broken.
    JSON output keeps the raw canonical text for machines.
    """
    if value == NULL_SENTINEL:
        return "NULL"
    if value == "":
        return "''"
    return value


def render_human(result: DiffResult, out: TextIO) -> None:
    """Write the report a person reads: verdict first, then the evidence.

    The rows-downloaded percentage is always printed - it is the proof that
    the tool pushed the work into the engines, and a figure that suddenly
    reads 100% is how someone finds out their key column is wrong.
    """
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
    else:
        headline = f"{sym['ok']} no differences in {total:,} rows"
    print(headline, file=out)

    dot = f" {sym['dot']} "
    queries = f"{stats.queries:,} quer{'y' if stats.queries == 1 else 'ies'}"
    print(
        f"  {queries}{dot}{stats.rows_downloaded:,} rows downloaded "
        f"({pct:.2f}% of both tables){dot}{stats.seconds:.1f}s",
        file=out,
    )

    if result.diffs:
        counts = [
            f"{len(result.by_kind(k)):,} {KIND_LABELS[k]}"
            for k in ("only_in_a", "only_in_b", "different")
            if result.by_kind(k)
        ]
        if len(counts) > 1:
            print(f"  {dot.join(counts).strip()}", file=out)

        print(file=out)
        shown = 0
        for kind in ("only_in_a", "only_in_b", "different"):
            for d in result.by_kind(kind):
                if shown >= DISPLAY_LIMIT:
                    break
                _render_diff(d, out)
                shown += 1
        if len(result.diffs) > DISPLAY_LIMIT:
            print(
                f"  ... {len(result.diffs) - DISPLAY_LIMIT:,} more difference(s) "
                f"found but not shown here; use --json for the full list",
                file=out,
            )

    print(file=out)
    print(
        f"  comparing floats and decimals at {result.float_scale} decimal places",
        file=out,
    )
    for warning in result.warnings:
        print(f"  ! {warning}", file=out)


def _render_diff(d: RowDiff, out: TextIO) -> None:
    """Write one difference.

    A changed row shows both values side by side, which is far easier to scan
    for the character that moved - until the values are too long to share a
    line, at which point they stack.
    """
    label = KIND_LABELS[d.kind]
    if d.kind != "different":
        print(f"  {label:<11} key {_display_key(d.key)}", file=out)
        return

    shown_key = _display_key(d.key)
    print(f"  {label:<11} key {shown_key:<14} columns: {', '.join(d.columns)}", file=out)
    name_w = max((len(c) for c in d.columns), default=0)
    a_vals = {c: _display(d.values_a.get(c, "")) for c in d.columns}
    b_vals = {c: _display(d.values_b.get(c, "")) for c in d.columns}

    # Side by side is far easier to scan for the character that moved, but only
    # while the values are short enough to stay on one line.
    a_w = max((len(v) for v in a_vals.values()), default=0)
    if a_w <= _SIDE_BY_SIDE_MAX:
        for col in d.columns:
            print(
                f"      {col:<{name_w}}   A {a_vals[col]:<{a_w}}   B {b_vals[col]}",
                file=out,
            )
    else:
        for col in d.columns:
            print(f"      {col:<{name_w}}   A {a_vals[col]}", file=out)
            print(f"      {'':<{name_w}}   B {b_vals[col]}", file=out)


def to_dict(result: DiffResult) -> dict[str, Any]:
    """Shape the result for `--json`.

    Carries the raw canonical text rather than the human-friendly rendering,
    so a machine sees exactly what was compared. `identical` is false whenever
    the walk was cut short, so a consumer reading only that field cannot be
    misled by a partial run.
    """
    stats = result.stats
    moveable = stats.rows_compared_a + stats.rows_compared_b
    return {
        # `identical` is false whenever the walk was cut short, so a consumer
        # that reads only this field can never be misled by a partial run.
        "identical": result.identical,
        "truncated": result.truncated,
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
        print(f"parity: {exc}", file=err)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
