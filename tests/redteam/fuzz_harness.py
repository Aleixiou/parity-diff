"""Live, cross-engine differential fuzzing of `parity.diff` against an oracle.

The red-team harness behind `test_fuzz.py`. Each case:

1. draws a random schema (integer, bigint, text or composite key; one to
   several columns of every supported type, nullable) and random rows biased
   towards edge values - type boundaries, NULLs, empty strings, values sitting
   exactly on a rounding boundary at the float scale, Unicode, magnitudes near
   2^53;
2. loads the *same logical rows* into two live engines through the drivers'
   parameter binding, then applies random mutations to one side: the smallest
   change that must be visible at the float scale and one that must not be,
   NULL <-> '' and NULL <-> FALSE flips, deletes, inserts, a row moved to a new
   key, values swapped between rows, case / whitespace / Unicode-normalisation
   changes;
3. computes the expected result with an **independent oracle**: both tables
   are fetched in full through the raw drivers and compared *by value* in
   Python - NULL is never equal to anything but NULL, strings compare exactly,
   numbers compare after rounding half away from zero to the float scale (the
   documented semantics, CLAUDE.md section 4.2). None of the tool's dialect
   code is called;
4. runs `parity.diff` with a random bisection factor, threshold, float scale,
   column selection and max_diffs, and requires exactly the oracle's
   differences - keys, kinds and columns - with `identical` iff the oracle is
   empty.

A disagreement is shrunk (rows, then columns) to a minimal case and returned.
Known, confirmed findings are excluded from generation unless asked for (see
`KNOWN`), so the default run stays green and anything it flags is new.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import random
import struct
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any

from parity.dialects.base import get_dialect
from parity.engine import diff

UTC = dt.timezone.utc

#: Decimal context wide enough for DECIMAL(38) and doubles up to 2^116.
_WIDE = Context(prec=200)

#: Engines that can hold IEEE infinities and NaN in a double column.
NONFINITE_OK = {"postgres", "duckdb"}  # BigQuery can, but a JSON load cannot carry them

KINDS = ("int", "bigint", "dec", "double", "real", "bool", "text", "date", "ts", "tstz")

#: Generation switches for confirmed findings, so the default run does not keep
#: re-finding them. Each names the xfail test in test_fuzz.py that pins it.
KNOWN = {
    "sentinel_text": "RT-E1: the string '\\N' is indistinguishable from NULL",
    "separator_text": "RT-E2: chr(31) inside text shifts field boundaries",
}


# --------------------------------------------------------------- schema


@dataclass(frozen=True)
class Col:
    name: str
    kind: str
    p: int = 0  # DECIMAL precision
    s: int = 0  # DECIMAL scale
    #: Side B stores the column as this kind instead - the migration case of
    #: DECIMAL becoming DOUBLE, INTEGER becoming BIGINT, REAL becoming DOUBLE.
    b_kind: str | None = None

    def on_b(self) -> Col:
        """This column as side B stores it (its `b_kind`, if any)."""
        return replace(self, kind=self.b_kind) if self.b_kind else self


@dataclass
class Case:
    seed: int
    a_engine: str
    b_engine: str
    key: list[Col]
    cols: list[Col]
    rows_a: list[tuple[Any, ...]]  # key values then column values
    rows_b: list[tuple[Any, ...]]
    float_scale: int = 6
    bisection_factor: int = 32
    threshold: int = 10_000
    max_diffs: int | None = None
    columns_arg: list[str] | None = None
    exclude_arg: list[str] = field(default_factory=list)
    b_upper: bool = False  # store side B's identifiers in upper case
    mutations: list[str] = field(default_factory=list)

    @property
    def all_cols(self) -> list[Col]:
        """Key columns, then value columns, in row order."""
        return [*self.key, *self.cols]

    def describe(self) -> str:
        """One line that reproduces this case: seed, engines, schema, knobs."""
        return (
            f"seed={self.seed} {self.a_engine}->{self.b_engine} "
            f"key={[(c.name, c.kind) for c in self.key]} "
            f"cols={[(c.name, c.kind, c.p, c.s) for c in self.cols]} "
            f"fs={self.float_scale} bf={self.bisection_factor} th={self.threshold} "
            f"max={self.max_diffs} columns={self.columns_arg} exclude={self.exclude_arg} "
            f"rows={len(self.rows_a)}/{len(self.rows_b)} mut={self.mutations}"
        )


def ddl_type(engine: str, c: Col, is_key: bool) -> str:
    """The column type each engine stores a logical kind in."""
    k = c.kind
    if engine == "snowflake":
        return _snowflake_ddl(c)
    if k in EXTRA_DDL:
        return EXTRA_DDL[k][engine](is_key)
    if k == "int":
        return {"bigquery": "INT64"}.get(engine, "integer")
    if k == "bigint":
        return {"bigquery": "INT64"}.get(engine, "bigint")
    if k == "dec":
        if engine == "bigquery":
            return (
                f"BIGNUMERIC({c.p},{c.s})"
                if c.p > 29 or c.s > 9
                else f"NUMERIC({c.p},{c.s})"
            )
        return (
            f"numeric({c.p},{c.s})" if engine == "postgres" else f"decimal({c.p},{c.s})"
        )
    if k == "double":
        return {
            "postgres": "double precision",
            "duckdb": "double",
            "mysql": "double",
            "mssql": "float",
            "bigquery": "FLOAT64",
        }[engine]
    if k == "real":
        return {"mysql": "float", "bigquery": "FLOAT64"}.get(engine, "real")
    if k == "bool":
        return {"mssql": "bit", "bigquery": "BOOL"}.get(engine, "boolean")
    if k == "text":
        if engine == "mysql":
            return "varchar(191)" if is_key else "longtext"
        if engine == "mssql":
            return "nvarchar(450)" if is_key else "nvarchar(max)"
        return {"postgres": "text", "duckdb": "varchar", "bigquery": "STRING"}[engine]
    if k == "date":
        return {"bigquery": "DATE"}.get(engine, "date")
    if k == "ts":
        return {
            "mysql": "datetime(6)",
            "mssql": "datetime2(6)",
            "bigquery": "DATETIME",
        }.get(engine, "timestamp")
    if k == "tstz":
        return {
            "postgres": "timestamptz",
            "duckdb": "timestamptz",
            "mysql": "timestamp(6)",
            "mssql": "datetimeoffset(6)",
            "bigquery": "TIMESTAMP",
        }[engine]
    raise AssertionError(k)


#: Kinds added for round H (`fuzz_cross.py`): TIME, BINARY, UUID and JSON,
#: as each engine stores them. JSON only where the engines' normal forms can
#: coincide (PostgreSQL jsonb, MySQL json, DuckDB json).
EXTRA_DDL: dict[str, dict[str, Callable[[bool], str]]] = {
    "time": {
        "postgres": lambda key: "time",
        "duckdb": lambda key: "time",
        "mysql": lambda key: "time(6)",
        "mssql": lambda key: "time(6)",
        "bigquery": lambda key: "TIME",
    },
    "binary": {
        "postgres": lambda key: "bytea",
        "duckdb": lambda key: "blob",
        "mysql": lambda key: "varbinary(64)" if key else "longblob",
        "mssql": lambda key: "varbinary(64)" if key else "varbinary(max)",
        "bigquery": lambda key: "BYTES",
    },
    "uuid": {
        "postgres": lambda key: "uuid",
        "duckdb": lambda key: "uuid",
        "mysql": lambda key: "char(36)",
        "mssql": lambda key: "uniqueidentifier",
        "bigquery": lambda key: "STRING",
    },
    "json": {
        "postgres": lambda key: "jsonb",
        "duckdb": lambda key: "json",
        "mysql": lambda key: "json",
    },
    # The same logical type in another native type: what a migration does.
    "small": {
        "postgres": lambda key: "smallint",
        "duckdb": lambda key: "smallint",
        "mysql": lambda key: "smallint",
        "mssql": lambda key: "smallint",
        "bigquery": lambda key: "INT64",
    },
    "tiny": {
        "postgres": lambda key: "smallint",
        "duckdb": lambda key: "utinyint",
        "mysql": lambda key: "tinyint unsigned",
        "mssql": lambda key: "tinyint",
        "bigquery": lambda key: "INT64",
    },
    "ubig": {  # only where the engine has a 64-bit unsigned integer
        "duckdb": lambda key: "ubigint",
        "mysql": lambda key: "bigint unsigned",
    },
    "char": {
        "postgres": lambda key: "char(12)",
        "duckdb": lambda key: "varchar",
        "mysql": lambda key: "char(12)",
        "mssql": lambda key: "nchar(12)",
        "bigquery": lambda key: "STRING",
    },
    "ascii": {
        "postgres": lambda key: "varchar(200)",
        "duckdb": lambda key: "varchar",
        "mysql": lambda key: "varchar(200) character set latin1",
        "mssql": lambda key: "varchar(200)",
        "bigquery": lambda key: "STRING",
    },
    "money": {
        "postgres": lambda key: "numeric(19,4)",
        "duckdb": lambda key: "decimal(19,4)",
        "mysql": lambda key: "decimal(19,4)",
        "mssql": lambda key: "money",
        "bigquery": lambda key: "NUMERIC(19,4)",
    },
    "ts3": {
        "postgres": lambda key: "timestamp(3)",
        "duckdb": lambda key: "timestamp_ms",
        "mysql": lambda key: "datetime(3)",
        "mssql": lambda key: "datetime2(3)",
        "bigquery": lambda key: "DATETIME",
    },
}

#: Kinds whose values are integers / exact decimals / naive timestamps / text.
INT_LIKE = {"int": 2**31, "bigint": 2**63, "small": 2**15, "tiny": 256, "ubig": 2**64}
TEXT_LIKE = {"text", "char", "ascii"}


def _snowflake_ddl(c: Col) -> str:
    """Snowflake's storage for each logical kind."""
    return {
        "int": "integer",
        "bigint": "bigint",
        "dec": f"number({c.p},{c.s})",
        "double": "double",
        "real": "float",
        "bool": "boolean",
        "text": "varchar",
        "date": "date",
        "ts": "timestamp_ntz(6)",
        "tstz": "timestamp_tz(6)",
        "time": "time(6)",
        "binary": "binary",
        "uuid": "varchar(36)",
        "small": "smallint",
        "tiny": "number(3,0)",
        "char": "varchar",
        "ascii": "varchar",
        "money": "number(19,4)",
        "ts3": "timestamp_ntz(3)",
    }[c.kind]


