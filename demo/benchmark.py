"""Time a real cross-engine diff and report what it cost.

The numbers this prints are the deliverable: wall time, query count, rows
downloaded, and the percentage of the two tables that crossed the network. The
last one is the whole argument for the tool, so it is never omitted.

    python demo/benchmark.py                 # whatever is in the databases now
    python demo/benchmark.py --expect-clean  # assert identical, fail if not
    python demo/benchmark.py --expect-planted

`--expect-planted` is what makes this a test rather than a demo: it asserts the
tool found *exactly* the differences `generate.py --plant` created. A benchmark
that only measured speed would happily report a fast wrong answer.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from generate import DATA_DIR, DEFAULT_PG, TABLE, plant_keys

from parity.dialects.base import get_dialect
from parity.engine import diff


def expected_planted(n: int) -> list[tuple[int, str]]:
    """Exactly what generate.py --plant created, as (key, kind) pairs.

    Asserted against rather than eyeballed - a benchmark that only measured
    speed would happily report a fast wrong answer.
    """
    keys = plant_keys(n)
    return sorted(
        [
            (keys["null_trap"], "different"),  # NULL -> ''
            (keys["deleted"], "only_in_b"),  # deleted from A
            (keys["changed"], "different"),  # amount moved
            (keys["bool_trap"], "different"),  # FALSE -> NULL
            (keys["extra"], "only_in_a"),  # inserted into A
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Time the diff and report what it cost, optionally checking the result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg", default=DEFAULT_PG)
    parser.add_argument("--duckdb", default=os.path.join(DATA_DIR, "new.duckdb"))
    parser.add_argument("--key", default="id")
    parser.add_argument("--bisection-factor", type=int, default=32)
    parser.add_argument("--threshold", type=int, default=10_000)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--expect-clean", action="store_true")
    parser.add_argument("--expect-planted", action="store_true")
    args = parser.parse_args(argv)

    a = get_dialect(args.pg, side="A")
    b = get_dialect(f"duckdb:///{args.duckdb}", side="B")
    try:
        rows = a.query(f"select count(*) from {TABLE}")[0][0]
        print(
            f"postgres {TABLE}: {rows:,} rows   duckdb {TABLE}: "
            f"{b.query(f'select count(*) from {TABLE}')[0][0]:,} rows"
        )
        print()

        results = []
        for run in range(args.repeat):
            started = time.perf_counter()
            result = diff(
                a,
                b,
                f"public.{TABLE}",
                f"main.{TABLE}",
                args.key,
                bisection_factor=args.bisection_factor,
                threshold=args.threshold,
            )
            wall = time.perf_counter() - started
            results.append((result, wall))

            s = result.stats
            moveable = s.rows_compared_a + s.rows_compared_b
            pct = 100 * s.rows_downloaded / moveable if moveable else 0.0
            label = f"run {run + 1}" if args.repeat > 1 else "result"
            print(
                f"  {label:<8} {len(result.diffs):>3} differences  "
                f"{s.queries:>4} queries  {s.segments_checked:>4} segments  "
                f"{s.rows_downloaded:>9,} rows downloaded ({pct:.4f}%)  "
                f"{wall:6.2f}s"
            )

        result, wall = results[-1]
        if result.diffs:
            print()
            for d in result.diffs:
                detail = (
                    f"  columns: {', '.join(d.columns)}" if d.kind == "different" else ""
                )
                print(f"    {d.kind:<11} id {d.key:>12,}{detail}")
        for w in result.warnings:
            print(f"    ! {w}")

        # ---- assertions: a fast wrong answer is worth nothing --------------
        if args.expect_clean:
            assert result.identical, f"expected identical, found {len(result.diffs)}"
            assert result.stats.rows_downloaded == 0, (
                f"downloaded {result.stats.rows_downloaded} rows on a clean match"
            )
            print("\nOK  identical, and not one row crossed the network")

        if args.expect_planted:
            found = sorted((d.key, d.kind) for d in result.diffs)
            want = expected_planted(result.stats.rows_compared_b)
            if found != want:
                # Never dump the whole list. A dataset that got rebuilt or
                # clobbered underneath the benchmark produces hundreds of
                # thousands of differences, and the traceback then runs to
                # hundreds of megabytes.
                shown = found[:10]
                more = f" ... and {len(found) - 10:,} more" if len(found) > 10 else ""
                raise AssertionError(
                    f"\n  expected {len(want)}: {want}"
                    f"\n  found    {len(found)}: {shown}{more}"
                    f"\n  (counts wildly off usually means the dataset was "
                    f"rebuilt or clobbered - rerun demo/generate.py)"
                )
            null_trap = next(
                d
                for d in result.diffs
                if d.key == plant_keys(result.stats.rows_compared_b)["null_trap"]
            )
            assert null_trap.columns == ["note"], null_trap.columns
            bool_trap = next(
                d
                for d in result.diffs
                if d.key == plant_keys(result.stats.rows_compared_b)["bool_trap"]
            )
            assert bool_trap.columns == ["is_refunded"], bool_trap.columns
            moveable = result.stats.rows_compared_a + result.stats.rows_compared_b
            pct = 100 * result.stats.rows_downloaded / moveable

            # The absolute bound is what the algorithm actually promises: each
            # planted difference drags in at most one leaf bucket per side, and
            # a leaf bucket holds at most `threshold` rows. The *percentage*
            # only looks impressive once the table is much larger than the
            # threshold, so asserting a flat "< 1%" would fail at small sizes
            # for a reason that has nothing to do with correctness.
            bound = 2 * args.threshold * (len(want) + 1)
            assert result.stats.rows_downloaded <= bound, (
                f"downloaded {result.stats.rows_downloaded:,} rows, more than "
                f"the {bound:,} the bisection should ever need"
            )
            if result.stats.rows_compared_b >= 1_000_000:
                assert pct < 1.0, (
                    f"downloaded {pct:.2f}% of the data; expected well under 1% "
                    f"at this scale"
                )
            print(
                "\nOK  found exactly the planted differences, "
                f"including NULL vs '' and FALSE vs NULL, moving {pct:.4f}% of the rows"
            )
    finally:
        a.close()
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
