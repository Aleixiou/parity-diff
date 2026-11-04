"""Dialect contract.

A dialect knows three things about one database engine:

1. how to read a column's type and map it onto a :class:`LogicalType`
2. how to render a value as *canonical text* - the same bytes any other
   engine would produce for the same logical value
3. how to fold canonical text into a 60-bit integer and aggregate it

Everything else (segmentation, recursion, reporting) is engine-independent
and lives in :mod:`parity.engine`.

The 60-bit width is deliberate: it is the widest prefix of an MD5 hex digest
that both PostgreSQL's ``bit(n)::bigint`` cast and DuckDB's hex-string cast
render as the same *positive* signed 64-bit integer. At 64 bits PostgreSQL
wraps to negative and the two engines disagree.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from parity.types import Column, KeySpec, KeyStats, LogicalType

_D = TypeVar("_D", bound="Dialect")

# Field separator inside a row's canonical text. Unit Separator (0x1f) is
# chosen because it effectively never appears in warehouse string data;
# `chr(31)` is spelled the same way in every engine we support.
SEPARATOR_SQL = (
    "chr(31)"  # default; a dialect overrides `separator_sql` if its spelling differs
)
NULL_SENTINEL = "\\N"

# ------------------------------------------------------- text escaping
#
# Row text is unambiguous only if no field can contain the separator and no
# non-NULL field can render as the NULL sentinel. Numbers, dates, timestamps
# and booleans render from a fixed alphabet that holds neither, but text does
# not: the string `\N` (the NULL marker of MySQL and PostgreSQL text dumps)
# rendered exactly like NULL, and a chr(31) inside a value moved a field
# boundary, so ('a\x1fb', 'c') and ('a', 'b\x1fc') were one row - false matches
# on ordinary data, in compared columns and in key identity alike (RT-A1..A4,
# B1, B3, E1, E2, F9). So every rendering whose text is arbitrary - STRING, and
# the fallback cast of an unmapped type such as JSON, an array or an interval -
# goes through `Dialect.escape_text`: each backslash is doubled first, then
# each chr(31) becomes a backslash followed by `s`. Afterwards every backslash
# in an escaped field is followed by `\` or `s`, so no field equals `\N`; no
# field contains chr(31), so the separators split a row one way only; and the
# escape is injective, so distinct values stay distinct. Text holding neither
# character renders byte-identically to before, so its checksums do not move.
#
# The backslash is always spelled as a function of its code point (`chr(92)`
# and the engines' equivalents), never as a string literal: MySQL's
# NO_BACKSLASH_ESCAPES and PostgreSQL's standard_conforming_strings decide what
# a literal backslash means, and with the latter off `'\N'` became `'N'`, so a
# NULL matched the one-letter string N (RT-D1).

#: What chr(31) is escaped to, after the backslash that introduces it.
SEPARATOR_ESCAPE = "s"


def escape_canonical_text(value: str) -> str:
    """The escape every dialect's `escape_text` applies, in Python: the spec."""
    return value.replace("\\", "\\\\").replace("\x1f", "\\" + SEPARATOR_ESCAPE)


def decode_canonical_field(field: str) -> str | None:
    """Turn one field of canonical text back into the value it renders.

    None for the NULL sentinel; otherwise the text with the escapes of
    `escape_canonical_text` undone. For *display* only: rows are always
    matched by their canonical text, never by this. A field that is not
    escaped text (a number, a date) holds no backslash and comes back as it
    is; a backslash not starting an escape is kept literally.
    """
    if field == NULL_SENTINEL:
        return None
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    while i < len(field):
        pair = field[i : i + 2]
        if pair == "\\\\":
            out.append("\\")
            i += 2
        elif pair == "\\" + SEPARATOR_ESCAPE:
            out.append("\x1f")
            i += 2
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


# ------------------------------------------------- BINARY, TIME and JSON
#
# The three most common types that used to fall to UNKNOWN and compare
# through an unchecked text cast. Each has an exact rule now; these functions
# are its executable spec, which every dialect's SQL and `parity.reference`
# are tested against (tests/test_types_rules.py, tests/conformance).

#: What every BINARY value's canonical text starts with: two backslashes and
#: `x`. That is PostgreSQL's bytea text (`\x00ff`) as escaped text, which is
#: exactly how PostgreSQL bytea and MySQL binary rendered when they were
#: unmapped (bytea through `::text`, MySQL through `_binary_text`), so their
#: checksums do not move. It can never equal the NULL sentinel.
BINARY_PREFIX = "\\\\x"


def canonical_binary_text(value: bytes) -> str:
    """A BINARY value's canonical text: `BINARY_PREFIX` + lower-case hex.

    Bytes compare exactly - no padding is trimmed, no encoding is read -
    because every engine stores bytes as given (MySQL `binary(n)` and SQL
    Server `binary(n)` store their zero padding, and so render it).
    """
    return BINARY_PREFIX + bytes(value).hex()


