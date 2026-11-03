"""The hard values of the conformance suite, by logical value group.

Each value carries a `key` that names the *logical* value, the same on every
engine: two engines that both store `dec:-0.0000004` must render it to the
same text (the cross-engine check), and each must render it to exactly what
`parity.reference` says the rules give it (the per-engine check).

`value` is in the form `parity.reference.reference_token` reads - a Python
object, or for what Python cannot hold (PostgreSQL infinities, BC dates,
sub-microsecond fractions, offsets) the engines' own text form. The per-engine
binders in `cov_engines.py` turn it into what each driver binds; nothing here
is ever spliced into SQL as a literal.

Groups, and why each value is there:

- `int`: every integer width's boundaries, 2^53 +- 1 (where a double would
  round), and the unsigned / 128-bit / NUMBER(38) maxima.
- `dec`: DECIMAL(38,9) - one type every engine has. Ties at scales 6 and 2,
  negatives that round to zero (which must render unsigned), the 29-digit
  integer maximum, one-ulp-of-scale values either side of a tie.
- `dec_wide`: past DECIMAL(38,9), on the engines that can hold it.
- `float`: a deterministic 220-value slice of the adversarial doubles of
  `tests/float_sample.py` plus the documented hard cases, non-finite values
  and signed zero. `float4` holds float32-representable doubles under the
  *same* keys, so a REAL and a DOUBLE holding one value must render alike.
- `float_refuse`: magnitudes at and past 2^116, which every engine must refuse.
- `bool`; `str` (escaping, Unicode, whitespace); `char` (CHAR(20) padding);
  `uuid`; `date` / `date_ext` (PostgreSQL / DuckDB infinities and BC);
  `ts` / `ts_ext` / `ts_sub` (naive, past 0001..9999, sub-microsecond);
  `tstz` (instants at odd offsets and DST edges); `time` / `time_ext` /
  `time_sub` / `time_dur` (24:00:00, past the microsecond, MySQL durations);
  `bytes` (zero bytes, padding, invalid UTF-8, all 256 values); `json`
  (documents every engine normalises alike) and `json_forms` (documents whose
  engines' normal forms differ - `FORM_DEPENDENT`).
"""

from __future__ import annotations

import datetime as dt
import math
import struct
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from float_sample import adversarial_doubles


@dataclass(frozen=True)
class Val:
    """One logical value: its stable key and its reference-readable form."""

    key: str
    value: Any
    note: str = ""


def _ints() -> list[Val]:
    """Integer boundaries of every width the engines offer."""
    xs = [0, 1, -1, 127, -128, 255, 32767, -32768, 65535, 8388607, -8388608]
    xs += [2**31 - 1, -(2**31), 2**32 - 1, 2**53 - 1, 2**53, 2**53 + 1]
    xs += [-(2**53) - 1, 10**18, 2**63 - 1, -(2**63), 2**64 - 1]
    xs += [10**37, 10**38 - 1, -(10**38) + 1, 2**127 - 1, -(2**127)]
    return [Val("i:<NULL>", None)] + [Val(f"i:{x}", x) for x in xs]


def _decs() -> list[Val]:
    """DECIMAL(38,9): ties, negative zeros, and the extremes of the type."""
    xs = [
        "0",
        "1",
        "-1",
        "1.5",
        "-1.5",
        "0.1",
        "0.333333333",
        "0.0000005",  # tie at scale 6: half away from zero -> 0.000001
        "-0.0000005",  # -> -0.000001
        "0.000000499",  # just under the tie -> 0.000000
        "0.000000500",  # the tie with trailing zeros -> 0.000001
        "-0.0000004",  # rounds to zero: must be unsigned 0.000000
        "-0.000000001",  # the smallest negative: unsigned zero at every scale
        "0.000000001",
        "1.2345675",  # tie at 6 -> 1.234568
        "-1.2345665",  # tie at 6 -> -1.234567 (half away, not to even)
        "2.0000005",
        "123456789.1234565",
        "0.005",  # tie at scale 2 -> 0.01
        "-0.005",  # -> -0.01
        "0.0049999",  # -> 0.00 at scale 2
        "-0.0049999",  # -> 0.00, unsigned
        "0.015",
        "0.025",  # half-even would give 0.02; half away 0.03
        "999999.9999995",  # carries into a new integer digit at scale 6
        "-999999.9999995",
        "12345678901234567890.123456789",
        "99999999999999999999999999999.999999999",  # DECIMAL(38,9) maximum
        "-99999999999999999999999999999.999999999",
        "10000000000000000000000000000",
        "0.999999999",
        "-0.999999999",
    ]
    return [Val("d:<NULL>", None)] + [Val(f"d:{x}", Decimal(x)) for x in xs]


