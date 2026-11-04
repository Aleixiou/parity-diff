"""The comparison rules in plain Python, for the independent sample check.

The checksum walk decides through SQL that every dialect writes for its own
engine: each value rendered to canonical text inside the database. A mistake
in one of those expressions - a rounding mode, a timezone, an escape - can
make two different values render alike, and then the walk reports a false
match that no checksum can see, because both sides agreed on the wrong text.
Every false match the red-team campaign found was of that kind.

This module is the second opinion. It takes the *raw values* the database
drivers return and reduces each to what the documented rules say the
comparison sees (CLAUDE.md sections 4.2 and 4.3), without calling a line of
dialect SQL:

- NULL is equal only to NULL;
- integers compare as integers;
- DECIMAL and FLOAT compare as decimal text rounded half away from zero to
  the float scale - a FLOAT through `canonical_float_text`, the executable
  spec every engine is tested against;
- booleans as booleans;
- text exactly, except that fixed-width CHAR loses its trailing spaces;
- dates as calendar dates, with PostgreSQL's infinities and BC years;
- timestamps to the microsecond, instants converted to UTC;
- times of day to the microsecond (`canonical_time_text`), MySQL durations
  included;
- binary as its bytes, exactly;
- JSON as its engine's normalised document, laid out and escaped one way
  (`canonical_json_text`);
- bit strings as their digits (`canonical_bits_text`), intervals as their
  three stored components (`canonical_interval_text`), times with a time
  zone as local time and offset (`canonical_timetz_text`), network addresses
  as address and prefix (`canonical_inet_text`), SQL Server variants as base
  type and exact value (`canonical_variant_text`);
- lists element by element, each by its own type's rule
  (`canonical_list_text`).

Two values the rules call equal reduce to equal tokens, whichever engine and
driver they came from. A type the rules do not cover (structs, geometry,
...) is `UNCHECKED`, and the engine reports that the column was not
independently verified rather than pretend it was.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import math
import re
import struct
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import TYPE_CHECKING, Any

from parity.dialects.base import (
    INTERVAL_INFINITY,
    INTERVAL_NEG_INFINITY,
    NULL_SENTINEL,
    ShapedList,
    canonical_binary_text,
    canonical_bits_text,
    canonical_float_text,
    canonical_inet_text,
    canonical_interval_text,
    canonical_json_text,
    canonical_list_text,
    canonical_time_text,
    canonical_timetz_text,
    canonical_variant_text,
    escape_canonical_text,
)
from parity.types import Column, LogicalType

if TYPE_CHECKING:
    from parity.dialects.base import Dialect

#: Returned for a value the rules do not cover; never equal to a real token.
UNCHECKED = object()

#: Wide enough for any DECIMAL(38) or BIGNUMERIC value at any float scale.
_WIDE = Context(prec=200)

_UTC = dt.timezone.utc

#: `[-]YYYY-MM-DD[ (BC)][( |T)HH:MM:SS[.f...]][offset][ BC]`, as engines print
#: temporal values as text: PostgreSQL's JSON output (which ignores DateStyle)
#: puts the era last, DuckDB's varchar cast puts it after the date. Years run
#: to seven digits: PostgreSQL and DuckDB dates reach 5874897 AD (RT-HP3).
_TEMPORAL = re.compile(
    r"^(?P<neg>-)?(?P<y>\d{1,7})-(?P<mo>\d\d)-(?P<d>\d\d)(?P<bc1> \(BC\))?"
    r"(?:[ T](?P<h>\d\d):(?P<mi>\d\d):(?P<s>\d\d)(?:\.(?P<f>\d{1,9}))?)?"
    r"(?P<tz>Z|[+-]\d\d(?::?\d\d)?(?::?\d\d)?)?"
    r"(?P<bc> BC| \(BC\))?$"
)

#: `[-]H...:MM:SS[.f...]`: a time of day or a MySQL duration as text.
_CLOCK = re.compile(
    r"^(?P<neg>-)?(?P<h>\d{1,3}):(?P<mi>\d\d):(?P<s>\d\d)(?:\.(?P<f>\d{1,9}))?$"
)

#: `HH:MM:SS[.f]` then an offset `+HH[:MM[:SS]]`: a timetz as PostgreSQL and
#: DuckDB print it (`12:00:00.5+05:45`, `24:00:00+00`, `00:00:00-15:59:59`).
_TIMETZ = re.compile(
    r"^(?P<clock>\d\d:\d\d:\d\d(?:\.\d{1,6})?)(?P<sign>[+-])(?P<h>\d\d)"
    r"(?::?(?P<m>\d\d))?(?::?(?P<s>\d\d))?$"
)

#: The pieces of an interval as PostgreSQL (IntervalStyle postgres: `-1 years
#: -2 mons +3 days -04:05:06`) or DuckDB (`1 year 2 months 3 days 04:05:06`)
#: prints it: signed counts with a unit, and at most one signed clock.
_IV_UNIT = re.compile(r"^(?P<n>[+-]?\d+)$")
_IV_CLOCK = re.compile(
    r"^(?P<sign>[+-])?(?P<h>\d+):(?P<mi>\d\d):(?P<s>\d\d)(?:\.(?P<f>\d{1,6}))?$"
)
_IV_MONTHS = {"year": 12, "years": 12, "mon": 1, "mons": 1, "month": 1, "months": 1}


def reference_token(
    logical_type: LogicalType,
    value: Any,
    float_scale: int,
    *,
    padded: bool = False,
    single_precision: bool = False,
    sub_micro: bool = False,
    json_verbatim: bool = False,
    element: Callable[[Any], Any] | None = None,
) -> Any:
    """Reduce one raw driver value to what the comparison rules see.

    `padded` marks a fixed-width CHAR column, whose trailing spaces carry no
    meaning; `single_precision` a float4 column, whose value the drivers may
    hand back as the shortest repr of the float rather than the float itself.
    `sub_micro` keeps a timestamp's digits past microseconds (both sides of
    the comparison hold them), as nine digits when there are any.
    `json_verbatim` marks an engine that stores JSON as unnormalised text
    (`Dialect.json_verbatim`). `element` reduces one element of a LIST
    (`column_token` builds it from the element's own column).
    """
    if value is None:
        return None
    try:
        if logical_type is LogicalType.INTEGER:
            if isinstance(value, Decimal) and value != value.to_integral_value():
                return UNCHECKED  # not an integer after all; do not guess
            return ("int", int(value))
        if logical_type is LogicalType.DECIMAL:
            return ("num", _decimal_text(value, float_scale))
        if logical_type is LogicalType.FLOAT:
            return ("num", _float_text(value, float_scale, single_precision))
        if logical_type is LogicalType.BOOLEAN:
            if isinstance(value, (bool, int)):
                return ("bool", bool(value))
            return UNCHECKED
        if logical_type is LogicalType.STRING:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return UNCHECKED
            text = str(value)
            return ("str", text.rstrip(" ") if padded else text)
        if logical_type is LogicalType.DATE:
            return ("date", _date_text(value))
        if logical_type is LogicalType.TIMESTAMP:
            return ("ts", _timestamp_text(value, sub_micro))
        if logical_type is LogicalType.TIME:
            return ("time", _time_text(value, sub_micro))
        if logical_type is LogicalType.BINARY:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return ("bin", canonical_binary_text(bytes(value)))
            return UNCHECKED  # a driver handing back text: do not guess its encoding
        if logical_type is LogicalType.JSON:
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("utf-8")
            if not isinstance(value, str):
                # A driver that parsed the document has already lost its
                # number spellings; the dialect's sample select reads text.
                return UNCHECKED
            return ("json", canonical_json_text(value, verbatim=json_verbatim))
        if logical_type is LogicalType.BITS:
            if not isinstance(value, str):
                # MySQL's connector hands back an integer, which has lost
                # the width; its dialect's `sample_value` restores it.
                return UNCHECKED
            return ("bits", canonical_bits_text(value))
        if logical_type is LogicalType.INTERVAL:
            return ("interval", _interval_text(value))
        if logical_type is LogicalType.TIMETZ:
            return ("timetz", _timetz_text(value))
        if logical_type is LogicalType.INET:
            return ("inet", _inet_text(value))
        if logical_type is LogicalType.VARIANT:
            if not (isinstance(value, tuple) and len(value) == 2):
                return UNCHECKED  # the driver cannot carry the base type
            return ("variant", canonical_variant_text(value[0], value[1]))
        if logical_type is LogicalType.LIST:
            dims = value.dims if isinstance(value, ShapedList) else None
            items = value.items if isinstance(value, ShapedList) else value
            if element is None or not isinstance(items, (list, tuple)):
                return UNCHECKED
            if any(isinstance(v, list) for v in items):
                return UNCHECKED  # a nested list from a driver: its shape is not known
            tokens = [element(v) for v in items]
            if any(t is UNCHECKED for t in tokens):
                return UNCHECKED
            texts = [canonical_text(t) for t in tokens]
            return ("list", canonical_list_text(texts, dims))
    except (ValueError, TypeError, ArithmeticError, OverflowError):
        return UNCHECKED
    return UNCHECKED


def column_token(
    dialect: Dialect, column: Column, value: Any, float_scale: int | None = None
) -> Any:
    """`reference_token` for one value of `column` as `dialect` read it.

    The per-column flags come from the dialect - which CHAR is padded, which
    FLOAT is float4, whose JSON is verbatim - and a LIST's elements are
    reduced the same way by the element's own column, so the rules for a
    list are exactly the rules for each of its elements.
    """
    scale = dialect.float_scale if float_scale is None else float_scale
    element: Callable[[Any], Any] | None = None
    if column.logical_type is LogicalType.LIST:
        inner = dialect.list_element(column)

        def element(v: Any) -> Any:
            """One element, by the element column's rule."""
            return column_token(dialect, inner, v, scale)

    return reference_token(
        column.logical_type,
        value,
        scale,
        padded=dialect.padded_char(column),
        single_precision=dialect.single_precision(column),
        sub_micro=column.sub_micro,
        json_verbatim=dialect.json_verbatim(column),
        element=element,
    )


def describe(token: Any) -> str:
    """A token as a reader would want it in a message."""
    if token is None:
        return "NULL"
    if token is UNCHECKED:
        return "(not checked)"
    return repr(token[1])


def _decimal_text(value: Any, scale: int) -> str:
    """Half away from zero at `scale` places, unsigned zero - CLAUDE.md 4.2."""
    if isinstance(value, float):
        return _float_text(value, scale, False)
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d.is_nan():
        return "NaN"
    if d.is_infinite():
        return "Infinity" if d > 0 else "-Infinity"
    q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=_WIDE)
    if q == 0:
        q = abs(q)
    return format(q, "f")


