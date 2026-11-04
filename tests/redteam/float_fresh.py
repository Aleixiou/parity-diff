"""Round H: FLOAT canonical form on fresh random doubles, every local engine.

`demo/float_agreement.py` renders the fixed adversarial sample from literals,
which SQL Server cannot take (its string-to-float parse is not correctly
rounded). This loads `--n` fresh doubles per engine into a table through the
driver's parameter binding (`fuzz_harness` writers), renders them with the
dialect's own FLOAT `normalize`, and compares each with the Python spec
`canonical_float_text`:

    python tests/redteam/float_fresh.py --n 100000 --seed 11 --scales 6 2 0 18

Tables are `rth_x_float_*` and dropped afterwards.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import fuzz_harness as fh  # noqa: E402
from fuzz_cross import urls  # noqa: E402

from parity.dialects.base import canonical_float_text, get_dialect  # noqa: E402
from parity.types import Column, LogicalType  # noqa: E402

LIMIT = 2.0**116


def fresh_doubles(rng: random.Random, n: int, subnormal: bool) -> list[float]:
    """`n` finite doubles below 2^116: bit patterns, decimal ties at several
    scales, 15-17 digit decimals, neighbours of 2^53 and of powers of ten."""
    out: list[float] = []
    while len(out) < n:
        r = rng.random()
        if r < 0.3:
            x = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
        elif r < 0.5:
            s = rng.choice([0, 1, 2, 6, 9, 12, 15, 18])
            ip = rng.choice([0, 1, 7, rng.randint(0, 10**6), rng.randint(0, 10**12)])
            x = float(f"{ip}.{rng.randint(0, 10**s - 1) if s else 0:0{s}d}5")
        elif r < 0.7:
            x = float(
                f"{rng.random():.{rng.choice([15, 16, 17])}g}"
            ) * 10.0 ** rng.randint(-12, 34)
        elif r < 0.8:
            base = rng.choice([2.0**53, 2.0**54, 2.0**60, 1e15, 1e16, 1e17, 2.0**100])
            x = math.nextafter(base, rng.choice([0.0, math.inf]))
            for _ in range(rng.randint(0, 5)):
                x = math.nextafter(x, rng.choice([0.0, math.inf]))
        elif r < 0.9:
            x = rng.randint(-(2**62), 2**62) / rng.choice([1, 2, 4, 128, 1024, 10**6])
        else:
            x = math.ldexp(rng.random(), rng.randint(-1074, 115))
        if rng.random() < 0.5:
            x = -x
        if not math.isfinite(x) or abs(x) >= LIMIT:
            continue
        if x != 0 and abs(x) < 2.2250738585072014e-308 and not subnormal:
            continue
        out.append(x)
    return out


def main() -> int:
    """Load, render, compare; print disagreements per engine and scale."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument(
        "--engines", nargs="*", default=["postgres", "duckdb", "mysql", "mssql"]
    )
    ap.add_argument("--scales", nargs="*", type=int, default=[6])
    args = ap.parse_args()
    u = urls()
    bad_total = 0
    for engine in args.engines:
        rng = random.Random(args.seed)
        xs = fresh_doubles(rng, args.n, subnormal=engine != "mysql")
        w = fh.open_engine(engine, u)
        table = f"rth_x_float_{args.seed}"
        col = [fh.Col("i", "int"), fh.Col("x", "double")]
        t0 = time.time()
        names = w.create(table, col, 1, upper=False)
        w.load(table, names, col, list(enumerate(xs)))
        if isinstance(w, fh.Duck):
            w.seal()
        back = dict(w.fetch(table, names, col)) if not isinstance(w, fh.Duck) else None
        if isinstance(w, fh.Duck):
            import duckdb

            con = duckdb.connect(w.path, read_only=True)
            back = dict(con.execute(f'select i, x from "{table}"').fetchall())
            con.close()
        lost = sum(1 for i, x in enumerate(xs) if back[i] != x)
        try:
            for scale in args.scales:
                if engine == "mssql" and scale > 18:
                    continue
                d = get_dialect(w.dialect_url(table), side="A", float_scale=scale)
                try:
                    expr = d.normalize(Column("x", LogicalType.FLOAT, "double"))
                    ref = w.table_ref(table)
                    rows = d.query(f"select {d.quote('i')}, {expr} from {d.qualify(ref)}")
                finally:
                    d.close()
                got = {int(i): str(t) for i, t in rows}
                bad = [
                    (x, got[i], canonical_float_text(back[i], scale))
                    for i, x in enumerate(xs)
                    if got[i] != canonical_float_text(back[i], scale)
                ]
                bad_total += len(bad)
                print(
                    f"{engine:9} scale {scale:2}: {len(bad)} of {len(xs)} differ from the "
                    f"spec (stored values lost in load: {lost}) {time.time() - t0:.0f}s"
                )
                for x, g, want in bad[:5]:
                    print(f"    {x!r}: got {g!r} want {want!r}")
                sys.stdout.flush()
        finally:
            w.drop(table)
            w.close()
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