def _decs_wide() -> list[Val]:
    """Past DECIMAL(38,9): more integer digits, or more fractional ones."""
    xs = [
        "1E+40",
        "-1E+40",
        "123456789012345678901234567890123456.5",
        "0.00000049999999999999999999",
        "0.00000050000000000000000001",
        "-0.00000050000000000000000000",
        "1.00000000000000000000000000000000000001",
    ]
    return [Val(f"dw:{x}", Decimal(x)) for x in xs]


def _floats() -> list[Val]:
    """A deterministic slice of the adversarial doubles, plus the named cases."""
    sample = adversarial_doubles()
    picked = sample[::8]  # 196 values spread over every family of the sample
    picked += [1.5, -0.125, 1 / 3, 0.0078125, -0.0078125, 2.0000005, 1e15 + 0.5]
    picked += [123456789.1234565, -262.4724295, 2**40 + 1 / 128, 38809635010537424.0]
    picked += [1e20 / 3, float(2**53 + 2), 5e-324, 4.9999999999e-7, 5e-7, -5e-7]
    picked += [2**50 + 0.25, 2.0**116 - 2.0**63, -(2.0**116 - 2.0**63)]
    picked += [0.5, -2.5, 0.015, 1e-7, -1e-7, 16777217.0, 2.0**63, -(2.0**63)]
    out = [Val("f:<NULL>", None), Val("f:-0.0", -0.0, "signed zero")]
    seen: set[str] = {"-0.0"}
    for x in picked:
        if math.isfinite(x) and abs(x) < 2.0**116 and repr(x) not in seen:
            seen.add(repr(x))
            out.append(Val(f"f:{x!r}", x))
    out += [
        Val("f:inf", math.inf, "non-finite"),
        Val("f:-inf", -math.inf, "non-finite"),
        Val("f:nan", math.nan, "non-finite"),
    ]
    return out


def as_float4(x: float) -> float:
    """The nearest float32, as the double the engines widen it to."""
    return float(struct.unpack("<f", struct.pack("<f", x))[0])


def _float4s() -> list[Val]:
    """float32-representable doubles, keyed exactly as the same double in `float`."""
    base = [1.5, -0.125, 0.1, 1 / 3, -4088288.25, 16777216.0, 16777218.0, 2.0e-7]
    base += [0.0078125, 1e-45, 1.17549435e-38, 3.0e-39, 123456.789, -98765.4321]
    base += [7.0e-7, 5.0e-7, 4.0e-7, 0.015, 262.4724295, 1e10, 3.3e33]
    base += [x for x in adversarial_doubles()[::40] if abs(x) < 3.0e34]
    out = [Val("f:<NULL>", None), Val("f:-0.0", -0.0)]
    seen: set[str] = {"-0.0"}
    for x in base:
        y = as_float4(x)
        if math.isfinite(y) and abs(y) < 2.0**116 and repr(y) not in seen:
            seen.add(repr(y))
            out.append(Val(f"f:{y!r}", y))
    out += [
        Val("f:inf", math.inf),
        Val("f:-inf", -math.inf),
        Val("f:nan", math.nan),
    ]
    return out