# ------------------------------------------------------------ generation


def _f32(x: float) -> float:
    """Round a double to the nearest float32, as a REAL column stores it."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


EDGE_TEXT = [
    "",
    " ",
    "a",
    "A",
    "a ",
    " a",
    "  ",
    "\t",
    "\n",
    "NULL",
    "null",
    "None",
    "true",
    "0",
    "-0",
    "0.000000",
    "é",  # NFC
    "é",  # NFD
    "ß",
    "Straße",
    "İstanbul",
    "日本語",
    "😀",
    "👨‍👩‍👧",
    "​",  # zero-width space
    "‮abc",  # RTL override
    "'; drop table x; --",
    '"quoted"',
    "back\\slash",
    "x" * 4001,  # past SQL Server's non-max 4,000-character concat cut
    "y" * 9000,
]


def gen_value(rng: random.Random, c: Col, engines: set[str], fs: int, known: set[str]):
    """One value for column `c`, biased towards edges every engine can store."""
    k = c.kind
    if k in (
        "time",
        "binary",
        "uuid",
        "json",
        "small",
        "tiny",
        "ubig",
        "char",
        "ascii",
        "money",
        "ts3",
    ):
        return gen_extra(rng, k, engines)
    if k == "int":
        return rng.choice(
            [0, 1, -1, 2**31 - 1, -(2**31), 7, rng.randint(-(2**31), 2**31 - 1)]
            + [rng.randint(-1000, 1000)] * 3
        )
    if k == "bigint":
        return rng.choice(
            [0, 2**63 - 1, -(2**63), 2**53, 2**53 + 1, -(2**53) - 1, 2**31]
            + [rng.randint(-(2**63), 2**63 - 1)] * 4
        )
    if k == "dec":
        return gen_decimal(rng, c, fs)
    if k == "double":
        return gen_double(
            rng, fs, nonfinite=engines <= NONFINITE_OK, subnormal="mysql" not in engines
        )
    if k == "real":
        x = _f32(gen_double(rng, fs, nonfinite=False, small=True, subnormal=False))
        return 0.0 if abs(x) < 1.1754943508222875e-38 else x  # no float32 subnormals
    if k == "bool":
        return rng.choice([True, False])
    if k == "text":
        if rng.random() < 0.5:
            v = rng.choice(EDGE_TEXT)
        else:
            alphabet = "abcAB xyz-_.é日😀́\t"
            v = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        if "postgres" not in engines and rng.random() < 0.03:
            v = "a\x00b"  # PostgreSQL text cannot hold NUL; the others can
        if "sentinel_text" in known and rng.random() < 0.1:
            v = "\\N"
        if "separator_text" in known and rng.random() < 0.1:
            v = v + "\x1f"
        return v
    if k == "date":
        lo = dt.date(1000, 1, 1) if "mysql" in engines else dt.date(1, 1, 1)
        return rng.choice(
            [
                lo,
                dt.date(9999, 12, 31),
                dt.date(2024, 2, 29),
                dt.date(1970, 1, 1),
                dt.date(1999, 12, 31),
                lo + dt.timedelta(days=rng.randint(0, (dt.date(9999, 12, 31) - lo).days)),
            ]
        )
    if k == "ts":
        lo = dt.datetime(1000, 1, 1) if "mysql" in engines else dt.datetime(1, 1, 1)
        span = int((dt.datetime(9999, 12, 31, 23, 59, 59) - lo).total_seconds())
        return rng.choice(
            [
                lo,
                dt.datetime(9999, 12, 31, 23, 59, 59, 999999),
                dt.datetime(2024, 2, 29, 13, 4, 5, 123456),
                dt.datetime(2024, 1, 1),
                dt.datetime(2024, 1, 1, 0, 0, 0, 1),
                dt.datetime(1999, 12, 31, 23, 59, 59, 999999),
                lo
                + dt.timedelta(
                    seconds=rng.randint(0, span), microseconds=rng.randint(0, 999999)
                ),
            ]
        )
    if k == "tstz":
        # MySQL TIMESTAMP holds only 1970-01-01 00:00:01 to 2038-01-19 UTC.
        lo = dt.datetime(1971, 1, 1, tzinfo=UTC)
        span = int((dt.datetime(2037, 12, 31, tzinfo=UTC) - lo).total_seconds())
        base = lo + dt.timedelta(
            seconds=rng.randint(0, span),
            microseconds=rng.choice([0, 1, 999999, rng.randint(0, 999999)]),
        )
        off = dt.timezone(dt.timedelta(minutes=rng.choice([0, 0, 330, -300, 840, -720])))
        return base.astimezone(off)
    raise AssertionError(k)


EDGE_BYTES = [
    b"",
    b"\x00",
    b"\x00\x00",
    b"a",
    b"a\x00",
    b"\x00a",
    b"\xff" * 8,
    b"\\N",
    b"\x1f",
    b"\x80\x81",
    b"\xed\xa0\x80",  # a UTF-8-encoded surrogate: invalid UTF-8
    b"abc",
    b"ABC",
    b"x" * 64,
]


def gen_json(rng: random.Random, depth: int = 0) -> Any:
    """A JSON value every normal form (jsonb, MySQL, DuckDB verbatim) keeps
    alike: integers, plain strings, objects whose keys are distinct and already
    in length-then-bytes order, so no engine reorders them."""
    r = rng.random()
    if depth > 2 or r < 0.4:
        return rng.choice(
            [
                0,
                1,
                -1,
                7,
                123456789,
                True,
                False,
                None,
                "",
                "a",
                "é",
                "日本",
                "a b",
                'q"t',
                "b\\s",
            ]
        )
    if r < 0.7:
        return [gen_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    keys = rng.sample(
        ["a", "b", "aa", "ab", "id", "x1", "key", "name"], rng.randint(0, 4)
    )
    keys.sort(key=lambda k: (len(k.encode()), k.encode()))
    return {k: gen_json(rng, depth + 1) for k in keys}


def json_text(v: Any) -> str:
    """The layout every engine's canonical JSON uses: `{"a": 1, "b": [1, 2]}`."""
    import json

    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


