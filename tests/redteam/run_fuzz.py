"""Command-line driver for the live fuzzer: `python tests/redteam/run_fuzz.py`.

Runs `--cases` cases per engine pair from `--seed`, prints a tally per pair
and every disagreement (shrunk) as a one-line repro. Engine URLs come from the
same environment variables the test suite reads (see tests/conftest.py).
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import fuzz_harness as fh  # noqa: E402


def urls() -> dict[str, str]:
    """Engine URLs from the same environment variables as tests/conftest.py."""
    from conftest import BIGQUERY_URL, MSSQL_URL, MYSQL_URL, PG_URL

    out = {"postgres": PG_URL, "mysql": MYSQL_URL, "duckdb": ""}
    if MSSQL_URL:
        out["mssql"] = MSSQL_URL
    if BIGQUERY_URL:
        out["bigquery"] = BIGQUERY_URL
    return out


def main() -> int:
    """Run the campaign; exit 1 if any case disagreed with the oracle."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--engines", default="postgres,duckdb,mysql,mssql")
    ap.add_argument("--known", default="", help="comma list of KNOWN triggers to enable")
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--max-rows", type=int, default=120)
    ap.add_argument(
        "--pairs", default="", help="comma list like mssql-postgres; default all"
    )
    args = ap.parse_args()
    u = urls()
    names = [n for n in args.engines.split(",") if n in u]
    engines = {n: fh.open_engine(n, u) for n in names}
    known = {k for k in args.known.split(",") if k}
    tally: dict[str, collections.Counter] = {}
    bad = 0
    wanted = {frozenset(p.split("-")) for p in args.pairs.split(",") if p}
    for a, b in fh.pairs(names):
        if wanted and frozenset((a, b)) not in wanted:
            continue
        t0 = time.time()
        c = tally.setdefault(f"{a}<->{b}", collections.Counter())
        for i in range(args.cases):
            seed = args.seed * 100_000 + i
            ea, eb = (a, b) if seed % 2 else (b, a)
            case = fh.random_case(seed, ea, eb, known, args.max_rows)
            out = fh.run_case(case, engines)
            v = out.verdict()
            c[v] += 1
            c["with_diffs"] += bool(out.expected)
            c["diffs"] += len(out.expected)
            if v != "OK":
                bad += 1
                kind = v
                if not args.no_shrink and v != "ERROR":
                    case = fh.shrink(
                        case, engines, lambda o, kind=kind: o.verdict() == kind
                    )
                    out = fh.run_case(case, engines)
                print(f"[{v}] {case.describe()}")
                print(f"    error={out.error}")
                print(f"    missed={sorted(map(repr, out.missed))[:6]}")
                print(f"    extra={sorted(map(repr, out.extra))[:6]}")
                if len(case.rows_a) + len(case.rows_b) <= 8:
                    print(f"    rows_a={case.rows_a!r}"[:1500])
                    print(f"    rows_b={case.rows_b!r}"[:1500])
                sys.stdout.flush()
        print(f"{a}<->{b}: {dict(c)} in {time.time() - t0:.1f}s")
        sys.stdout.flush()
    for e in engines.values():
        e.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