def _float_refusals() -> list[Val]:
    """Doubles no engine can render canonically: they must be refused."""
    xs = [2.0**116, -(2.0**116), 1e300, -1.7976931348623157e308]
    return [Val(f"fr:{x!r}", x) for x in xs]


BOOLS = [Val("b:<NULL>", None), Val("b:true", True), Val("b:false", False)]

#: Text designed to break escaping, padding, encoding and collation.
_STRINGS = [
    "",
    " ",
    "  ",
    "a",
    "A",
    " a",
    "a ",
    "a  ",
    "\\N",  # the NULL sentinel, as text
    "\\",
    "\\\\",
    "\\s",  # the separator's escape, as text
    "a\\sb",
    "\x1f",  # the separator itself
    "a\x1fb",
    "\\\x1f",
    "NULL",
    "null",
    "\t",
    "\n",
    "\r\n",
    "a\nb",
    "'",
    '"',
    "''",
    "%_",
    "\u00e9",  # e-acute, precomposed
    "e\u0301",  # e + combining acute: a different string, must stay different
    "\u00fc\u2713\U0001f600\u65e5\u672c",
    "\U0001f600",  # astral
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # ZWJ sequence
    "\u200b",  # zero width space
    "\u00a0",  # no-break space
    "\ufeff",  # BOM
    "\x7f",
    "\x01",
    "\u00df",  # sharp s
    "SS",
    "ss",
    "\u0130",  # dotted capital I
    "\u2126",  # Ohm sign
    "\u03a9",  # Omega: canonically equivalent, different code point
    "x" * 5000,
    "y" * 4000 + "\u00e9" * 10,
]


def _strings() -> list[Val]:
    """Every hard string, keyed by its code points so keys stay printable."""
    out = [Val("s:<NULL>", None)]
    for s in _STRINGS:
        out.append(Val("s:" + s.encode("unicode_escape").decode("ascii")[:60], s))
    out.append(Val("s:nul-inside", "a\x00b", "NUL inside"))
    out.append(Val("s:nul-trailing", "a\x00", "trailing NUL"))
    return out


def _chars() -> list[Val]:
    """CHAR(20): padding is not data, so 'a', 'a ' and 'a  ' are one value."""
    xs = ["", " ", "a", " a", "a ", "a  ", "a b", "\\N", "\\", "a\x1fb", "\u00e9"]
    xs += ["\U0001f600", "abcdefghijklmnopqrst", "NULL", "\u00a0"]
    out = [Val("c:<NULL>", None)]
    for s in xs:
        out.append(Val("c:" + s.encode("unicode_escape").decode("ascii"), s))
    return out


UUIDS = [Val("u:<NULL>", None)] + [
    Val(f"u:{u}", u)
    for u in (
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "abcdef00-0000-4000-8000-000000000001",
        "0123abcd-4567-89ef-0123-456789abcdef",
    )
]


def _dates() -> list[Val]:
    """Calendar edges inside 0001..9999, Julian-gap and leap days included."""
    ds = [
        (1, 1, 1),
        (999, 12, 31),
        (1000, 1, 1),
        (1582, 10, 10),  # inside the Julian-to-Gregorian gap: proleptic everywhere?
        (1752, 9, 5),
        (1899, 12, 30),
        (1900, 2, 28),
        (1900, 3, 1),
        (1969, 12, 31),
        (1970, 1, 1),
        (2000, 2, 29),
        (2024, 2, 29),
        (2038, 1, 19),
        (2100, 2, 28),
        (9999, 12, 31),
    ]
    out = [Val("dt:<NULL>", None)]
    out += [Val(f"dt:{y:04d}-{m:02d}-{d:02d}", dt.date(y, m, d)) for y, m, d in ds]
    return out