def gen_extra(rng: random.Random, k: str, engines: set[str]) -> Any:
    """A value of one of the round-H kinds."""
    if k == "time":
        return rng.choice(
            [
                dt.time(0, 0),
                dt.time(23, 59, 59, 999999),
                dt.time(12, 0, 0, 1),
                dt.time(0, 0, 0, 999999),
                dt.time(
                    rng.randint(0, 23),
                    rng.randint(0, 59),
                    rng.randint(0, 59),
                    rng.choice([0, rng.randint(0, 999999)]),
                ),
            ]
        )
    if k == "binary":
        if rng.random() < 0.5:
            return rng.choice(EDGE_BYTES)
        return bytes(rng.randrange(256) for _ in range(rng.randint(0, 20)))
    if k == "uuid":
        return rng.choice(
            [
                uuid.UUID(int=0),
                uuid.UUID(int=2**128 - 1),
                uuid.UUID(int=rng.getrandbits(128)),
                uuid.UUID(int=rng.getrandbits(128)),
            ]
        )
    if k == "json":
        return json_text(gen_json(rng))
    if k == "small":
        return rng.choice([0, 1, -1, 32767, -32768, rng.randint(-32768, 32767)])
    if k == "tiny":
        return rng.choice([0, 1, 255, 127, 128, rng.randint(0, 255)])
    if k == "ubig":
        return rng.choice(
            [0, 1, 2**63 - 1, 2**63, 2**64 - 1, 2**53 + 1, rng.randint(0, 2**64 - 1)]
        )
    if k == "char":
        # No trailing spaces: char(n) cannot hold them apart from padding.
        v = rng.choice(
            [
                "",
                "a",
                "A",
                " a",
                "abc",
                "é",
                "日本",
                "x" * 12,
                "a b",
                "".join(rng.choice("abcXYZ 0é") for _ in range(rng.randint(0, 12))),
            ]
        )
        return v.rstrip(" ")
    if k == "ascii":
        return rng.choice(
            [
                "",
                " ",
                "a",
                "A",
                "a ",
                "it's",
                "back\\slash",
                "%_",
                "".join(chr(rng.randint(32, 126)) for _ in range(rng.randint(0, 30))),
            ]
        )
    if k == "money":
        top = 922337203685476  # money max is ...477.5807
        return Decimal(
            rng.choice(
                [
                    0,
                    1,
                    -1,
                    top,
                    -top,
                    rng.randint(-top, top),
                    rng.randint(-(10**6), 10**6),
                ]
            )
        ) + Decimal(rng.choice([0, 5, 9999, 5000, rng.randint(0, 9999)])).scaleb(-4)
    if k == "ts3":
        base = dt.datetime(1900, 1, 1) + dt.timedelta(seconds=rng.randint(0, 6 * 10**9))
        return base.replace(
            microsecond=rng.choice([0, 1, 999, 500, rng.randint(0, 999)]) * 1000
        )
    raise AssertionError(k)


def gen_decimal(rng: random.Random, c: Col, fs: int) -> Decimal:
    """A DECIMAL(p, s) value. All arithmetic in `_WIDE`: the default context
    keeps 28 digits and would silently round a DECIMAL(38) value."""
    w = _WIDE
    q = Decimal(1).scaleb(-c.s, context=w)
    intd = c.p - c.s
    biggest = w.subtract(w.power(Decimal(10), intd), q)
    choice = rng.random()
    if choice < 0.15:
        v = rng.choice([biggest, biggest.copy_negate(), Decimal(0), q, q.copy_negate()])
    elif choice < 0.4 and c.s > fs:
        # Exactly on, or just either side of, a rounding tie at the float scale.
        whole = Decimal(rng.randint(0, 10 ** min(intd, 6) - 1)) if intd else Decimal(0)
        half = Decimal(5).scaleb(-(fs + 1), context=w)
        v = w.add(w.add(whole, half), rng.choice([Decimal(0), q, q.copy_negate()]))
    else:
        digits = rng.randint(0, c.p)
        n = rng.randint(0, 10**digits - 1) if digits else 0
        v = Decimal(n).scaleb(-c.s, context=w)
    if rng.random() < 0.5:
        v = v.copy_negate()
    v = v.quantize(q, context=w)
    if v.copy_abs() > biggest:
        v = biggest.copy_sign(v)
    return v


_ADV: list[float] = []


def _adversarial() -> list[float]:
    """The repo's adversarial doubles (ties at six places, 15-17 digits, 2^53
    neighbours), minus what no engine compares (non-finite, >= 2^116)."""
    if not _ADV:
        from float_sample import adversarial_doubles

        _ADV.extend(
            x for x in adversarial_doubles() if math.isfinite(x) and abs(x) < 2.0**116
        )
    return _ADV


def gen_double(
    rng: random.Random,
    fs: int,
    nonfinite: bool,
    small: bool = False,
    subnormal: bool = True,
) -> float:
    """A double for the float scale `fs`, biased to ties, 2^53 neighbours and
    the adversarial sample; non-finite and subnormal only where storable."""
    half = 5 * 10.0 ** -(fs + 1)
    unit = 10.0**-fs
    pool = [
        0.0,
        -0.0,
        1.5,
        -0.125,
        1 / 3,
        0.1 + 0.2,
        half,
        -half,
        half * 0.999,
        unit,
        1.0 + half,
        2.0000005,
        2.0**53,
        2.0**53 - 1,
        2.0**53 + 2,
        1e15 + 0.5,
        -4088288.25,
        123456.789,
        1e-300,
        5e-324,
        1e30,
        -1e30,
        2.0**100,
        rng.uniform(-1e6, 1e6),
        rng.uniform(-1, 1),
        round(rng.uniform(-1000, 1000), fs + 1),
        math.ldexp(rng.random(), rng.randint(-60, 115)),
    ]
    if rng.random() < 0.3:
        pool = _adversarial()
    if small:
        pool = [p for p in pool if abs(p) < 1e30] or [0.5]
    if not subnormal:
        # MySQL DOUBLE refuses subnormals ("Data truncated", seed 300007).
        pool = [p for p in pool if p == 0 or abs(p) >= 2.2250738585072014e-308]
    if nonfinite:
        pool = [*pool, math.inf, -math.inf, math.nan]  # never += : pool may be _ADV
    return rng.choice(pool)


def gen_key(rng: random.Random, key: list[Col], n: int, engines: set[str]) -> list[tuple]:
    """`n` distinct keys. Case- and accent-only differences between text
    keys are welcome everywhere since RT-E5 / RT-E8 were fixed."""
    out: list[tuple] = []
    seen: set[Any] = set()
    folded: set[Any] = set()
    sparse = rng.random() < 0.3
    tries = 0
    while len(out) < n and tries < n * 50:
        tries += 1
        vals = []
        for c in key:
            if c.kind == "int":
                vals.append(
                    rng.randint(-(2**31), 2**31 - 1)
                    if sparse
                    else rng.randint(-50, n * 3)
                )
            elif c.kind == "bigint":
                vals.append(
                    rng.choice([2**63 - 1, -(2**63), rng.randint(-(2**63), 2**63 - 1)])
                    if sparse
                    else rng.randint(0, n * 3)
                )
            elif c.kind == "text":
                if rng.random() < 0.2:
                    vals.append(
                        rng.choice(["", " ", "x", "日本", "😀", "a b", "z" * 150])
                    )
                else:
                    vals.append(
                        "".join(
                            rng.choice("abcdefgh0123") for _ in range(rng.randint(1, 8))
                        )
                    )
            elif c.kind == "date":
                vals.append(dt.date(2000, 1, 1) + dt.timedelta(days=rng.randint(0, 9000)))
            elif c.kind == "dec":
                q = Decimal(1).scaleb(-c.s, context=_WIDE)
                top = 10 ** min(c.p, 15) - 1
                small = min(50, top)
                n_ = rng.choice(
                    [0, 1, top, rng.randint(-top, top), rng.randint(-small, small)]
                )
                vals.append((Decimal(n_) * q).quantize(q, context=_WIDE))
            elif c.kind == "ts":
                base = dt.datetime(2000, 1, 1) + dt.timedelta(
                    seconds=rng.randint(0, 10**9)
                )
                vals.append(
                    base.replace(
                        microsecond=rng.choice([0, 1, 999999, rng.randint(0, 999999)])
                    )
                )
            elif c.kind == "uuid":
                vals.append(uuid.UUID(int=rng.getrandbits(128)))
            elif c.kind == "time":
                vals.append(
                    dt.time(
                        rng.randint(0, 23),
                        rng.randint(0, 59),
                        rng.randint(0, 59),
                        rng.randint(0, 999999),
                    )
                )
            elif c.kind == "binary":
                vals.append(bytes(rng.randrange(256) for _ in range(rng.randint(1, 6))))
            else:
                raise AssertionError(c.kind)
        t = tuple(vals)
        f = _mysql_key_fold(t)
        if t in seen or (f in folded and "mysql" in engines):
            continue
        seen.add(t)
        folded.add(f)
        out.append(t)
    return out