def _float_text(value: Any, scale: int, single: bool) -> str:
    """The canonical float form; non-finite values as the shared tokens."""
    x = float(value)
    if single and math.isfinite(x):
        # Widen exactly as the engines do: the nearest float4, as a double.
        x = struct.unpack("<f", struct.pack("<f", x))[0]
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    return canonical_float_text(x, scale)


def _date_text(value: Any) -> str:
    """YYYY-MM-DD, or a PostgreSQL infinity, or an astronomical BC year."""
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    return _parse_temporal(str(value), with_time=False)


def _timestamp_text(value: Any, sub_micro: bool = False) -> str:
    """YYYY-MM-DD HH:MM:SS.ffffff; an instant in UTC. With `sub_micro`, a
    value with digits past microseconds keeps all nine."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_UTC).replace(tzinfo=None)
        return (
            f"{value.year:04d}-{value.month:02d}-{value.day:02d} "
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}."
            f"{value.microsecond:06d}"
        )
    return _parse_temporal(str(value), with_time=True, sub_micro=sub_micro)


def _time_text(value: Any, sub_micro: bool = False) -> str:
    """A time of day, or a MySQL duration, as `canonical_time_text`.

    Drivers hand back `datetime.time` (no `24:00:00`, so dialects that can
    hold it read text), `datetime.timedelta` (MySQL's TIME, a duration), or
    text, whose digits past the microsecond are truncated as the rule says -
    unless `sub_micro` (both sides hold them, COV-2), when a value that has
    any keeps nine, as timestamps do.
    """
    if isinstance(value, dt.time):
        if value.tzinfo is not None:
            raise ValueError("a time with an offset is not a TIME value")
        return canonical_time_text(
            ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
            + value.microsecond
        )
    if isinstance(value, dt.timedelta):
        micros = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
        return canonical_time_text(micros)
    m = _CLOCK.match(str(value).strip())
    if not m:
        raise ValueError(f"unrecognised time text {value!r}")
    digits = (m["f"] or "").ljust(9, "0")
    micros = ((int(m["h"]) * 60 + int(m["mi"])) * 60 + int(m["s"])) * 1_000_000
    text = canonical_time_text(
        -(micros + int(digits[:6])) if m["neg"] else micros + int(digits[:6])
    )
    if sub_micro and digits[6:9] != "000" and not m["neg"]:
        text += digits[6:9]
    return text


def _interval_text(value: Any) -> str:
    """An interval as `canonical_interval_text`.

    From (months, days, microseconds), or from the text PostgreSQL
    (IntervalStyle `postgres`, pinned) or DuckDB prints. Never from a
    `datetime.timedelta`: psycopg and DuckDB's driver both fold a month into
    30 days, so `1 mon` and `30 days` would read alike - the dialects'
    sample select reads text instead.
    """
    if isinstance(value, tuple) and len(value) == 3:
        months, days, micros = (int(x) for x in value)
        return canonical_interval_text(months, days, micros)
    if not isinstance(value, str):
        raise ValueError(f"an interval cannot be read from {type(value).__name__}")
    text = value.strip()
    if text in (INTERVAL_INFINITY, INTERVAL_NEG_INFINITY):
        return text
    months = days = micros = 0
    words = text.split()
    i = 0
    while i < len(words):
        clock = _IV_CLOCK.match(words[i])
        if clock:
            whole = (int(clock["h"]) * 60 + int(clock["mi"])) * 60 + int(clock["s"])
            us = whole * 1_000_000 + int((clock["f"] or "").ljust(6, "0"))
            micros += -us if clock["sign"] == "-" else us
            i += 1
            continue
        n = _IV_UNIT.match(words[i])
        if not n or i + 1 >= len(words):
            raise ValueError(f"unrecognised interval text {value!r}")
        unit = words[i + 1]
        if unit in _IV_MONTHS:
            months += int(n["n"]) * _IV_MONTHS[unit]
        elif unit in ("day", "days"):
            days += int(n["n"])
        else:
            raise ValueError(f"unrecognised interval unit {unit!r} in {value!r}")
        i += 2
    return canonical_interval_text(months, days, micros)


def _timetz_text(value: Any) -> str:
    """A time with time zone as `canonical_timetz_text`, from a
    `datetime.time` carrying its offset or from the engines' text (which
    `24:00:00` needs: Python's time stops at 23:59:59.999999)."""
    if isinstance(value, dt.time):
        offset = value.utcoffset()
        if offset is None:
            raise ValueError("a time without an offset is not a TIMETZ value")
        micros = (
            (value.hour * 60 + value.minute) * 60 + value.second
        ) * 1_000_000 + value.microsecond
        return canonical_timetz_text(micros, int(offset.total_seconds()))
    m = _TIMETZ.match(str(value).strip())
    if not m:
        raise ValueError(f"unrecognised timetz text {value!r}")
    clock = _CLOCK.match(m["clock"])
    if clock is None:  # pragma: no cover - the outer pattern admits only a clock
        raise ValueError(f"unrecognised timetz text {value!r}")
    micros = (int(clock["h"]) * 60 + int(clock["mi"])) * 60 + int(clock["s"])
    micros = micros * 1_000_000 + int((clock["f"] or "").ljust(6, "0"))
    east = int(m["h"]) * 3600 + int(m["m"] or 0) * 60 + int(m["s"] or 0)
    return canonical_timetz_text(micros, -east if m["sign"] == "-" else east)


def _inet_text(value: Any) -> str:
    """A network address as `canonical_inet_text`, from the `ipaddress`
    objects psycopg returns (inet as an interface, cidr as a network) or from
    `address[/prefix]` text, whose prefix defaults to the full width."""
    if isinstance(value, (ipaddress.IPv4Interface, ipaddress.IPv6Interface)):
        return canonical_inet_text(value.ip.packed, value.network.prefixlen)
    if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return canonical_inet_text(value.network_address.packed, value.prefixlen)
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        # psycopg's form of an inet whose prefix is the full width.
        return canonical_inet_text(value.packed, value.max_prefixlen)
    if isinstance(value, str):
        iface = ipaddress.ip_interface(value.strip())
        return canonical_inet_text(iface.ip.packed, iface.network.prefixlen)
    raise ValueError(f"an inet cannot be read from {type(value).__name__}")


def _parse_temporal(text: str, *, with_time: bool, sub_micro: bool = False) -> str:
    """Reduce an engine's text form of a date or timestamp to the token text.

    Only for values a driver cannot carry as a Python object: infinities, BC
    or year-0 dates, and engines whose sample select reads text on purpose.
    Fractions past microseconds are truncated, as the canonical form does.
    """
    t = text.strip()
    if t in ("infinity", "-infinity"):
        return t
    m = _TEMPORAL.match(t)
    if not m:
        raise ValueError(f"unrecognised temporal text {text!r}")
    year = int(m["y"])
    if m["bc"] or m["bc1"]:
        year = 1 - year  # 1 BC is astronomical year 0
    if m["neg"]:
        year = -year
    ys = f"{year:04d}" if year >= 0 else f"-{-year:02d}"
    day = f"{ys}-{m['mo']}-{m['d']}"
    if not with_time:
        return day
    if m["h"] is None:
        return f"{day} 00:00:00.000000"
    digits = (m["f"] or "").ljust(9, "0")
    frac = digits[:9] if sub_micro and digits[6:9] != "000" else digits[:6]
    clock = f"{m['h']}:{m['mi']}:{m['s']}.{frac}"
    tz = m["tz"]
    if tz and tz not in ("Z", "+00", "+00:00", "-00", "+0000"):
        if not (0 < year <= 9999):
            raise ValueError("offset on a year Python cannot represent")
        stamp = dt.datetime.fromisoformat(f"{day} {m['h']}:{m['mi']}:{m['s']}")
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
        seconds = int(digits[4:6] or 0)
        offset = dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
        utc = stamp - sign * offset
        return f"{utc:%Y-%m-%d %H:%M:%S}.{frac}"
    return f"{day} {clock}"


def canonical_text(token: Any) -> str:
    """The canonical text a dialect's `normalize` must produce for a value
    whose reference token is `token` - the spec of CLAUDE.md 4.2/4.3 in one
    function, so an engine's SQL can be tested against the rules directly:
    NULL as the sentinel, text escaped, every other type as its token text."""
    if token is None:
        return NULL_SENTINEL
    if token is UNCHECKED:
        raise ValueError("an unchecked value has no canonical text in the rules")
    kind, value = token
    if kind == "int":
        return str(value)
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("str", "json"):
        # JSON is text with backslashes in its escapes, so it is escaped as
        # any text is (it cannot hold chr(31) or equal the NULL sentinel, but
        # the invariant is cheaper to keep than to argue about per type).
        return escape_canonical_text(value)
    return str(value)  # num, date, ts, time, bin: the token is already the text


__all__ = ["UNCHECKED", "canonical_text", "column_token", "describe", "reference_token"]