#: PostgreSQL / DuckDB only: the infinities and BC (astronomical year) dates,
#: as PostgreSQL prints them - the form the reference reads.
DATES_EXT = [
    Val("dt:infinity", "infinity"),
    Val("dt:-infinity", "-infinity"),
    Val("dt:0001-01-01 BC", "0001-01-01 BC"),
    Val("dt:0044-03-15 BC", "0044-03-15 BC"),
    Val("dt:4713-01-01 BC", "4713-01-01 BC"),
]


def _timestamps() -> list[Val]:
    """Naive timestamps: 0/6 fractional digits, pre-epoch fractions, extremes."""
    ts = [
        dt.datetime(1, 1, 1),
        dt.datetime(999, 12, 31, 23, 59, 59, 999999),
        dt.datetime(1000, 1, 1),
        dt.datetime(1753, 1, 1),
        dt.datetime(1900, 1, 1),
        dt.datetime(1969, 12, 31, 23, 59, 59, 999999),
        dt.datetime(1969, 12, 31, 23, 59, 59, 1),
        dt.datetime(1970, 1, 1),
        dt.datetime(1970, 1, 1, 0, 0, 0, 1),
        dt.datetime(2000, 1, 1, 12, 0, 0, 500000),
        dt.datetime(2024, 1, 1),
        dt.datetime(2024, 2, 29, 13, 4, 5, 123456),
        dt.datetime(2024, 3, 10, 2, 30),  # does not exist in New York
        dt.datetime(2024, 11, 3, 1, 30),  # exists twice in New York
        dt.datetime(2024, 1, 1, 0, 0, 0, 3000),  # SQL Server datetime ticks
        dt.datetime(2024, 1, 1, 0, 0, 0, 7000),
        dt.datetime(2079, 6, 6, 23, 59),
        dt.datetime(9999, 12, 31, 23, 59, 59, 999999),
        dt.datetime(9999, 12, 31, 23, 59),
    ]
    return [Val("ts:<NULL>", None)] + [Val(f"ts:{t.isoformat(' ')}", t) for t in ts]


#: Infinity and BC timestamps (PostgreSQL, DuckDB).
TIMESTAMPS_EXT = [
    Val("ts:infinity", "infinity"),
    Val("ts:-infinity", "-infinity"),
    Val("ts:0044-03-15 12:30:00 BC", "0044-03-15 12:30:00 BC"),
]

#: Sub-microsecond fractions. The rules compare to the microsecond, digits
#: past the sixth *truncated* (CLAUDE.md 4.2, SQL Server datetime2(7)).
TIMESTAMPS_SUB = [
    Val("tsn:2024-01-01 12:00:00.1234567", "2024-01-01 12:00:00.1234567"),
    Val("tsn:2024-01-01 12:00:00.0000001", "2024-01-01 12:00:00.0000001"),
    Val("tsn:2024-01-01 23:59:59.9999995", "2024-01-01 23:59:59.9999995"),
    Val("tsn:1969-12-31 23:59:59.9999999", "1969-12-31 23:59:59.9999999"),
    Val("tsn:2024-01-01 12:00:00.123456789", "2024-01-01 12:00:00.123456789", "9 digits"),
    Val("tsn:1969-12-31 23:59:59.999999999", "1969-12-31 23:59:59.999999999", "9 digits"),
    Val("tsn:2024-01-01 12:00:00.000000001", "2024-01-01 12:00:00.000000001", "9 digits"),
]