def _mysql_key_fold(t: tuple) -> Any:
    """A key as MySQL's uniqueness count sees it - now exactly the key.

    MySQL used to count keys under utf8mb4_0900_ai_ci, which merged 'a'/'A',
    'e'/'é' (RT-E5) and, with the separator ignorable, (5, 944)/(59, 44)
    (RT-E8), so the generator kept such keys apart. Both are fixed (it counts
    bytes), so the generator now produces them on purpose.
    """
    return t


def _mysql_fold(s: str) -> str:
    """Roughly utf8mb4_0900_ai_ci equality: case- and accent-blind, NO PAD."""
    d = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in d if not unicodedata.combining(ch)).casefold()


# ------------------------------------------------------------- mutations


def mutate(rng: random.Random, case: Case, engines: set[str], known: set[str]) -> None:
    """Apply 0-4 random mutations to one side (usually B), recording each."""
    nkey = len(case.key)
    target_b = rng.random() < 0.8
    rows = case.rows_b if target_b else case.rows_a
    tag = "B" if target_b else "A"
    want = rng.choice([0, 1, 1, 2, 3, 4])
    done = 0
    for _ in range(want * 10):
        if not rows or done >= want:
            break
        extra_ops = []
        if "sentinel_text" in known:
            extra_ops += ["sentinel"] * 3
        if "separator_text" in known:
            extra_ops += ["separator"] * 3
        op = rng.choice(
            [
                *extra_ops,
                "num_visible",
                "num_invisible",
                "null_flip",
                "empty_flip",
                "bool_flip",
                "delete",
                "insert",
                "move",
                "swap",
                "case",
                "space",
                "nfd",
                "ts_micro",
                "int_bump",
            ]
        )
        i = rng.randrange(len(rows))
        r = list(rows[i])
        cols = list(range(nkey, nkey + len(case.cols)))
        text_cols = [j for j in cols if case.cols[j - nkey].kind == "text"]
        if op == "delete":
            del rows[i]
        elif op == "sentinel" and text_cols:
            # NULL on one side, the two characters backslash-N on the other.
            j = rng.choice(text_cols)
            if i >= min(len(case.rows_a), len(case.rows_b)):
                continue
            ra, rb = list(case.rows_a[i]), list(case.rows_b[i])
            if ra[:nkey] != rb[:nkey]:
                continue
            ra[j], rb[j] = None, "\\N"
            case.rows_a[i], case.rows_b[i] = tuple(ra), tuple(rb)
        elif op == "separator" and len(text_cols) >= 2:
            # Move a chr(31) across the boundary between two adjacent columns.
            j = rng.randrange(len(text_cols) - 1)
            j1, j2 = text_cols[j], text_cols[j + 1]
            if j2 != j1 + 1:
                continue
            if i >= min(len(case.rows_a), len(case.rows_b)):
                continue
            ra, rb = list(case.rows_a[i]), list(case.rows_b[i])
            if ra[:nkey] != rb[:nkey]:
                continue
            x, y = "p", "q"
            ra[j1], ra[j2] = x + "\x1f", y
            rb[j1], rb[j2] = x, "\x1f" + y
            case.rows_a[i], case.rows_b[i] = tuple(ra), tuple(rb)
        elif op in ("insert", "move"):
            present = {tuple(x[:nkey]) for x in case.rows_a} | {
                tuple(x[:nkey]) for x in case.rows_b
            }
            new = gen_key(rng, case.key, len(present) + 1, engines)
            fresh = [k for k in new if k not in present]
            folded = {_mysql_key_fold(k) for k in present}
            fresh = [k for k in fresh if _mysql_key_fold(k) not in folded]
            if not fresh:
                continue
            if op == "insert":
                vals = [
                    gen_value(rng, c, engines, case.float_scale, known) for c in case.cols
                ]
                rows.append((*fresh[0], *vals))
            else:
                rows[i] = (*fresh[0], *r[nkey:])
        elif op == "swap" and cols and len(rows) > 1:
            j = rng.randrange(len(rows))
            ci = rng.choice(cols)
            rj = list(rows[j])
            r[ci], rj[ci] = rj[ci], r[ci]
            rows[i], rows[j] = tuple(r), tuple(rj)
        elif cols:
            ci = rng.choice(cols)
            c = case.cols[ci - nkey]
            v = r[ci]
            nv = _mutate_cell(rng, op, c, v, case.float_scale, engines)
            if nv is _SKIP:
                continue
            r[ci] = nv
            rows[i] = tuple(r)
        else:
            continue
        done += 1
        case.mutations.append(f"{tag}:{op}@{i}")


_SKIP = object()


def _mutate_cell(rng: random.Random, op: str, c: Col, v: Any, fs: int, engines: set[str]):
    """The smallest change of kind `op` to one cell value, or `_SKIP` if the
    op does not apply to this column kind or value."""
    k = c.kind
    if op == "null_flip":
        if v is None:
            return gen_value(rng, c, engines, fs, set())
        return None
    if op == "empty_flip" and k == "text":
        return None if v == "" else ""
    if op == "bool_flip" and k == "bool":
        return (
            {True: None, False: None, None: False}[v]
            if rng.random() < 0.5
            else (not v if v is not None else True)
        )
    if op in ("num_visible", "num_invisible") and v is not None:
        unit = Decimal(1).scaleb(-fs)
        if k == "dec":
            step = unit if op == "num_visible" else Decimal(1).scaleb(-(fs + 3))
            q = Decimal(1).scaleb(-c.s)
            nv = _WIDE.add(v, step).quantize(q, context=_WIDE)
            if nv == v or nv.copy_abs() >= _WIDE.power(Decimal(10), c.p - c.s):
                return _SKIP
            return nv
        if k in ("double", "real") and math.isfinite(v):
            step = float(unit) if op == "num_visible" else float(unit) / 1000
            nv = v + step
            return _f32(nv) if k == "real" else nv
        if k in ("int", "bigint"):
            return _SKIP
    if op == "int_bump" and k in INT_LIKE and v is not None:
        lim = INT_LIKE[k]
        lo = 0 if k in ("tiny", "ubig") else -lim
        nv = v + rng.choice([1, -1])
        return nv if lo <= nv < lim else _SKIP
    if op in ("num_visible", "num_invisible") and k == "money" and v is not None:
        step = Decimal(1).scaleb(-min(fs, 4)) if op == "num_visible" else _SKIP
        if step is _SKIP or fs > 4:
            return _SKIP
        nv = v + step
        return nv if abs(nv) < Decimal("922337203685477.5807") else _SKIP
    if op == "ts_micro" and k == "ts3" and v is not None:
        return v + dt.timedelta(milliseconds=rng.choice([1, -1]))
    if op == "case" and k in ("char", "ascii") and v:
        nv = v.swapcase()
        return nv if nv != v else _SKIP
    if op == "space" and k == "char" and not v:
        return _SKIP  # " " + "" is a trailing space, which char(n) cannot hold
    if op == "space" and k in ("char", "ascii") and v is not None:
        nv = " " + v if k == "char" else rng.choice([v + " ", " " + v])
        return nv if len(nv) <= 12 or k == "ascii" else _SKIP
    if op == "case" and k == "text" and v:
        nv = v.swapcase()
        return nv if nv != v else _SKIP
    if op == "space" and k == "text" and v is not None:
        return rng.choice([v + " ", " " + v, v + "\t"])
    if op == "nfd" and k == "text" and v:
        nv = unicodedata.normalize("NFD", v)
        if nv == v:
            nv = unicodedata.normalize("NFC", v)
        return nv if nv != v else "é"
    if op == "time_micro" and k == "time" and v is not None:
        full = dt.datetime.combine(dt.date(2000, 1, 2), v)
        nv = (full + dt.timedelta(microseconds=rng.choice([1, -1]))).time()
        return nv if nv != v else _SKIP
    if op == "bytes_tweak" and k == "binary" and v is not None:
        return rng.choice(
            [
                v + b"\x00",
                b"\x00" + v,
                v[:-1],
                v.upper() if v.upper() != v else v + b"a",
                bytes([v[0] ^ 1]) + v[1:] if v else b"\x00",
            ]
        )
    if op == "uuid_tweak" and k == "uuid" and v is not None:
        return uuid.UUID(int=v.int ^ (1 << rng.randrange(128)))
    if op == "json_tweak" and k == "json" and v is not None:
        import json

        doc = json.loads(v)
        nv = json_text(rng.choice([[doc], {"a": doc}, 0 if doc != 0 else 1, str(doc)]))
        return nv if nv != v else _SKIP
    if op == "ts_micro" and k in ("ts", "tstz") and v is not None:
        try:
            return v + dt.timedelta(microseconds=rng.choice([1, -1]))
        except OverflowError:
            return _SKIP
    return _SKIP


