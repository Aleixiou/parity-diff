"""An adversarial sample of doubles, and the means to render it on any engine.

Shared by `test_float_canonical.py` and `demo/float_agreement.py`. FLOAT
canonical text is where "equivalent" expressions quietly are not: PostgreSQL's
`round(c::numeric, 6)` and DuckDB's `cast(c as decimal(38,6))` disagreed on 350
of these 1,565 values before the canonical form was pinned down. A sample that
only holds `1.5` and `1/3` proves nothing, so this one is built to hit every
place the engines were seen to diverge:

- exact ties at the sixth decimal (odd multiples of 1/128 - the only doubles
  that sit *exactly* halfway at six places), alone and on large integer parts
- decimal literals one digit past the scale ending in 5 (`x.dddddd5`), whose
  double lands a hair either side of the halfway point
- 15-17 significant digits across magnitudes 1e-8 .. 1e31
- random bit patterns, subnormals, signed zeros, values just either side of
  the zero-rounding threshold, integers stored as double around 2^53, and
  powers of ten up to 1e31

Every value travels to the engine as its shortest repr in a string literal
cast to the engine's double type, which every engine parses correctly
rounded; `render` reads the double back and asserts it arrived bit-exact, so
a mismatch can only come from the rendering under test.
"""

from __future__ import annotations

import math
import random
import struct
from collections.abc import Callable, Sequence

from parity.dialects.base import Dialect
from parity.types import Column, LogicalType

#: Each engine's name for an IEEE double, used to cast the literals.
DOUBLE_TYPE = {
    "postgres": "float8",
    "duckdb": "double",
    "mysql": "double",
    "bigquery": "float64",
    "snowflake": "float",
}

FLOAT_COLUMN = Column("x", LogicalType.FLOAT, "double")


def adversarial_doubles(seed: int = 7) -> list[float]:
    """1,565 finite doubles of magnitude below 1e31, deterministic per seed."""
    rnd = random.Random(seed)
    out: list[float] = []
    # Exact ties at the sixth decimal: n/128 for odd n has seven decimals and
    # ends in 5. With an integer part they stay exact up to 2^45.
    out += [n / 128 for n in range(1, 256, 2)]
    for ip in (1, 7, 262, 123456, 2**30, 2**40, 2**45):
        for n in (1, 3, 5, 127):
            out += [ip + n / 128, -(ip + n / 128)]
    # Near-halfway decimal literals: the double is a hair above or below.
    for _ in range(300):
        ip = rnd.choice([0, 1, 2, 3, 262, 9999, 123456789, rnd.randint(0, 10**9)])
        x = float(f"{ip}.{rnd.randint(0, 999999):06d}5")
        out.append(x if rnd.random() < 0.5 else -x)
    # The values the divergence was first reported on, and familiar ones.
    out += [-262.4724295, 262.4724295, 2.0000005, 123456789.1234565, 1e15 + 0.5]
    out += [-1e15 - 0.5, 1e20 / 3, 0.1, 0.2, 0.3, 1 / 3, 2 / 3]
    # 15-17 significant digits over magnitudes 1e-8 .. ~3e31.
    for _ in range(300):
        x = float(f"{rnd.random():.17f}") * 10 ** rnd.uniform(-8, 31.5)
        out.append(x if rnd.random() < 0.5 else -x)
    # Random bit patterns: every exponent, mostly unremarkable mantissas.
    while len(out) < 1500:
        bits = rnd.getrandbits(64)
        x = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if math.isfinite(x) and abs(x) < 1e31:
            out.append(x)
    # Subnormals, signed zeros, and both sides of the zero-rounding threshold.
    out += [5e-324, -5e-324, 2.2250738585072014e-308, 1e-310, 0.0, -0.0]
    out += [4e-7, 5e-7, 4.9999999999e-7, 5.0000000001e-7, -5e-7, 1e-7, 5e-8]
    out += [5e-10, 9.9999995e-7, 0.0000015, 0.0000025]
    # Integers stored as double, around 2^53 and beyond.
    out += [float(2**53), float(2**53 + 2), float(2**53 - 1), 1e15, 1e16, 1e17]
    out += [1e20, 1e22, 1e25, 9.999999999999999e30, 123456789012345678.0]
    out += [-9007199254740993.0, 4503599627370495.5, 4503599627370496.5]
    for k in range(15, 32):
        out += [10.0**k, -(10.0**k) * 1.2345678901234567]
    return out


def render(
    dialect: Dialect,
    engine: str,
    xs: Sequence[float],
    expression: Callable[[str], str] | None = None,
    chunk: int = 500,
) -> list[str]:
    """Render each double in `xs` on a live engine; one string per value.

    By default the expression is the dialect's own `normalize` for a FLOAT
    column. Pass `expression` (column SQL -> text SQL) to measure another
    spelling instead, as the demo does for the pre-fix expressions.
    """
    if expression is None:
        template = dialect.normalize(FLOAT_COLUMN)
        quoted = dialect.quote(FLOAT_COLUMN.name)
        expression = lambda col: template.replace(quoted, col)  # noqa: E731
    out: list[str] = []
    for start in range(0, len(xs), chunk):
        part = xs[start : start + chunk]
        rows = " union all ".join(
            f"select {i} as i, cast('{x!r}' as {DOUBLE_TYPE[engine]}) as x"
            for i, x in enumerate(part)
        )
        sql = f"select i, x, {expression('x')} from ({rows}) t order by i"
        for (_, back, text), want in zip(dialect.query(sql), part, strict=True):
            # Bit-exact arrival, sign of zero aside (some engines fold -0.0).
            assert back == want, f"{engine}: {want!r} arrived as {back!r}"
            out.append(str(text))
    return out