def _instants() -> list[Val]:
    """Instants written with odd offsets, across DST edges and the epoch."""
    texts = [
        "2024-06-15 12:00:00+00:00",
        "2024-06-15 12:00:00+05:45",  # Nepal
        "2024-06-15 12:00:00-00:30",  # no zone uses it; the arithmetic must
        "2024-06-15 12:00:00+14:00",
        "2024-06-15 12:00:00-12:00",
        "2024-01-01 00:15:00+05:30",  # moves the UTC date back
        "2023-12-31 23:45:00-09:30",  # moves the UTC year forward
        "2024-03-10 06:59:59.999999+00:00",  # one microsecond before US DST
        "2024-03-10 03:00:00-04:00",  # the first instant of US DST
        "2024-11-03 01:30:00-04:00",  # New York's first 01:30
        "2024-11-03 01:30:00-05:00",  # and its second, one hour later
        "2024-03-31 02:30:00+02:00",  # inside the EU spring-forward gap
        "1969-12-31 23:59:59.999999+00:00",
        "1970-01-01 00:00:01+00:00",
        "2038-01-19 03:14:07.999999+00:00",  # MySQL TIMESTAMP's last instant
        "2000-02-29 23:59:59.5-01:00",  # crosses into March 1 in UTC
        "0001-01-01 00:00:00+00:00",
        "9999-12-31 23:59:59.999999+00:00",
    ]
    out = [Val("tz:<NULL>", None)]
    for t in texts:
        out.append(Val(f"tz:{t}", dt.datetime.fromisoformat(t)))
    return out


TIMES = [Val("tm:<NULL>", None)] + [
    Val(f"tm:{t}", dt.time.fromisoformat(t))
    for t in (
        "00:00:00",
        "00:00:01",
        "00:01:00",
        "01:00:00",
        "12:34:56",
        "12:34:56.5",
        "12:34:56.05",  # must not equal .5: the fraction is not trimmed
        "12:34:56.123456",
        "23:59:59",
        "23:59:59.999999",
        "00:00:00.000001",
        "10:00:00.100000",
    )
]

#: `24:00:00`, which PostgreSQL, DuckDB and MySQL accept: it must render as
#: itself, never as the next midnight (`00:00:00`, a different value).
TIMES_EXT = [Val("tm:24:00:00", "24:00:00")]

#: Digits past the microsecond (SQL Server time(7), Snowflake TIME(9)): the
#: rule truncates them, as for timestamps.
TIMES_SUB = [
    Val("tmn:12:34:56.1234567", "12:34:56.1234567"),
    Val("tmn:23:59:59.9999999", "23:59:59.9999999"),
    Val("tmn:00:00:00.0000001", "00:00:00.0000001"),
    Val("tmn:12:00:00.123456789", "12:00:00.123456789", "9 digits"),
]

#: MySQL's TIME is a duration: negative, and past 24 hours.
TIMES_DUR = [
    Val(f"tmd:{t}", t)
    for t in (
        "-00:00:00.500000",
        "-01:00:00",
        "-838:59:59",
        "100:00:00.5",
        "838:59:59",
        "24:00:00.000001",
    )
]

BYTES = [Val("by:<NULL>", None)] + [
    Val(f"by:{b.hex()}", b)
    for b in (
        b"",
        b"\x00",
        b"\x00\x00",  # a different value from one zero byte
        b"\xff\x0a",
        b"abc",
        b"ABC",
        b"abc ",  # trailing space is data in bytes
        b"\\N",
        b"\\x00",  # the characters a hex rendering of b"\x00" might print
        b"\x1f",  # the separator as a byte
        b"\x5c\x5c",
        b"\xc3\xa9",  # valid UTF-8 (e-acute) - still bytes, not text
        b"\xff\xfe\x00\xd8",  # invalid UTF-8 and UTF-16
        bytes(range(16)),
        bytes(range(256)),
    )
]