def random_case(
    seed: int,
    a_engine: str,
    b_engine: str,
    known: set[str] = frozenset(),
    max_rows: int = 120,
) -> Case:
    """One reproducible case from `seed`: schema, rows on both sides, knobs
    and mutations. Everything random is drawn from this one seed."""
    rng = random.Random(seed)
    engines = {a_engine, b_engine}
    fs = rng.choice([0, 1, 2, 3, 6, 6, 6, 6, 9, 12, 15, 18])
    key_shape = rng.choice(
        ["int", "int", "bigint", "text", "int+text", "text+date", "bigint+int"]
    )
    key = [Col(f"k{i}", kind) for i, kind in enumerate(key_shape.split("+"))]
    kinds = [k for k in KINDS if not (k == "bool" and "mysql" in engines)]
    cols: list[Col] = []
    shape = rng.random()
    # Mostly narrow; sometimes wider than MAX_CONCAT_ARGS (64), so the row
    # text nests; sometimes tall, so the bisection goes several levels deep.
    width = rng.randint(65, 130) if shape < 0.05 else rng.randint(1, 6)
    n = rng.choice([0, 1, 2, 5, 20, 60, max_rows])
    if shape < 0.05:
        n = min(n, 15)
    elif shape > 0.95:
        n = 1500
    for i in range(width):
        kind = rng.choice(kinds)
        name = f"c{i:03d}" if width > 9 else f"c{i}"
        if rng.random() < 0.05:
            # BigQuery column names are letters, digits and underscores.
            odd = (
                ["Mixed_Case", "Select"]
                if "bigquery" in engines
                else ["Mixed Case", "naïve", "select", "a-b", "x.y"]
            )
            name = rng.choice(odd) + str(i)
        b_kind = None
        if kind == "dec":
            p = rng.randint(1, 38)
            s = rng.randint(0, min(p, 12))
            # DuckDB and SQL Server render through decimal(38, scale), so wider
            # integer parts raise loudly - a refusal, not a finding.
            p = min(p, s + 38 - fs)
            if rng.random() < 0.2:
                b_kind = "double"
                # As a double, 10^35 and up is past 2^116: a loud refusal by
                # design (RT-E10 pins its message), not a comparison.
                p = min(p, s + 34)
            cols.append(Col(name, kind, p, s, b_kind))
            continue
        if kind == "int" and rng.random() < 0.2:
            b_kind = "bigint"
        if kind == "real" and rng.random() < 0.3:
            b_kind = "double"
        cols.append(Col(name, kind, b_kind=b_kind))
    keys = gen_key(rng, key, n, engines)
    rows = []
    null_rate = rng.choice([0.0, 0.1, 0.3])
    for k in keys:
        vals = [
            None if rng.random() < null_rate else gen_value(rng, c, engines, fs, known)
            for c in cols
        ]
        rows.append((*k, *vals))
    case = Case(
        seed=seed,
        a_engine=a_engine,
        b_engine=b_engine,
        key=key,
        cols=cols,
        rows_a=list(rows),
        rows_b=list(rows),
        float_scale=fs,
        bisection_factor=rng.choice([2, 3, 4, 7, 32]),
        threshold=rng.choice([1, 2, 5, 50, 10_000]),
        max_diffs=rng.choice([None, None, None, 1, 3]),
        b_upper=rng.random() < 0.25,
    )
    if cols and rng.random() < 0.15:
        case.columns_arg = rng.sample([c.name for c in cols], rng.randint(1, len(cols)))
    elif cols and rng.random() < 0.15:
        case.exclude_arg = [rng.choice(cols).name]
    mutate(rng, case, engines, known)
    return case


# ---------------------------------------------------------------- oracle


def num_text(v: Any, fs: int) -> str:
    """CLAUDE.md section 4.2, written from the prose: the value the user would
    have typed (shortest repr for a double below 2^53, the exact integer from
    2^53), rounded half away from zero to `fs` places, unsigned zero."""
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        d = Decimal(int(v)) if abs(v) >= 2**53 else Decimal(repr(v))
    else:
        d = Decimal(v)
    q = d.quantize(Decimal(1).scaleb(-fs), rounding=ROUND_HALF_UP, context=_WIDE)
    if q == 0:
        q = abs(q)
    return format(q, "f")


def key_text(v: Any, kind: str) -> str:
    """How a hashed key's component reads in the report (canonical text)."""
    if kind in ("int", "bigint"):
        return str(v)
    if kind == "date":
        return f"{v.year:04d}-{v.month:02d}-{v.day:02d}"
    if kind == "dec":
        t = format(Decimal(v), "f")
        if "." in t:
            t = t.rstrip("0").rstrip(".")
        return "0" if t in ("-0", "") else t
    if kind == "ts":
        return v.strftime("%Y-%m-%d %H:%M:%S.") + f"{v.microsecond:06d}"
    if kind == "time":
        v = logical(v, "time", 6)
        return v.strftime("%H:%M:%S.") + f"{v.microsecond:06d}"
    if kind == "uuid":
        return str(v).lower()
    if kind == "binary":
        return "\\\\x" + bytes(v).hex()  # BINARY_PREFIX: an escaped backslash
    if kind == "text":
        return v.replace("\\", "\\\\").replace("\x1f", "\\s")
    return v


def logical(v: Any, kind: str, fs: int) -> Any:
    """Reduce a fetched value to what the comparison is *documented* to see."""
    if v is None:
        return None
    if kind == "money":
        return ("num", num_text(v, fs))
    if kind in ("small", "tiny", "ubig"):
        return int(v)
    if kind == "ts3" and isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if kind == "char":
        return v.rstrip(" ")  # char(n) pads; the rules trim (CLAUDE.md 4.2)
    if kind in ("dec", "double", "real"):
        if kind == "real":
            v = _f32(float(v))
        return ("num", num_text(v, fs))
    if kind == "bool":
        return bool(v)
    if kind == "tstz":
        if isinstance(v, str):
            v = dt.datetime.fromisoformat(v)
        if v.tzinfo is not None:
            v = v.astimezone(UTC).replace(tzinfo=None)
        return v
    if kind == "ts" and isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.replace(tzinfo=None)
    if kind in ("int", "bigint"):
        return int(v)
    if kind == "time":
        if isinstance(v, dt.timedelta):  # MySQL's driver reads TIME as a duration
            us = v // dt.timedelta(microseconds=1)
            return dt.time(
                us // 3_600_000_000,
                us // 60_000_000 % 60,
                us // 1_000_000 % 60,
                us % 1_000_000,
            )
        if isinstance(v, str):
            return dt.time.fromisoformat(v)
        return v
    if kind == "binary":
        return bytes(v)
    if kind == "uuid":
        return str(v).lower()
    if kind == "json":
        import json

        doc = json.loads(v) if isinstance(v, (str, bytes)) else v
        return json.dumps(doc, sort_keys=True, ensure_ascii=False)
    if kind == "dec" and not isinstance(v, Decimal):
        return v
    return v


Expected = set[tuple[Any, str, tuple[str, ...]]]


