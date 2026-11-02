"""Measure FLOAT canonical-text agreement across engines, before and after.

Renders the adversarial sample in `tests/float_sample.py` (1,565 doubles) on
every reachable engine, with the dialect's current FLOAT expression and with
the expression it used before the canonical form was pinned down, and prints
for each: how many values differ from the Python spec
(`canonical_float_text`), and how many differ between each pair of engines.

    python demo/float_agreement.py                 # every reachable engine
    python demo/float_agreement.py --scale 2       # another --float-scale
    python demo/float_agreement.py --engines postgres duckdb

Engines are reached as the test suite reaches them: PostgreSQL and MySQL at
the `PARITY_TEST_PG` / `PARITY_TEST_MYSQL` URLs (defaults as in
tests/conftest.py), DuckDB in memory, BigQuery only if `PARITY_TEST_BIGQUERY`
is set. Read-only: every query is a SELECT over literals.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from float_sample import adversarial_doubles, render  # noqa: E402

from parity.dialects.base import canonical_float_text, get_dialect  # noqa: E402

URLS = {
    "postgres": os.environ.get(
        "PARITY_TEST_PG", "postgres://parity:parity@127.0.0.1:5432/parity"
    ),
    "duckdb": "duckdb:///:memory:",
    "mysql": os.environ.get(
        "PARITY_TEST_MYSQL", "mysql://parity:parity@127.0.0.1:3306/parity_test"
    ),
    "bigquery": os.environ.get("PARITY_TEST_BIGQUERY", ""),
}


def legacy(engine: str, s: int) -> Callable[[str], str]:
    """The finite-FLOAT expression each dialect used before this change."""
    if engine == "postgres":
        return lambda c: f"cast(round(({c})::numeric, {s}) as text)"
    if engine == "duckdb":
        return lambda c: f"cast(cast({c} as decimal(38,{s})) as varchar)"
    if engine == "mysql":
        return lambda c: f"cast(cast({c} as decimal(38,{s})) as char)"
    if engine == "bigquery":
        return lambda c: (
            f"format('%.{s}f', cast(round({c} * 1e{s}) as bignumeric) "
            f"/ bignumeric '1{'0' * s}')"
        )
    raise ValueError(engine)


def main() -> None:
    """Render the sample before and after on each engine and print the counts."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scale", type=int, default=6)
    p.add_argument("--engines", nargs="*", default=list(URLS))
    args = p.parse_args()

    xs = adversarial_doubles()
    spec = [canonical_float_text(x, args.scale) for x in xs]
    print(f"{len(xs)} doubles, float scale {args.scale}\n")
    results: dict[str, dict[str, list[str]]] = {"before": {}, "after": {}}
    for engine in args.engines:
        if not URLS.get(engine):
            print(f"{engine:9} skipped: no URL configured")
            continue
        try:
            d = get_dialect(URLS[engine], side="A", float_scale=args.scale)
        except Exception as exc:  # noqa: BLE001 - report and carry on
            print(f"{engine:9} skipped: {type(exc).__name__}: {exc}")
            continue
        try:
            for label, expr in (("before", legacy(engine, args.scale)), ("after", None)):
                try:
                    got = render(d, engine, xs, expr)
                except Exception as exc:  # noqa: BLE001
                    print(f"{engine:9} {label:6} ERROR {type(exc).__name__}: {exc}")
                    continue
                results[label][engine] = got
                bad = [(x, g, w) for x, g, w in zip(xs, got, spec, strict=True) if g != w]
                example = (
                    f"  e.g. {bad[0][0]!r}: {bad[0][1]} != {bad[0][2]}" if bad else ""
                )
                print(f"{engine:9} {label:6} differs from spec on {len(bad):4}{example}")
        finally:
            d.close()

    for label in ("before", "after"):
        print(f"\npairwise disagreement, {label}:")
        for a, b in combinations(results[label], 2):
            ra, rb = results[label][a], results[label][b]
            n = sum(x != y for x, y in zip(ra, rb, strict=True))
            print(f"  {a:9} <-> {b:9} {n:4} of {len(xs)}")

    # Does the change move checksums? Per engine, how many values render to
    # different text now; and of the values on which *every* engine already
    # agreed before, how many moved (those are the only ones whose diffs could
    # have been trusted before, so moving them is the real behaviour change).
    common = [e for e in results["before"] if e in results["after"]]
    if common:
        print("\ntext changed by this fix (same engine, before -> after):")
        agreed = [
            i
            for i in range(len(xs))
            if len({results["before"][e][i] for e in common}) == 1
        ]
        for e in common:
            old, new = results["before"][e], results["after"][e]
            moved = sum(old[i] != new[i] for i in range(len(xs)))
            moved_agreed = sum(old[i] != new[i] for i in agreed)
            print(
                f"  {e:9} {moved:4} of {len(xs)}; of the {len(agreed)} values all "
                f"engines agreed on before, {moved_agreed} moved"
            )


if __name__ == "__main__":
    main()