#: JSON every engine with a JSON type renders alike: no key whose order
#: differs between the engines' normal forms, numbers every engine spells
#: alike, string escapes every engine decodes to the same character, and any
#: layout - the canonical one is applied everywhere.
JSONS = [Val("js:<NULL>", None)] + [
    Val(f"js:{j}", j)
    for j in (
        '{"a": 1}',
        '{"b": [1, 2]}',
        '"1"',
        "1",
        "null",
        "true",
        "false",
        "[]",
        "{}",
        "[[], {}]",
        '{"s": "\\\\N"}',  # a backslash then N: the sentinel's spelling
        '{"a":1}',  # compact: the same document as '{"a": 1}'
        '{ "a" :\n 1 }',  # and so is this
        '[1, "1", true, null]',
        '{"ab": 1, "cd": 2}',  # equal lengths: both key orders agree
        '{"a": 1, "bb": 2}',  # shorter first and lower first: both agree
        '{"a": {"b": {"c": [[], {}]}}}',
        '"x, y: z"',  # separators inside a string are not separators
        '{"a, b": "c: d"}',
        '{"k": "a\\"b"}',
        '"\\t\\n\\r\\b\\f\\u0001\\u001f"',  # control characters, canonically escaped
        '"\\\\u00e9"',  # a literal backslash-u, not an escape
        '"é\U0001f600"',
        "[-1, 0, 123456789012]",
        '"\\\\"',
        '{"": ""}',
    )
]

#: JSON whose canonical text depends on the engine's normal form: key order
#: (length-first on PostgreSQL and MySQL, code point on BigQuery and
#: Snowflake, as written on DuckDB), duplicate keys, number spelling, and
#: escapes DuckDB keeps as written. Each engine is held to the rule for its
#: own form; across engines they are expected to differ (`FORM_DEPENDENT`).
JSONS_FORMS = [
    Val(f"jf:{j}", j)
    for j in (
        '{"b": 1, "a": 2}',
        '{"aa": 1, "b": 2}',
        '{"é": 1, "z": 2, "Z": 3, "ab": 4}',
        '{"a": 1, "a": 2}',
        "1.50",
        "1.0",
        "1e2",
        "-0",
        "0.1",
        "12345678901234567890123",
        "1.7976931348623157e308",
        '"\\u00e9"',
        '"\\u007f"',
        '"\\/"',
        '"\\ud83d\\ude00"',
        '[1.0, 2.50, {"y": 1e-7, "x": -0.0}]',
    )
]

GROUPS: dict[str, list[Val]] = {
    "int": _ints(),
    "dec": _decs(),
    "dec_wide": _decs_wide(),
    "float": _floats(),
    "float4": _float4s(),
    "float_refuse": _float_refusals(),
    "bool": BOOLS,
    "str": _strings(),
    "char": _chars(),
    "uuid": UUIDS,
    "date": _dates(),
    "date_ext": DATES_EXT,
    "ts": _timestamps(),
    "ts_ext": TIMESTAMPS_EXT,
    "ts_sub": TIMESTAMPS_SUB,
    "tstz": _instants(),
    "time": TIMES,
    "time_ext": TIMES_EXT,
    "time_sub": TIMES_SUB,
    "time_dur": TIMES_DUR,
    "bytes": BYTES,
    "json": JSONS,
    "json_forms": JSONS_FORMS,
}

#: Groups whose values the rules (`parity.reference`) cover.
RULED = {
    "int",
    "dec",
    "dec_wide",
    "float",
    "float4",
    "float_refuse",
    "bool",
    "str",
    "char",
    "uuid",
    "date",
    "date_ext",
    "ts",
    "ts_ext",
    "ts_sub",
    "tstz",
    "time",
    "time_ext",
    "time_sub",
    "time_dur",
    "bytes",
    "json",
    "json_forms",
}

#: Ruled groups whose canonical text depends on the engine's own normal form
#: (JSON key order, duplicates, number spelling): each engine is held to the
#: rules, but two engines may legitimately differ, and the run warns.
FORM_DEPENDENT = {"json_forms"}

#: Values the canonical text must refuse, never render.
REFUSE = {"float_refuse"}


def cross_group(group: str) -> str:
    """The family two engines' values are compared within.

    REAL and DOUBLE share keys, so a float32 value must render as the same
    double does; naive and sub-microsecond timestamps are one family too.
    """
    return {
        "float4": "float",
        "ts_ext": "ts",
        "date_ext": "date",
        "dec_wide": "dec",
        "time_ext": "time",
        "time_sub": "time",
        "time_dur": "time",
    }.get(group, group)