def oracle(case: Case, fetched_a: list[tuple], fetched_b: list[tuple]) -> Expected:
    """The differences `diff` must report, computed from the raw rows alone -
    no dialect SQL - with the documented semantics (NULL, exact text,
    numbers at the float scale)."""
    nkey = len(case.key)
    single_int = nkey == 1 and case.key[0].kind in ("int", "bigint")
    compared = list(case.cols)
    if case.columns_arg is not None:
        compared = [c for c in compared if c.name in case.columns_arg]
    compared = [c for c in compared if c.name not in case.exclude_arg]
    idx = {c.name: nkey + i for i, c in enumerate(case.cols)}

    def report_key(r: tuple) -> Any:
        """The key as `diff` reports it: an int, or the canonical text joined
        by chr(31) for a hashed key."""
        if single_int:
            return int(r[0])
        return "\x1f".join(key_text(r[i], case.key[i].kind) for i in range(nkey))

    def by_key(rows: list[tuple]) -> dict:
        """Rows indexed by logical key; a duplicate is a generator bug."""
        out = {}
        for r in rows:
            # Exact key text, never rounded (a DECIMAL key is not at the
            # float scale - `normalize_key`).
            k = tuple(key_text(r[i], case.key[i].kind) for i in range(nkey))
            assert k not in out, f"duplicate key generated: {k}"
            out[k] = r
        return out

    a, b = by_key(fetched_a), by_key(fetched_b)
    exp: Expected = set()
    for k in set(a) | set(b):
        ra, rb = a.get(k), b.get(k)
        if rb is None:
            exp.add((report_key(ra), "only_in_a", ()))
        elif ra is None:
            exp.add((report_key(rb), "only_in_b", ()))
        else:
            changed = tuple(
                sorted(
                    c.name.casefold()
                    for c in compared
                    if logical(ra[idx[c.name]], c.kind, case.float_scale)
                    != logical(rb[idx[c.name]], c.b_kind or c.kind, case.float_scale)
                )
            )
            if changed:
                exp.add((report_key(ra), "different", changed))
    return exp


def observed(result: Any) -> Expected:
    """The diff result reduced to the oracle's shape: (key, kind, columns)."""
    return {
        (
            d.key,
            d.kind,
            tuple(sorted(c.casefold() for c in d.columns))
            if d.kind == "different"
            else (),
        )
        for d in result.diffs
    }


# --------------------------------------------------------------- engines


class Engine:
    """Write side of one engine: create, load, fetch, drop - via the raw driver."""

    name: str

    def __init__(self, url: str) -> None:
        """Remember the URL; subclasses connect."""
        self.url = url

    def ident(self, s: str) -> str:
        """Quote an identifier the way this engine's DDL needs."""
        return '"' + s + '"'

    def table_ref(self, table: str) -> str:
        """The table name as passed to `diff` (schema-qualified where needed)."""
        return table

    def create(
        self,
        table: str,
        cols: list[Col],
        nkey: int,
        upper: bool,
        names: list[str] | None = None,
    ) -> list[str]:
        """Drop and create the table; returns the stored column names."""
        if names is None:
            names = [c.name.upper() if upper else c.name for c in cols]
        spec = ", ".join(
            f"{self.ident(n)} {ddl_type(self.name, c, i < nkey)}"
            for i, (n, c) in enumerate(zip(names, cols, strict=True))
        )
        self.exec(f"drop table if exists {self.ident_table(table)}")
        self.exec(f"create table {self.ident_table(table)} ({spec})")
        return names

    def ident_table(self, table: str) -> str:
        """The table name, quoted for this engine's DDL."""
        return self.ident(table)

    def adapt(self, v: Any, c: Col) -> Any:
        """Turn a Python value into what this driver binds losslessly."""
        return v

    def load(
        self, table: str, names: list[str], cols: list[Col], rows: list[tuple]
    ) -> None:
        """Insert every row through parameter binding."""
        if not rows:
            return
        ph = ", ".join([self.ph] * len(cols))
        sql = (
            f"insert into {self.ident_table(table)} "
            f"({', '.join(self.ident(n) for n in names)}) values ({ph})"
        )
        data = [
            tuple(self.adapt(v, c) for v, c in zip(r, cols, strict=True)) for r in rows
        ]
        self.many(sql, data)

    def fetch(
        self, table: str, names: list[str], cols: list[Col], where: str | None = None
    ) -> list[tuple]:
        """Every row back through the raw driver, for the oracle; with
        `where`, only the rows that predicate selects (the engine's own SQL)."""
        sel = ", ".join(self.fetch_expr(n, c) for n, c in zip(names, cols, strict=True))
        sql = f"select {sel} from {self.ident_table(table)}"
        if where:
            sql += f" where ({where})"
        return [tuple(r) for r in self.rows(sql)]

    def fetch_expr(self, name: str, c: Col) -> str:
        """How the oracle selects one column so its value survives the driver."""
        return self.ident(name)

    def drop(self, table: str) -> None:
        """Drop the table, ignoring that it may not exist."""
        self.exec(f"drop table if exists {self.ident_table(table)}")

    def dialect_url(self, table: str) -> str:
        """The connection string the tool's dialect opens."""
        return self.url

    def close(self) -> None:
        """Close the writer's connection."""


class PG(Engine):
    name = "postgres"
    ph = "%s"

    def __init__(self, url: str) -> None:
        """Connect the writer for this engine."""
        import psycopg

        super().__init__(url)
        self.conn = psycopg.connect(url, autocommit=True)
        self.conn.execute("set time zone 'UTC'")

    def exec(self, sql: str) -> None:
        """Run one statement, discarding any result."""
        self.conn.execute(sql)

    def many(self, sql: str, data: list[tuple]) -> None:
        """Run one statement for many parameter rows."""
        with self.conn.cursor() as cur:
            cur.executemany(sql, data)

    def rows(self, sql: str) -> list[tuple]:
        """Run a query and return every row as a tuple."""
        return self.conn.execute(sql).fetchall()

    def adapt(self, v: Any, c: Col) -> Any:
        """Turn a Python value into what this driver binds losslessly."""
        if isinstance(v, float) and not math.isfinite(v):
            return v  # psycopg sends 'Infinity'/'NaN' for float8
        return v

    def fetch_expr(self, name: str, c: Col) -> str:
        """JSON as its text: psycopg would parse it, and a JSON string `"a"`
        would then be indistinguishable from the text it holds."""
        if c.kind == "json":
            return f"{self.ident(name)}::text"
        return self.ident(name)

    def close(self) -> None:
        """Close the writer's connection."""
        self.conn.close()


class Duck(Engine):
    name = "duckdb"
    ph = "?"

    def __init__(self, url: str = "") -> None:
        """A private temp directory; the file is created per case."""
        super().__init__(url)
        self.dir = tempfile.mkdtemp(prefix="rt_fz_")
        self.path = ""
        self.conn: Any = None

    def _open(self, table: str) -> None:
        """Create this case's DuckDB file on first use."""
        import duckdb

        if self.conn is None:
            self.path = os.path.join(self.dir, f"{table}.duckdb")
            self.conn = duckdb.connect(self.path)
            self.conn.execute("set TimeZone='UTC'")

    def exec(self, sql: str) -> None:
        """Run one statement, discarding any result."""
        self.conn.execute(sql)

    def create(
        self,
        table: str,
        cols: list[Col],
        nkey: int,
        upper: bool,
        names: list[str] | None = None,
    ) -> list[str]:
        """Drop and create the table; returns the stored column names."""
        self._open(table)
        return super().create(table, cols, nkey, upper, names)

    def many(self, sql: str, data: list[tuple]) -> None:
        """Run one statement for many parameter rows."""
        self.conn.executemany(sql, data)

    def rows(self, sql: str) -> list[tuple]:
        """Run a query and return every row as a tuple."""
        return self.conn.execute(sql).fetchall()

    def adapt(self, v: Any, c: Col) -> Any:
        """Turn a Python value into what this driver binds losslessly."""
        if c.kind == "tstz" and v is not None:
            return v.isoformat(sep=" ")
        if c.kind in ("dec", "money") and v is not None:
            # The driver binds a Python Decimal through DOUBLE, which loses
            # digits past 2^53 silently; text casts to DECIMAL exactly.
            return format(v, "f")
        return v

    def fetch_expr(self, name: str, c: Col) -> str:
        """How the oracle selects one column so its value survives the driver."""
        if c.kind == "tstz":
            # Microseconds since the epoch: no dependence on pytz or the
            # session zone in the oracle's own read.
            return f"epoch_us({self.ident(name)})"
        return self.ident(name)

    def fetch(
        self, table: str, names: list[str], cols: list[Col], where: str | None = None
    ) -> list[tuple]:
        """Every row back through the raw driver, for the oracle."""
        out = super().fetch(table, names, cols, where)
        tz = [i for i, c in enumerate(cols) if c.kind == "tstz"]
        if not tz:
            return out
        epoch = dt.datetime(1970, 1, 1, tzinfo=UTC)
        fixed = []
        for r in out:
            r = list(r)
            for i in tz:
                if r[i] is not None:
                    r[i] = epoch + dt.timedelta(microseconds=r[i])
            fixed.append(tuple(r))
        return fixed

    def seal(self) -> None:
        """Close the writer so the read-only dialect can open the file."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def dialect_url(self, table: str) -> str:
        """The connection string the tool's dialect opens."""
        return "duckdb:///" + self.path.replace("\\", "/")

    def drop(self, table: str) -> None:
        """Drop the table, ignoring that it may not exist."""
        self.seal()
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.path = ""