def canonical_time_text(micros: int) -> str:
    """A TIME value's canonical text, from its microseconds since midnight.

    `HH:MM:SS.ffffff`, always six fractional digits, as TIMESTAMP's clock
    part. Hours are at least two digits and run past 23 only for values no
    time-of-day type holds: PostgreSQL and DuckDB accept `24:00:00` itself,
    and MySQL's TIME is a duration (-838:59:59 .. 838:59:59), whose negative
    values carry a leading minus - exactly what MySQL's `time_format` prints,
    so a MySQL duration still renders exactly and only ever matches MySQL.
    Digits past the microsecond are not part of the rule: an engine holding
    more (SQL Server time(7), Snowflake TIME(9)) truncates them, and says so.
    """
    sign = "-" if micros < 0 else ""
    m = abs(micros)
    seconds, frac = divmod(m, 1_000_000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{minute:02d}:{sec:02d}.{frac:06d}"


#: JSON's control-character escapes as every engine but one spells them:
#: the six named ones, and `\u00xx` in lower case for the rest of C0.
_JSON_NAMED = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n"}
_JSON_NAMED.update({"\r": "\\r", "\t": "\\t"})


def json_string_literal(value: str) -> str:
    r"""A JSON string as the canonical text spells it.

    `"` and `\` escaped, `\b \f \n \r \t` by name, the rest of U+0000..U+001F
    as `\u00xx` in lower case, and *every other character raw* - `/`, U+007F,
    U+2028, non-ASCII, astral characters as themselves rather than surrogate
    pairs. That is PostgreSQL jsonb's, MySQL's and BigQuery's output exactly
    (verified live); Snowflake escapes U+007F and its dialect undoes it.
    """
    out = ['"']
    for ch in value:
        esc = _JSON_NAMED.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < " ":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class _JsonNumber(str):
    """A JSON number token, kept exactly as the engine spelled it."""


def _json_number(token: str) -> _JsonNumber:
    """`json.loads` hook: keep a number's text instead of converting it."""
    return _JsonNumber(token)


#: One token of JSON text: a string, a run of whitespace, a structural `,` or
#: `:`, or anything else (brackets, numbers, literals). The string alternative
#: comes first, so a `,` or a space inside a string is never mistaken for one
#: outside it - the same alternation every dialect's SQL uses.
_JSON_LAYOUT = re.compile(
    r'("(?:[^"\\]|\\.)*")|([ \t\n\r]+)|([,:])|([^", :\t\n\r]+)', re.S
)


def json_layout(text: str) -> str:
    """JSON text laid out canonically, tokens otherwise untouched.

    Insignificant whitespace removed, then one space after every structural
    comma and colon: `{"a": [1, 2]}`, PostgreSQL jsonb's and MySQL's own
    layout. Strings, numbers and key order pass through verbatim.
    """
    out: list[str] = []
    for m in _JSON_LAYOUT.finditer(text):
        string, space, sep, other = m.groups()
        if string is not None:
            out.append(string)
        elif sep is not None:
            out.append(sep + " ")
        elif other is not None:
            out.append(other)
    return "".join(out)


def canonical_json_text(text: str, *, verbatim: bool = False) -> str:
    """A JSON document's canonical text, from its engine's serialisation.

    The engines normalise JSON on the way in, each its own way: PostgreSQL
    jsonb and MySQL order object keys by length and then bytes and keep the
    last of duplicate keys, BigQuery and Snowflake order them by code point
    (BigQuery keeps the first duplicate, Snowflake refuses them), and numbers
    are exact decimals on PostgreSQL and Snowflake but doubles on MySQL and
    BigQuery. That normal form *is* each engine's JSON value, so key order,
    duplicates and number spellings are taken from it as given. What the
    rule fixes is everything else: the layout (`json_layout`) and every
    string's escaping (`json_string_literal`), so two engines whose normal
    forms agree render byte-identical text whatever their output style.

    `verbatim` is for an engine that stores JSON as text without normalising
    it (DuckDB): only the layout is canonical, and strings, numbers, key
    order and duplicates are compared exactly as stored - an escape spelled
    `\\u00e9` stays different from `é`. Stricter than JSON equality, never
    looser, so it can report two equal documents as different but cannot
    call two different ones equal.

    Raises ValueError on text that is not JSON.
    """
    if verbatim:
        json.loads(text)  # refuse anything that is not JSON at all
        return json_layout(text)
    parsed = json.loads(
        text,
        object_pairs_hook=_JsonObject,
        parse_float=_json_number,
        parse_int=_json_number,
        parse_constant=_json_number,
    )
    return _json_serialise(parsed)


class _JsonObject(list):  # type: ignore[type-arg]
    """A parsed JSON object: its (key, value) pairs in the engine's order,
    duplicates included - so `{}` is never mistaken for `[]`."""


def _json_serialise(value: Any) -> str:
    """Serialise what `canonical_json_text` parsed, in the canonical layout."""
    if isinstance(value, _JsonNumber):
        return str(value)
    if isinstance(value, str):
        return json_string_literal(value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, _JsonObject):
        pairs = (f"{json_string_literal(k)}: {_json_serialise(v)}" for k, v in value)
        return "{" + ", ".join(pairs) + "}"
    return "[" + ", ".join(_json_serialise(v) for v in value) + "]"


# ------------------------- BITS, INTERVAL, TIMETZ, LIST, INET and VARIANT
#
# The second batch of types that used to fall to UNKNOWN. Same contract as
# above: each function is the executable spec its dialects' SQL and
# `parity.reference` are held to (tests/test_types_rules2.py, tests/conformance).
# Every one renders from a fixed alphabet or escaped text, so none holds
# chr(31), and none can equal the NULL sentinel.


def canonical_bits_text(digits: str) -> str:
    """A bit string's canonical text: its digits, every one, at full width.

    `bit(8)` b'101' is `00000101`, never `101` and never `5`: the width is
    part of the value (PostgreSQL `B'101' = B'0101'` is false), and the
    digits are exactly what PostgreSQL bit/varbit, DuckDB BIT and MySQL BIT(n)
    rendered before the type had a rule, so their checksums do not move. A
    varbit may be empty, which renders as the empty text - still distinct
    from NULL. Raises ValueError on anything but 0 and 1.
    """
    if digits.strip("01"):
        raise ValueError(f"not a bit string: {digits!r}")
    return digits


#: An infinite interval (PostgreSQL 17+), as its date/timestamp infinities.
INTERVAL_INFINITY = "infinity"
INTERVAL_NEG_INFINITY = "-infinity"


def canonical_interval_text(months: int, days: int, micros: int) -> str:
    """An interval's canonical text: its three stored components, exactly.

    `<months> mon <days> day <microseconds> us`, each a signed integer. That
    is how PostgreSQL and DuckDB *store* an interval - months, days and
    microseconds, never normalised into one another - so `1 day`, `24:00:00`
    and `1 mon` against `30 days` render differently. Both engines' `=` calls
    `1 day` equal to `24 hours`, but they are different values: added to a
    timestamp across a DST change they give different instants, and they
    print differently. So two engines that agree on the components agree on
    the text, and two different stored intervals never share one (the rule
    is stricter than `=`, never looser: a false alarm at worst).
    """
    return f"{months} mon {days} day {micros} us"


def canonical_timetz_text(micros: int, offset_seconds: int) -> str:
    """A time-with-time-zone value's canonical text: local time, then offset.

    The local time as `canonical_time_text` (`24:00:00` included) and the
    offset east of UTC as `+HH:MM`, with `:SS` only when it has seconds
    (PostgreSQL and DuckDB accept offsets to the second). The offset is part
    of the value: `12:00+05:45` and `06:15+00` are the same instant of
    different days' clocks, and PostgreSQL's own `=` calls them different.
    """
    sign = "-" if offset_seconds < 0 else "+"
    hours, rest = divmod(abs(offset_seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    offset = f"{sign}{hours:02d}:{minutes:02d}"
    if seconds:
        offset += f":{seconds:02d}"
    return canonical_time_text(micros) + offset


#: What follows every element of a list: a backslash and a comma. No element
#: text can contain it - an escaped text's backslashes are always followed by
#: `\` or `s`, the sentinel is `\N`, and every other rule's alphabet has no
#: backslash - so a list's text splits into its elements one way only.
LIST_TERMINATOR = "\\,"

#: Element types a list can hold and still have a rule. JSON is left out (its
#: sample path reads text a list cannot carry), nested lists and every
#: UNKNOWN type too: such a list is UNKNOWN, as it always was.
LIST_ELEMENT_TYPES = frozenset(
    {
        LogicalType.INTEGER,
        LogicalType.DECIMAL,
        LogicalType.FLOAT,
        LogicalType.BOOLEAN,
        LogicalType.STRING,
        LogicalType.DATE,
        LogicalType.TIMESTAMP,
        LogicalType.TIME,
        LogicalType.BINARY,
        LogicalType.BITS,
        LogicalType.INTERVAL,
        LogicalType.TIMETZ,
        LogicalType.INET,
    }
)


#: Brackets the dimensions of a PostgreSQL array that is not one-dimensional
#: from 1 (`canonical_list_text`). No element text can hold it, and a plain
#: list's text cannot start `[\=`, so the two forms never meet.
LIST_DIMS_MARK = "\\="


@dataclass(frozen=True)
class ShapedList:
    """A PostgreSQL array that is not one-dimensional from 1, as the sample
    check reads it: its dimensions as `array_dims` prints them
    (`[1:2][1:2]`, `[0:1]`) and its elements in storage (row-major) order."""

    dims: str
    items: tuple[Any, ...]


def canonical_list_text(elements: Sequence[str], dims: str | None = None) -> str:
    r"""A list's canonical text, from its elements' canonical texts.

    `[` + each element followed by `LIST_TERMINATOR` + `]`: `[]` is empty,
    `[\,]` holds one empty string, `[\N\,]` one NULL, `[a,b\,]` one string
    with a comma and `[a\,b\,]` two strings. Each element is rendered by its
    own type's rule (a NULL as the sentinel, text escaped), so the text is
    injective: it starts with `[`, so it never equals the sentinel, and no
    element can forge a terminator. Element order is part of the value.

    A PostgreSQL array with more than one dimension or a lower bound other
    than 1 carries that shape in its value (`'[0:1]={1,2}'` is not `'{1,2}'`,
    nor `'{{1,2}}'`), so its `dims` come first, between `LIST_DIMS_MARK`s:
    `[\=[1:2][1:2]\=1\,2\,3\,4\,]`. No other engine holds such a value.
    """
    head = "[" if dims is None else "[" + LIST_DIMS_MARK + dims + LIST_DIMS_MARK
    return head + "".join(e + LIST_TERMINATOR for e in elements) + "]"


def canonical_inet_text(packed: bytes, prefix: int) -> str:
    """A PostgreSQL inet/cidr value's canonical text: `address/prefix`.

    PostgreSQL's own `::text` (not its output function, which drops a full
    `/32`): every byte of the address, then the prefix length, so
    `10.0.0.1/32` and `10.0.0.1/8` differ, as they do in PostgreSQL. IPv4 is
    dotted; IPv6 is lower-case hex groups with the longest run of two or more
    zero groups - the first, on a tie - written `::`, and the last 32 bits
    dotted when the address is IPv4-compatible or IPv4-mapped (BIND's
    `inet_net_ntop`, which PostgreSQL uses). An inet and a cidr holding the
    same network render alike, as PostgreSQL's `=` holds them equal.
    """
    if len(packed) == 4:
        return ".".join(str(b) for b in packed) + f"/{prefix}"
    if len(packed) != 16:
        raise ValueError(f"not an IPv4 or IPv6 address: {packed!r}")
    words = [packed[i] << 8 | packed[i + 1] for i in range(0, 16, 2)]
    best_base, best_len, cur_base, cur_len = -1, 0, -1, 0
    for i, w in enumerate(words):
        if w == 0:
            if cur_base == -1:
                cur_base, cur_len = i, 1
            else:
                cur_len += 1
        elif cur_base != -1:
            if best_base == -1 or cur_len > best_len:
                best_base, best_len = cur_base, cur_len
            cur_base = -1
    if cur_base != -1 and (best_base == -1 or cur_len > best_len):
        best_base, best_len = cur_base, cur_len
    if best_base != -1 and best_len < 2:
        best_base = -1
    out = ""
    for i, w in enumerate(words):
        if best_base != -1 and best_base <= i < best_base + best_len:
            if i == best_base:
                out += ":"
            continue
        if i:
            out += ":"
        if (
            i == 6
            and best_base == 0
            and (
                best_len == 6
                or (best_len == 7 and words[7] != 1)
                or (best_len == 5 and words[5] == 0xFFFF)
            )
        ):
            out += ".".join(str(b) for b in packed[12:])
            break
        out += f"{w:x}"
    if best_base != -1 and best_base + best_len == 8:
        out += ":"
    return f"{out}/{prefix}"


#: sql_variant base types whose value is an integer (or bit), a decimal at its
#: own scale, or text, rendered by SQL Server's default conversion.
_VARIANT_INTEGERS = frozenset({"tinyint", "smallint", "int", "bigint", "bit"})
_VARIANT_TEXTS = frozenset({"char", "varchar", "nchar", "nvarchar"})


def _ns_clock(ns: int, digits: int) -> str:
    """`HH:MM:SS.fff...` from nanoseconds since midnight, `digits` places."""
    seconds, frac = divmod(ns, 1_000_000_000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    text = f"{hours:02d}:{minute:02d}:{sec:02d}"
    if digits:
        text += "." + f"{frac:09d}"[:digits]
    return text


def canonical_variant_text(base: str, value: Any) -> str:
    """A SQL Server sql_variant's canonical text: `<base type>:<exact value>`.

    The base type leads, so int 1, bigint 1, N'1' and 1.5 as float against
    decimal never meet; the value follows in an exact spelling of that base
    type (escaped, since it can be text). `value` is the base type's value
    as Python holds it:

    - tinyint/smallint/int/bigint/bit: int - its digits;
    - decimal/numeric: Decimal at its declared scale - `1.50` stays `1.50`;
    - float/real: float - the IEEE bits in hex, `0x3FF8000000000000` (real:
      eight digits), with -0.0 as 0.0. SQL Server prints no exact decimal
      for a double: style 3 is not correctly rounded (1626983080611305.75
      printed ...057, not ...058) and raised "Arithmetic overflow" for any
      negative value past e-100 and for -0.0, so the bits are the value;
    - money/smallmoney: Decimal - four places, `-1.5000`;
    - date: datetime.date - `YYYY-MM-DD`;
    - datetime, smalldatetime, datetime2: (date, nanoseconds since midnight) -
      datetime as its displayed milliseconds (`.003`, `.007`), smalldatetime
      to the second, datetime2 to seven places;
    - datetimeoffset: (local date, local nanoseconds, offset minutes) -
      seven places and ` +HH:MM`;
    - time: nanoseconds since midnight - seven places;
    - binary/varbinary: bytes - `0x` and upper-case hex;
    - uniqueidentifier: uuid.UUID - lower case;
    - char/varchar/nchar/nvarchar: str, exactly (a char keeps its padding).

    Raises ValueError for any other base type.
    """
    import struct
    import uuid

    b = base.lower()
    if b in _VARIANT_INTEGERS:
        text = str(int(value))
    elif b in ("decimal", "numeric"):
        text = format(Decimal(value), "f")
    elif b in ("float", "real"):
        x = float(value)
        if x == 0:
            x = 0.0  # -0.0: equal to 0.0 in SQL Server, so one value
        fmt = ">d" if b == "float" else ">f"
        text = "0x" + struct.pack(fmt, x).hex().upper()
    elif b in ("money", "smallmoney"):
        text = format(Decimal(value).quantize(Decimal("0.0001")), "f")
    elif b == "date":
        d = value
        text = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    elif b in ("datetime", "smalldatetime", "datetime2"):
        d, ns = value
        digits = {"datetime": 3, "smalldatetime": 0, "datetime2": 7}[b]
        text = f"{d.year:04d}-{d.month:02d}-{d.day:02d} {_ns_clock(ns, digits)}"
    elif b == "datetimeoffset":
        d, ns, offset = value
        sign = "-" if offset < 0 else "+"
        hh, mm = divmod(abs(offset), 60)
        text = (
            f"{d.year:04d}-{d.month:02d}-{d.day:02d} {_ns_clock(ns, 7)} "
            f"{sign}{hh:02d}:{mm:02d}"
        )
    elif b == "time":
        text = _ns_clock(int(value), 7)
    elif b in ("binary", "varbinary"):
        text = "0x" + bytes(value).hex().upper()
    elif b == "uniqueidentifier":
        text = str(uuid.UUID(str(value))).lower()
    elif b in _VARIANT_TEXTS:
        text = str(value)
    else:
        raise ValueError(f"sql_variant base type {base!r} has no rule")
    return f"{b}:{escape_canonical_text(text)}"


# Number of MD5 hex characters folded into the row hash. 15 nibbles = 60 bits.
HASH_HEX_CHARS = 15

#: Most values a single `concat_ws` call may take. PostgreSQL's
#: `max_function_args` is 100 and fixed at compile time; DuckDB allows more.
#: 64 leaves clear headroom on the strictest engine and keeps both sides
#: nesting identically, which is what guarantees identical canonical text.
MAX_CONCAT_ARGS = 64

#: Decimal places at which DECIMAL/FLOAT columns are compared. A deliberate,
#: documented limitation - see CLAUDE.md section 4.2.
DEFAULT_FLOAT_SCALE = 6

# ------------------------------------------------------------------ FLOAT
#
# The canonical text of a finite double. Every engine must compute exactly
# `canonical_float_text` below, and the obvious per-engine spellings do not:
# PostgreSQL's `float8::numeric` keeps only 15 significant digits, and DuckDB's
# `cast(double as decimal)` multiplies by 10^scale in double precision, so the
# two disagreed on a fifth of an adversarial sample of doubles - rounding-only
# false alarms (1e15+0.5 rendered `...0.500000` against `...0.524288`). The
# definition below is a function of the IEEE value alone, so two equal doubles
# always render identically, and every engine can reach it:
#
# - |x| < 2^53: take the *shortest round-trip* decimal (what Python's repr,
#   PostgreSQL 12+'s float8out, DuckDB's and MySQL's double-to-text all print)
#   and round it half away from zero to the float scale. It is also the value
#   a user typed, so a DECIMAL 2.0000005 and a DOUBLE 2.0000005 still agree.
# - 2^53 <= |x| < 2^116: every such double is an integer; render it exactly.
#   Not the shortest repr, because PostgreSQL's shortest-repr algorithm (Ryu)
#   excludes the rounding-interval boundaries where MySQL's and DuckDB's (dtoa)
#   include them, and above 2^54 those boundaries are short integers - so
#   3.8809635010537424e16 prints with 17 digits on one and 16 on the others.
#   Every engine splits x into trunc(x / 2^53) and the exact remainder, both
#   held exactly in a 64-bit integer, and recombines them in a decimal type.
# - |x| >= 2^116: the high half no longer fits 64 bits. Refused loudly, with
#   an error naming the value, on every engine - never rendered approximately.

#: Below this magnitude a double's shortest round-trip decimal is the same on
#: every engine; at and above it every double is an integer and is rendered
#: exactly. 2^53, exactly representable.
FLOAT_EXACT_INT_FROM = 2**53

#: Doubles at or above this magnitude are refused: trunc(x / 2^53) no longer
#: fits a signed 64-bit integer. 2^116 ~= 8.3e34, exactly representable.
FLOAT_MAX_MAGNITUDE = 2**116

#: `FLOAT_MAX_MAGNITUDE` as a decimal string that parses to exactly 2^116.
FLOAT_MAX_MAGNITUDE_SQL = "83076749736557242056487941267521536"

#: The text every engine raises with for a double past `FLOAT_MAX_MAGNITUDE`.
#: The refusing branch must be `when c is not null then ...`, never `else`: a
#: NULL fails every magnitude test and would land in it - BigQuery then raised
#: "Input to ERROR function must not be NULL" on an ordinary NULL double. The
#: boolean `else` trap again (CLAUDE.md section 4.3), in a new place.
FLOAT_OUT_OF_RANGE_MESSAGE = (
    "parity: FLOAT value out of the comparable range (|x| >= 2^116, about "
    "8.3e34) - refusing to round it: "
)


def float_range_where(column: Column) -> str:
    """What follows the value in a FLOAT refusal: the column it is in.

    Each dialect puts it in the SQL text it raises with, as a literal of that
    engine's own quoting, so the error names the column as well as the value
    - and the dialect's `query` adds the side (CLAUDE.md section 6). Without
    it the refusal on DuckDB and PostgreSQL was a bare driver error naming
    neither (RT-E10).
    """
    return f" in column {column.name!r}"


#: Largest --float-scale a FLOAT column can be compared at. Every double below
#: 2^53 needs 16 integer digits, and BigQuery's BIGNUMERIC holds 38 decimal
#: places, which a 17-significant-digit rounding of a value just above
#: 10^-(scale+1) needs scale + 18 of. Past it MySQL's DECIMAL cast would clamp
#: silently (2e15 at 23 places became 999999999999999.999...) and BigQuery
#: would round from a truncated value, so it is refused instead.
FLOAT_MAX_SCALE = 20


def float_tiny_sql(scale: int) -> str:
    """Magnitude below which a double renders as zero at `scale`, as a literal.

    10^-(scale+1) is far below the half-unit 5 * 10^-(scale+1), so anything
    smaller rounds to zero under any reading of the value. Engines use it to
    short-circuit values whose text would be in exponent notation, which
    DuckDB's string-to-decimal cast rounds wrongly (`'6.4e-10'` became
    `0.000001`).
    """
    return f"1e-{scale + 1}"


def float_zero_text(scale: int) -> str:
    """Zero at `scale` places, unsigned: `0.000000`, or `0` at scale 0."""
    return "0." + "0" * scale if scale else "0"


def canonical_float_text(x: float, scale: int = DEFAULT_FLOAT_SCALE) -> str:
    """The canonical text of a double, in Python: the executable spec.

    Every dialect's FLOAT expression must agree with this byte for byte; the
    cross-engine tests and `demo/float_agreement.py` check them against it.
    Raises ValueError for non-finite values (the dialects emit the tokens
    `Infinity`, `-Infinity`, `NaN`) and past `FLOAT_MAX_MAGNITUDE`.
    """
    import math
    from decimal import ROUND_HALF_UP, Context, Decimal

    if not math.isfinite(x):
        raise ValueError(f"not finite: {x!r}")
    if abs(x) >= FLOAT_MAX_MAGNITUDE:
        raise ValueError(FLOAT_OUT_OF_RANGE_MESSAGE + repr(x))
    exact_int = abs(x) >= FLOAT_EXACT_INT_FROM
    d = Decimal(int(x)) if exact_int else Decimal(repr(x))
    ctx = Context(prec=200)
    q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=ctx)
    if q == 0:
        return float_zero_text(scale)
    return format(q, "f")


def float_key_refusal(column: Column) -> str:
    """Why a FLOAT column cannot be a key - see `Dialect.normalize_key`."""
    return (
        f"FLOAT column {column.name!r} cannot be used as a key. A key is "
        "matched by exact text, and doubles are only compared rounded to the "
        "float scale, which would merge distinct keys into one row; no "
        "rendering of a double is both exact and identical on every engine. "
        "Key by another column, or by a DECIMAL/integer cast of this one in a view"
    )


def canonical_decimal_key_text(value: Decimal) -> str:
    """A DECIMAL key's canonical text, in Python: the spec `decimal_key_text`
    implements on every engine. The exact value in plain notation, without
    trailing fractional zeros or a trailing point, and never `-0`."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def sql_literal(value: str) -> str:
    """Quote a string literal. Only ever used for schema and table names read
    back from `information_schema`, never for user data."""
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Row filters (`--where`)
# ---------------------------------------------------------------------------
#
# A row filter is the user's own SQL, run against the user's own database, so
# this is not a security boundary - the user can already run any SQL they
# like there. What it must guarantee is that the tool's read stays one read:
# the predicate is pasted, parenthesised, into the WHERE clause of every query
# that reads the table, so it must not be able to end that statement (`;`),
# hide the rest of it (a comment), or close the parentheses it is wrapped in
# and so escape the bucket bounds it is ANDed with (`1=1) or (1=1`). The
# sessions are read-only besides (CLAUDE.md 4.9, 4.11), which is what keeps
# a predicate that calls a writing function from writing.

#: Quote characters a predicate may use: string literals everywhere, quoted
#: identifiers (`"` standard, `` ` `` MySQL and BigQuery). A quote of one kind
#: ends only at the same kind; doubling it (`'it''s'`) closes and reopens, on
#: every engine alike. `[` opens a SQL Server identifier (or a subscript) and
#: is skipped to the first `]`: the engine's own region is never shorter.
_FILTER_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}

#: Text that changes where a quoted region ends on some engines and not on
#: others, so the scan below could disagree with an engine about which
#: parentheses are code: a backslash before a quote (an escape on MySQL,
#: BigQuery and Snowflake, a plain character elsewhere), BigQuery's
#: triple-quoted strings, and dollar quoting (`$$...$$`, `$tag$...$tag$`) on
#: PostgreSQL and Snowflake.
_FILTER_AMBIGUOUS = re.compile(r"""\\['"`]|'''|\"\"\"|\$\w*\$""")


def _filter_scan(predicate: str) -> str | None:
    """Find what would let `predicate` escape its parentheses, or None.

    Every quoted region is skipped as the engines skip it - whatever could
    make them disagree is refused first (`_FILTER_AMBIGUOUS`) - so the
    parentheses counted here are the ones every engine parses as code.
    """
    depth = 0
    close = ""
    for ch in predicate:
        if close:
            if ch == close:
                close = ""
        elif ch in _FILTER_QUOTES:
            close = _FILTER_QUOTES[ch]
        elif ch == "#":
            # MySQL reads `#` to the end of the line as a comment.
            return "contains '#' outside a quoted string (MySQL reads it as a comment)"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return (
                    "closes a parenthesis it never opened, which would let it "
                    "escape the parentheses it is wrapped in"
                )
    if close:
        return f"has an unterminated quote (no closing {close})"
    if depth:
        return "has an unclosed parenthesis"
    return None


def row_filter_problem(predicate: str) -> str | None:
    """Why `predicate` cannot be used as a row filter, or None if it can.

    A sanity check, not a parser: it refuses what could end or escape the
    statement the predicate is pasted into, and anything engines tokenise
    differently. It is deliberately stricter than SQL - `;` or `--` inside a
    string literal is refused too - because a refusal is an inconvenience
    and a predicate read differently from how it was checked is not.
    """
    if not predicate.strip():
        return "is empty"
    if any(ord(c) < 32 and c not in "\t\n\r" for c in predicate):
        return "contains a control character"
    if ";" in predicate:
        return "contains ';' (one predicate, not a statement; refused even in a string)"
    for marker in ("--", "/*", "*/"):
        if marker in predicate:
            return f"contains {marker!r}, a SQL comment marker (refused even in a string)"
    found = _FILTER_AMBIGUOUS.search(predicate)
    if found:
        return (
            f"contains {found.group(0)!r}, which engines quote differently (a "
            "backslash before a quote, a triple-quoted or a dollar-quoted "
            "string); spell the value another way, with chr() for instance"
        )
    return _filter_scan(predicate)


class Dialect(ABC):
    """One database engine's half of a comparison."""

    name: str

    def __init__(
        self,
        float_scale: int = DEFAULT_FLOAT_SCALE,
        side: str = "?",
    ) -> None:
        """Configure one side of a comparison.

        Both settings are per-instance rather than per-class, which matters:
        see `float_scale` below for what a shared class attribute would do.
        """
        if float_scale < 0:
            raise ValueError(f"float_scale must be >= 0, got {float_scale}")
        #: Decimal places at which DECIMAL/FLOAT columns are compared. This is
        #: per-instance, not per-class: as a class attribute a change on one
        #: side would leak to the other, or worse, not leak - and two sides
        #: rounding differently reports *every* float row as different. Use
        #: `require_matching_scales` before comparing.
        self.float_scale = float_scale
        #: "A" or "B". Carried only so errors can name which side failed;
        #: "table not found" is useless when two databases are in play.
        self.side = side

    def _err(self, message: str) -> ValueError:
        """Build an error that names which side and which engine it came from.

        "table not found" is useless when two databases are in play.
        """
        return ValueError(f"[side {self.side}: {self.name}] {message}")

    # ---------------------------------------------------------------- setup

    @abstractmethod
    def connect(self, connection_string: str) -> None:
        """Open a connection. Must be read-only, and should pin UTC."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""

    def __enter__(self: _D) -> _D:
        """`with get_dialect(...) as a:` closes the connection however it ends.

        `diff` never closes the dialects it is handed - the caller owns them.
        A PostgreSQL side left open after a diff raises sits idle in its
        REPEATABLE READ snapshot, holding back vacuum, so library callers
        should close in a `finally` or use this.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the connection on leaving the `with` block."""
        self.close()

    @abstractmethod
    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a list of tuples."""

    def cancel(self) -> None:  # noqa: B027 - optional by design, see below
        """Abort whatever query is in flight, from another thread.

        Both sides are queried on worker threads, so a Ctrl-C reaches the main
        thread while the workers sit blocked on the database. Without this the
        interrupt is not acted on until the queries finish on their own - which
        on the ten-minute diff someone actually wants to abort is the whole
        problem. Optional: a dialect that cannot do it inherits a no-op and
        simply behaves as before.
        """

    # ------------------------------------------------------------ metadata

    #: Where an unqualified table name is looked up. `public` on PostgreSQL,
    #: `main` on DuckDB.
    default_schema: str = "public"

    def columns(self, table: str) -> list[Column]:
        """Introspect a table's columns.

        Both supported engines expose `information_schema.columns`, so this is
        shared. Override it in a dialect whose engine does not.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            f"select column_name, data_type from {self._columns_catalog(schema)} "
            f"where table_schema = {self.string_literal(schema)} "
            f"and table_name = {self.string_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [Column(r[0], map_type(r[1]), r[1]) for r in rows]

    def _columns_catalog(self, schema: str) -> str:
        """The relation that lists columns for tables in `schema`.

        One global `information_schema.columns` on PostgreSQL, DuckDB, MySQL
        and Snowflake. BigQuery has no global one: its `INFORMATION_SCHEMA`
        lives inside each dataset (`` `project.dataset`.INFORMATION_SCHEMA ``),
        and the region-wide view needs project-level permissions a narrowly
        granted service account does not hold. A hook rather than a second
        copy of `_not_found`, so the near-miss hint keeps working there too.
        """
        return "information_schema.columns"

    def string_literal(self, value: str) -> str:
        """Quote a schema or table name as a string literal for this engine.

        Standard SQL doubles an embedded quote, which is `sql_literal`.
        BigQuery does not accept that - it escapes with a backslash, and treats
        a bare backslash as the start of an escape sequence - so it overrides
        this. Only ever used for names, never for user data.
        """
        return sql_literal(value)

    def int_literal(self, value: int) -> str:
        """Render an integer the bucket expression needs as a SQL literal.

        Plain digits everywhere but BigQuery, which refuses to *parse* an
        integer literal outside INT64 ("Invalid integer literal") - and the
        precomputed key span of a range as wide as bigint is up to 2^64. Every
        other engine reads such a literal as numeric/hugeint on its own.
        """
        return str(value)

    def _not_found(self, table: str, schema: str, name: str) -> str:
        """Explain a missing table, and point at a case mismatch if that is it.

        Identifiers are always quoted, so lookup is exact and case-sensitive.
        That is correct, but unquoted SQL gets folded to lower case by the
        server, so `--a-table Orders` against a table the server stored as
        `orders` is a very easy mistake with a very unhelpful default message.
        """
        if self._exists_but_unreadable(schema, name):
            return (
                f"table {table} exists but this role cannot read it. "
                f"`information_schema` only lists tables you hold privileges "
                f"on, so a missing GRANT looks exactly like a missing table. "
                f"Ask for SELECT on {schema}.{name}."
            )

        message = f"table not found: {table} (looked in schema {schema!r})"
        try:
            near = self.query(
                "select distinct table_schema, table_name "
                f"from {self._columns_catalog(schema)} "
                f"where lower(table_name) = lower({self.string_literal(name)})"
            )
        except Exception:  # noqa: BLE001 - diagnosing an error must never replace it
            return message
        others = [f"{s}.{t}" for s, t in near if (s, t) != (schema, name)]
        if others:
            message += (
                f". Names are matched exactly, including case - did you mean "
                f"{' or '.join(sorted(others))}?"
            )
        return message

    def _exists_but_unreadable(self, schema: str, name: str) -> bool:
        """Whether the table is really there and this role simply cannot see it.

        Engines with a privilege model filter `information_schema` by what the
        current role may access, so "not found" and "not granted" arrive as the
        same empty result - and telling someone their table does not exist when
        it does sends them hunting for a typo instead of asking for a GRANT.
        Default False for engines with no privilege model.
        """
        return False

    @abstractmethod
    def quote(self, identifier: str) -> str:
        """Quote one identifier. The injection boundary - see the dialects."""

    def qualify(self, table: str) -> str:
        """Quote a possibly schema-qualified table name."""
        return ".".join(self.quote(part) for part in table.split("."))

    def split_table(self, table: str, default_schema: str) -> tuple[str, str]:
        """Split ``schema.table``, refusing anything it cannot honour.

        Without the length check a three-part name like ``db.schema.table``
        silently fell through to the unqualified branch and was looked up as a
        *table* called ``db`` in the default schema - so the eventual "table
        not found" named a schema the user never mentioned.
        """
        parts = table.split(".")
        if len(parts) == 1:
            return default_schema, parts[0]
        if len(parts) == 2:
            return parts[0], parts[1]
        raise self._err(
            f"table name {table!r} has {len(parts)} dot-separated parts; "
            f"expected 'table' or 'schema.table'"
        )

    # ----------------------------------------------------------- rendering

    @abstractmethod
    def normalize(self, column: Column) -> str:
        """SQL expression rendering ``column`` as canonical text.

        Implementations MUST be null-safe: a NULL value renders as
        :data:`NULL_SENTINEL`, never as SQL NULL.
        """

    @abstractmethod
    def hash_expr(self, text_expr: str) -> str:
        """SQL expression folding canonical text into a 60-bit integer."""

    @abstractmethod
    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. ``/`` is *not* portable: PostgreSQL
        truncates on integers, DuckDB promotes to double."""

    @abstractmethod
    def sum_wide(self, expr: str) -> str:
        """Sum ``expr`` in a type wide enough not to overflow.

        Row hashes are up to 2^60; summing millions of them overflows a
        64-bit accumulator, so both sides must aggregate in a 128-bit or
        arbitrary-precision type.
        """

    @abstractmethod
    def wide_int(self, expr: str) -> str:
        """Widen an integer expression beyond 64 bits before arithmetic.

        The bucket expression multiplies the key offset by the bucket count,
        and ``span * n_segments`` exceeds a signed 64-bit integer as soon as
        the key range is wider than about 2.9e17 - which is ordinary for
        sparse bigint keys, and guaranteed once keys are hashed into the full
        bigint range. Both engines raise rather than wrap, so this shows up as
        a crash rather than a wrong answer, but it is still a hole.
        """

    # ------------------------------------------------------------ building

    def separator_sql(self) -> str:
        """SQL for the field separator, ASCII Unit Separator (0x1f).

        `chr(31)` on PostgreSQL and DuckDB. MySQL spells it `char(31)`, and
        must force a character result (`using utf8mb4`) or the join returns a
        binary string, which would push the composite-key identity back to
        bytes when fetched.
        """
        return SEPARATOR_SQL

    def float_scale_for(self, column: Column) -> int:
        """The float scale, refused if a FLOAT column cannot be rendered at it.

        See `FLOAT_MAX_SCALE`: past it at least one engine would round from a
        clamped or truncated value without saying so.
        """
        if self.float_scale > FLOAT_MAX_SCALE:
            raise self._err(
                f"FLOAT column {column.name!r} cannot be compared at "
                f"float_scale {self.float_scale}; the maximum is {FLOAT_MAX_SCALE}"
            )
        return self.float_scale

    def float_caveat(self) -> str | None:
        """A known gap in this engine's FLOAT rendering, or None.

        None means the dialect renders finite doubles exactly as
        `canonical_float_text` does, verified against a live engine. A dialect
        that cannot claim that returns the caveat, and the engine prints it as
        a warning whenever a FLOAT column is compared, so the limitation
        surfaces in the output instead of hiding in a docstring.
        """
        return None

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """Known limitations of this side's comparison, for the output.

        CLAUDE.md section 8: an approximation the docs admit to must also reach
        the output, or a reader takes the verdict as exact. The engine calls
        this once per side with that side's compared columns, after
        `connect`, and prints every line as a warning naming the side. A
        dialect returns what holds for this connection - SQL Server, say,
        whether the walk is pinned to one snapshot, which only the server can
        tell it. Default: nothing to report.
        """
        return []

    # --------------------------------------------------- SQL spelling hooks
    #
    # The shared queries below are standard SQL that every engine before
    # SQL Server accepted. Each hook returns exactly the text the queries
    # always had, so overriding is the only way any engine's SQL changes.

    def count(self, expr: str = "*") -> str:
        """`count(expr)`. SQL Server overrides it with `count_big`, because its
        `count` returns a 32-bit int and raises past 2,147,483,647 rows."""
        return f"count({expr})"

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count the distinct key identities - the uniqueness check.

        Equality here is the engine's, and on SQL Server that is the column's
        collation: case-insensitive by default, and blind to trailing spaces
        under every collation, so two genuinely distinct text keys would merge
        and raise a false "not unique". A dialect with such equality counts
        something exact instead. Default `count(distinct identity)`.
        """
        return f"count(distinct {identity})"

    def group_by(self, position: int, expr: str) -> str:
        """How the checksum query names its grouping column.

        By position (`1`) everywhere but SQL Server, whose GROUP BY accepts
        neither an ordinal nor a select-list alias, so it repeats the
        expression - `expr` is exactly the text the select list holds.
        """
        return str(position)

    def backslash_sql(self) -> str:
        r"""SQL for one backslash, built from its code point: `chr(92)`.

        Never a literal: whether `'\'` means a backslash depends on MySQL's
        sql_mode and PostgreSQL's standard_conforming_strings (RT-D1).
        """
        return "chr(92)"

    def concat_sql(self, left: str, right: str) -> str:
        """Concatenate two text expressions. `||` here; MySQL and SQL Server
        override it (`||` is logical OR in MySQL, `+` in T-SQL)."""
        return f"({left} || {right})"

    def null_sentinel_sql(self) -> str:
        r"""SQL producing the NULL sentinel `\N`, from `chr(92)`.

        It used to be the literal `'\N'`, which PostgreSQL reads as `'N'` when
        standard_conforming_strings is off - a role, database or PGOPTIONS
        default - so a NULL matched the string N (RT-D1). Same two bytes on
        every engine, so no checksum moves.
        """
        return self.concat_sql(self.backslash_sql(), "'N'")

    def escape_text(self, expr: str) -> str:
        r"""Escape arbitrary text so row text stays unambiguous.

        Backslash doubled first, then chr(31) as `\s` - see the comment at
        `SEPARATOR_ESCAPE`, and `escape_canonical_text` for the spec. Applied
        to the STRING branch and the unmapped-type fallback of every dialect.
        A NULL stays NULL, so the sentinel still comes from the coalesce.
        """
        bs = self.backslash_sql()
        escaped_sep = self.concat_sql(bs, "'" + SEPARATOR_ESCAPE + "'")
        return (
            f"replace(replace({expr}, {bs}, {self.concat_sql(bs, bs)}), "
            f"{self.separator_sql()}, {escaped_sep})"
        )

    def row_text(self, columns: Sequence[Column]) -> str:
        """Join every column's canonical text into one string per row.

        Fields are separated by ASCII Unit Separator, and no field can contain
        one - text is escaped (`escape_text`) and every other type renders from
        an alphabet without it - so ("ab", "c") cannot collide with ("a", "bc"),
        nor ("a\\x1fb", "c") with ("a", "b\\x1fc").
        """
        if not columns:
            # Two tables can legitimately share only their key - after
            # `--columns`/`--exclude`, or when the schemas have diverged
            # entirely. `concat_ws(chr(31), )` is a syntax error, so render a
            # constant instead. The checksum query never passes an empty column
            # set here: `segment_checksums` always folds the key in, so in that
            # mode the key itself is what gets hashed. This branch stays as a
            # defensive fallback for any other caller.
            return "''"
        return self._concat([self.normalize(c) for c in columns])

    def _concat(self, parts: list[str]) -> str:
        """Join rendered columns, nesting to stay under the argument limit.

        PostgreSQL's `max_function_args` is 100 and is fixed at compile time,
        so a flat `concat_ws(sep, c1, ..., cN)` raises "cannot pass more than
        100 arguments to a function" the moment a table has ~99 comparable
        columns - which a denormalised warehouse fact table routinely does.
        DuckDB happily accepts 150, so the failure was asymmetric: the same
        table worked on one side and not the other.

        Nesting is **exact, not an approximation**. `concat_ws` joins its
        arguments with the separator and skips only NULLs, and every argument
        here has already been through `coalesce`, so none is ever NULL.
        Therefore `concat_ws(s, concat_ws(s, a, b), c)` is byte-identical to
        `concat_ws(s, a, b, c)`, and a table narrow enough to fit in one call
        renders exactly the SQL it always did - no checksum moves.
        """
        if len(parts) <= MAX_CONCAT_ARGS:
            return f"concat_ws({self.separator_sql()}, {', '.join(parts)})"
        groups = [
            self._concat(parts[i : i + MAX_CONCAT_ARGS])
            for i in range(0, len(parts), MAX_CONCAT_ARGS)
        ]
        return self._concat(groups)

    def row_hash(self, columns: Sequence[Column]) -> str:
        """Fold a whole row down to one 60-bit integer, inside the engine.

        This is what makes the tool cheap: summed per bucket, it means a few
        integers cross the network instead of the table.
        """
        if not columns:
            return self.hash_expr(self.row_text(columns))
        return self.hash_parts([self.normalize(c) for c in columns])

    def hash_parts(self, parts: list[str]) -> str:
        """Hash already-rendered canonical fields joined as row text.

        The one place a row's fields become its hash, used by `row_hash` and
        by the segment checksum (which folds the key text in first), so a
        dialect that must guard the join - MySQL, whose `concat_ws` returns
        NULL past max_allowed_packet - overrides exactly one method and every
        checksum gets the guard. When the checksum briefly built its own hash
        inline, MySQL's guard was bypassed and a row too wide to join dropped
        out of the sum again (RT-A17).
        """
        return self.hash_expr(self._concat(parts))

    def key_spec(self, table: str, *names: str) -> KeySpec:
        """Build a `KeySpec` for these columns of this table.

        Introspects the table, then decides on hashing the same way the engine
        does: a single integer column is used directly, anything else - a uuid,
        a natural string key, several columns together - is hashed.

        The engine builds its own specs so it can compare the two sides' types
        before deciding. This is for calling a dialect on its own.
        """
        if not names:
            raise self._err("a key needs at least one column")
        found = {c.name: c for c in self.columns(table)}
        missing = [n for n in names if n not in found]
        if missing:
            raise self._err(
                f"key column(s) {missing} not in {table}. Columns are: {sorted(found)}"
            )
        cols = tuple(found[n] for n in names)
        hashed = not (len(cols) == 1 and cols[0].logical_type is LogicalType.INTEGER)
        return KeySpec(cols, hashed)

    def key_bucket(self, key: KeySpec) -> str:
        """The integer the bisection divides.

        A single integer column is itself; anything else is hashed down to 60
        bits. Collisions here are harmless - two rows sharing a bucket are
        still compared individually by `key_identity`.
        """
        if not key.hashed:
            return self.quote(key.columns[0].name)
        return self.hash_expr(self._concat(self._key_parts(key)))

    def key_identity(self, key: KeySpec) -> str:
        """The key as the user would recognise it, and as rows are matched by.

        Never the hash. Identity has to be exact, or two unrelated rows that
        happened to collide would be compared against each other - a false
        difference at best and a masked one at worst. Exact means the key text,
        not the value text: see `normalize_key`.
        """
        if not key.hashed:
            return self.quote(key.columns[0].name)
        return self._concat(self._key_parts(key))

    def _key_parts(self, key: KeySpec) -> list[str]:
        """Each key column's exact canonical text, in key order."""
        return [self.normalize_key(c) for c in key.columns]

    def normalize_key(self, column: Column) -> str:
        """SQL rendering a *key* column as exact canonical text, null-safe.

        `normalize` is right for values and wrong for keys. It rounds DECIMAL
        and FLOAT to the float scale and timestamps to microseconds, which for
        a value is a documented, printed precision limit - but a key's text is
        its identity, so the same rounding merged distinct rows: key 1.00000001
        on one side "was" 1.00000002 on the other and the pair read identical,
        and a unique decimal(18,8) key was refused as not unique (RT-A18, B5,
        B6). A key is therefore rendered exactly or refused:

        - DECIMAL: the exact value with trailing fractional zeros (and a bare
          point) removed - `decimal_key_text`. Independent of the declared
          scale, so numeric(10,2) 1.50 and decimal(18,8) 1.50000000 are both
          `1.5`, and an integral decimal is `42`, the text an integer key has.
        - FLOAT: refused (`check_key`). A double's exact decimal expansion has
          up to 767 digits and its shortest repr differs between engines above
          2^53; no rendering is both exact and the same everywhere.
        - Sub-microsecond timestamps: a dialect whose engine holds them
          overrides this (DuckDB TIMESTAMP_NS, SQL Server datetime2(7)).
        - Everything else already renders exactly and goes through `normalize`.
        """
        if column.logical_type is LogicalType.FLOAT:
            raise self._err(float_key_refusal(column))
        if column.logical_type is LogicalType.DECIMAL:
            text = self.decimal_key_text(self.quote(column.name), column)
            return f"coalesce({text}, {self.null_sentinel_sql()})"
        return self.normalize(column)

    def decimal_key_text(self, c: str, column: Column) -> str:
        """A DECIMAL key's exact value as minimal plain text, NULL for NULL.

        No trailing fractional zeros and no trailing point: `1.5`, `-0.25`,
        `42`, `0`. Every dialect implements it from its own exact
        decimal-to-text conversion; the default refuses, so a new dialect
        cannot silently round a key.
        """
        raise self._err(
            f"DECIMAL column {column.name!r} cannot be used as a key on "
            f"{self.name}: no exact key rendering is defined for it"
        )

    def check_key(self, table: str, key: KeySpec) -> None:
        """Refuse a key that cannot be matched exactly, naming side and table.

        A FLOAT key, and a LIST key: its elements render by the value rules,
        which round DECIMAL and FLOAT elements to the float scale, so two
        distinct list keys could merge into one row.
        """
        for c in key.columns:
            if c.logical_type is LogicalType.FLOAT:
                raise self._err(f"in {table}: {float_key_refusal(c)}")
            if c.logical_type is LogicalType.LIST:
                raise self._err(
                    f"in {table}: list column {c.name!r} cannot be used as a key. "
                    "Its elements are compared by the value rules, which round "
                    "decimals and floats, so distinct keys could merge into one row"
                )

    def bisects_natively(self, column: Column) -> bool:
        """Whether a single INTEGER key column can be bucketed as itself.

        The bucket expression widens the key with `wide_int` before any
        arithmetic. An integer type as wide as that widening cannot be: DuckDB
        UHUGEINT holds 2^128 - 1, which no signed 128-bit type holds, and
        mixing it with a signed operand silently turns DuckDB arithmetic into
        DOUBLE. Such a key is hashed instead, as any non-integer key is - its
        exact decimal text, hashed, then matched by that text. True by
        default.
        """
        return True

    # ------------------------------------------------------------- lists

    #: The name an element is bound to while its own rule renders it: a list
    #: comprehension variable on DuckDB, an `unnest` alias on PostgreSQL. It
    #: shadows a column of the same name inside the element expression only.
    LIST_ELEMENT_NAME = "__parity_element"

    def list_element(self, column: Column) -> Column:
        """The element of a LIST column, as a column its rule can render.

        Its raw type is the list's without the trailing `[]` - `INTEGER[]` on
        DuckDB, `integer[]` as the PostgreSQL dialect spells an array.
        """
        suffix = _ARRAY_SUFFIX.search(column.raw_type)
        if not suffix:
            raise self._err(f"column {column.name!r} ({column.raw_type}) is not a list")
        inner = column.raw_type[: suffix.start()]
        return Column(self.LIST_ELEMENT_NAME, map_type(inner), inner)

    def list_element_text(self, column: Column) -> str:
        """The element's canonical text, by its own type's rule, null-safe.

        Refuses an element whose engine holds more than the rule compares
        (DuckDB TIMESTAMP_NS): a scalar column of it carries a caveat, and a
        list's element could not, so the digits would vanish silently.
        """
        element = self.list_element(column)
        if element.logical_type not in LIST_ELEMENT_TYPES:
            raise self._err(f"list column {column.name!r} has no element rule")
        if self.holds_sub_micro(element):
            raise self._err(
                f"list column {column.name!r} holds {element.raw_type} elements, "
                "whose digits past the microsecond a list cannot compare; "
                "leave it out with --exclude"
            )
        # A refusal inside the element's rule (a FLOAT past 2^116) names the
        # column it renders; make that the list's, not the stand-in variable.
        where = sql_literal(float_range_where(element))
        named = sql_literal(f" in an element of column {column.name!r}")
        return self.normalize(element).replace(where, named)

    def list_terminator_sql(self) -> str:
        r"""SQL for `LIST_TERMINATOR` (`\,`), the backslash from its code point."""
        return self.concat_sql(self.backslash_sql(), "','")

    def list_dims_mark_sql(self) -> str:
        r"""SQL for `LIST_DIMS_MARK` (`\=`), the backslash from its code point."""
        return self.concat_sql(self.backslash_sql(), "'='")

    # ------------------------------------------------------------- queries

    def check_where(self, where: str) -> None:
        """Refuse a row filter this side cannot run as one read (`--where`).

        Called by the engine before any query. The shared check
        (`row_filter_problem`) refuses what could end or escape the statement;
        a dialect with its own guard on what it runs (SQL Server) adds to it.
        """
        problem = row_filter_problem(where)
        if problem:
            raise self._err(f"row filter {where!r} {problem}")

    def rows_from(self, table: str, where: str | None, *conditions: str) -> str:
        """`from <table> where (<filter>) and <conditions...>`.

        The one place a query that reads the table's rows names it, so a row
        filter reaches every such query - the key stats, every checksum level,
        every download and the sample - or none. The filter is parenthesised
        and comes first, ANDed with the tool's own conditions, so `a or b`
        cannot widen the bucket bounds. With no filter the text is exactly
        what the queries always emitted, so no unfiltered SQL changed.
        """
        conds = ([f"({where.strip()})"] if where else []) + list(conditions)
        sql = f"from {self.qualify(table)}"
        if conds:
            sql += " where " + " and ".join(conds)
        return sql

    def key_stats(self, table: str, key: KeySpec, where: str | None = None) -> KeyStats:
        """Key range plus row and distinct-key counts, in one scan.

        The range is over the *bucketing* value, which is the column itself for
        an integer key and its hash otherwise. The counts are over the
        *identity*, because that is what decides whether two rows are the same
        row - counting distinct hashes would let a collision hide a duplicate.

        Uniqueness rides along with min/max deliberately: the query already has
        to visit the key, and without it a non-unique key goes undetected,
        collapsing rows in `fetch_range` so their differences disappear.
        """
        self.check_key(table, key)
        bucket = self.key_bucket(key)
        identity = self.key_identity(key)
        # `count(identity)` counts non-NULL keys only, while `count(*)` counts
        # every row. Carrying both is what lets a NULL key be diagnosed as a
        # NULL key rather than misreported as a duplicate - `count(distinct)`
        # also ignores NULLs.
        #
        # A hashed key's identity is coalesced text and so never NULL: a NULL
        # component became the key `\N`, was never refused, and was matched to
        # the other side's NULL - or, before text was escaped, to a real `\N`
        # (RT-B1, B2). So for a hashed key the rows counted as keyed are those
        # whose every key column is non-NULL, and the engine refuses the rest
        # exactly as it refuses a NULL integer key.
        if key.hashed:
            all_set = " and ".join(
                f"{self.quote(c.name)} is not null" for c in key.columns
            )
            non_null_sql = self.count(f"case when {all_set} then 1 end")
        else:
            non_null_sql = self.count(identity)
        sql = (
            f"select min({bucket}), max({bucket}), {self.count()}, "
            f"{non_null_sql}, {self.count_distinct_key(key, identity)} "
            f"{self.rows_from(table, where)}"
        )
        lo, hi, rows, non_null, distinct = self.query(sql)[0]
        if lo is None:
            return KeyStats(None, None, int(rows), 0, int(non_null))
        try:
            return KeyStats(int(lo), int(hi), int(rows), int(distinct), int(non_null))
        except (TypeError, ValueError) as exc:  # pragma: no cover - see below
            # Only reachable if a dialect's `key_bucket` returned something
            # non-integer; the engine hashes any non-integer key before we get
            # here, so this is a guard against a broken dialect rather than
            # against user data.
            raise self._err(
                f"key {key.label} in {table} did not produce an integer to "
                f"bisect on (min value {lo!r})"
            ) from exc

    def segment_checksums(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
        where: str | None = None,
    ) -> dict[int, tuple[int, int]]:
        """Return ``{segment_index: (row_count, checksum)}`` for ``[lo, hi)``.

        This is the whole point of the tool: one query per side per level,
        with the hashing pushed into the engine. Nothing but a handful of
        integers crosses the network.
        """
        return {
            int(seg): (int(count), int(checksum or 0))
            for seg, count, checksum in self.query(
                self._segment_sql(table, key, columns, lo, hi, n_segments, where)
            )
        }

    def _segment_sql(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
        where: str | None = None,
    ) -> str:
        """The checksum query. Split out so its shape can be tested directly."""
        k = self.key_bucket(key)
        # Every part of the bucket expression has to survive a key range as wide
        # as bigint itself, which is what a hashed key produces.
        #
        # 1. Widen the key *before* subtracting, not after. `wide_int(k - lo)`
        #    still computes `k - lo` in the column's own type first, and that
        #    overflows outright when lo is near the bottom of the range and the
        #    key is near the top.
        # 2. Emit the span as one precomputed literal. Letting SQL evaluate
        #    `hi - lo` puts the same overflow back.
        # 3. Bound the range inclusively. `hi` is `max_key + 1`, so for a table
        #    holding the largest bigint it is one past what the type can hold;
        #    `<= hi - 1` keeps every literal inside the column's own range.
        #
        # Widening is unconditional rather than only for wide ranges. The
        # conditional version was measured at 10M rows and saved nothing - 39.3s
        # against 38.4s, inside run-to-run noise, because the cost here is
        # dominated by MD5 over every row, not by integer arithmetic. Paying a
        # couple of percent to delete a second code path is the right trade in
        # the one function whose off-by-one would make the walker skip rows.
        bucket = self.bucket_expr(key, k, lo, hi, n_segments)
        # Fold the *key* into every row's hash, not just the comparable columns.
        # A count plus a content-only sum cannot see a same-content insert and
        # delete in one bucket: the counts balance (one in, one out) and equal
        # content sums to the same value, so the bucket reads clean - a false
        # "identical", on data as ordinary as two rows sharing a status or an
        # empty string. Including the key makes an inserted key and a deleted
        # key hash to different values, so the bucket sum changes and the walker
        # recurses in. This only ever *adds* sensitivity: a checksum that
        # differs is always re-checked by downloading and comparing the real
        # rows, so an incidental mismatch costs a query, never a wrong verdict.
        # It also subsumes the no-comparable-columns mode, where the key becomes
        # the only thing hashed. `columns` never contains the key, so the key is
        # hashed exactly once - as its exact key text (`normalize_key`), or two
        # keys that round alike would hash alike here too.
        content = self.hash_parts(
            [*self._key_parts(key), *(self.normalize(c) for c in columns)]
        )
        in_range = (
            f"{k} >= {self.int_literal(lo)}",
            f"{k} <= {self.int_literal(hi - 1)}",
        )
        return (
            f"select {bucket} as seg, {self.count()}, "
            f"{self.sum_wide(content)} "
            f"{self.rows_from(table, where, *in_range)} "
            f"group by {self.group_by(1, bucket)}"
        )

    def bucket_expr(self, key: KeySpec, k: str, lo: int, hi: int, n_segments: int) -> str:
        """`floor((k - lo) * n_segments / (hi - lo))`, overflow-safe - see the
        comment in `_segment_sql`. Must agree with `engine.bucket_bounds` for
        every key, on every engine. Overridden only where a key type is as
        wide as `wide_int` itself (DuckDB HUGEINT)."""
        offset = f"({self.wide_int(k)} - ({self.int_literal(lo)}))"
        return self.int_div(f"{offset} * {n_segments}", f"({self.int_literal(hi - lo)})")

    def fetch_text(self, expr: str) -> str:
        """How `fetch_range` selects one canonical-text expression.

        Itself, everywhere but SQL Server, whose driver drops trailing NUL
        characters from an nvarchar(max) result: the server hashed `'a' +
        nchar(0)`, the checksums proved it differs, and the download then read
        `'a'` - a proven difference reported as identical (RT-A9). A dialect
        whose driver cannot carry text exactly selects bytes here instead.
        """
        return expr

    def fetched(self, value: Any) -> Any:
        """Turn one value `fetch_text` selected back into text. Identity
        unless `fetch_text` is overridden."""
        return value

    def fetch_range(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        where: str | None = None,
    ) -> dict[int | str, tuple[str, ...]]:
        """Download canonical text for every row in ``[lo, hi)``.

        Only ever called on ranges the checksums already proved to differ,
        and only once they are small enough to be cheap.
        """
        bucket = self.key_bucket(key)
        identity = self.key_identity(key)
        # With no comparable columns the row tuple is empty and only key
        # presence distinguishes the sides - see `row_text`.
        exprs = "".join(", " + self.fetch_text(self.normalize(c)) for c in columns)
        if key.hashed:
            identity = self.fetch_text(identity)
        # Selected by identity, filtered and ordered by bucket. Those differ
        # for a hashed key, and the distinction is the whole safety property:
        # two rows may share a bucket, but they are still returned and compared
        # under their own keys.
        #
        # Inclusive upper bound, for the same reason as the checksum query:
        # `hi` is `max_key + 1`, which for a table holding the largest bigint is
        # one past what the column type can represent.
        in_range = (f"{bucket} >= {lo}", f"{bucket} <= {hi - 1}")
        sql = (
            f"select {identity}{exprs} "
            f"{self.rows_from(table, where, *in_range)} "
            f"order by {bucket}"
        )
        out: dict[int | str, tuple[str, ...]] = {}
        for row in self.query(sql):
            row = tuple(self.fetched(v) for v in row)
            rk: int | str = row[0] if key.hashed else int(row[0])
            if rk in out:
                # Second line of defence behind the up-front uniqueness check
                # in `key_stats`: a key that duplicates only inside a fetched
                # range would otherwise overwrite the earlier row and silently
                # drop a real difference.
                raise self._err(
                    f"duplicate key {rk!r} in {table}: key {key.label} is not "
                    f"unique, so rows cannot be compared one-to-one"
                )
            out[rk] = tuple(row[1:])
        return out

    # ------------------------------------------------ the independent sample

    def sample_select(self, column: Column) -> str:
        """How the sample check selects one column: its raw value.

        Deliberately *not* `normalize` - the point of the check is to compare
        what the driver hands back, reduced by `parity.reference`, so that a
        mistake in this dialect's canonical-text SQL cannot hide itself. A
        dialect overrides this only where its driver cannot carry a value
        faithfully (PostgreSQL's infinite dates, DuckDB's tz-aware stamps).
        """
        return self.quote(column.name)

    def sample_value(self, column: Column, value: Any) -> Any:
        """Turn one value `sample_select` returned into what
        `reference_token` reads. Identity by default."""
        return value

    def holds_sub_micro(self, column: Column) -> bool:
        """Whether a TIMESTAMP column can hold digits past microseconds.
        False by default; the engines that can override it."""
        return False

    def single_precision(self, column: Column) -> bool:
        """Whether a FLOAT column is float4, which the reference widens the
        way the engines do. `real`/`float4` everywhere; MySQL and DuckDB
        spell float4 `float` too and override this."""
        return column.raw_type.lower().split("(")[0].strip() in ("real", "float4")

    def padded_char(self, column: Column) -> bool:
        """Whether a STRING column is fixed-width CHAR, whose trailing
        spaces every engine's canonical text drops (CLAUDE.md 4.2)."""
        t = column.raw_type.lower().split("(")[0].strip()
        return t in ("char", "nchar", "character", "bpchar")

    # ------------------------------------------------- BINARY, TIME and JSON

    def binary_prefix_sql(self) -> str:
        r"""SQL for `BINARY_PREFIX` (`\\x`), the backslashes from code points."""
        bs = self.backslash_sql()
        return self.concat_sql(self.concat_sql(bs, bs), "'x'")

    def spell_sql(self, text: str) -> str:
        r"""`text` as a SQL string expression with no backslash literal.

        Regular expressions need backslashes, and RT-D1 bans them from
        rendering SQL - what a literal backslash means is a session setting
        on PostgreSQL and MySQL. So each backslash is `backslash_sql()` and
        each control character `chr(n)` (BigQuery's plain literals cannot
        hold a newline), joined by `concat_sql`; the rest is literal text,
        which must hold no quote.
        """
        if "'" in text:
            raise ValueError("spell_sql takes no quotes")
        pieces: list[str] = []
        lit = ""
        for ch in text:
            if ch == "\\" or ch < " ":
                if lit:
                    pieces.append(f"'{lit}'")
                    lit = ""
                pieces.append(self.backslash_sql() if ch == "\\" else f"chr({ord(ch)})")
            else:
                lit += ch
        if lit or not pieces:
            pieces.append(f"'{lit}'")
        out = pieces[0]
        for p in pieces[1:]:
            out = self.concat_sql(out, p)
        return out

    def json_verbatim(self, column: Column) -> bool:
        """Whether this engine stores a JSON column as unnormalised text, so
        `canonical_json_text` compares it verbatim (DuckDB). False: the
        engine's JSON is a normalised value."""
        return False

    def json_semantics(self, column: Column) -> str:
        """How this engine normalises a JSON value, for a reader.

        Two sides whose descriptions differ can render equal documents
        differently - key order, duplicates, number spelling - and the engine
        prints both descriptions as a warning. A false alarm only: equal
        canonical text always means the same document, whatever the engine.
        Empty for an engine without a JSON type.
        """
        return ""

    def json_compact_layout(self, expr: str) -> str:
        """Lay out *compact* JSON text canonically (`json_layout`).

        BigQuery, Snowflake and DuckDB print JSON without spaces, the
        canonical layout has one after every structural `,` and `:`, and
        only a regular expression can tell those from a comma inside a
        string. The engines with no conditional replacement do it in three
        steps, which every one of them can spell:

        1. `regexp_replace(t, '("(\\.|[^"\\])*"|[^",:]*)([,:]?)', '\1\3' + chr(1))`
           puts chr(1) after every token - a whole string, or a run of
           anything else - and its separator, if it has one;
        2. `,` + chr(1) becomes `, `, and `:` + chr(1) becomes `: ` - only a
           structural separator can be followed by chr(1), because a string
           token ends in `"` and the other run excludes `,` and `:`;
        3. every remaining chr(1) is removed.

        chr(1) cannot already be in the text: JSON escapes every control
        character inside a string, and none can stand outside one. Verified
        live on BigQuery, Snowflake and DuckDB against `json_layout`. A
        dialect whose engine prints JSON spaced already never calls this.
        """
        raise self._err("this engine has no compact JSON layout to fix")

    def fetch_sample(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        modulus: int,
        residue: int,
        where: str | None = None,
    ) -> dict[int | str, tuple[Any, ...]]:
        """Raw values of the rows whose key hash is `residue` modulo `modulus`.

        The selection is a hash of the key's exact text, so both sides pick the
        same keys and the sample spreads over the whole table rather than one
        key range; `modulus` 1 takes every row. The hash is of the key only,
        never the row, so it costs a fraction of a checksum pass. Rows are
        matched by the same identity `fetch_range` uses.
        """
        identity = self.key_identity(key)
        if key.hashed:
            identity = self.fetch_text(identity)
        exprs = "".join(", " + self.sample_select(c) for c in columns)
        chosen: list[str] = []
        if modulus > 1:
            h = self.hash_expr(self._concat(self._key_parts(key)))
            rem = f"(({h}) - {self.int_div(h, str(modulus))} * {modulus})"
            chosen.append(f"{rem} = {residue}")
        # Filtered like every other read: a sampled row outside the filter
        # would be one the walk never saw, and read as a contradiction.
        sql = f"select {identity}{exprs} {self.rows_from(table, where, *chosen)}"
        out: dict[int | str, tuple[Any, ...]] = {}
        for row in self.query(sql):
            rk: int | str = self.fetched(row[0]) if key.hashed else int(row[0])
            if rk in out:
                raise self._err(
                    f"duplicate key {rk!r} in {table}: key {key.label} is not "
                    f"unique, so rows cannot be compared one-to-one"
                )
            out[rk] = tuple(
                self.sample_value(c, v) for c, v in zip(columns, row[1:], strict=True)
            )
        return out


def get_dialect(
    connection_string: str,
    side: str = "?",
    float_scale: int = DEFAULT_FLOAT_SCALE,
) -> Dialect:
    """Open a connection and return the dialect for it.

    Drivers are imported lazily so a DuckDB-only user never needs a PostgreSQL
    driver installed, and vice versa.
    """
    scheme = connection_string.split(":", 1)[0].lower()
    if scheme in ("duckdb",):
        from parity.dialects.duckdb_dialect import DuckDBDialect

        dialect: Dialect = DuckDBDialect(float_scale=float_scale, side=side)
    elif scheme in ("postgres", "postgresql"):
        from parity.dialects.postgres_dialect import PostgresDialect

        dialect = PostgresDialect(float_scale=float_scale, side=side)
    elif scheme in ("mysql",):
        from parity.dialects.mysql_dialect import MySQLDialect

        dialect = MySQLDialect(float_scale=float_scale, side=side)
    elif scheme in ("snowflake",):
        from parity.dialects.snowflake_dialect import SnowflakeDialect

        dialect = SnowflakeDialect(float_scale=float_scale, side=side)
    elif scheme in ("bigquery",):
        from parity.dialects.bigquery_dialect import BigQueryDialect

        dialect = BigQueryDialect(float_scale=float_scale, side=side)
    elif scheme in ("mssql", "sqlserver"):
        from parity.dialects.mssql_dialect import SQLServerDialect

        dialect = SQLServerDialect(float_scale=float_scale, side=side)
    else:
        raise ValueError(
            f"[side {side}] no dialect for scheme {scheme!r}. "
            f"Supported: duckdb, postgres, mysql, snowflake, bigquery, mssql."
        )
    try:
        dialect.connect(connection_string)
    except ValueError:
        # Already one of ours, and already says which side. Re-wrapping would
        # print the prefix twice.
        raise
    except Exception as exc:
        # A bare driver error says nothing about which of the two endpoints
        # failed, which is the first thing anyone needs to know.
        raise ValueError(
            f"[side {side}: {dialect.name}] could not connect: {exc}"
        ) from exc
    return dialect


def require_matching_scales(a: Dialect, b: Dialect) -> None:
    """Refuse to compare two sides that round floats differently.

    If the scales disagree, every DECIMAL and FLOAT row renders to different
    canonical text and the tool reports the whole table as changed. That looks
    like a catastrophic migration bug rather than a configuration mistake, so
    fail before any query runs.
    """
    if a.float_scale != b.float_scale:
        raise ValueError(
            f"float_scale differs between sides: A={a.float_scale} "
            f"B={b.float_scale}. Both sides must round identically or every "
            f"float and decimal row reports as different."
        )


#: A list's type suffix: `[]`, or DuckDB's fixed-size ARRAY `[n]`.
_ARRAY_SUFFIX = re.compile(r"\[\d*\]$")


def map_type(raw: str) -> LogicalType:
    """Map an engine's type name onto a logical category."""
    suffix = _ARRAY_SUFFIX.search(raw)
    if suffix:
        # A list (DuckDB `INTEGER[]`) or array (PostgreSQL, whose dialect
        # spells the element type the same way, `integer[]`), or a DuckDB
        # fixed-size ARRAY (`VARCHAR[2]`), whose elements are a list's.
        # Checked before the `(` split, or `DECIMAL(10,2)[]` would read as a
        # decimal and `ENUM('a', 'b')[2]` as a string (RT-HD8, HD9: the
        # array was then rendered by the scalar rule, or by a varchar cast
        # that printed its strings unquoted before DuckDB 2.0, RT-HD5).
        # Ruled only when its element is (`LIST_ELEMENT_TYPES`); a nested
        # list, a list of structs or of JSON stays UNKNOWN.
        element = map_type(raw[: suffix.start()])
        return LogicalType.LIST if element in LIST_ELEMENT_TYPES else LogicalType.UNKNOWN
    t = raw.lower().split("(")[0].strip()
    if t in {
        "int",
        "int2",
        "int4",
        "int8",
        "integer",
        "bigint",
        "smallint",
        "tinyint",
        "hugeint",
        # DuckDB's unsigned 128-bit integer: its varchar cast is the exact
        # decimal, as every integer's is. It cannot be widened into HUGEINT
        # for bucketing, so as a key it is hashed (`bisects_natively`).
        "uhugeint",
        "utinyint",
        "usmallint",
        "uinteger",
        "ubigint",
        "serial",
        "bigserial",
    }:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric", "money"}:
        # PostgreSQL `money` is exact cents. Its `::text` is locale-formatted
        # (`$1,234.56`, by lc_monetary), so as an unmapped type it never
        # matched a migrated decimal; as DECIMAL it renders through
        # `round(c::numeric, s)`, exact, like SQL Server's money.
        return LogicalType.DECIMAL
    if t in {"float", "float4", "float8", "double", "real", "double precision"}:
        return LogicalType.FLOAT
    if t in {"bool", "boolean"}:
        return LogicalType.BOOLEAN
    if t in {"date"}:
        return LogicalType.DATE
    if t.startswith(("timestamp", "datetime")):
        return LogicalType.TIMESTAMP
    # Exactly these spellings: `time with time zone` (PostgreSQL timetz,
    # DuckDB TIMETZ) carries an offset, which TIMETZ keeps; DuckDB's TIME_NS
    # has not been verified and stays unmapped.
    if t in {"time", "time without time zone"}:
        return LogicalType.TIME
    if t in {"time with time zone", "timetz"}:
        return LogicalType.TIMETZ
    if t == "interval":
        return LogicalType.INTERVAL
    # PostgreSQL bit(n) and bit varying (information_schema says `bit` and
    # `bit varying`), DuckDB BIT. SQL Server's and MySQL's `bit` have their
    # own maps: a boolean and a BIT(n) there.
    if t in {"bit", "bit varying", "varbit"}:
        return LogicalType.BITS
    if t in {"inet", "cidr"}:
        return LogicalType.INET
    # PostgreSQL bytea; DuckDB BLOB (BYTEA, VARBINARY and BINARY are aliases
    # it reports as BLOB).
    if t in {"bytea", "blob"}:
        return LogicalType.BINARY
    if t in {"json", "jsonb"}:
        return LogicalType.JSON
    if t in {
        "text",
        "varchar",
        "char",
        "bpchar",
        "character varying",
        "character",
        "string",
        "uuid",
        # DuckDB reports an enum column as `ENUM('a', 'b')`; its varchar cast
        # is the label, exactly the text a STRING column holding it renders.
        "enum",
        # PostgreSQL's 63-byte identifier type: its `::text` is the name, as
        # a text column holding it renders (and rendered, unmapped, before).
        "name",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