class MySQL(Engine):
    name = "mysql"
    ph = "%s"

    def __init__(self, url: str) -> None:
        """Connect the writer for this engine."""
        from urllib.parse import unquote, urlparse

        import mysql.connector

        super().__init__(url)
        u = urlparse(url)
        self.conn = mysql.connector.connect(
            host=u.hostname,
            port=u.port or 3306,
            user=unquote(u.username or ""),
            password=unquote(u.password or ""),
            database=u.path.lstrip("/"),
            autocommit=True,
        )
        self.exec("set session time_zone = '+00:00'")

    def ident(self, s: str) -> str:
        """Quote an identifier the way this engine's DDL needs."""
        return "`" + s + "`"

    def exec(self, sql: str) -> None:
        """Run one statement, discarding any result."""
        cur = self.conn.cursor()
        cur.execute(sql)
        if cur.with_rows:
            cur.fetchall()
        cur.close()

    def many(self, sql: str, data: list[tuple]) -> None:
        """Run one statement for many parameter rows."""
        cur = self.conn.cursor()
        cur.executemany(sql, data)
        cur.close()

    def rows(self, sql: str) -> list[tuple]:
        """Run a query and return every row as a tuple."""
        cur = self.conn.cursor()
        cur.execute(sql)
        out = cur.fetchall()
        cur.close()
        return out

    def adapt(self, v: Any, c: Col) -> Any:
        """Turn a Python value into what this driver binds losslessly."""
        if c.kind == "tstz" and v is not None:
            return v.astimezone(UTC).replace(tzinfo=None)
        if c.kind == "uuid" and v is not None:
            return str(v)
        if c.kind in ("real", "double") and v is not None:
            # As shortest-repr text, which MySQL parses correctly rounded and
            # FLOAT (single precision) rounds back to the float32 exactly.
            return repr(v)
        return v

    def fetch_expr(self, name: str, c: Col) -> str:
        """How the oracle selects one column so its value survives the driver."""
        if c.kind == "real":
            # The driver reads FLOAT through a 6-digit text form; the double
            # cast is the stored float32 exactly.
            return f"cast({self.ident(name)} as double)"
        return self.ident(name)

    def close(self) -> None:
        """Close the writer's connection."""
        self.conn.close()


class MSSQL(Engine):
    name = "mssql"
    ph = "?"

    def __init__(self, url: str) -> None:
        """Connect the writer for this engine."""
        import mssql_python

        from parity.dialects.mssql_dialect import parse_mssql_url

        super().__init__(url)
        odbc, _ = parse_mssql_url(url)
        self.conn = mssql_python.connect(odbc, autocommit=True)

    def ident(self, s: str) -> str:
        """Quote an identifier the way this engine's DDL needs."""
        return "[" + s + "]"

    def ident_table(self, table: str) -> str:
        """The table name, quoted for this engine's DDL."""
        return "[dbo].[" + table + "]"

    def table_ref(self, table: str) -> str:
        """The table name as passed to `diff` (schema-qualified where needed)."""
        return "dbo." + table

    def exec(self, sql: str) -> None:
        """Run one statement, discarding any result."""
        cur = self.conn.cursor()
        cur.execute(sql)
        cur.close()

    def many(self, sql: str, data: list[tuple]) -> None:
        """Run one statement for many parameter rows."""
        cur = self.conn.cursor()
        # Row by row: executemany's parameter typing is decided from the first
        # row, which mis-sizes a later longer string or wider decimal.
        for r in data:
            cur.execute(sql, r)
        cur.close()

    def rows(self, sql: str) -> list[tuple]:
        """Run a query and return every row as a tuple."""
        cur = self.conn.cursor()
        cur.execute(sql)
        out = [tuple(r) for r in cur.fetchall()]
        cur.close()
        return out

    def adapt(self, v: Any, c: Col) -> Any:
        """Turn a Python value into what this driver binds losslessly."""
        if v is None:
            return None
        if c.kind == "tstz":
            return v.isoformat(sep=" ")
        if c.kind in ("ts", "ts3"):
            return v.isoformat(sep=" ")
        if c.kind == "date":
            # The driver binds a date as datetime, whose range starts in 1753.
            return v.isoformat()
        if c.kind in ("dec", "money"):
            return format(v, "f")
        if c.kind == "real":
            return float(v)
        if c.kind in ("time", "uuid", "json"):
            return str(v)
        return v

    def load(
        self, table: str, names: list[str], cols: list[Col], rows: list[tuple]
    ) -> None:
        """Insert every row through parameter binding."""
        if not rows:
            return
        # Typed parameters through explicit casts, so a text-bound decimal or
        # timestamp lands in the column type exactly.
        ph = ", ".join(
            f"cast(? as {ddl_type('mssql', c, False)})"
            if c.kind in ("dec", "ts", "tstz", "date", "time", "uuid", "money", "ts3")
            else "?"
            for c in cols
        )
        head = (
            f"insert into {self.ident_table(table)} "
            f"({', '.join(self.ident(n) for n in names)}) values "
        )
        data = [
            tuple(self.adapt(v, c) for v, c in zip(r, cols, strict=True)) for r in rows
        ]
        # Multi-row VALUES, each parameter bound on its own (no executemany
        # typing from the first row), under the 2,100-parameter limit.
        per = max(1, min(1000, 2000 // len(cols)))
        cur = self.conn.cursor()
        for i in range(0, len(data), per):
            chunk = data[i : i + per]
            sql = head + ", ".join([f"({ph})"] * len(chunk))
            cur.execute(sql, [v for r in chunk for v in r])
        cur.close()

    def fetch_expr(self, name: str, c: Col) -> str:
        """How the oracle selects one column so its value survives the driver."""
        if c.kind == "tstz":
            return f"convert(varchar(40), {self.ident(name)}, 127)"
        if c.kind in ("ts", "ts3"):
            return f"convert(varchar(40), {self.ident(name)}, 126)"
        if c.kind == "money":  # money's default text has two decimals
            return f"convert(varchar(80), cast({self.ident(name)} as decimal(19,4)))"
        if c.kind == "dec":
            return f"convert(varchar(80), {self.ident(name)})"
        if c.kind == "real":
            return f"cast({self.ident(name)} as float)"
        if c.kind == "time":
            return f"cast({self.ident(name)} as varchar(20))"
        return self.ident(name)

    def fetch(
        self, table: str, names: list[str], cols: list[Col], where: str | None = None
    ) -> list[tuple]:
        """Every row back through the raw driver, for the oracle."""
        out = []
        for r in super().fetch(table, names, cols, where):
            r = list(r)
            for i, c in enumerate(cols):
                if r[i] is None:
                    continue
                if c.kind == "tstz":
                    r[i] = dt.datetime.fromisoformat(r[i].replace("Z", "+00:00"))
                elif c.kind in ("ts", "ts3"):
                    r[i] = dt.datetime.fromisoformat(r[i])
                elif c.kind in ("dec", "money"):
                    r[i] = Decimal(r[i])
                elif c.kind == "time" and isinstance(r[i], str):
                    r[i] = dt.time.fromisoformat(r[i])
            out.append(tuple(r))
        return out

    def close(self) -> None:
        """Close the writer's connection."""
        self.conn.close()


class BigQuery(Engine):
    """Loads through a JSON load job (no DML, no streaming buffer). Billed:
    keep the case count small."""

    name = "bigquery"

    def __init__(self, url: str) -> None:
        """Connect the writer for this engine."""
        from google.cloud import bigquery

        from parity.dialects.bigquery_dialect import parse_bigquery_url

        super().__init__(url)
        self.project, self.dataset, location = parse_bigquery_url(url)
        self.client = bigquery.Client(project=self.project, location=location)

    def ident(self, s: str) -> str:
        """Quote an identifier the way this engine's DDL needs."""
        return "`" + s + "`"

    def ident_table(self, table: str) -> str:
        """The table name, quoted for this engine's DDL."""
        return f"`{self.project}`.`{self.dataset}`.`{table}`"

    def exec(self, sql: str) -> None:
        """Run one statement, discarding any result."""
        self.client.query_and_wait(sql)

    def rows(self, sql: str) -> list[tuple]:
        """Run a query and return every row as a tuple."""
        return [tuple(r.values()) for r in self.client.query_and_wait(sql)]

    def load(
        self, table: str, names: list[str], cols: list[Col], rows: list[tuple]
    ) -> None:
        """Insert every row through parameter binding."""
        if not rows:
            return
        from google.cloud import bigquery

        def js(v: Any, c: Col) -> Any:
            """One value as the JSON a BigQuery load job accepts."""
            if v is None:
                return None
            if c.kind in ("dec", "money"):
                return format(v, "f")
            if c.kind in ("date", "ts", "ts3"):
                return v.isoformat()
            if c.kind == "tstz":
                return v.astimezone(UTC).isoformat()
            if c.kind == "time":
                return v.isoformat()
            if c.kind == "binary":
                import base64

                return base64.b64encode(v).decode()
            if c.kind == "uuid":
                return str(v)
            return v

        data = [
            {n: js(v, c) for n, v, c in zip(names, r, cols, strict=True)} for r in rows
        ]
        cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
        self.client.load_table_from_json(
            data, f"{self.project}.{self.dataset}.{table}", job_config=cfg
        ).result()

    def dialect_url(self, table: str) -> str:
        """The connection string the tool's dialect opens."""
        return self.url

    def drop(self, table: str) -> None:
        """Drop the table, ignoring that it may not exist."""
        self.client.delete_table(
            f"{self.project}.{self.dataset}.{table}", not_found_ok=True
        )

    def close(self) -> None:
        """Close the writer's connection."""
        self.client.close()


def open_engine(name: str, urls: dict[str, str]) -> Engine:
    """The writer for engine `name`, connected through `urls`."""
    if name == "postgres":
        return PG(urls["postgres"])
    if name == "duckdb":
        return Duck()
    if name == "mysql":
        return MySQL(urls["mysql"])
    if name == "mssql":
        return MSSQL(urls["mssql"])
    if name == "bigquery":
        return BigQuery(urls["bigquery"])
    raise AssertionError(name)


# ----------------------------------------------------------------- runner


@dataclass
class Outcome:
    case: Case
    expected: Expected
    got: Expected | None
    identical: bool | None
    truncated: bool | None
    error: str | None = None

    @property
    def missed(self) -> Expected:
        """Differences the oracle expected that `diff` did not report."""
        return self.expected - (self.got or set()) if self.got is not None else set()

    @property
    def extra(self) -> Expected:
        """Differences `diff` reported that the oracle did not expect."""
        return (self.got or set()) - self.expected if self.got is not None else set()

    @property
    def ok(self) -> bool:
        """Did `diff` agree with the oracle? With max_diffs hit, a truthful
        partial answer counts."""
        if self.error is not None or self.got is None:
            return False
        limit = self.case.max_diffs
        if limit is not None and len(self.expected) > limit:
            # A partial answer: what it lists must be real, and it must say so.
            return (
                bool(self.truncated) and not self.identical and self.got <= self.expected
            )
        # Stopping at exactly `limit` real differences may still flag the
        # answer partial; that is conservative, never a false verdict.
        complete = self.got == self.expected
        truthful = self.identical == (not self.expected)
        return complete and truthful

    def verdict(self) -> str:
        """OK, ERROR, FALSE MATCH, MISSED DIFF or FALSE DIFF - worst first."""
        if self.error is not None:
            return "ERROR"
        if self.ok:
            return "OK"
        if self.identical and self.expected:
            return "FALSE MATCH"
        if self.missed:
            return "MISSED DIFF"
        return "FALSE DIFF"


def _to_b(row: tuple, cols: list[Col]) -> tuple:
    """Convert a row to side B's storage kinds (DECIMAL -> DOUBLE: nearest)."""
    return tuple(
        float(v) if v is not None and c.kind == "dec" and c.b_kind == "double" else v
        for v, c in zip(row, cols, strict=True)
    )


class LoadError(RuntimeError):
    """The harness could not build a case's tables: a harness bug, not a finding."""


def run_case(case: Case, engines: dict[str, Engine]) -> Outcome:
    """Build both tables, ask the oracle and `diff`, and drop the tables -
    whatever happens. A load failure is a harness bug (`LoadError`)."""
    table_a = f"rt_fz_a_{case.seed}_{uuid.uuid4().hex[:6]}"
    table_b = f"rt_fz_b_{case.seed}_{uuid.uuid4().hex[:6]}"
    ea, eb = engines[case.a_engine], engines[case.b_engine]
    if ea is eb:
        raise AssertionError("pairs use two engine objects")
    cols = case.all_cols
    nkey = len(case.key)
    da = db = None
    try:
        names_a = ea.create(table_a, cols, nkey, upper=False)
        cols_b = [c.on_b() for c in cols]
        names_b = eb.create(table_b, cols_b, nkey, upper=case.b_upper)
        try:
            ea.load(table_a, names_a, cols, case.rows_a)
            eb.load(table_b, names_b, cols_b, [_to_b(r, cols) for r in case.rows_b])
        except Exception as exc:
            raise LoadError(f"{case.describe()}: {exc}") from exc
        fa = ea.fetch(table_a, names_a, cols)
        fb = eb.fetch(table_b, names_b, cols_b)
        for e in (ea, eb):
            if isinstance(e, Duck):
                e.seal()
        expected = oracle(case, fa, fb)
        da = get_dialect(ea.dialect_url(table_a), side="A", float_scale=case.float_scale)
        db = get_dialect(eb.dialect_url(table_b), side="B", float_scale=case.float_scale)
        try:
            res = diff(
                da,
                db,
                ea.table_ref(table_a),
                eb.table_ref(table_b),
                key=[c.name for c in case.key],
                columns=case.columns_arg,
                exclude=case.exclude_arg,
                bisection_factor=case.bisection_factor,
                threshold=case.threshold,
                max_diffs=case.max_diffs,
            )
        except Exception as exc:
            return Outcome(
                case, expected, None, None, None, f"{type(exc).__name__}: {exc}"[:600]
            )
        return Outcome(case, expected, observed(res), res.identical, res.truncated)
    finally:
        for d in (da, db):
            if d is not None:
                try:
                    d.close()
                except Exception:  # noqa: S110 - cleanup must not mask the verdict
                    pass
        for e, t in ((ea, table_a), (eb, table_b)):
            try:
                e.drop(t)
            except Exception:  # noqa: S110 - cleanup must not mask the verdict
                pass


def shrink(
    case: Case, engines: dict[str, Engine], still_bad: Callable[[Outcome], bool]
) -> Case:
    """Greedy: drop whole keys, then columns, while the disagreement persists."""
    nkey = len(case.key)

    def keys_of(c: Case) -> list[tuple]:
        """Every key present on either side, in first-seen order."""
        seen = dict.fromkeys(tuple(r[:nkey]) for r in [*c.rows_a, *c.rows_b])
        return list(seen)

    def without_key(c: Case, k: tuple) -> Case:
        """This case with key `k` removed from both sides."""
        return replace(
            c,
            rows_a=[r for r in c.rows_a if tuple(r[:nkey]) != k],
            rows_b=[r for r in c.rows_b if tuple(r[:nkey]) != k],
            mutations=list(c.mutations),
        )

    best = case
    changed = True
    while changed:
        changed = False
        ks = keys_of(best)
        # Halves first, then singles.
        chunk = max(1, len(ks) // 2)
        while chunk >= 1:
            i = 0
            while i < len(keys_of(best)):
                ks = keys_of(best)
                trial = best
                for k in ks[i : i + chunk]:
                    trial = without_key(trial, k)
                if trial is not best and still_bad(run_case(trial, engines)):
                    best, changed = trial, True
                else:
                    i += chunk
            chunk //= 2
        for ci in range(len(best.cols) - 1, -1, -1):
            if len(best.cols) <= 1:
                break
            name = best.cols[ci].name
            if (
                best.columns_arg and name in best.columns_arg
            ) or name in best.exclude_arg:
                continue
            pos = nkey + ci
            trial = replace(
                best,
                cols=[c for j, c in enumerate(best.cols) if j != ci],
                rows_a=[r[:pos] + r[pos + 1 :] for r in best.rows_a],
                rows_b=[r[:pos] + r[pos + 1 :] for r in best.rows_b],
                mutations=list(best.mutations),
            )
            if still_bad(run_case(trial, engines)):
                best, changed = trial, True
    return best


def pairs(names: Sequence[str]) -> list[tuple[str, str]]:
    """Every unordered pair of engine names, in a stable order."""
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
